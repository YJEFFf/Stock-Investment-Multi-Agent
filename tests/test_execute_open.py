"""scripts/execute_open.py — 09:00 KST 장 시작 집행 전담 진입점.

pipeline.finalize_sell/execute_buy_order 자체의 동작(체결가 조회, 로그, 알림)은
각자의 테스트 파일에서 이미 검증된다. 여기서는 execute_open.py가:
1. pending_sells.json을 pending_buys.json보다 먼저 집행하는지(매도 먼저)
2. pending_buys.json의 day가 오늘과 다르면 매수를 스킵하는지
3. 파일이 아예 없을 때 조용히 넘어가는지
4. 다 쓴 pending 파일을 소비(삭제)하는지
5. pending_sells.json이 너무 오래됐으면 실행하지 않고 지우는지
만 확인한다 — 그래서 pipeline.finalize_sell/execute_buy_order를 직접 가짜로
바꿔치기해서 호출 순서/인자만 관찰한다.
"""

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import scripts.execute_open as eo
from src import kis, portfolio_store
from src.schemas import AnalystOpinion, Decision, GateResult, PortfolioState, Position, SellAction

TODAY = date(2026, 8, 12)


def _position(**overrides) -> Position:
    defaults = dict(ticker="005930", sector="반도체", weight=0.10, entry_price=100.0, peak_price=100.0, quantity=10)
    defaults.update(overrides)
    return Position(**defaults)


def _decision_payload(ticker="000660", sector="반도체", trade_weight=0.08) -> dict:
    decision = Decision(
        ticker=ticker,
        action="BUY",
        reason="test",
        inputs=[AnalystOpinion(agent="chart", ticker=ticker, score=0.9, confidence=0.9, evidence=["e"], as_of=date(2026, 8, 11))],
        degraded=False,
    )
    gate = GateResult(approved=True, rejected_by=None)
    return {
        "ticker": ticker,
        "sector": sector,
        "trade_weight": trade_weight,
        "decision": decision.model_dump(mode="json"),
        "gate_result": gate.model_dump(mode="json"),
    }


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    import datetime as _dt

    class _FixedDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(TODAY.year, TODAY.month, TODAY.day, 9, 0, tzinfo=tz)

    monkeypatch.setattr(eo, "datetime", _FixedDatetime)
    monkeypatch.setattr(eo, "is_krx_trading_day", lambda day: True)
    monkeypatch.setattr(eo.notify, "send_telegram_alert", lambda message: True)
    # notion_sync.py가 import 시점에 .env를 읽어 NOTION_*를 채워둘 수 있다 — 로컬에
    # 실제 노션 워크스페이스가 설정돼 있으면 이 값들이 살아있어 테스트가 실제
    # API를 호출해버린다. 노션 동기화 자체는 test_sync_trade_journal_* 테스트에서
    # 따로 검증하므로, 그 외 테스트에서는 기본적으로 꺼둔다.
    monkeypatch.delenv("NOTION_TRADE_JOURNAL_DB_ID", raising=False)

    # execute_buy_order/finalize_sell이 텔레그램 알림 전 판단 로그를 한국어로
    # 옮기는 단계(src/translate.py)가 실제 Claude API를 타지 않게 막는다.
    async def _identity(text, label="translate"):
        return text

    monkeypatch.setattr(eo.pipeline.translate, "to_korean", _identity)

    # execute_open은 락을 놓기 전에 손절/익절을 한 번 평가한다(09:01 사각지대 대응).
    # 그 경로가 실제 KIS를 때리지 않게 막는다 — .env에 자격증명이 살아있는 머신에서는
    # 진짜 시세가 돌아와, entry_price=100짜리 더미 포지션이 익절 문턱을 넘어버린다.
    # 기본값은 "조회 불가"라 평가가 아무것도 하지 않는다. 평가가 실제로 도는지는
    # test_open_run_also_evaluates_holdings에서 따로 건다.
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: None)
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days=60, **kw: None)


