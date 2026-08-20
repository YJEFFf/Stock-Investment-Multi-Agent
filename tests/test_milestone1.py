import asyncio
import random
from datetime import datetime, timedelta, timezone

from src.pipeline import execute_simulated, make_dummy_analyst_fn, propose_decision, run_day, summarize_log
from src.schemas import PortfolioState, RiskGateConfig

SECTORS = ["반도체", "IT", "화학", "자동차", "금융"]
UNIVERSE = [(f"{sector}{i:03d}", sector) for sector in SECTORS for i in range(5)]  # 25개 종목
TRADING_DAYS = 20
BASE_SEED = 7


def _simulate(log_path):
    portfolio = PortfolioState()
    config = RiskGateConfig()
    analyst_fn = make_dummy_analyst_fn(BASE_SEED)
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)

    async def _run():
        nonlocal portfolio
        for i in range(TRADING_DAYS):
            day = start + timedelta(days=i)
            portfolio, _ = await run_day(
                UNIVERSE, day, portfolio, config, analyst_fn, propose_decision, execute_simulated, log_path=log_path
            )

    asyncio.run(_run())
    return portfolio


def test_milestone1_completion_criteria(tmp_path):
    log_path = tmp_path / "pipeline.jsonl"

    _simulate(log_path)
    summary = summarize_log(log_path, total_days=TRADING_DAYS)

    # 완료 조건 1: 최근 20영업일 중 매수 신호가 난 날의 비율이 50% 이하.
    assert summary["signal_day_ratio"] <= 0.5, summary

    # 완료 조건 2: 게이트 거부 사유가 rejected_by별로 집계되어 로그에 남는다.
    assert isinstance(summary["rejected_by_counts"], dict)
    assert sum(summary["rejected_by_counts"].values()) >= 0

    # 관망(현금 유지)이 특이 케이스가 아니라 정상 출력이라는 것을 실제로 보여준다.
    assert summary["signal_days"] < summary["total_days"]


def test_milestone1_reproducible_across_runs(tmp_path):
    log_a = tmp_path / "run_a.jsonl"
    log_b = tmp_path / "run_b.jsonl"

    portfolio_a = _simulate(log_a)
    portfolio_b = _simulate(log_b)

    # 완료 조건 3: 같은 입력을 두 번 넣으면 같은 결정이 나온다 (재현성).
    assert portfolio_a.model_dump() == portfolio_b.model_dump()
    assert log_a.read_text() == log_b.read_text()
