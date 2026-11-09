"""The liquidation ledger must conserve shares/cash without reaching a broker order."""
import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src import app_notifications, collectors, exit_only, kis, mobile_data, operating_mode, sell
from src.mobile_app import create_app
from src.mobile_store import Store
from src.schemas import PortfolioState, Position

START = datetime(2026, 11, 9, 16, 15, tzinfo=exit_only.KST)
NEXT = datetime(2026, 11, 10, 9, 1, tzinfo=exit_only.KST)


@pytest.fixture(autouse=True)
def mode(monkeypatch):
    monkeypatch.setattr(operating_mode, "EXIT_ONLY", True)


def quote(price=100, high=None, low=None):
    return kis.Quote(price=price, day_high=high or price, day_low=low or price,
                     open_price=100, prev_close=100)


def initialized(path, *, quantity=9, start=START, stage=0):
    portfolio = PortfolioState(positions=[Position(
        ticker="005930", sector="반도체", weight=.1, entry_price=100, peak_price=100,
        entry_day=date(2026, 11, 2), quantity=quantity, take_profit_stage=stage,
        exit_plan=sell.DEFAULT_EXIT_PLAN)], cash_weight=.9)
    raw = portfolio.model_dump_json().encode()
    account = dict(total=2000, cash=50, securities=100 * quantity, observed_at=start.isoformat(),
                   holdings=[dict(pdno="005930", prdt_name="삼성전자", hldg_qty=quantity, prpr=100)])
    return exit_only.initialize(raw, account, at=start, path=path), raw, account


def test_transition_freezes_positions_and_includes_unsettled_cash(tmp_path):
    path = tmp_path / "ledger.json"
    state, raw, account = initialized(path)
    assert state.portfolio == PortfolioState.model_validate_json(raw)
    assert state.cash == state.initial_cash == 1100  # Not the broker's 50 cash field.
    assert state.source_portfolio_sha256 == hashlib.sha256(raw).hexdigest()
    assert exit_only.load(path) == state
    with pytest.raises(FileExistsError):
        exit_only.initialize(raw, account, at=START, path=path)
    account["holdings"][0]["hldg_qty"] = 8
    with pytest.raises(ValueError, match="quantities differ"):
        exit_only.initialize(raw, account, at=START, path=tmp_path / "wrong.json")


def test_first_partial_trailing_reset_and_cash_are_preserved(tmp_path):
    path = tmp_path / "ledger.json"
    state, raw, _ = initialized(path)
    frozen = state.portfolio.positions[0].exit_plan.model_copy()
    exit_only.apply_quotes(state, {"005930": (quote(120), "kis")}, at=NEXT)
    p = state.portfolio.positions[0]
    assert (p.quantity, p.take_profit_stage, p.peak_price, p.peak_reset_day) == (6, 1, 120, NEXT.date())
    sale = state.sales[0]
    assert sale.reason == "take_profit_first"
    assert state.cash == pytest.approx(1100 + 360 * (1 - state.fee_rate - state.tax_rate))
    assert sale.net_pnl_amount == pytest.approx(360 * (1 - state.fee_rate - state.tax_rate) - 300 * (1 + state.fee_rate))
    exit_only.apply_quotes(state, {"005930": (quote(119, high=140), "kis")}, at=NEXT + timedelta(minutes=1))
    assert len(state.sales) == 1  # Daily high from before reset cannot create repeat sales.
    exit_only.apply_quotes(state, {"005930": (quote(110, high=140), "kis")}, at=NEXT + timedelta(minutes=2))
    assert state.portfolio.positions[0].quantity == 4
    assert state.sales[-1].reason == "take_profit_trail"
    assert state.portfolio.positions[0].exit_plan == frozen
    assert state.source_portfolio == PortfolioState.model_validate_json(raw)
    exit_only.save(state, path)
    assert exit_only.load(path) == state


@pytest.mark.parametrize("quantity", [1, 2])
def test_fraction_rounding_liquidates_dust_like_existing_execution(tmp_path, quantity):
    state, _, _ = initialized(tmp_path / "ledger.json", quantity=quantity)
    exit_only.apply_quotes(state, {"005930": (quote(120), "kis")}, at=NEXT)
    assert state.sales[0].shares_sold == quantity
    assert not state.portfolio.positions
    assert state.completed_at == NEXT


