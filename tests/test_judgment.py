import asyncio
from datetime import datetime, timezone

import pytest

from src import judgment
from src.judgment import (
    BEAR_PROMPT_PATH,
    BULL_PROMPT_PATH,
    EXIT_PROMPT_PATH,
    MANAGER_PROMPT_PATH,
    STAY_PROMPT_PATH,
    _DebateResponse,
    _ManagerResponse,
    _prompt_version,
    _SellManagerResponse,
    debate,
    debate_holding,
    judge,
    judge_sell,
    portfolio_manager,
    portfolio_manager_sell,
)
from src.schemas import AnalystOpinion, SellAction


def _opinion(agent: str, score: float, confidence: float = 0.6) -> AnalystOpinion:
    return AnalystOpinion(
        agent=agent,
        ticker="005930",
        score=score,
        confidence=confidence,
        evidence=[f"prompt:{agent}@abc123"],
        as_of=datetime(2026, 1, 5, tzinfo=timezone.utc),
    )


def _fake_debate_and_manager(bull_strength=0.7, bear_strength=0.4, action="HOLD"):
    async def fake_call_structured(*, system, user, response_model, json_schema, **kwargs):
        if "bull-case debater" in user:
            return _DebateResponse(argument="상승 여력 충분", strength=bull_strength)
        if "bear-case debater" in user:
            return _DebateResponse(argument="하락 리스크 존재", strength=bear_strength)
        if "portfolio manager" in user:
            return _ManagerResponse(action=action, reasoning="종합 판단 근거")
        raise AssertionError(f"unexpected prompt: {user[:80]}")

    return fake_call_structured


def test_debate_produces_independent_bull_and_bear(monkeypatch):
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_debate_and_manager())

    opinions = [_opinion("chart", 0.5), _opinion("news", -0.2)]
    bull, bear = asyncio.run(debate("005930", opinions))

    assert bull.stance == "bull"
    assert bull.strength == 0.7
    assert bear.stance == "bear"
    assert bear.strength == 0.4
    assert bull.ticker == "005930"

    expected_bull_version = _prompt_version(BULL_PROMPT_PATH.read_text())
    expected_bear_version = _prompt_version(BEAR_PROMPT_PATH.read_text())
    assert bull.evidence == [f"prompt:debate_bull@{expected_bull_version}"]
    assert bear.evidence == [f"prompt:debate_bear@{expected_bear_version}"]


def test_judge_returns_none_without_opinions():
    assert asyncio.run(judge([], total_expected_analysts=3)) is None


def test_judge_builds_decision_via_debate_and_manager(monkeypatch):
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_debate_and_manager(action="BUY"))

    opinions = [_opinion("chart", 0.9, 0.8), _opinion("news", 0.6, 0.7)]
    decision = asyncio.run(judge(opinions, total_expected_analysts=3))

    assert decision is not None
    assert decision.ticker == "005930"
    assert decision.action == "BUY"
    assert decision.reason == "종합 판단 근거"
    assert decision.inputs == opinions
    assert decision.degraded is True  # 2/3 분석가만 응답
    assert {arg.stance for arg in decision.debate} == {"bull", "bear"}

    expected_manager_version = _prompt_version(MANAGER_PROMPT_PATH.read_text())
    assert decision.evidence == [f"prompt:manager@{expected_manager_version}"]


def test_judge_not_degraded_when_all_analysts_responded(monkeypatch):
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_debate_and_manager(action="HOLD"))

    opinions = [_opinion("chart", 0.1), _opinion("news", -0.1), _opinion("disclosure", 0.0)]
    decision = asyncio.run(judge(opinions, total_expected_analysts=3))

    assert decision is not None
    assert decision.degraded is False


def test_portfolio_manager_propagates_llm_failure(monkeypatch):
    async def failing(**kwargs):
        raise RuntimeError("llm exhausted retries")

    monkeypatch.setattr(judgment.llm, "call_structured", failing)

    opinions = [_opinion("chart", 0.5)]
    with pytest.raises(RuntimeError):
        asyncio.run(judge(opinions, total_expected_analysts=1))


