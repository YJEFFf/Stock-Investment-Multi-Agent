import asyncio
import json
from datetime import datetime, timezone

import pytest

from src import kis, pipeline
from src.schemas import PortfolioState, Position


def _position(**overrides) -> Position:
    defaults = dict(ticker="005930", sector="반도체", weight=0.10, entry_price=100.0, peak_price=100.0)
    defaults.update(overrides)
    return Position(**defaults)


DAY = datetime(2026, 8, 9, tzinfo=timezone.utc)


def test_returns_unchanged_when_no_positions(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("포지션이 없으면 KIS를 호출하면 안 된다")

    monkeypatch.setattr(kis, "fetch_current_price", fail)

    portfolio = PortfolioState()
    result = asyncio.run(pipeline.evaluate_holdings(portfolio, DAY))

    assert result == portfolio


def test_skips_position_when_price_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: None)

    position = _position()
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, log_path=log_path))

    assert result == portfolio
    assert not log_path.exists()


def test_no_sell_when_price_within_normal_range(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 105.0)

    position = _position(entry_price=100.0, peak_price=100.0)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, log_path=log_path))

    assert len(result.positions) == 1
    assert result.positions[0].peak_price == 105.0  # 고점은 갱신됨
    assert result.positions[0].weight == 0.10  # 매도는 없었음
    assert not log_path.exists()


def test_stop_loss_removes_position_and_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 89.0)  # -11%

    position = _position(entry_price=100.0, peak_price=100.0, weight=0.10)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, log_path=log_path))

    assert result.positions == []
    assert result.cash_weight == pytest.approx(1.0)

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["reason"] == "stop_loss"
    assert entries[0]["ticker"] == "005930"


def test_take_profit_partial_sell_and_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 120.0)  # +20%

    position = _position(entry_price=100.0, peak_price=100.0, weight=0.09, take_profit_stage=0)
    portfolio = PortfolioState(positions=[position], cash_weight=0.91)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, log_path=log_path))

    assert len(result.positions) == 1
    remaining = result.positions[0]
    assert remaining.weight == pytest.approx(0.06)
    assert remaining.take_profit_stage == 1
    assert remaining.peak_price == 120.0

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert entries[0]["reason"] == "take_profit_trail"


def test_handles_multiple_positions_independently(monkeypatch, tmp_path):
    prices = {"005930": 89.0, "000660": 101.0}
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: prices[ticker])

    positions = [
        _position(ticker="005930", entry_price=100.0, peak_price=100.0, weight=0.10),
        _position(ticker="000660", entry_price=100.0, peak_price=100.0, weight=0.08),
    ]
    portfolio = PortfolioState(positions=positions, cash_weight=0.82)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, log_path=log_path))

    remaining_tickers = {p.ticker for p in result.positions}
    assert remaining_tickers == {"000660"}  # 005930은 손절로 제거됨
    assert result.positions[0].peak_price == 101.0


def test_price_fetch_exception_is_treated_like_unavailable(monkeypatch, tmp_path):
    def raise_error(ticker):
        raise RuntimeError("boom")

    monkeypatch.setattr(kis, "fetch_current_price", raise_error)

    position = _position()
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, log_path=log_path))

    assert result == portfolio
    assert not log_path.exists()
