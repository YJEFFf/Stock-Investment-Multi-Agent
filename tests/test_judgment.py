import asyncio
import json
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


def _fake_debate_and_manager(
    bull_strength=0.7,
    bear_strength=0.4,
    action="HOLD",
    stop_loss_pct=6.0,
    take_profit_fraction=0.25,
    trail_pct=5.0,
):
    async def fake_call_structured(*, system, user, response_model, json_schema, **kwargs):
        if "bull-case debater" in user:
            return _DebateResponse(argument="상승 여력 충분", strength=bull_strength)
        if "bear-case debater" in user:
            return _DebateResponse(argument="하락 리스크 존재", strength=bear_strength)
        if "portfolio manager" in user:
            return _ManagerResponse(
                action=action,
                reasoning="종합 판단 근거",
                stop_loss_pct=stop_loss_pct,
                take_profit_fraction=take_profit_fraction,
                trail_pct=trail_pct,
            )
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


# --- ExitPlan: 진입 시 LLM이 정하는 종목별 출구 규칙 (사용자 확정 2026-08-15) ---


def test_manager_exit_plan_derives_take_profit_at_two_to_one(monkeypatch):
    """익절선은 LLM이 내지 않는다 — 손절폭의 2배를 코드가 계산한다."""
    monkeypatch.setattr(
        judgment.llm, "call_structured", _fake_debate_and_manager(action="BUY", stop_loss_pct=6.0)
    )

    opinions = [_opinion("chart", 0.9), _opinion("news", 0.6), _opinion("disclosure", 0.4)]
    decision = asyncio.run(judge(opinions, total_expected_analysts=3))

    assert decision.exit_plan.stop_loss_pct == pytest.approx(-0.06)
    assert decision.exit_plan.take_profit_pct == pytest.approx(0.12)


def test_manager_exit_plan_allows_looser_than_default(monkeypatch):
    """양방향 허용(사용자 확정) — 고정값 -10%보다 넓은 손절선도 그대로 통과한다."""
    monkeypatch.setattr(
        judgment.llm, "call_structured", _fake_debate_and_manager(action="BUY", stop_loss_pct=13.0)
    )

    opinions = [_opinion("chart", 0.9), _opinion("news", 0.6), _opinion("disclosure", 0.4)]
    decision = asyncio.run(judge(opinions, total_expected_analysts=3))

    assert decision.exit_plan.stop_loss_pct == pytest.approx(-0.13)
    assert decision.exit_plan.take_profit_pct == pytest.approx(0.26)


@pytest.mark.parametrize(
    "stop_loss_pct,expected",
    [(0.5, -0.03), (40.0, -0.15), (-8.0, -0.08)],  # 하한 / 상한 / 음수 부호 흘림
)
def test_manager_exit_plan_clamps_out_of_range_values(monkeypatch, stop_loss_pct, expected):
    """구조화 출력이 범위를 안 지키는 경우가 있다. 범위 밖 값이 조용히 통과하면
    손절선이 사라지는 쪽으로 틀리기 때문에 코드에서 한 번 더 자른다."""
    monkeypatch.setattr(
        judgment.llm, "call_structured", _fake_debate_and_manager(action="BUY", stop_loss_pct=stop_loss_pct)
    )

    opinions = [_opinion("chart", 0.9), _opinion("news", 0.6), _opinion("disclosure", 0.4)]
    decision = asyncio.run(judge(opinions, total_expected_analysts=3))

    assert decision.exit_plan.stop_loss_pct == pytest.approx(expected)


def test_manager_exit_plan_clamps_fraction_and_trail(monkeypatch):
    monkeypatch.setattr(
        judgment.llm,
        "call_structured",
        _fake_debate_and_manager(action="BUY", take_profit_fraction=0.95, trail_pct=30.0),
    )

    opinions = [_opinion("chart", 0.9), _opinion("news", 0.6), _opinion("disclosure", 0.4)]
    decision = asyncio.run(judge(opinions, total_expected_analysts=3))

    assert decision.exit_plan.take_profit_fraction == pytest.approx(0.60)
    assert decision.exit_plan.trail_pct == pytest.approx(-0.12)


def test_degraded_decision_falls_back_to_default_exit_plan(monkeypatch):
    """분석가가 일부 빠진 상태에서 나온 판단에는 손절선을 넓힐 재량까지 주지 않는다 —
    게이트에서 기준을 높이는 기존 원칙과 같은 방향."""
    monkeypatch.setattr(
        judgment.llm, "call_structured", _fake_debate_and_manager(action="BUY", stop_loss_pct=14.0)
    )

    opinions = [_opinion("chart", 0.9)]  # 3명 중 1명만 응답
    decision = asyncio.run(judge(opinions, total_expected_analysts=3))

    assert decision.degraded is True
    assert decision.exit_plan is None  # sell.DEFAULT_EXIT_PLAN으로 떨어진다


