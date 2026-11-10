"""Fault regressions for the full-system audit. No live market/account/model calls."""
import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import requests

from src import (analysts, collectors, evaluation, exit_only, kis, market_calendar as calendar,
                 mobile_data, notion_sync, operating_mode, order_guard, order_recovery,
                 pipeline, portfolio_store, run_history, sell)
from src.schemas import (AnalystOpinion, Decision, FillRecord, GateResult, NewsItem,
                         OHLCVBar, PortfolioState, Position, SellAction)

AT = datetime(2026, 11, 10, 14, tzinfo=calendar.KST)
TICKER = "005930"
REAL_NAVER = collectors.fetch_naver_quotes
REAL_KIS_QUOTE = kis.fetch_quote
PRICE_HISTORY_DEFAULT = evaluation.DEFAULT_PRICE_HISTORY_DIR
REAL_CLOSING_QUOTE = kis.fetch_closing_quote


def ledger(tmp_path):
    path = tmp_path / "exit_only_state.json"
    start = AT.replace(day=9, hour=16)
    portfolio = PortfolioState(positions=[Position(ticker=TICKER, sector="test", quantity=9,
        weight=.1, entry_price=100, peak_price=100, entry_day=date(2026, 11, 2),
        exit_plan=sell.DEFAULT_EXIT_PLAN)], cash_weight=.9)
    state = exit_only.initialize(portfolio.model_dump_json().encode(), {
        "total": 2000, "securities": 900, "observed_at": start.isoformat(),
        "holdings": [{"pdno": TICKER, "hldg_qty": 9, "prpr": 100}],
    }, at=start, path=path)
    state.share_basis[TICKER] = {"listed_shares": 1000, "par_value": 500, "day": AT.date().isoformat()}
    exit_only.save(state, path)
    return state, path


def quote(price=80, stamp=AT, **kwargs):
    return kis.Quote(price=price, day_high=max(price, 100), day_low=min(price, 100),
        provider_at=stamp, prev_close=100, listed_shares=1000, par_value=500, **kwargs)


@pytest.mark.parametrize("stamp", [AT.replace(hour=9), AT - timedelta(days=1), None, AT + timedelta(seconds=1)])
def test_f01_stale_missing_or_future_provider_time_cannot_sell(tmp_path, stamp):
    state, _ = ledger(tmp_path)
    exit_only.apply_quotes(state, {TICKER: (quote(stamp=stamp), "kis")}, at=AT)
    assert not state.sales and state.portfolio.positions[0].quantity == 9
    assert state.missing_quotes == [TICKER]


def test_f01_old_daily_range_cannot_borrow_a_new_minute_timestamp(monkeypatch):
    payloads = iter([
        {"output": {"stck_prpr": "100", "stck_hgpr": "120", "stck_lwpr": "80",
                    "prdy_vrss": "0", "acml_vol": "100", "acml_tr_pbmn": "10000"}},
        {"output1": {"acml_vol": "200", "acml_tr_pbmn": "20000", "stck_prdy_clpr": "100", "stck_prpr": "100", "prdy_vrss": "0"},
         "output2": [{"stck_bsop_date": "20261110", "stck_cntg_hour": "140000",
                      "stck_prpr": "100", "stck_hgpr": "100", "stck_lwpr": "100"}]},
    ])
    monkeypatch.setattr(kis, "_kis_get", lambda *a, **k: next(payloads))
    result = REAL_KIS_QUOTE(TICKER, verify_time=True)
    assert result.price == result.day_high == result.day_low == 100
    assert result.provider_at == AT
    assert result.range_source == "minute"


