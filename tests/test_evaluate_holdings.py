import asyncio
import json
from datetime import datetime, timezone

import pytest

from src import kis, pipeline, sell
from src.schemas import AnalystOpinion, PortfolioState, Position, SellAction


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
    result = asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert result == portfolio


def test_skips_position_when_price_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: None)

    position = _position()
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(
        pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated, log_path=log_path)
    )

    assert result == portfolio
    assert not log_path.exists()


def test_no_sell_when_price_within_normal_range(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 105.0)

    position = _position(entry_price=100.0, peak_price=100.0)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(
        pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated, log_path=log_path)
    )

    assert len(result.positions) == 1
    assert result.positions[0].peak_price == 105.0  # 고점은 갱신됨
    assert result.positions[0].weight == 0.10  # 매도는 없었음
    assert not log_path.exists()


def test_stop_loss_removes_position_and_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 89.0)  # -11%

    position = _position(entry_price=100.0, peak_price=100.0, weight=0.10, entry_day=DAY.date())
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"
    trade_journal_log_path = tmp_path / "trade_journal.jsonl"

    result = asyncio.run(
        pipeline.evaluate_holdings(
            portfolio,
            DAY,
            sell.execute_sell_simulated,
            log_path=log_path,
            trade_journal_log_path=trade_journal_log_path,
        )
    )

    assert result.positions == []
    assert result.cash_weight == pytest.approx(1.0)

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["reason"] == "stop_loss"
    assert entries[0]["ticker"] == "005930"

    journal_entries = [json.loads(line) for line in trade_journal_log_path.read_text().splitlines()]
    assert len(journal_entries) == 1
    assert journal_entries[0]["event"] == "sell"
    assert journal_entries[0]["reason"] == "stop_loss"
    assert journal_entries[0]["reasoning"] is None
    assert journal_entries[0]["exit_price"] == 89.0
    assert journal_entries[0]["realized_pnl_pct"] == pytest.approx(-0.11)
    assert journal_entries[0]["holding_days"] == 0


def test_take_profit_partial_sell_and_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 120.0)  # +20%

    position = _position(entry_price=100.0, peak_price=100.0, weight=0.09, take_profit_stage=0)
    portfolio = PortfolioState(positions=[position], cash_weight=0.91)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(
        pipeline.evaluate_holdings(
            portfolio,
            DAY,
            sell.execute_sell_simulated,
            log_path=log_path,
            trade_journal_log_path=tmp_path / "trade_journal.jsonl",
        )
    )

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

    result = asyncio.run(
        pipeline.evaluate_holdings(
            portfolio,
            DAY,
            sell.execute_sell_simulated,
            log_path=log_path,
            trade_journal_log_path=tmp_path / "trade_journal.jsonl",
        )
    )

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

    result = asyncio.run(
        pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated, log_path=log_path)
    )

    assert result == portfolio
    assert not log_path.exists()


# --- LLM 재량 매도 계층 (analyst_fn/judge_sell_fn) ---


def test_llm_layer_skipped_by_default(monkeypatch, tmp_path):
    """analyst_fn/judge_sell_fn을 안 넣으면 결정론적 안전장치만 돈다 — 트리거
    안 되는 정상 범위 가격에서는 아무것도 재평가하지 않는다."""
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 105.0)

    def fail(*a, **k):
        raise AssertionError("analyst_fn을 안 줬으면 호출되면 안 된다")

    position = _position(entry_price=100.0, peak_price=100.0)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(
        pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated, log_path=log_path)
    )

    assert result.positions[0].weight == 0.10


def test_llm_layer_not_consulted_when_deterministic_already_triggered(monkeypatch, tmp_path):
    """코드가 이미 손절을 결정했으면 같은 포지션에 대해 LLM에게 다시 묻지 않는다."""
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 89.0)  # -11%, 손절 트리거

    async def fail_analyst(ticker, sector, day):
        raise AssertionError("결정론적 매도가 이미 트리거됐으면 재분석하면 안 된다")

    async def fail_judge(ticker, opinions, unrealized_pct):
        raise AssertionError("결정론적 매도가 이미 트리거됐으면 judge_sell을 부르면 안 된다")

    position = _position(entry_price=100.0, peak_price=100.0)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(
        pipeline.evaluate_holdings(
            portfolio,
            DAY,
            sell.execute_sell_simulated,
            analyst_fn=fail_analyst,
            judge_sell_fn=fail_judge,
            log_path=log_path,
            trade_journal_log_path=tmp_path / "trade_journal.jsonl",
        )
    )

    assert result.positions == []


def test_llm_layer_consulted_when_no_deterministic_trigger(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 105.0)  # 트리거 없음

    captured = {}

    async def fake_analyst(ticker, sector, day):
        captured["analyst_called_with"] = (ticker, sector)
        return [AnalystOpinion(agent="chart", ticker=ticker, score=-0.6, confidence=0.7, evidence=["e"], as_of=day)]

    async def fake_judge(ticker, opinions, unrealized_pct):
        captured["judge_called_with"] = (ticker, len(opinions), round(unrealized_pct, 4))
        return SellAction(ticker=ticker, reason="llm_discretionary", sell_fraction=1.0, reasoning="근거 없어짐")

    position = _position(entry_price=100.0, peak_price=100.0, weight=0.10, entry_day=DAY.date())
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"
    trade_journal_log_path = tmp_path / "trade_journal.jsonl"

    result = asyncio.run(
        pipeline.evaluate_holdings(
            portfolio,
            DAY,
            sell.execute_sell_simulated,
            analyst_fn=fake_analyst,
            judge_sell_fn=fake_judge,
            log_path=log_path,
            trade_journal_log_path=trade_journal_log_path,
        )
    )

    assert captured["analyst_called_with"] == ("005930", "반도체")
    assert captured["judge_called_with"] == ("005930", 1, 0.05)
    assert result.positions == []  # llm_discretionary는 전량 매도

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert entries[0]["reason"] == "llm_discretionary"

    journal_entries = [json.loads(line) for line in trade_journal_log_path.read_text().splitlines()]
    assert journal_entries[0]["reason"] == "llm_discretionary"
    assert journal_entries[0]["reasoning"] == "근거 없어짐"
    assert journal_entries[0]["holding_days"] == 0


def test_llm_layer_holds_when_judge_sell_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 105.0)

    async def fake_analyst(ticker, sector, day):
        return [AnalystOpinion(agent="chart", ticker=ticker, score=0.3, confidence=0.5, evidence=["e"], as_of=day)]

    async def fake_judge(ticker, opinions, unrealized_pct):
        return None  # HOLD

    position = _position(entry_price=100.0, peak_price=100.0, weight=0.10)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(
        pipeline.evaluate_holdings(
            portfolio,
            DAY,
            sell.execute_sell_simulated,
            analyst_fn=fake_analyst,
            judge_sell_fn=fake_judge,
            log_path=log_path,
        )
    )

    assert result.positions[0].weight == 0.10
    assert not log_path.exists()


def test_llm_layer_analyst_failure_treated_as_no_opinions(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 105.0)

    async def failing_analyst(ticker, sector, day):
        raise RuntimeError("boom")

    captured = {}

    async def fake_judge(ticker, opinions, unrealized_pct):
        captured["opinions"] = opinions
        return None

    position = _position(entry_price=100.0, peak_price=100.0)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(
        pipeline.evaluate_holdings(
            portfolio,
            DAY,
            sell.execute_sell_simulated,
            analyst_fn=failing_analyst,
            judge_sell_fn=fake_judge,
            log_path=log_path,
        )
    )

    assert captured["opinions"] == []
    assert result.positions[0].weight == 0.10
