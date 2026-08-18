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