@pytest.mark.parametrize("case", ["moving", "no_trade", "basis_mismatch"])
def test_active_trading_is_usable_but_zero_volume_and_changed_basis_are_not(monkeypatch, tmp_path, case):
    summary = {"acml_vol": "200", "acml_tr_pbmn": "20000", "stck_prdy_clpr": "100",
               "stck_prpr": "101", "prdy_vrss": "1"}
    if case == "no_trade":
        summary["acml_vol"] = "0"
    if case == "basis_mismatch":
        summary["prdy_vrss"] = "51"
    responses = iter([
        {"output": {"stck_prpr": "100", "stck_hgpr": "120", "stck_lwpr": "80", "prdy_vrss": "0",
                    "acml_vol": "100", "acml_tr_pbmn": "10000", "lstn_stcn": "1000", "stck_fcam": "500"}},
        {"output1": summary, "output2": [{"stck_bsop_date": "20261110", "stck_cntg_hour": "140000",
                    "stck_prpr": "101", "stck_hgpr": "101", "stck_lwpr": "100"}]},
    ])
    monkeypatch.setattr(kis, "_kis_get", lambda *a, **k: next(responses))
    q = REAL_KIS_QUOTE(TICKER, verify_time=True)
    if case != "moving":
        assert q is None
        return
    assert (q.price, q.day_high, q.day_low, q.range_source) == (101, 101, 100, "minute")
    state, _ = ledger(tmp_path)
    state.share_basis.clear()
    from src.schemas import ExitOnlyQuote
    close = calendar.market_session(date(2026, 11, 9)).close
    state.quotes[TICKER] = ExitOnlyQuote(price=100, provider_at=close, observed_at=close, source="kis")
    exit_only.apply_quotes(state, {TICKER: (q, "kis")}, at=AT)
    assert not state.missing_quotes and state.share_basis[TICKER]["day"] == "2026-11-10"


@pytest.mark.parametrize("bad", ["baseline", "overfill"])
def test_buy_missing_baseline_or_overfill_cannot_book_requested_quantity(monkeypatch, tmp_path, bad):
    initial = PortfolioState()
    portfolio_store.save_portfolio(initial)
    decision = Decision(ticker=TICKER, action="BUY", reason="verified", inputs=[], degraded=False)
    bar = OHLCVBar(date=AT.date(), open=100, high=100, low=100, close=100, volume=1000)
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda *a, **k: [bar] * 25)
    monkeypatch.setattr(kis, "fetch_current_price", lambda *a, **k: 100.)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 10000.)
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda *a, **k: None if bad == "baseline" else (0, 0., 0.))
    posts = []
    monkeypatch.setattr(kis, "place_market_buy_order", lambda *a: posts.append(a) or "123")
    monkeypatch.setattr(kis, "fill_after_order", lambda *a, **k: FillRecord(quantity=11, amount=1100.))
    monkeypatch.setattr(pipeline, "display_name", lambda t: t)
    result = asyncio.run(pipeline.execute_buy_order(decision, GateResult(approved=True, rejected_by=None), initial, "test", .1,
                                                   log_path=tmp_path / "journal.jsonl"))
    assert result == initial and portfolio_store.load_portfolio() == initial
    assert len(posts) == (0 if bad == "baseline" else 1)


def test_f02_naver_closing_snapshot_values_without_afterhours_sale(tmp_path, monkeypatch):
    state, _ = ledger(tmp_path)
    class Response:
        def json(self):
            return {"datas": [{"itemCode": TICKER, "closePriceRaw": 80, "highPriceRaw": 100,
                "lowPriceRaw": 80, "openPriceRaw": 100, "compareToPreviousClosePriceRaw": -20,
                "marketStatus": "CLOSE", "localTradedAt": "2026-11-10T15:30:00+09:00"}]}
    monkeypatch.setattr(collectors.requests, "get", lambda *a, **k: Response())
    q = REAL_NAVER([TICKER], today=AT.date())[TICKER]
    exit_only.apply_quotes(state, {TICKER: (q, "naver")}, at=AT.replace(hour=15, minute=35))
    assert state.quotes[TICKER].price == 80 and not state.sales
    assert state.nav_history[-1]["total"] == pytest.approx(1820)


