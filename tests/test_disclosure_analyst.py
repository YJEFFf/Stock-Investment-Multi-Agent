import asyncio
from datetime import date, datetime, timezone

import pytest

from src import analysts as analysts_module
from src.analysts import (
    DISCLOSURE_PROMPT_PATH,
    _DisclosureAnalysisResponse,
    _prompt_version,
    disclosure_analyst,
)
from src.schemas import DisclosureContext, DisclosureItem


def _disclosure_item(report_name: str) -> DisclosureItem:
    return DisclosureItem(
        report_name=report_name,
        submitter="테스트회사",
        received_at=date(2026, 1, 5),
        receipt_no="20260105000001",
        remark="유",
        url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260105000001",
    )


def _context(disclosures=None) -> DisclosureContext:
    return DisclosureContext(
        ticker="005930",
        as_of=datetime(2026, 1, 5, tzinfo=timezone.utc),
        disclosures=disclosures or [],
    )


def test_disclosure_analyst_returns_none_without_any_disclosures():
    opinion = asyncio.run(disclosure_analyst(_context(disclosures=[])))

    assert opinion is None


def test_disclosure_analyst_builds_opinion_with_prompt_version(monkeypatch):
    async def fake_call_structured(**kwargs):
        return _DisclosureAnalysisResponse(score=0.4, confidence=0.7, reasoning="자사주 취득 결정")

    monkeypatch.setattr(analysts_module.llm, "call_structured", fake_call_structured)

    context = _context(disclosures=[_disclosure_item("주요사항보고서(자기주식취득결정)")])
    opinion = asyncio.run(disclosure_analyst(context))

    expected_version = _prompt_version(DISCLOSURE_PROMPT_PATH.read_text())

    assert opinion is not None
    assert opinion.agent == "disclosure"
    assert opinion.ticker == "005930"
    assert opinion.score == 0.4
    assert opinion.confidence == 0.7
    assert opinion.evidence == [f"prompt:disclosure@{expected_version}"]
    assert opinion.as_of == context.as_of


def test_disclosure_analyst_propagates_llm_failure(monkeypatch):
    async def failing(**kwargs):
        raise RuntimeError("llm exhausted retries")

    monkeypatch.setattr(analysts_module.llm, "call_structured", failing)

    with pytest.raises(RuntimeError):
        asyncio.run(disclosure_analyst(_context(disclosures=[_disclosure_item("아무 공시")])))