def test_noop_when_neither_pending_file_exists(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=1.0))

    def fail(*a, **k):
        raise AssertionError("집행할 게 없으면 KIS를 호출하면 안 된다")

    monkeypatch.setattr(eo.pipeline, "finalize_sell", fail)
    monkeypatch.setattr(eo.pipeline, "execute_buy_order", fail)

    asyncio.run(eo.main())

    assert portfolio_store.load_portfolio() == PortfolioState(cash_weight=1.0)


def test_sells_execute_before_buys(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])
    portfolio_store.save_portfolio(portfolio)

    eo.PENDING_SELLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    eo.PENDING_SELLS_PATH.write_text(
        json.dumps(
            {
                "decided_on": (TODAY - timedelta(days=1)).isoformat(),
                "actions": [{"ticker": "005930", "reason": "llm_discretionary", "sell_fraction": 1.0, "reasoning": "r"}],
            }
        )
    )
    eo.PENDING_BUYS_PATH.write_text(json.dumps({"day": TODAY.isoformat(), "decisions": [_decision_payload()]}))

    order = []

    async def fake_finalize_sell(portfolio, action, position, current_price, day, sell_execute_fn, log_path, tj_path):
        order.append("sell")
        return PortfolioState(cash_weight=portfolio.cash_weight + position.weight, positions=[])

    async def fake_execute_buy_order(decision, gate_result, portfolio, sector, trade_weight):
        order.append("buy")
        return PortfolioState(
            cash_weight=portfolio.cash_weight - trade_weight,
            positions=[*portfolio.positions, Position(ticker=decision.ticker, sector=sector, weight=trade_weight)],
        )

    monkeypatch.setattr(eo.pipeline, "finalize_sell", fake_finalize_sell)
    monkeypatch.setattr(eo.pipeline, "execute_buy_order", fake_execute_buy_order)
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 100.0)

    asyncio.run(eo.main())

    assert order == ["sell", "buy"]
    assert not eo.PENDING_SELLS_PATH.exists()
    assert not eo.PENDING_BUYS_PATH.exists()

    saved = portfolio_store.load_portfolio()
    assert saved.cash_weight == pytest.approx(0.90 + 0.10 - 0.08)
    assert [p.ticker for p in saved.positions] == ["000660"]