def test_f03_evening_retains_monitor_and_corrupt_history_warnings(tmp_path, monkeypatch):
    state, path = ledger(tmp_path)
    monkeypatch.setattr(operating_mode, "EXIT_ONLY", True)
    (tmp_path / "trade_journal.jsonl").write_text("broken json\n")
    snapshot = mobile_data.snapshot(tmp_path, now=AT.replace(hour=20))
    assert any("읽을 수 없는" in w for w in snapshot.warnings)
    assert any("예정 확인" in w for w in snapshot.warnings)
    assert not any("일별 계좌 기록" in w for w in snapshot.warnings)


def test_f03_monitor_process_has_a_hard_deadline():
    shell = (Path(__file__).parents[1] / "scripts/monitor_exit_only.sh").read_text()
    assert "timeout --signal=TERM --kill-after=3s 50s" in shell


def test_f04_special_session_unknown_year_and_close_gap(tmp_path, monkeypatch):
    assert not calendar.is_krx_trading_day(date(2026, 6, 3))
    assert not calendar.is_krx_trading_day(date(2026, 7, 17))
    special = calendar.market_session(date(2026, 11, 19))
    assert special.open.hour == 10 and special.close.hour == 16
    assert calendar.deployment_blocked(special.close - timedelta(minutes=15))
    assert not calendar.deployment_blocked(datetime(2027, 1, 4, 21, tzinfo=calendar.KST))
    assert calendar.monitor_due(AT.replace(hour=15, minute=34)).minute == 30
    ledger(tmp_path)
    monkeypatch.setattr(operating_mode, "EXIT_ONLY", True)
    result = mobile_data.snapshot(tmp_path, now=datetime(2027, 1, 4, 20, tzinfo=calendar.KST))
    assert result.holdings and any("달력" in w for w in result.warnings)


def test_f05_required_news_failure_cannot_be_replaced_with_sector_news(monkeypatch):
    item = NewsItem(title="업종 호재", press=None, published_at=AT - timedelta(hours=1), url="https://test/1")
    monkeypatch.setattr(collectors, "fetch_company_news", lambda *a: None)
    monkeypatch.setattr(collectors, "fetch_sector_news", lambda *a: [item])
    monkeypatch.setattr(pipeline, "news_analyst", lambda *a: pytest.fail("model must not run"))
    assert asyncio.run(pipeline.make_news_analyst_fn()(TICKER, "test", AT)) == []


def test_f06_empty_sector_does_not_request_or_match_every_headline(monkeypatch):
    monkeypatch.setattr(collectors, "_fetch_with_retries", lambda *a, **k: pytest.fail("empty sector request"))
    assert collectors.fetch_sector_news("  ") == []
    assert collectors._parse_sector_news('<a href="/x">all news</a>', "") == []


def test_f07_cutoff_and_age_are_visible_to_analyst(monkeypatch):
    old = NewsItem(title="old", press=None, published_at=AT - timedelta(days=60), url="https://test/old")
    future = old.model_copy(update={"title": "future", "published_at": AT + timedelta(seconds=1)})
    monkeypatch.setattr(collectors, "fetch_company_news", lambda *a: [old, future])
    monkeypatch.setattr(collectors, "fetch_sector_news", lambda *a: [])
    captured = []
    async def analyst(context):
        captured.append(context)
        return None
    monkeypatch.setattr(pipeline, "news_analyst", analyst)
    asyncio.run(pipeline.make_news_analyst_fn()(TICKER, "test", AT))
    assert captured[0].company_news == [old]
    assert "age_days=60" in analysts._format_news_items([old], AT)


def test_f08_duplicate_invalid_stale_bars_and_flat_rsi():
    from src.data_quality import validate_bars
    bar = OHLCVBar(date=date(2026, 10, 1), open=100, high=100, low=100, close=100, volume=1)
    for rows in [[bar, bar], [bar.model_copy(update={"high": 99})], [bar.model_copy(update={"volume": -1})]]:
        with pytest.raises(ValueError):
            validate_bars(rows)
    with pytest.raises(ValueError, match="stale"):
        validate_bars([bar], as_of=AT)
    assert collectors._rsi([100] * 30) == 50


