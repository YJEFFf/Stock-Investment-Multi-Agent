"""scripts/decide_llm_sell.py — 장 마감 후(15:35) LLM 재량 매도 "판단만" 하는
진입점. 판단 자체(judgment.judge_sell)는 tests/test_judgment.py가 이미
검증하므로, 여기서는 이 스크립트가 실제로 집행하지 않고(포트폴리오/브로커를
안 건드림) 결과를 logs/pending_sells.json에 정확히 남기는지만 본다.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import scripts.decide_llm_sell as dls
from src import kis, portfolio_store
from src.schemas import AnalystOpinion, PortfolioState, Position, SellAction


def _position(**overrides) -> Position:
    defaults = dict(ticker="005930", sector="반도체", weight=0.10, entry_price=100.0, peak_price=100.0)
    defaults.update(overrides)
    return Position(**defaults)


@pytest.fixture(autouse=True)
def _no_real_notify_or_name_lookup(monkeypatch):
    # main()이 이제 판단 결과 요약(0건 포함)을 텔레그램으로 보낸다 — 목킹 안 하면
    # 테스트가 실제 텔레그램 메시지를 보내고 실제 네이버를 긁는다.
    monkeypatch.setattr(dls.notify, "send_telegram_alert", lambda message: True)
    monkeypatch.setattr(dls.pipeline, "display_name", lambda ticker: ticker)
    # notion_sync.py가 import 시점에 .env를 읽어 NOTION_*를 채워둘 수 있다 — 로컬에
    # 실제 노션 워크스페이스가 설정돼 있으면 이 값들이 살아있어 테스트가 실제
    # API를 호출해버린다. 노션 동기화 자체는 test_sync_daily_report_* 테스트에서
    # 따로 검증하므로, 그 외 테스트에서는 기본적으로 꺼둔다.
    monkeypatch.delenv("NOTION_DAILY_REPORT_DB_ID", raising=False)
    # main()이 매매일지 동기화도 부른다(장중 매도를 그날 안에 올리려고) — 같은 이유로
    # 끈다. 이 키가 살아있으면 테스트가 실제 노션 매매일지에 행을 쓴다.
    monkeypatch.delenv("NOTION_TRADE_JOURNAL_DB_ID", raising=False)


def test_noop_when_not_trading_day(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: False)

    def fail(*a, **k):
        raise AssertionError("휴장일엔 KIS를 호출하면 안 된다")

    monkeypatch.setattr(kis, "fetch_current_price", fail)

    asyncio.run(dls.main())
    assert not dls.PENDING_SELLS_PATH.exists()


def test_writes_empty_actions_when_no_positions(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=1.0))

    asyncio.run(dls.main())

    payload = json.loads(dls.PENDING_SELLS_PATH.read_text())
    assert payload["actions"] == []


def test_decides_sell_without_executing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)

    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])
    portfolio_store.save_portfolio(portfolio)

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 105.0)

    async def fake_analyst_fn(ticker, sector, day):
        return [AnalystOpinion(agent="chart", ticker=ticker, score=-0.6, confidence=0.7, evidence=["e"], as_of=day)]

    monkeypatch.setattr(dls.pipeline, "make_combined_analyst_fn", lambda fns: fake_analyst_fn)

    captured = {}

    async def fake_judge_sell(ticker, opinions, unrealized_pct):
        captured["called_with"] = (ticker, len(opinions), round(unrealized_pct, 4))
        return SellAction(ticker=ticker, reason="llm_discretionary", sell_fraction=1.0, reasoning="근거 약화")

    monkeypatch.setattr(dls.judgment, "judge_sell", fake_judge_sell)

    asyncio.run(dls.main())

    # 포트폴리오는 절대 안 바뀐다 — 판단만 하고 집행은 다음날 execute_open.py 몫이다.
    assert portfolio_store.load_portfolio() == portfolio

    assert captured["called_with"] == ("005930", 1, 0.05)

    payload = json.loads(dls.PENDING_SELLS_PATH.read_text())
    assert len(payload["actions"]) == 1
    assert payload["actions"][0]["ticker"] == "005930"
    assert payload["actions"][0]["reason"] == "llm_discretionary"
    assert payload["actions"][0]["reasoning"] == "근거 약화"


def test_skips_position_when_price_unavailable(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=0.90, positions=[_position()]))

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: None)

    def fail(*a, **k):
        raise AssertionError("시세 조회가 실패했으면 분석가/judge_sell을 부르면 안 된다")

    monkeypatch.setattr(dls.pipeline, "make_combined_analyst_fn", lambda fns: fail)
    monkeypatch.setattr(dls.judgment, "judge_sell", fail)

    asyncio.run(dls.main())

    payload = json.loads(dls.PENDING_SELLS_PATH.read_text())
    assert payload["actions"] == []


def test_sync_daily_report_skipped_when_not_configured(monkeypatch):
    monkeypatch.delenv("NOTION_DAILY_REPORT_DB_ID", raising=False)

    async def fail(*a, **k):
        raise AssertionError("설정 안 됐으면 notion_sync를 호출하면 안 된다")

    monkeypatch.setattr(dls.notion_sync, "sync_daily_report", fail)

    asyncio.run(dls._sync_daily_report(None, PortfolioState(cash_weight=1.0)))


def test_sync_daily_report_called_when_configured(monkeypatch):
    monkeypatch.setenv("NOTION_DAILY_REPORT_DB_ID", "db-report")

    from datetime import date

    monkeypatch.setattr(dls.kis, "fetch_account_balance", lambda: 100_000_000.0)

    captured = {}

    async def fake_sync(day, portfolio, db_id, total_value=None):
        captured["args"] = (day, db_id, total_value)
        return True

    monkeypatch.setattr(dls.notion_sync, "sync_daily_report", fake_sync)

    portfolio = PortfolioState(cash_weight=1.0)
    asyncio.run(dls._sync_daily_report(date(2026, 8, 12), portfolio))

    assert captured["args"] == ("2026-08-12", "db-report", 100_000_000.0)


def test_sync_daily_report_passes_none_total_value_when_balance_unavailable(monkeypatch):
    monkeypatch.setenv("NOTION_DAILY_REPORT_DB_ID", "db-report")

    from datetime import date

    monkeypatch.setattr(dls.kis, "fetch_account_balance", lambda: None)

    captured = {}

    async def fake_sync(day, portfolio, db_id, total_value=None):
        captured["args"] = (day, db_id, total_value)
        return True

    monkeypatch.setattr(dls.notion_sync, "sync_daily_report", fake_sync)

    portfolio = PortfolioState(cash_weight=1.0)
    asyncio.run(dls._sync_daily_report(date(2026, 8, 12), portfolio))

    assert captured["args"] == ("2026-08-12", "db-report", None)


def test_sync_daily_report_sends_error_alert_on_failure(monkeypatch):
    monkeypatch.setenv("NOTION_DAILY_REPORT_DB_ID", "db-report")

    from datetime import date

    monkeypatch.setattr(dls.kis, "fetch_account_balance", lambda: 100_000_000.0)

    async def fail(day, portfolio, db_id, total_value=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(dls.notion_sync, "sync_daily_report", fail)

    alerts = []
    monkeypatch.setattr(dls.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    asyncio.run(dls._sync_daily_report(date(2026, 8, 12), PortfolioState(cash_weight=1.0)))

    assert len(alerts) == 1
    assert "노션 일일 리포트 동기화 실패" in alerts[0]


def test_hold_when_judge_sell_returns_none(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=0.90, positions=[_position()]))

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 105.0)

    async def fake_analyst_fn(ticker, sector, day):
        return []

    monkeypatch.setattr(dls.pipeline, "make_combined_analyst_fn", lambda fns: fake_analyst_fn)

    async def fake_judge_sell(ticker, opinions, unrealized_pct):
        return None

    monkeypatch.setattr(dls.judgment, "judge_sell", fake_judge_sell)

    asyncio.run(dls.main())

    payload = json.loads(dls.PENDING_SELLS_PATH.read_text())
    assert payload["actions"] == []


def test_main_syncs_the_trade_journal_before_the_daily_report(monkeypatch, tmp_path):
    """장중 매도(check_stop_loss, 1분 주기)는 09:00 동기화 이후에 난다. 15:35에도
    한 번 올려야 그날 안에 매매일지에서 보인다 — 2026-08-18에 13:22 매도 2건이
    그날 매매일지에 없었던 게 이 때문이다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=1.0))

    calls = []

    async def fake_journal_sync(log_path):
        calls.append("journal")
        return {"synced": 1, "failed": 0, "skipped": 0}

    async def fake_report(today_kst, portfolio):
        calls.append("report")

    monkeypatch.setattr(dls.notion_sync, "sync_trade_journal_if_configured", fake_journal_sync)
    monkeypatch.setattr(dls, "_sync_daily_report", fake_report)

    asyncio.run(dls.main())

    assert calls == ["journal", "report"]


