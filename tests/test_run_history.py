import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from src import collectors, pipeline, run_history
from src.schemas import DataCollectionUnavailable, MarketContext, PortfolioState, RiskGateConfig


def _record():
    return run_history.new_run(datetime(2026, 10, 6, 8, 30, tzinfo=run_history.KST), "codex_plan", "gpt-5.6-sol")


def test_all_price_collection_failures_are_not_normal_hold(monkeypatch):
    monkeypatch.setattr(collectors, "fetch_kospi200_index_bars", lambda n: None)
    monkeypatch.setattr(collectors, "fetch_market_context", lambda t, n: None)
    record = _record()
    token = run_history.CURRENT.set(record)
    try:
        with pytest.raises(DataCollectionUnavailable, match="all_market_contexts_unavailable"):
            asyncio.run(pipeline.quant_prefilter([("AAA", ""), ("BBB", "")]))
        assert record.collection_failed == 2
        assert not record.analysis_started
    finally:
        run_history.CURRENT.reset(token)


def test_zero_candidates_is_completed_collection_and_claims_day(monkeypatch):
    async def build():
        return [("AAA", "")]
    monkeypatch.setattr(pipeline, "build_universe_with_sectors", build)
    monkeypatch.setattr(collectors, "fetch_kospi200_index_bars", lambda n: None)
    monkeypatch.setattr(collectors, "fetch_market_context", lambda t, n: MarketContext(
        ticker=t, as_of=datetime.now(run_history.KST), bars=[], indicators={"rsi14": 50}))
    async def should_not_call(*a):
        raise AssertionError("0후보에는 분석가를 부르지 않는다")
    record = _record()
    token = run_history.CURRENT.set(record)
    try:
        result = asyncio.run(pipeline.run_daily(record.started_at, PortfolioState(), RiskGateConfig(),
                                               should_not_call, should_not_call, should_not_call))
        assert result[1] == []
        assert record.candidates == 0 and record.collection_failed == 0
        assert run_history.already_analyzed(record.day, pipeline.DEFAULT_LOG_PATH)
    finally:
        run_history.CURRENT.reset(token)


def test_calendar_window_does_not_pull_old_signal_days_forward(tmp_path):
    log = tmp_path / "decisions.jsonl"
    log.write_text(json.dumps({"day": "2026-08-12", "ticker": "AAA", "action": "BUY", "approved": True}) + "\n")
    result = pipeline.summarize_recent_trading_days(log, 20, as_of=date(2026, 10, 2))
    assert result["total_days"] == 20 and result["signal_days"] == 0
    assert result["days_without_decision_log"] == 20
    assert result["coverage"]["from"] == "2026-09-03"


def test_nav_survives_missing_observation_without_fabricating_daily_return():
    snapshot = lambda t: SimpleNamespace(total=t, cash=t / 2, securities=t / 2, holdings=[])
    run_history.record_nav("2026-10-06", snapshot(100))
    run_history.record_nav("2026-10-07", None)
    run_history.record_nav("2026-10-08", snapshot(90))
    rows = [json.loads(line) for line in run_history.NAV_LOG_PATH.read_text().splitlines()]
    assert rows[1]["status"] == "unavailable"
    assert rows[-1]["daily_return"] is None
    assert rows[-1]["return_since_first_observation"] == pytest.approx(-0.1)
    assert rows[-1]["max_drawdown_since_first_observation"] == pytest.approx(-0.1)


def test_normal_empty_disclosure_is_distinct_from_failed_company_news():
    record = _record()
    record.degraded_decisions = 2  # 정상 공시 없음 때문에 빠진 의견일 수 있다.
    token = run_history.CURRENT.set(record)
    try:
        run_history.observe("AAA", "disclosure", [])
        assert not run_history.input_failed(record)
        assert record.sources[-1].status == "empty"
        run_history.observe("AAA", "company_news", None)
        run_history.observe("AAA", "sector_news", ["background"], required=False)
        assert run_history.input_failed(record)
        assert record.sources[-2].status == "failed"
    finally:
        run_history.CURRENT.reset(token)
