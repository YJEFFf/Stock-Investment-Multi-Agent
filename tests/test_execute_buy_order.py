import asyncio
import json
from datetime import date

import pytest

from src import kis, pipeline
from src.schemas import Decision, GateResult, PortfolioState, Position

TICKER = "005930"


def _decision(action="BUY") -> Decision:
    from src.schemas import AnalystOpinion

    return Decision(
        ticker=TICKER,
        action=action,
        reason="test",
        inputs=[
            AnalystOpinion(
                agent="chart", ticker=TICKER, score=0.9, confidence=0.9, evidence=["e"], as_of=date(2026, 8, 9)
            )
        ],
        degraded=False,
    )


def _prev_bars(close: float):
    from src.schemas import OHLCVBar

    return [
        OHLCVBar(date=date(2026, 8, 8), open=close, high=close, low=close, close=close, volume=1000),
    ]


def test_skips_when_decision_not_buy(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("BUY가 아니면 KIS를 호출하면 안 된다")

    monkeypatch.setattr(kis, "fetch_daily_ohlcv", fail)

    portfolio = PortfolioState()
    result = asyncio.run(
        pipeline.execute_buy_order(_decision(action="HOLD"), GateResult(approved=False, rejected_by=None), portfolio, "반도체", 0.08)
    )

    assert result == portfolio


def test_skips_when_gate_not_approved(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("게이트 미승인이면 KIS를 호출하면 안 된다")

    monkeypatch.setattr(kis, "fetch_daily_ohlcv", fail)

    portfolio = PortfolioState()
    result = asyncio.run(
        pipeline.execute_buy_order(_decision(), GateResult(approved=False, rejected_by="position_limit"), portfolio, "반도체", 0.08)
    )

    assert result == portfolio


def test_skips_when_gap_too_large(monkeypatch):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 110.0)  # +10%, 문턱(3%) 초과

    def fail(*a, **k):
        raise AssertionError("갭이 크면 잔고 조회까지 가면 안 된다")

    monkeypatch.setattr(kis, "fetch_account_balance", fail)

    portfolio = PortfolioState()
    result = asyncio.run(
        pipeline.execute_buy_order(_decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08)
    )

    assert result == portfolio


def test_skips_when_price_data_unavailable(monkeypatch):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: None)
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 100.0)

    portfolio = PortfolioState()
    result = asyncio.run(
        pipeline.execute_buy_order(_decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08)
    )

    assert result == portfolio


def test_skips_when_balance_unavailable(monkeypatch):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 101.0)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: None)

    portfolio = PortfolioState()
    result = asyncio.run(
        pipeline.execute_buy_order(_decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08)
    )

    assert result == portfolio


def test_skips_when_order_rejected(monkeypatch):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 101.0)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: None)

    portfolio = PortfolioState()
    result = asyncio.run(
        pipeline.execute_buy_order(_decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08)
    )

    assert result == portfolio


def test_opens_new_position_with_fill_price(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(230000.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 231000.0)  # 갭 ~0.4%, 문턱 이내
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)

    captured_qty = {}

    def fake_order(ticker, qty):
        captured_qty["qty"] = qty
        return "ODNO123"

    monkeypatch.setattr(kis, "place_market_buy_order", fake_order)
    monkeypatch.setattr(kis, "fetch_fill_price", lambda ticker, order_date: 231200.0)

    portfolio = PortfolioState(cash_weight=1.0)
    log_path = tmp_path / "trade_journal.jsonl"
    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08, log_path=log_path
        )
    )

    assert len(result.positions) == 1
    pos = result.positions[0]
    assert pos.ticker == TICKER
    assert pos.entry_price == 231200.0
    assert pos.peak_price == 231200.0
    assert pos.weight == 0.08
    assert pos.entry_day == date.today()
    assert result.cash_weight == pytest.approx(0.92)
    # 수량 = floor(100_000_000 * 0.08 / 231000) = floor(34.6...) = 34
    assert captured_qty["qty"] == int((100_000_000 * 0.08) // 231000.0)

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["event"] == "buy"
    assert entries[0]["ticker"] == TICKER
    assert entries[0]["entry_price"] == 231200.0
    assert entries[0]["order_no"] == "ODNO123"
    assert entries[0]["decision"]["reason"] == "test"


def test_falls_back_to_current_price_when_fill_price_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 101.0)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: "ODNO123")
    monkeypatch.setattr(kis, "fetch_fill_price", lambda ticker, order_date: None)

    portfolio = PortfolioState(cash_weight=1.0)
    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(),
            GateResult(approved=True, rejected_by=None),
            portfolio,
            "반도체",
            0.08,
            log_path=tmp_path / "trade_journal.jsonl",
        )
    )

    assert result.positions[0].entry_price == 101.0  # current_price로 근사


def test_adds_to_existing_position_with_weighted_average_entry_price(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(200.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 200.0)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: "ODNO456")
    monkeypatch.setattr(kis, "fetch_fill_price", lambda ticker, order_date: 200.0)

    existing = Position(
        ticker=TICKER, sector="반도체", weight=0.08, entry_day=date(2026, 1, 5), entry_price=100.0, peak_price=120.0
    )
    portfolio = PortfolioState(positions=[existing], cash_weight=0.92)

    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(),
            GateResult(approved=True, rejected_by=None),
            portfolio,
            "반도체",
            0.08,
            log_path=tmp_path / "trade_journal.jsonl",
        )
    )

    assert len(result.positions) == 1
    pos = result.positions[0]
    assert pos.weight == pytest.approx(0.16)
    # 가중평균: (100*0.08 + 200*0.08) / 0.16 = 150
    assert pos.entry_price == pytest.approx(150.0)
    assert pos.peak_price == 200.0  # 새 체결가가 기존 고점보다 높음
    assert pos.entry_day == date(2026, 1, 5)  # 추가매수해도 최초 진입일 유지
