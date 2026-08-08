import asyncio
from datetime import datetime, timezone

import pytest

from src import analysts as analysts_module
from src.analysts import NEWS_PROMPT_PATH, _NewsAnalysisResponse, _prompt_version, news_analyst
from src.schemas import NewsContext, NewsItem


def _context(company_news=None, sector_news=None) -> NewsContext:
    return NewsContext(
        ticker="005930",
        sector="반도체",
        as_of=datetime(2026, 1, 5, tzinfo=timezone.utc),
        company_news=company_news or [],
        sector_news=sector_news or [],
    )


def _news_item(title: str) -> NewsItem:
    return NewsItem(title=title, press="테스트언론사", published_at=None, url="https://example.com")


def test_news_analyst_returns_none_without_any_news():
    context = _context(company_news=[], sector_news=[])

    opinion = asyncio.run(news_analyst(context))

    assert opinion is None


def test_news_analyst_builds_opinion_with_prompt_version(monkeypatch):
    async def fake_call_structured(**kwargs):
        return _NewsAnalysisResponse(score=-0.3, confidence=0.6, reasoning="부정적 실적 뉴스")

    monkeypatch.setattr(analysts_module.llm, "call_structured", fake_call_structured)

    context = _context(company_news=[_news_item("실적 부진 발표")])
    opinion = asyncio.run(news_analyst(context))

    expected_version = _prompt_version(NEWS_PROMPT_PATH.read_text())

    assert opinion is not None
    assert opinion.agent == "news"
    assert opinion.ticker == "005930"
    assert opinion.score == -0.3
    assert opinion.confidence == 0.6
    assert opinion.evidence == [f"prompt:news@{expected_version}"]
    assert opinion.as_of == context.as_of


def test_news_analyst_runs_with_sector_news_only(monkeypatch):
    async def fake_call_structured(**kwargs):
        return _NewsAnalysisResponse(score=0.1, confidence=0.3, reasoning="업종 배경만 있음")

    monkeypatch.setattr(analysts_module.llm, "call_structured", fake_call_structured)

    context = _context(company_news=[], sector_news=[_news_item("반도체 업황 개선")])
    opinion = asyncio.run(news_analyst(context))

    assert opinion is not None
    assert opinion.confidence == 0.3


def test_news_analyst_propagates_llm_failure(monkeypatch):
    async def failing(**kwargs):
        raise RuntimeError("llm exhausted retries")

    monkeypatch.setattr(analysts_module.llm, "call_structured", failing)

    with pytest.raises(RuntimeError):
        asyncio.run(news_analyst(_context(company_news=[_news_item("아무 뉴스")])))
