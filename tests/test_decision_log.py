"""판단 로그(pipeline.jsonl)에 무엇이 남는가.

특히 exit_plan: 매니저는 매수 여부와 무관하게 출구 계획을 낸다
(prompts/portfolio_manager.md의 "Answer these fields on a HOLD as well"). 그 값이
매수 성사된 종목에서만 기록되면, 매수 없는 날이 정상인 이 시스템에서는 표본이
거의 안 쌓여 "LLM이 종목마다 손절폭을 실제로 다르게 잡는가"를 확인할 수 없다.
"""

import asyncio
import json
from datetime import datetime, timezone

from src.pipeline import execute_simulated, make_dummy_analyst_fn, run_day
from src.schemas import AnalystOpinion, Decision, ExitPlan, PortfolioState, RiskGateConfig

UNIVERSE = [("005930", "반도체"), ("000660", "반도체")]
DAY = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _judge_holding_with_plan(stop_loss_pct: float):
    """항상 HOLD하되 종목별 출구 계획은 내는 판단 함수 — 실제 매니저와 같은 모양."""

    async def judge(opinions: list[AnalystOpinion], total_expected_analysts: int) -> Decision | None:
        if not opinions:
            return None
        return Decision(
            ticker=opinions[0].ticker,
            action="HOLD",
            reason="테스트용 관망",
            inputs=opinions,
            degraded=False,
            exit_plan=ExitPlan(
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=abs(stop_loss_pct) * 2,
                take_profit_fraction=0.33,
                trail_pct=-0.07,
            ),
        )

    return judge


def _run(tmp_path, judge_fn):
    log_path = tmp_path / "pipeline.jsonl"
    asyncio.run(
        run_day(
            UNIVERSE, DAY, PortfolioState(), RiskGateConfig(),
            make_dummy_analyst_fn(base_seed=42), judge_fn, execute_simulated, log_path=log_path,
        )
    )
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def test_exit_plan_is_logged_on_hold_decisions(tmp_path):
    """매수가 안 나도 그날 매니저가 정한 손절폭이 로그에 남아야 한다."""
    entries = _run(tmp_path, _judge_holding_with_plan(-0.06))

    assert entries
    for e in entries:
        assert e["action"] == "HOLD"
        assert e["exit_plan"] is not None
        assert e["exit_plan"]["stop_loss_pct"] == -0.06
        assert e["exit_plan"]["take_profit_pct"] == 0.12  # 항상 2배


def test_missing_exit_plan_is_logged_as_null_not_omitted(tmp_path):
    """degraded 판단은 출구 계획을 안 낸다(고정 기본값으로 떨어진다). 그 사실이
    '값이 없음'으로 남아야 나중에 갈라볼 수 있다 — 키 자체가 빠지면 안 된다."""

    async def judge(opinions, total_expected_analysts):
        if not opinions:
            return None
        return Decision(
            ticker=opinions[0].ticker, action="HOLD", reason="degraded",
            inputs=opinions, degraded=True, exit_plan=None,
        )

    entries = _run(tmp_path, judge)

    assert entries
    for e in entries:
        assert "exit_plan" in e
        assert e["exit_plan"] is None


def _analysts_fn(agents: list[str]):
    """지정한 분석가들만 의견을 내는 AnalystFn — 일부 분석가가 빠진 상황을 만든다."""

    async def _fn(ticker: str, sector: str, day: datetime) -> list[AnalystOpinion]:
        return [
            AnalystOpinion(
                agent=agent, ticker=ticker, score=0.1, confidence=0.5,
                evidence=[f"prompt:{agent}@test"], as_of=day,
            )
            for agent in agents
        ]

    return _fn


def _judge_echo(degraded: bool):
    async def judge(opinions: list[AnalystOpinion], total_expected_analysts: int) -> Decision | None:
        if not opinions:
            return None
        return Decision(
            ticker=opinions[0].ticker, action="HOLD", reason="테스트용 관망",
            inputs=opinions, degraded=degraded,
        )

    return judge


def _run_with(tmp_path, analyst_fn, judge_fn):
    tmp_path.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "pipeline.jsonl"
    asyncio.run(
        run_day(
            UNIVERSE, DAY, PortfolioState(), RiskGateConfig(),
            analyst_fn, judge_fn, execute_simulated, log_path=log_path,
        )
    )
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def test_contributing_analysts_are_logged(tmp_path):
    """어떤 분석가가 그 판단에 기여했는지가 결정 로그에 남아야 한다.

    이 필드가 없어서 2026-08-24 점검 때 "매수 판단이 정상으로 돌고 있는가"에
    답하려고 분석가 커버리지를 llm_calls.jsonl 타임스탬프로 역산해야 했다.
    분석가가 조용히 빠지기 시작하면 결정 로그만 봐서는 알 수 없다.
    """
    rows = _run_with(tmp_path, _analysts_fn(["news", "chart"]), _judge_echo(degraded=False))

    assert rows, "판단이 하나도 안 남았다"
    for row in rows:
        assert row["analysts"] == ["chart", "news"]  # 정렬해서 남긴다


def test_degraded_flag_is_logged(tmp_path):
    """분석가가 빠진 채 나온 판단인지가 로그에 남아야 한다 (스키마 계약의 degraded)."""
    degraded_rows = _run_with(tmp_path / "a", _analysts_fn(["chart"]), _judge_echo(degraded=True))
    healthy_rows = _run_with(
        tmp_path / "b", _analysts_fn(["chart", "news", "disclosure"]), _judge_echo(degraded=False)
    )

    assert all(r["degraded"] is True for r in degraded_rows)
    assert all(r["degraded"] is False for r in healthy_rows)
    assert all(r["analysts"] == ["chart"] for r in degraded_rows)