def test_skips_buys_when_pending_day_does_not_match_today(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=1.0))

    stale_day = (TODAY - timedelta(days=1)).isoformat()
    eo.PENDING_BUYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    eo.PENDING_BUYS_PATH.write_text(json.dumps({"day": stale_day, "decisions": [_decision_payload()]}))

    def fail(*a, **k):
        raise AssertionError("날짜가 안 맞으면 매수를 집행하면 안 된다")

    monkeypatch.setattr(eo.pipeline, "execute_buy_order", fail)

    alerts = []
    monkeypatch.setattr(eo.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    asyncio.run(eo.main())

    assert len(alerts) == 1
    assert "날짜 불일치" in alerts[0]
    # 날짜가 안 맞는 pending_buys.json은 지우지 않는다 — decide_buys가 뒤늦게라도
    # 같은 날 다시 성공하면 그걸로 덮어써질 수 있어야 하고, 원인 파악용으로도 남겨둔다.
    assert eo.PENDING_BUYS_PATH.exists()


def test_stale_pending_sells_are_skipped_and_removed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])
    portfolio_store.save_portfolio(portfolio)

    very_old = (TODAY - timedelta(days=10)).isoformat()
    eo.PENDING_SELLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    eo.PENDING_SELLS_PATH.write_text(
        json.dumps({"decided_on": very_old, "actions": [{"ticker": "005930", "reason": "llm_discretionary", "sell_fraction": 1.0}]})
    )

    def fail(*a, **k):
        raise AssertionError("너무 오래된 결정을 집행하면 안 된다")

    monkeypatch.setattr(eo.pipeline, "finalize_sell", fail)

    alerts = []
    monkeypatch.setattr(eo.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    asyncio.run(eo.main())

    assert any("오래됨" in a for a in alerts)
    assert not eo.PENDING_SELLS_PATH.exists()
    assert portfolio_store.load_portfolio() == portfolio  # 아무 변화 없음


def test_pending_sell_for_ticker_no_longer_held_is_skipped(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    portfolio = PortfolioState(cash_weight=1.0, positions=[])  # 이미 다 청산된 상태
    portfolio_store.save_portfolio(portfolio)

    eo.PENDING_SELLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    eo.PENDING_SELLS_PATH.write_text(
        json.dumps(
            {
                "decided_on": (TODAY - timedelta(days=1)).isoformat(),
                "actions": [{"ticker": "005930", "reason": "llm_discretionary", "sell_fraction": 1.0}],
            }
        )
    )

    def fail(*a, **k):
        raise AssertionError("보유하지 않은 종목을 팔려고 하면 안 된다")

    monkeypatch.setattr(eo.pipeline, "finalize_sell", fail)

    asyncio.run(eo.main())

    assert not eo.PENDING_SELLS_PATH.exists()
    assert portfolio_store.load_portfolio() == portfolio


def test_sync_trade_journal_skipped_when_not_configured(monkeypatch):
    monkeypatch.delenv("NOTION_TRADE_JOURNAL_DB_ID", raising=False)

    async def fail(*a, **k):
        raise AssertionError("설정 안 됐으면 notion_sync를 호출하면 안 된다")

    monkeypatch.setattr(eo.notion_sync, "sync_trade_journal", fail)

    asyncio.run(eo._sync_trade_journal())


def test_sync_trade_journal_called_when_configured(monkeypatch):
    monkeypatch.setenv("NOTION_TRADE_JOURNAL_DB_ID", "db-trade")

    captured = {}

    async def fake_sync(log_path, db_id):
        captured["db_id"] = db_id
        return {"synced": 1, "failed": 0, "skipped": 0}

    monkeypatch.setattr(eo.notion_sync, "sync_trade_journal", fake_sync)

    asyncio.run(eo._sync_trade_journal())

    assert captured["db_id"] == "db-trade"


def test_sync_trade_journal_sends_error_alert_on_failure(monkeypatch):
    monkeypatch.setenv("NOTION_TRADE_JOURNAL_DB_ID", "db-trade")

    async def fail(log_path, db_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(eo.notion_sync, "sync_trade_journal", fail)

    alerts = []
    monkeypatch.setattr(eo.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    asyncio.run(eo._sync_trade_journal())

    assert len(alerts) == 1
    assert "노션 매매일지 동기화 실패" in alerts[0]


def test_pending_sell_is_carried_over_when_price_is_unavailable(monkeypatch, tmp_path):
    """시세 조회 실패로 못 판 매도를 파일에서 지우면, LLM이 "나가라"고 한
    포지션을 아무도 모르게 계속 들고 있게 된다. 매수와 달리 매도는 침묵이
    안전한 방향이 아니다 — 다음 거래일에 다시 시도해야 한다."""
    monkeypatch.chdir(tmp_path)
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])
    portfolio_store.save_portfolio(portfolio)

    decided_on = (TODAY - timedelta(days=1)).isoformat()
    eo.PENDING_SELLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    eo.PENDING_SELLS_PATH.write_text(
        json.dumps(
            {
                "decided_on": decided_on,
                "actions": [{"ticker": "005930", "reason": "llm_discretionary", "sell_fraction": 1.0}],
            }
        )
    )

    def fail(*a, **k):
        raise AssertionError("시세를 못 받았으면 집행하면 안 된다")

    monkeypatch.setattr(eo.pipeline, "finalize_sell", fail)
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: None)

    alerts = []
    monkeypatch.setattr(eo.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    asyncio.run(eo.main())

    assert eo.PENDING_SELLS_PATH.exists(), "이월돼야 다음 거래일에 재시도된다"
    payload = json.loads(eo.PENDING_SELLS_PATH.read_text())
    assert [a["ticker"] for a in payload["actions"]] == ["005930"]
    # decided_on을 오늘로 갱신하면 만료가 매일 미뤄져 영원히 재시도된다 —
    # 원래 값을 그대로 둬야 STALE_PENDING_SELLS_DAYS가 계속 세어진다.
    assert payload["decided_on"] == decided_on
    assert any("재시도" in a for a in alerts)
    assert portfolio_store.load_portfolio().positions[0].ticker == "005930"


def test_carried_over_sell_still_expires_by_staleness(monkeypatch, tmp_path):
    """이월이 무한 재시도가 되지 않는지 — decided_on이 보존되므로 4일이 지나면
    기존 만료 경로가 그대로 걷어간다."""
    monkeypatch.chdir(tmp_path)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=0.90, positions=[_position()]))

    too_old = (TODAY - timedelta(days=eo.STALE_PENDING_SELLS_DAYS + 1)).isoformat()
    eo.PENDING_SELLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    eo.PENDING_SELLS_PATH.write_text(
        json.dumps(
            {"decided_on": too_old, "actions": [{"ticker": "005930", "reason": "llm_discretionary", "sell_fraction": 1.0}]}
        )
    )

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: None)
    monkeypatch.setattr(eo.notify, "send_telegram_alert", lambda message: True)

    asyncio.run(eo.main())

    assert not eo.PENDING_SELLS_PATH.exists()