def test_stop_and_range_use_observed_price_never_invent_threshold_fill(tmp_path):
    state, _, _ = initialized(tmp_path / "ledger.json")
    exit_only.apply_quotes(state, {"005930": (quote(95, low=80), "naver")}, at=NEXT)
    assert not state.portfolio.positions
    assert state.sales[0].shares_sold == 9
    assert state.sales[0].exit_price == 95
    assert state.sales[0].from_day_range
    assert state.sales[0].quote_source == "naver"


def test_transition_does_not_replay_prior_intraday_stop(tmp_path):
    at = START.replace(hour=13)
    state, _, _ = initialized(tmp_path / "ledger.json", start=at)
    exit_only.apply_quotes(state, {"005930": (quote(100, high=140, low=80), "kis")}, at=at + timedelta(minutes=1))
    assert state.portfolio.positions[0].peak_price == 100
    assert not state.sales


def test_closing_price_updates_nav_but_never_executes(tmp_path):
    state, _, _ = initialized(tmp_path / "ledger.json")
    exit_only.apply_quotes(state, {"005930": (quote(70), "kis")}, at=NEXT.replace(hour=15, minute=35))
    assert not state.sales
    assert state.nav_history[-1]["total"] == 1730


@pytest.mark.parametrize("bad", [None, quote(0), kis.Quote(price=100), quote(100, high=float("nan")), quote(100, low=101)])
def test_missing_or_invalid_quote_keeps_old_price_and_position(tmp_path, bad):
    state, _, _ = initialized(tmp_path / "ledger.json")
    quotes = {} if bad is None else {"005930": (bad, "kis")}
    exit_only.apply_quotes(state, quotes, at=NEXT)
    assert state.quotes["005930"].observed_at == START
    assert state.missing_quotes == ["005930"]
    assert not state.sales


