"""응답 유실·프로세스 중단·장부 쓰기 실패를 다음 회차까지 이어서 검증한다."""

import asyncio
import json
from datetime import datetime

import pytest

from scripts.drill_order_paths import FakeBroker, TICKER, PRICE, KST, _wired, _decision, _order_posts
from src import order_guard, pipeline, portfolio_store, sell
from src.schemas import GateResult, PortfolioState, Position, SellAction


def _buy(portfolio, log):
    return asyncio.run(pipeline.execute_buy_order(_decision(), GateResult(approved=True, rejected_by=None), portfolio,
                                                  "반도체", 0.08, log_path=log))


def test_buy_journal_failure_does_not_release_order_marker(monkeypatch):
    b = FakeBroker(order_behavior="lost_filled")
    with _wired(b) as (tmp, _):
        original = pipeline._append_log
        monkeypatch.setattr(pipeline, "_append_log", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        with pytest.raises(OSError):
            _buy(PortfolioState(), tmp / "journal.jsonl")
        assert order_guard.is_blocked(TICKER)
        monkeypatch.setattr(pipeline, "_append_log", original)
        result = _buy(PortfolioState(), tmp / "journal.jsonl")
        assert result.positions == [] and _order_posts(b) == 1


def test_full_fill_marker_released_only_after_matching_portfolio_saved(monkeypatch):
    b = FakeBroker(order_behavior="lost_filled")
    with _wired(b) as (tmp, _):
        monkeypatch.setattr(portfolio_store, "PORTFOLIO_STATE_PATH", tmp / "portfolio.json")
        result = _buy(PortfolioState(), tmp / "journal.jsonl")
        assert order_guard.is_blocked(TICKER)
        portfolio_store.save_portfolio(PortfolioState())
        assert order_guard.is_blocked(TICKER)
        result.positions[0].peak_price += 100
        portfolio_store.save_portfolio(result)
        assert portfolio_store.load_portfolio() == result
        assert not order_guard.is_blocked(TICKER)


def test_sell_lost_response_blocks_later_round_and_has_no_zero_share_journal():
    b = FakeBroker(order_behavior="lost_no_fill", held_qty=30)
    p = Position(ticker=TICKER, sector="반도체", weight=0.08, entry_price=12000,
                 peak_price=12000, quantity=30)
    original = PortfolioState(positions=[p], cash_weight=0.92)
    action = SellAction(ticker=TICKER, reason="stop_loss", sell_fraction=1)
    with _wired(b) as (tmp, _):
        for _ in range(2):
            result = asyncio.run(pipeline.finalize_sell(original, action, p, PRICE, datetime.now(KST),
                sell.execute_sell_order, tmp / "sell.jsonl", tmp / "journal.jsonl"))
            assert result == original
        assert _order_posts(b) == 1
        assert order_guard.is_blocked(TICKER)
        assert not (tmp / "journal.jsonl").exists()
        assert not (tmp / "sell.jsonl").exists()


def test_order_marker_corruption_fails_closed():
    order_guard.STATE_PATH.write_text('{"broken":')
    with pytest.raises(json.JSONDecodeError):
        order_guard.is_blocked(TICKER)


@pytest.mark.parametrize("behavior", ["missing_order_number", "missing_result"])
def test_malformed_acceptance_response_is_not_treated_as_rejection(behavior):
    b = FakeBroker(order_behavior=behavior)
    with _wired(b) as (tmp, _):
        first = _buy(PortfolioState(), tmp / "journal.jsonl")
        assert first.positions[0].quantity == b.held_qty
        second = _buy(PortfolioState(), tmp / "journal.jsonl")
        assert second.positions == [] and _order_posts(b) == 1
        assert order_guard.is_blocked(TICKER)