@pytest.mark.parametrize("existing_basis", [True, False])
def test_f09_split_or_unverified_initial_basis_never_changes_frozen_holding(tmp_path, existing_basis):
    state, _ = ledger(tmp_path)
    if not existing_basis:
        state.share_basis.clear()
    frozen = state.portfolio.model_copy(deep=True)
    split = replace(quote(50), listed_shares=2000, par_value=250, prev_close=50)
    exit_only.apply_quotes(state, {TICKER: (split, "kis")}, at=AT)
    assert state.portfolio == frozen and not state.sales and state.basis_warnings == [TICKER]


def test_f09_kis_requests_adjusted_history(monkeypatch):
    captured = []
    monkeypatch.setattr(kis, "_kis_get", lambda path, tr, params, **k: captured.append(params))
    kis.fetch_daily_ohlcv(TICKER)
    assert captured[0]["FID_ORG_ADJ_PRC"] == "0"
    assert PRICE_HISTORY_DEFAULT.name == "price_history_adjusted_v1"


def test_adjustment_revision_archives_old_basis_instead_of_mixing_windows(tmp_path):
    history = evaluation.PriceHistory(tmp_path)
    def bar(day, value):
        return OHLCVBar(date=date(2026, 11, day), open=value, high=value, low=value, close=value, volume=1)
    history.merge(TICKER, [bar(2, 100), bar(3, 100)])
    history.merge(TICKER, [bar(3, 50), bar(4, 50)])
    assert list(history.load(TICKER)) == [date(2026, 11, 3), date(2026, 11, 4)]
    assert len(list((tmp_path / "epochs").glob("*.json"))) == 1


def test_closing_bar_does_not_borrow_afterhours_current_price(monkeypatch):
    calls = []
    def get(path, tr, params, **kw):
        calls.append((path, params))
        return {"output1": {"stck_prpr": "90"}, "output2": [{"stck_bsop_date": "20261110",
            "stck_cntg_hour": "153000", "stck_prpr": "100", "stck_hgpr": "100", "stck_lwpr": "100"}]}
    monkeypatch.setattr(kis, "_kis_get", get)
    q = REAL_CLOSING_QUOTE(TICKER, AT.date())
    assert q.price == 100 and q.provider_at == calendar.market_session(AT.date()).close
    assert calls[0][0] == kis.MINUTE_PRICE_PATH


@pytest.mark.parametrize("wrong", [False, True])
def test_legacy_close_verification_preserves_financial_state(tmp_path, wrong):
    from scripts.verify_exit_only_close import verified_state
    state, _ = ledger(tmp_path)
    state.share_basis.clear()
    close = calendar.market_session(AT.date()).close
    from src.schemas import ExitOnlyQuote
    state.quotes[TICKER] = ExitOnlyQuote(price=100, observed_at=close + timedelta(minutes=5), source="kis")
    at = AT.replace(day=11, hour=1)
    q = quote(99 if wrong else 100, stamp=close, market_status="CLOSE")
    before = state.model_dump(mode="json")
    if wrong:
        with pytest.raises(ValueError, match="unverified"):
            verified_state(state, {TICKER: q}, at)
    else:
        result = verified_state(state, {TICKER: q}, at)
        assert result.quotes[TICKER].provider_at == close
        after = result.model_dump(mode="json")
        for key in before.keys() - {"quotes", "updated_at"}:
            assert before[key] == after[key]
    assert state.model_dump(mode="json") == before


@pytest.mark.parametrize("missing_baseline", [True, False])
def test_sell_missing_baseline_or_overfill_never_changes_ledger(monkeypatch, tmp_path, missing_baseline):
    state, _ = ledger(tmp_path)
    initial = state.portfolio
    portfolio_store.save_portfolio(initial)
    calls = []
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda *a, **k: None if missing_baseline else (0, 0., 0.))
    monkeypatch.setattr(kis, "place_market_sell_order", lambda *a: calls.append(a) or "123")
    monkeypatch.setattr(kis, "fill_after_order", lambda *a, **k: FillRecord(quantity=10, amount=1000))
    action = SellAction(ticker=TICKER, reason="stop_loss", sell_fraction=1.)
    result, fill = asyncio.run(sell.execute_sell_order(initial, action, 80))
    assert result == initial and fill is None
    assert len(calls) == (0 if missing_baseline else 1)
    assert portfolio_store.load_portfolio() == initial