def test_atomic_ledger_save_failure_keeps_previous_positions_and_cash(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    state, _, _ = initialized(path)
    old = path.read_bytes()
    exit_only.apply_quotes(state, {"005930": (quote(80), "kis")}, at=NEXT)
    monkeypatch.setattr(exit_only.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        exit_only.save(state, path)
    assert path.read_bytes() == old
    assert exit_only.load(path).portfolio.positions[0].quantity == 9


def test_outbox_crash_retry_has_stable_id_and_store_deduplicates(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    state, _, _ = initialized(path)
    original_save = exit_only.save
    received = []
    monkeypatch.setattr(app_notifications, "enqueue_alert", lambda alert: received.append(alert) or True)
    monkeypatch.setattr(exit_only, "save", lambda *a: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(OSError):
        exit_only.flush_outbox(state, path)
    monkeypatch.setattr(exit_only, "save", original_save)
    exit_only.flush_outbox(exit_only.load(path), path)
    assert len(received) == 2 and received[0].id == received[1].id
    store = Store(tmp_path / "app.sqlite3")
    for alert in received:
        store.add_alert(alert)
    assert len(store.alerts()) == 1
    assert not exit_only.load(path).outbox


def test_monitor_uses_only_held_quotes_falls_back_then_stops_when_empty(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    initialized(path)
    calls = []
    monkeypatch.setattr(kis, "fetch_quote", lambda t, **kw: calls.append(t))
    monkeypatch.setattr(collectors, "fetch_naver_quotes", lambda ts: {t: quote(80) for t in ts})
    monkeypatch.setattr(app_notifications, "enqueue_alert", lambda a: True)
    asyncio.run(exit_only.monitor(at=START, path=path))
    assert not calls
    asyncio.run(exit_only.monitor(at=NEXT, path=path))
    assert calls == ["005930"]
    state = exit_only.load(path)
    assert len(state.sales) == 1 and state.completed_at == NEXT
    asyncio.run(exit_only.monitor(at=NEXT + timedelta(minutes=1), path=path))
    assert calls == ["005930"]
    assert len(exit_only.load(path).sales) == 1


def test_one_preopen_quote_does_not_abort_other_holdings(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    state, _, _ = initialized(path)
    second = state.portfolio.positions[0].model_copy(deep=True, update={"ticker": "000660"})
    state.portfolio.positions.append(second)
    state.source_portfolio.positions.append(second.model_copy(deep=True))
    state.names[second.ticker] = "SK하이닉스"
    state.quotes[second.ticker] = state.quotes["005930"].model_copy()
    state.cash = state.initial_cash = 200
    exit_only.save(state, path)
    fallback_requests = []
    monkeypatch.setattr(kis, "fetch_quote", lambda t, **kw: kis.Quote(price=100) if t == "005930" else quote(80))
    monkeypatch.setattr(collectors, "fetch_naver_quotes", lambda ts: fallback_requests.extend(ts) or {})
    monkeypatch.setattr(app_notifications, "enqueue_alert", lambda a: True)
    asyncio.run(exit_only.monitor(at=NEXT, path=path))
    state = exit_only.load(path)
    assert fallback_requests == ["005930"]
    assert state.missing_quotes == ["005930"]
    assert [s.ticker for s in state.sales] == ["000660"]
    assert [p.ticker for p in state.portfolio.positions] == ["005930"]


@pytest.mark.parametrize("mutate", ["cash", "quantity", "plan", "quote"])
def test_damaged_ledger_fails_closed_without_broker_resurrection(tmp_path, mutate):
    path = tmp_path / "exit_only_state.json"
    state, _, _ = initialized(path)
    if mutate == "cash":
        state.cash += 1
    elif mutate == "quantity":
        state.portfolio.positions[0].quantity -= 1
    elif mutate == "plan":
        state.portfolio.positions[0].exit_plan.stop_loss_pct = -.15
    else:
        state.quotes.clear()
    exit_only.save(state, path)
    with pytest.raises(ValueError):
        exit_only.load(path)
    result = mobile_data.snapshot(tmp_path, now=NEXT)
    assert result.account is None and result.holdings == []
    assert result.operation["status"] == "failed"


def test_app_values_remaining_simulated_shares_and_refresh_never_opens_socket(tmp_path, monkeypatch):
    path = tmp_path / "data/exit_only_state.json"
    state, _, _ = initialized(path)
    exit_only.apply_quotes(state, {"005930": (quote(120), "kis")}, at=NEXT)
    exit_only.save(state, path)
    result = mobile_data.snapshot(path.parent, now=NEXT)
    assert result.holdings[0]["quantity"] == 6
    assert result.account["total"] == pytest.approx(state.cash + 720)
    assert result.trades[0]["simulated"] is True
    assert result.operation["mode"] == "exit_only"
    later = mobile_data.snapshot(path.parent, now=NEXT + timedelta(days=20))
    assert later.monitoring["days"] == result.monitoring["days"]
    app = create_app({"TESTING": True, "ORIGIN": "https://sima.test", "STATE_DIR": tmp_path / "app", "DATA_DIR": path.parent})
    client = app.test_client()
    headers = {"Origin": "https://sima.test", "X-SIMA-Request": "1"}
    client.post("/api/login", base_url="https://sima.test", headers=headers,
                json={"code": app.extensions["sima_store"].pairing_code()})
    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("broker refresh socket used"))
    response = client.post("/api/account/refresh", base_url="https://sima.test", headers=headers, json={})
    assert response.status_code == 200
    assert response.json["holdings"][0]["quantity"] == 6


def test_broker_boundary_blocks_orders_accounts_and_history_before_auth(monkeypatch):
    monkeypatch.setattr(kis, "get_access_token", lambda: pytest.fail("authentication attempted"))
    for method, path in [("POST", kis.ORDER_CASH_PATH), ("GET", kis.BALANCE_PATH), ("GET", kis.DAILY_CHART_PATH)]:
        with pytest.raises(RuntimeError, match="exit_only"):
            kis._kis_request(method, path, "test")


def test_current_price_http_still_allowed(monkeypatch):
    calls = []
    monkeypatch.setattr(kis, "get_access_token", lambda: "test")
    monkeypatch.setattr(kis, "_throttle", lambda: None)
    class Response:
        def json(self):
            return {"rt_cd": "0", "output": {}}
    monkeypatch.setattr(kis.requests, "get", lambda url, **kw: calls.append(url) or Response())
    assert kis._kis_get(kis.CURRENT_PRICE_PATH, "test", {}) == {"rt_cd": "0", "output": {}}
    assert len(calls) == 1


def test_disabled_analysis_entry_points_do_no_work():
    from scripts import check_codex_plan, decide_buys, decide_llm_sell, execute_open, run_daily
    for module in [check_codex_plan, decide_buys, decide_llm_sell, execute_open, run_daily]:
        asyncio.run(module.main())


def test_missing_state_never_initializes_itself(tmp_path):
    path = tmp_path / "missing.json"
    with pytest.raises(FileNotFoundError):
        asyncio.run(exit_only.monitor(at=NEXT, path=path))
    assert not path.exists()
