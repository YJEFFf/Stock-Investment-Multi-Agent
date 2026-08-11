"""scripts/run_daily.py의 오케스트레이션(main())을 검증한다 — 개별 단계
(evaluate_holdings/run_daily/notify/notion_sync)는 각자의 테스트 파일에서
이미 충분히 다뤄지므로, 여기서는 그 단계들이 cron 진입점 안에서 올바른
순서·인자로 서로 이어지는지만 확인한다. 전부 mock — 실제 KIS/LLM/텔레그램/
노션 호출은 하나도 없다.
"""

import asyncio
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import scripts.run_daily as rd
from src import portfolio_store
from src.schemas import Decision, GateResult, PortfolioState, Position

# _isolate 픽스처가 테스트 전체에서 rd._sync_notion/_log_monitoring_summary를
# no-op으로 덮어쓰기 때문에, 그 함수들 자체의 동작을 검증하는 테스트는 몽키패치가
# 걸리기 전의 실제 함수를 붙잡아뒀다가 그걸 직접 호출해야 한다.
_real_sync_notion = rd._sync_notion
_real_log_monitoring_summary = rd._log_monitoring_summary


def _decision(ticker="005930", action="BUY", approved=True):
    from src.schemas import AnalystOpinion

    d = Decision(
        ticker=ticker,
        action=action,
        reason="test",
        inputs=[AnalystOpinion(agent="chart", ticker=ticker, score=0.9, confidence=0.9, evidence=["e"], as_of=date(2026, 8, 10))],
        degraded=False,
    )
    return d, GateResult(approved=approved, rejected_by=None if approved else "position_limit")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(portfolio_store, "PORTFOLIO_STATE_PATH", tmp_path / "portfolio_state.json")
    monkeypatch.setattr(rd, "is_krx_trading_day", lambda day: True)
    monkeypatch.setattr(rd, "_log_monitoring_summary", lambda: None)
    monkeypatch.setattr(rd, "_sync_notion", lambda today, portfolio: None)
    monkeypatch.setattr(rd.notify, "send_telegram_alert", lambda message: True)


def test_main_skips_everything_on_non_trading_day(monkeypatch):
    monkeypatch.setattr(rd, "is_krx_trading_day", lambda day: False)

    calls = {"evaluate_holdings": 0, "run_daily": 0, "monitoring": 0, "notion": 0}

    async def fail_evaluate(*a, **k):
        calls["evaluate_holdings"] += 1
        raise AssertionError("휴장일엔 evaluate_holdings가 호출되면 안 된다")

    async def fail_run_daily(*a, **k):
        calls["run_daily"] += 1
        raise AssertionError("휴장일엔 run_daily가 호출되면 안 된다")

    monkeypatch.setattr(rd.pipeline, "evaluate_holdings", fail_evaluate)
    monkeypatch.setattr(rd.pipeline, "run_daily", fail_run_daily)
    monkeypatch.setattr(rd, "_log_monitoring_summary", lambda: calls.__setitem__("monitoring", calls["monitoring"] + 1))
    monkeypatch.setattr(rd, "_sync_notion", lambda today, portfolio: calls.__setitem__("notion", calls["notion"] + 1))

    asyncio.run(rd.main())

    assert calls == {"evaluate_holdings": 0, "run_daily": 0, "monitoring": 0, "notion": 0}
    assert not portfolio_store.PORTFOLIO_STATE_PATH.exists()


def test_main_happy_path_runs_all_stages_in_order(monkeypatch):
    order = []
    starting_portfolio = PortfolioState(cash_weight=1.0)
    after_sell = PortfolioState(cash_weight=1.0, positions=[])
    decision, gate = _decision()
    after_buy = PortfolioState(cash_weight=0.92, positions=[Position(ticker="005930", sector="반도체", weight=0.08)])

    async def fake_evaluate_holdings(portfolio, day, sell_execute_fn, analyst_fn=None, judge_sell_fn=None):
        order.append("evaluate_holdings")
        assert portfolio == starting_portfolio
        return after_sell

    async def fake_run_daily(day, portfolio, config, analyst_fn, judge_fn, execute_fn, total_expected_analysts):
        order.append("run_daily")
        assert portfolio == after_sell
        return after_buy, [(decision, gate)]

    monkeypatch.setattr(rd.pipeline, "evaluate_holdings", fake_evaluate_holdings)
    monkeypatch.setattr(rd.pipeline, "run_daily", fake_run_daily)

    monitoring_calls = []
    notion_calls = []
    monkeypatch.setattr(rd, "_log_monitoring_summary", lambda: (order.append("monitoring"), monitoring_calls.append(True)))
    monkeypatch.setattr(
        rd, "_sync_notion", lambda today, portfolio: (order.append("notion"), notion_calls.append((today, portfolio)))
    )

    asyncio.run(rd.main())

    assert order == ["evaluate_holdings", "run_daily", "monitoring", "notion"]
    assert notion_calls[0][1] == after_buy

    saved = PortfolioState.model_validate_json(portfolio_store.PORTFOLIO_STATE_PATH.read_text())
    assert saved == after_buy