def test_manager_schema_has_no_unsupported_number_constraints():
    """number에 minimum/maximum을 넣으면 API가 400을 낸다("For 'number' type,
    properties maximum, minimum are not supported", 2026-08-15 실호출로 확인).
    스키마만 보고 되돌리기 쉬운 실수라 못박아 둔다 — 범위 강제는 _exit_plan_from의
    클램핑이 하고, 여기 넣으면 매수 판단 자체가 통째로 실패한다."""
    from src.judgment import _MANAGER_RESPONSE_SCHEMA

    for name, spec in _MANAGER_RESPONSE_SCHEMA["properties"].items():
        if spec.get("type") == "number":
            assert "minimum" not in spec, name
            assert "maximum" not in spec, name


def test_manager_schema_forbids_additional_properties():
    """손으로 쓴 스키마에 additionalProperties: False가 없으면 API가 400을 낸다
    (docs/PLAN.md에 기록된 기존 함정)."""
    from src.judgment import _MANAGER_RESPONSE_SCHEMA

    assert _MANAGER_RESPONSE_SCHEMA["additionalProperties"] is False


# --- 보유 종목 재평가의 종목별 기록 (2026-08-27) ---
#
# 매도 판단은 11거래일 연속 0건이었는데, HOLD가 None으로 반환되고 스크립트는 집계
# 한 줄만 찍어서 **"왜 안 팔았는가"를 사후에 볼 방법이 아예 없었다.** 반환 계약은
# 그대로 두고(HOLD면 None), 근거만 이 계층에서 남긴다.


def _judgment_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_hold_is_written_to_the_judgment_log_even_though_it_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_sell_debate_and_manager(action="HOLD"))
    log_path = tmp_path / "sell_judgment.jsonl"

    result = asyncio.run(
        judge_sell("005930", [_opinion("chart", 0.5)], unrealized_pct=0.05, log_path=log_path)
    )

    assert result is None  # 계약은 그대로
    (row,) = _judgment_rows(log_path)
    assert row["ticker"] == "005930"
    assert row["outcome"] == "HOLD"
    assert row["reasoning"] == "재평가 근거"
    assert row["unrealized_pct"] == 0.05
    assert row["analysts"] == ["chart"]


def test_sell_is_written_with_the_same_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_sell_debate_and_manager(action="SELL"))
    log_path = tmp_path / "sell_judgment.jsonl"

    asyncio.run(judge_sell("005930", [_opinion("chart", -0.5)], unrealized_pct=-0.03, log_path=log_path))

    (row,) = _judgment_rows(log_path)
    assert row["outcome"] == "SELL"
    assert row["stay_strength"] == 0.6
    assert row["exit_strength"] == 0.5


def test_missing_opinions_are_logged_apart_from_a_hold(tmp_path):
    """반환값은 둘 다 None이지만 로그에서는 갈라져야 한다 — "분석이 죽어서 조용한 날"과
    "판단하고 안 판 날"은 다른 상태다(스키마 계약의 AnalystOpinion | None과 같은 구분)."""
    log_path = tmp_path / "sell_judgment.jsonl"

    assert asyncio.run(judge_sell("005930", [], unrealized_pct=0.0, log_path=log_path)) is None

    (row,) = _judgment_rows(log_path)
    assert row["outcome"] == "no_opinions"
    assert row["analysts"] == []


def test_judgment_row_carries_the_prompt_version(monkeypatch, tmp_path):
    """성능 변화가 프롬프트 때문인지 데이터 때문인지 가릴 유일한 단서다(CLAUDE.md 계약)."""
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_sell_debate_and_manager(action="HOLD"))
    log_path = tmp_path / "sell_judgment.jsonl"

    asyncio.run(judge_sell("005930", [_opinion("chart", 0.5)], unrealized_pct=0.0, log_path=log_path))

    expected = _prompt_version(judgment.SELL_MANAGER_PROMPT_PATH.read_text())
    assert _judgment_rows(log_path)[0]["evidence"] == [f"prompt:manager_sell@{expected}"]


def test_a_broken_log_path_does_not_lose_the_sell_decision(monkeypatch, tmp_path):
    """로그 한 줄 때문에 매도 판정을 잃는 건 앞뒤가 바뀐 것이다."""
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_sell_debate_and_manager(action="SELL"))
    blocked = tmp_path / "file.txt"
    blocked.write_text("not a directory")

    result = asyncio.run(
        judge_sell("005930", [_opinion("chart", -0.5)], unrealized_pct=-0.03, log_path=blocked / "x.jsonl")
    )

    assert result == SellAction(
        ticker="005930", reason="llm_discretionary", sell_fraction=1.0, reasoning="재평가 근거"
    )


def test_default_log_path_is_resolved_at_call_time(monkeypatch, tmp_path):
    """기본값을 def 시점에 굳히면 conftest의 격리가 안 먹어서 테스트가 운영 logs/에 쓴다.
    실제로 한 번 그랬다(2026-08-27) — 알림 마커가 겪은 사고와 같은 모양이다."""
    monkeypatch.setattr(judgment.llm, "call_structured", _fake_sell_debate_and_manager(action="HOLD"))
    redirected = tmp_path / "redirected.jsonl"
    monkeypatch.setattr(judgment, "DEFAULT_SELL_JUDGMENT_LOG_PATH", redirected)

    asyncio.run(judge_sell("005930", [_opinion("chart", 0.5)], unrealized_pct=0.0))

    assert redirected.exists()