# --- 복구를 못 본 채 장이 끝난 시세 공백 (2026-08-21 15:07~15:29) ---


def test_reports_a_blackout_that_never_recovered_before_close(monkeypatch, tmp_path):
    """복구 알림은 "시세가 돌아온 회차"에서만 나온다. 공백이 그대로 마감으로
    이어지면 그 회차가 영영 안 오므로, 장 마감 후 첫 실행인 이 스크립트가 닫는다."""
    from datetime import datetime, timedelta

    from src import notify

    # 같은 날 안에서만 이어 센다(자정을 넘긴 상태 파일은 무시) — 그래서 고정 날짜가
    # 아니라 "오늘 장중"으로 만든다. 운영에서도 15:35는 공백과 같은 날이다.
    monkeypatch.setattr(notify, "BLACKOUT_STATE_DIR", tmp_path / "markers")
    now = datetime.now(notify.KST)
    notify.track_blackout(dls.pipeline.BLACKOUT_CONTEXT, True, now=now - timedelta(minutes=28))
    notify.track_blackout(dls.pipeline.BLACKOUT_CONTEXT, True, now=now - timedelta(minutes=6))

    alerts = []
    monkeypatch.setattr(dls.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    dls._report_unresolved_blackout()

    assert len(alerts) == 1
    assert "공백인 채로 장 마감" in alerts[0]


def test_no_blackout_report_when_the_day_was_clean(monkeypatch, tmp_path):
    from src import notify

    monkeypatch.setattr(notify, "BLACKOUT_STATE_DIR", tmp_path / "markers")

    alerts = []
    monkeypatch.setattr(dls.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    dls._report_unresolved_blackout()

    assert alerts == []


def test_main_logs_the_monitoring_summary(monkeypatch, tmp_path):
    """감시 지표가 매일 실제로 남아야 한다.

    이 집계는 원래 run_daily.py 안에만 있었고, 스크립트를 쪼갤 때 딸려가지 않아
    크론 어디에서도 안 불렸다 — 2026-08-24까지 cron.log에 monitoring_signal_rate가
    0회였다. 장 마감 작업이 그날 마지막 크론이라 여기가 제자리다.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=1.0))

    called = []
    monkeypatch.setattr(dls.pipeline, "log_monitoring_summary", lambda: called.append(True))

    asyncio.run(dls.main())

    assert called == [True]


def test_monitoring_summary_failure_does_not_break_the_sell_path(monkeypatch, tmp_path):
    """지표 집계가 터져도 장 마감 처리는 끝나야 한다 — 읽기 전용 리포트일 뿐이다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=1.0))

    def boom():
        raise RuntimeError("집계 실패")

    monkeypatch.setattr(dls.pipeline, "log_monitoring_summary", boom)

    asyncio.run(dls.main())  # 예외가 새어나오면 안 된다

    payload = json.loads(dls.PENDING_SELLS_PATH.read_text())
    assert payload["actions"] == []



def test_a_ticker_skipped_for_price_failure_still_appears_in_the_judgment_log(monkeypatch, tmp_path):
    """빠진 종목이 파일에 아예 없으면, 읽는 사람이 "판단하고 안 팔았다"와 "판단을
    못 했다"를 기록의 *부재*로 역추정해야 한다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dls, "is_krx_trading_day", lambda day: True)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=0.90, positions=[_position()]))

    log_path = tmp_path / "sell_judgment.jsonl"
    monkeypatch.setattr(dls.judgment, "DEFAULT_SELL_JUDGMENT_LOG_PATH", log_path)
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: None)

    asyncio.run(dls.main())

    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert [(r["outcome"], r["reason"], r["ticker"]) for r in rows] == [
        ("skipped", "price_unavailable", "005930")
    ]
    assert rows[0]["unrealized_pct"] is None  # 시세가 없으니 손익도 없다 — 0.0으로 지어내지 않는다