def test_main_saves_portfolio_after_evaluate_holdings_even_if_run_daily_fails(monkeypatch):
    after_sell = PortfolioState(cash_weight=0.95, positions=[])

    async def fake_evaluate_holdings(*a, **k):
        return after_sell

    async def failing_run_daily(*a, **k):
        raise RuntimeError("run_daily boom")

    monkeypatch.setattr(rd.pipeline, "evaluate_holdings", fake_evaluate_holdings)
    monkeypatch.setattr(rd.pipeline, "run_daily", failing_run_daily)

    with pytest.raises(RuntimeError):
        asyncio.run(rd.main())

    saved = PortfolioState.model_validate_json(portfolio_store.PORTFOLIO_STATE_PATH.read_text())
    assert saved == after_sell  # evaluate_holdings 이후 저장은 살아있어야 한다


def test_main_continues_to_buy_judgment_when_evaluate_holdings_fails(monkeypatch):
    alerts = []
    monkeypatch.setattr(rd.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    async def failing_evaluate_holdings(*a, **k):
        raise RuntimeError("evaluate_holdings boom")

    run_daily_called = {"n": 0}

    async def fake_run_daily(day, portfolio, config, analyst_fn, judge_fn, execute_fn, total_expected_analysts):
        run_daily_called["n"] += 1
        assert portfolio == PortfolioState()  # evaluate_holdings 실패 -> 원래 로드한 포트폴리오 그대로 다음 단계로
        return portfolio, []

    monkeypatch.setattr(rd.pipeline, "evaluate_holdings", failing_evaluate_holdings)
    monkeypatch.setattr(rd.pipeline, "run_daily", fake_run_daily)

    asyncio.run(rd.main())

    assert run_daily_called["n"] == 1
    assert len(alerts) == 1
    assert "evaluate_holdings 실패" in alerts[0]
    assert "RuntimeError" in alerts[0]
    # evaluate_holdings가 실패해도 run_daily는 실패 전 포트폴리오 그대로 이어받아 저장한다.
    saved = PortfolioState.model_validate_json(portfolio_store.PORTFOLIO_STATE_PATH.read_text())
    assert saved == PortfolioState()


def test_sync_notion_skips_when_neither_db_configured(monkeypatch):
    monkeypatch.delenv("NOTION_TRADE_JOURNAL_DB_ID", raising=False)
    monkeypatch.delenv("NOTION_DAILY_REPORT_DB_ID", raising=False)

    def fail(*a, **k):
        raise AssertionError("설정 안 됐으면 notion_sync를 호출하면 안 된다")

    monkeypatch.setattr(rd.notion_sync, "sync_trade_journal", fail)
    monkeypatch.setattr(rd.notion_sync, "sync_daily_report", fail)

    _real_sync_notion(date(2026, 8, 10), PortfolioState())


def test_sync_notion_calls_both_when_configured(monkeypatch):
    monkeypatch.setenv("NOTION_TRADE_JOURNAL_DB_ID", "db-trade")
    monkeypatch.setenv("NOTION_DAILY_REPORT_DB_ID", "db-report")

    captured = {}
    monkeypatch.setattr(
        rd.notion_sync, "sync_trade_journal", lambda log_path, db_id: captured.setdefault("trade_db", db_id)
    )
    monkeypatch.setattr(
        rd.notion_sync,
        "sync_daily_report",
        lambda day, portfolio, db_id: captured.setdefault("report_call", (day, db_id)),
    )

    portfolio = PortfolioState(cash_weight=0.5)
    _real_sync_notion(date(2026, 8, 10), portfolio)

    assert captured["trade_db"] == "db-trade"
    assert captured["report_call"] == ("2026-08-10", "db-report")


def test_sync_notion_sends_error_alert_on_failure(monkeypatch):
    monkeypatch.setenv("NOTION_TRADE_JOURNAL_DB_ID", "db-trade")
    monkeypatch.delenv("NOTION_DAILY_REPORT_DB_ID", raising=False)

    def failing_sync(*a, **k):
        raise RuntimeError("notion boom")

    monkeypatch.setattr(rd.notion_sync, "sync_trade_journal", failing_sync)

    alerts = []
    monkeypatch.setattr(rd.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    _real_sync_notion(date(2026, 8, 10), PortfolioState())

    assert len(alerts) == 1
    assert "노션 동기화 실패" in alerts[0]


def test_log_monitoring_summary_handles_missing_logs_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(rd.pipeline, "DEFAULT_LOG_PATH", tmp_path / "pipeline.jsonl")
    monkeypatch.setattr(rd.llm, "DEFAULT_LLM_CALL_LOG_PATH", tmp_path / "llm_calls.jsonl")

    _real_log_monitoring_summary()  # 로그 파일이 아예 없어도 예외 없이 끝나야 한다