def test_partial_sell_recovery_keeps_original_stage_and_only_books_remaining_leg(monkeypatch, tmp_path):
    state, _ = ledger(tmp_path)
    initial = state.portfolio
    portfolio_store.save_portfolio(initial)
    action = SellAction(ticker=TICKER, reason="take_profit_trail", sell_fraction=1/3)
    context = {"action": action.model_dump(mode="json"), "day": AT.date().isoformat(), "current_price": 120}
    order_guard.begin(TICKER, "sell", 3, (0, 0., None), portfolio=initial, context=context)
    order_guard.mark_accepted(TICKER, "123")
    partial = sell.execute_sell(initial, action.model_copy(update={"sell_fraction": 1/9}), 120, today=AT.date())
    partial.positions[0].quantity = 8
    journal = tmp_path / "journal.jsonl"
    order_guard.commit_effect(TICKER, initial, partial, [(journal, {"event": "sell", "shares_sold": 1})],
                              complete=False, quantity=1, amount=120, fee=None)
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda t, d, side, **k:
                        (3, 360., None) if side == "sell" else (0, 0., None))
    monkeypatch.setattr(kis, "fetch_holdings", lambda **k: {TICKER: (6, 100.)})
    monkeypatch.setattr(pipeline, "display_name", lambda t: t)
    assert order_recovery.recover_observed_fill(datetime.now(calendar.KST) + timedelta(minutes=1), TICKER)
    final = portfolio_store.load_portfolio().positions[0]
    assert final.quantity == 6 and final.take_profit_stage == partial.positions[0].take_profit_stage == 1
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [r["shares_sold"] for r in rows] == [1, 2]
    assert rows[-1]["exit_trigger"] == "take_profit_first"


def test_f10_f11_partial_fill_without_fee_recovers_only_remaining_shares(monkeypatch, tmp_path):
    initial = PortfolioState()
    portfolio_store.save_portfolio(initial)
    context = {"sector": "test", "trade_weight": .1, "day": AT.date().isoformat(),
               "exit_plan": sell.DEFAULT_EXIT_PLAN.model_dump(mode="json"), "decision": {},
               "exit_plan_source": "default", "log_path": str(tmp_path / "journal.jsonl")}
    order_guard.begin(TICKER, "buy", 100, (0, 0.0, None), portfolio=initial, context=context)
    order_guard.mark_accepted(TICKER, "123")
    partial = PortfolioState(positions=[Position(ticker=TICKER, sector="test", weight=.02, quantity=20,
        entry_price=100, peak_price=100, entry_day=AT.date(), exit_plan=sell.DEFAULT_EXIT_PLAN)], cash_weight=.98)
    order_guard.commit_effect(TICKER, initial, partial, [(context["log_path"], {"event": "buy", "quantity": 20})],
                              complete=False, quantity=20, amount=2000, fee=None)
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda t, d, side, **k:
                        (100, 10000.0, 10.0) if side == "buy" else (0, 0.0, None))
    monkeypatch.setattr(kis, "fetch_holdings", lambda **k: {TICKER: (100, 100.0)})
    now = datetime.now(calendar.KST) + timedelta(minutes=1)
    assert order_recovery.recover_observed_fill(now, TICKER)
    assert not order_recovery.recover_observed_fill(now, TICKER)
    saved = portfolio_store.load_portfolio()
    assert saved.positions[0].quantity == 100 and saved.cash_weight == pytest.approx(.9)
    rows = [json.loads(line) for line in Path(context["log_path"]).read_text().splitlines()]
    assert [r["quantity"] for r in rows] == [20, 80]
    assert rows[-1]["fee_amount"] is None
    assert not order_guard.is_blocked(TICKER)