def test_portfolio_manager_uses_bull_and_bear_arguments(monkeypatch):
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_debate_and_manager(action="HOLD"))

    opinions = [_opinion("chart", 0.5)]
    bull, bear = asyncio.run(debate("005930", opinions))
    decision = asyncio.run(portfolio_manager("005930", opinions, bull, bear, degraded=False))

    assert decision.action == "HOLD"
    assert decision.debate == [bull, bear]


def _fake_sell_debate_and_manager(stay_strength=0.6, exit_strength=0.5, action="HOLD"):
    async def fake_call_structured(*, system, user, response_model, json_schema, **kwargs):
        normalized = " ".join(user.split())  # 프롬프트 마크다운 줄바꿈 때문에 공백 정규화 후 매칭
        if '"stay" debater' in normalized:
            return _DebateResponse(argument="여전히 유효한 상승 논리", strength=stay_strength)
        if '"exit" debater' in normalized:
            return _DebateResponse(argument="처음 매수 근거가 무너짐", strength=exit_strength)
        if "reassessing a position that is already held" in normalized:
            return _SellManagerResponse(action=action, reasoning="재평가 근거")
        raise AssertionError(f"unexpected prompt: {user[:80]}")

    return fake_call_structured


def test_debate_holding_produces_independent_stay_and_exit(monkeypatch):
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_sell_debate_and_manager())

    opinions = [_opinion("chart", 0.5), _opinion("news", -0.2)]
    stay, exit_case = asyncio.run(debate_holding("005930", opinions, unrealized_pct=0.05))

    assert stay.stance == "bull"
    assert exit_case.stance == "bear"

    expected_stay_version = _prompt_version(STAY_PROMPT_PATH.read_text())
    expected_exit_version = _prompt_version(EXIT_PROMPT_PATH.read_text())
    assert stay.evidence == [f"prompt:debate_stay@{expected_stay_version}"]
    assert exit_case.evidence == [f"prompt:debate_exit@{expected_exit_version}"]


def test_judge_sell_returns_none_without_opinions():
    assert asyncio.run(judge_sell("005930", [], unrealized_pct=0.0)) is None


def test_judge_sell_returns_none_on_hold(monkeypatch):
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_sell_debate_and_manager(action="HOLD"))

    opinions = [_opinion("chart", 0.5)]
    result = asyncio.run(judge_sell("005930", opinions, unrealized_pct=0.05))

    assert result is None


def test_judge_sell_returns_sell_action_on_sell(monkeypatch):
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_sell_debate_and_manager(action="SELL"))

    opinions = [_opinion("chart", -0.5)]
    result = asyncio.run(judge_sell("005930", opinions, unrealized_pct=-0.03))

    assert result == SellAction(
        ticker="005930", reason="llm_discretionary", sell_fraction=1.0, reasoning="재평가 근거"
    )


def test_portfolio_manager_sell_propagates_llm_failure(monkeypatch):
    async def failing(**kwargs):
        raise RuntimeError("llm exhausted retries")

    monkeypatch.setattr(judgment.llm, "call_structured", failing)

    opinions = [_opinion("chart", 0.5)]
    with pytest.raises(RuntimeError):
        asyncio.run(judge_sell("005930", opinions, unrealized_pct=0.0))


def test_portfolio_manager_sell_uses_stay_and_exit_arguments(monkeypatch):
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_sell_debate_and_manager(action="SELL"))

    opinions = [_opinion("chart", -0.4)]
    stay, exit_case = asyncio.run(debate_holding("005930", opinions, unrealized_pct=-0.05))
    action = asyncio.run(portfolio_manager_sell("005930", opinions, stay, exit_case, unrealized_pct=-0.05))

    assert action == SellAction(
        ticker="005930", reason="llm_discretionary", sell_fraction=1.0, reasoning="재평가 근거"
    )