def test_executed_and_unavailable_sells_are_split(monkeypatch, tmp_path):
    """한 건은 집행되고 한 건은 시세 실패일 때, 집행된 것만 빠지고 실패분만 남아야
    한다 — 통째로 남기면 이미 판 종목을 다음날 또 팔려고 든다."""
    monkeypatch.chdir(tmp_path)
    portfolio_store.save_portfolio(
        PortfolioState(
            cash_weight=0.80,
            positions=[_position(), _position(ticker="000660", sector="반도체")],
        )
    )

    eo.PENDING_SELLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    eo.PENDING_SELLS_PATH.write_text(
        json.dumps(
            {
                "decided_on": (TODAY - timedelta(days=1)).isoformat(),
                "actions": [
                    {"ticker": "005930", "reason": "llm_discretionary", "sell_fraction": 1.0},
                    {"ticker": "000660", "reason": "llm_discretionary", "sell_fraction": 1.0},
                ],
            }
        )
    )

    sold = []

    async def fake_finalize_sell(portfolio, action, position, current_price, day, sell_execute_fn, log_path, tj_path):
        sold.append(action.ticker)
        return PortfolioState(
            cash_weight=portfolio.cash_weight + position.weight,
            positions=[p for p in portfolio.positions if p.ticker != action.ticker],
        )

    monkeypatch.setattr(eo.pipeline, "finalize_sell", fake_finalize_sell)
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 100.0 if ticker == "005930" else None)
    monkeypatch.setattr(eo.notify, "send_telegram_alert", lambda message: True)

    asyncio.run(eo.main())

    assert sold == ["005930"]
    payload = json.loads(eo.PENDING_SELLS_PATH.read_text())
    assert [a["ticker"] for a in payload["actions"]] == ["000660"]


# --- 09:01 사각지대 (2026-08-27) ---


def test_open_run_also_evaluates_holdings(monkeypatch, tmp_path):
    """execute_open이 락을 쥔 1분(09:01) 동안 check_stop_loss는 스스로 건너뛴다.
    그 회차를 여기서 대신 돌지 않으면 **매 거래일 09:01이 안전장치의 사각지대**다 —
    2026-08-27에 192820의 당일 저가가 정확히 그 분에 찍혔고 트레일링 익절이 안 나갔다."""
    monkeypatch.chdir(tmp_path)
    # stage>0이라 트레일링 감시 대상. 고점 200 대비 -7%면 186 아래에서 발동한다.
    position = _position(entry_price=100.0, peak_price=200.0, take_profit_stage=1, quantity=9)
    portfolio_store.save_portfolio(PortfolioState(cash_weight=0.90, positions=[position]))

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 180.0)

    sold = []

    async def fake_finalize(portfolio, action, *a, **k):
        sold.append(action)
        return portfolio

    monkeypatch.setattr(eo.pipeline, "finalize_sell", fake_finalize)

    asyncio.run(eo.main())

    assert [a.reason for a in sold] == ["take_profit_trail"]