@pytest.mark.parametrize("weight", [float("nan"), float("inf"), -.1])
def test_f12_invalid_portfolio_and_missing_file_fail_closed(tmp_path, weight):
    decision = Decision(ticker=TICKER, action="BUY", reason="test", inputs=[], degraded=False)
    from src.schemas import RiskGateConfig
    assert not pipeline.check_gate(decision, PortfolioState(cash_weight=weight), RiskGateConfig(), .08).approved
    with pytest.raises(FileNotFoundError):
        portfolio_store.load_portfolio(tmp_path / "absent.json")


def test_f13_incomplete_account_and_unknown_fees_are_not_zero(monkeypatch):
    monkeypatch.setattr(kis, "_kis_get", lambda *a, **k: {"ctx_area_nk100": "more", "output1": [],
        "output2": [{"tot_evlu_amt": "100", "dnca_tot_amt": "100", "scts_evlu_amt": "0"}]})
    assert kis.fetch_account_snapshot() is None
    monkeypatch.setattr(kis, "_kis_get", lambda *a, **k: {"output2": {"tot_ccld_qty": "10", "tot_ccld_amt": "1000"}})
    assert kis.fetch_daily_fill_totals(TICKER, AT.date(), "buy") == (10, 1000, None)
    assert kis.fill_between((0, 0, None), (10, 1000, None)).fee is None


def test_f14_missing_stock_or_benchmark_day_does_not_move_horizon():
    days = [date(2026, 11, d) for d in (9, 10, 11, 12)]
    bars = {d: OHLCVBar(date=d, open=100, high=110, low=100, close=110, volume=1) for d in days}
    incomplete = {d: b for d, b in bars.items() if d != days[1]}
    assert evaluation.forward_excess_return(incomplete, bars, days[0], 2) is None
    assert evaluation.forward_excess_return(bars, incomplete, days[0], 2) is None
    entries = [{"day": "2026-11-10", "opinions": [{"agent": "chart", "prompt": "prompt:chart@old"}]},
               {"day": "2026-09-01", "opinions": [{"agent": "chart", "prompt": "prompt:chart@7a9654"}]}]
    assert evaluation.entries_since(entries, "2026-09-14") == [entries[1]]


def test_f15_nonidempotent_post_response_loss_is_not_retried(monkeypatch):
    calls = []
    def lost(*a, **k):
        calls.append(1)
        raise requests.Timeout("response lost")
    monkeypatch.setattr(notion_sync.requests, "request", lost)
    assert notion_sync._notion_request("POST", "/pages", {}) is None
    assert len(calls) == 1


def test_f15_failed_block_delete_never_reports_success(monkeypatch):
    children = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}}]
    blocks = [{"id": "old"}]
    def request(method, path, body=None):
        if method == "GET":
            return {"results": list(blocks)}
        if method == "PATCH":
            blocks.extend({**child, "id": "new"} for child in body["children"])
            return {}
        return None
    monkeypatch.setattr(notion_sync, "_notion_request", request)
    assert not notion_sync._replace_children("page", children)
    assert [b["id"] for b in blocks] == ["old", "new"]
    assert not notion_sync._replace_children("page", children)
    assert len(blocks) == 2  # Readback recognizes the prior append.


def test_f16_judgment_receives_reasoning_evidence_and_actual_context():
    from src import judgment
    opinion = AnalystOpinion(agent="news", ticker=TICKER, score=.5, confidence=.8,
        evidence=["https://test/original", "prompt:news@test"], as_of=AT, reasoning="original supporting facts")
    rendered = json.loads(judgment._format_opinions([opinion]))
    assert rendered[0]["reasoning"] == opinion.reasoning
    assert rendered[0]["evidence"] == opinion.evidence
    assert judgment._MANAGER_RESPONSE_SCHEMA["required"] == ["action", "reasoning"]


def test_f17_deployment_review_covers_new_dependencies():
    script = (Path(__file__).parents[1] / "deploy/pull.sh").read_text()
    assert "TRADING_PATHS=(src/ prompts/ scripts/ deploy/ config/ conftest.py)" in script
