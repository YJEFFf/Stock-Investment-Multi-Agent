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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 100.0)

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
