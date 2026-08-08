import asyncio
from datetime import date, datetime, timezone

import pytest

from src import analysts as analysts_module
from src.analysts import CHART_PROMPT_PATH, _ChartAnalysisResponse, _prompt_version, chart_analyst
from src.schemas import MarketContext, OHLCVBar


def _context() -> MarketContext:
    return MarketContext(
        ticker="005930",
        as_of=datetime(2026, 1, 5, tzinfo=timezone.utc),
        bars=[
            OHLCVBar(date=date(2026, 1, 2), open=70000, high=71000, low=69500, close=70500, volume=1000000),
            OHLCVBar(date=date(2026, 1, 5), open=70500, high=72000, low=70000, close=71800, volume=1200000),
        ],
        indicators={"sma5": 71000.0, "rsi14": 55.0},
    )


def test_chart_analyst_builds_opinion_with_prompt_version(monkeypatch):
    async def fake_call_structured(**kwargs):
        return _ChartAnalysisResponse(score=0.5, confidence=0.8, reasoning="상승 추세, RSI 중립")

    monkeypatch.setattr(analysts_module.llm, "call_structured", fake_call_structured)

    context = _context()
    opinion = asyncio.run(chart_analyst(context))

    expected_version = _prompt_version(CHART_PROMPT_PATH.read_text())

    assert opinion is not None
    assert opinion.agent == "chart"
    assert opinion.ticker == "005930"
    assert opinion.score == 0.5
    assert opinion.confidence == 0.8
    assert opinion.evidence == [f"prompt:chart@{expected_version}"]
    assert opinion.as_of == context.as_of


def test_chart_analyst_propagates_llm_failure(monkeypatch):
    async def failing(**kwargs):
        raise RuntimeError("llm exhausted retries")

    monkeypatch.setattr(analysts_module.llm, "call_structured", failing)

    with pytest.raises(RuntimeError):
        asyncio.run(chart_analyst(_context()))
