import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from src import kis, pipeline, sell
from src.schemas import AnalystOpinion, ExitPlan, OHLCVBar, PortfolioState, Position, SellAction


def _position(**overrides) -> Position:
    defaults = dict(ticker="005930", sector="반도체", weight=0.10, entry_price=100.0, peak_price=100.0)
    defaults.update(overrides)
    return Position(**defaults)


DAY = datetime(2026, 8, 9, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_real_notify_or_name_lookup(monkeypatch, tmp_path):
    # evaluate_holdings가 매도마다 텔레그램 알림 + 종목명 조회를 시도한다 — 목킹
    # 안 하면 테스트가 실제 텔레그램 메시지를 보내고 실제 네이버를 긁는다
    # (2026-08-09, execute_buy_order 쪽과 같은 이유로 실수로 한 번 겪음).
    monkeypatch.setattr(pipeline.notify, "send_telegram_alert", lambda message: True)
    monkeypatch.setattr(pipeline, "display_name", lambda ticker: ticker)

    # execute_sell_order가 주문 전후로 누적 체결 집계를 조회한다 — 막지 않으면
    # 실제 KIS를 때리고 타임아웃까지 기다린다. 기본은 "조회 불가"라 체결 확인
    # 실패 경로가 돌고 호가로 폴백한다.
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: None)

    # 텔레그램에 보이기 전 action.reasoning을 한국어로 옮기는 단계(src/translate.py)가
    # 실제 Claude API를 타지 않게 항등 함수로 막는다.
    async def _identity(text, label="translate"):
        return text

    monkeypatch.setattr(pipeline.translate, "to_korean", _identity)

    # "하루 1회" 알림 마커를 tmp로 돌린다. 기본값은 레포의 logs/alert_markers라,
    # 안 막으면 테스트가 실제 운영 마커를 써버린다 — EC2에서 테스트를 한 번
    # 돌리면 그날 진짜 장애 알림이 "이미 보냈다"로 묻힌다.
    monkeypatch.setattr(pipeline.notify, "ALERT_MARKER_DIR", tmp_path / "alert_markers")

    # 시세 공백이 복구되면 _audit_blackout_window가 일봉을 받아 사후 판정한다 —
    # 막지 않으면 그 경로를 지나는 테스트가 실제 KIS를 때린다. 기본값은 "조회 불가"라
    # 감사는 checked=0으로 끝나고, 일봉이 필요한 테스트는 _daily_bars로 따로 건다.
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days=60, **kw: None)


def _daily_bars(monkeypatch, low, high, bar_date=None):
    """공백 사후 판정용 일봉을 건다. 기본 날짜는 DAY와 같은 날이라 판정 대상이 된다."""
    bar = OHLCVBar(
        date=bar_date or DAY.date(), open=high, high=high, low=low, close=low, volume=1
    )
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days=60, **kw: [bar])


def test_returns_unchanged_when_no_positions(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("포지션이 없으면 KIS를 호출하면 안 된다")

    monkeypatch.setattr(kis, "fetch_current_price", fail)

    portfolio = PortfolioState()
    result = asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert result == portfolio


def test_skips_position_when_price_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: None)

    position = _position()
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    log_path = tmp_path / "sell.jsonl"

    result = asyncio.run(
        pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated, log_path=log_path)
    )

    assert result == portfolio
    assert not log_path.exists()


def test_no_sell_when_price_within_normal_range(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 105.0)

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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 89.0)  # -11%

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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 120.0)  # +20%

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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: prices[ticker])

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
    def raise_error(ticker, policy=None):
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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 105.0)

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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 89.0)  # -11%, 손절 트리거

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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 105.0)  # 트리거 없음

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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 105.0)

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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 105.0)

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


# --- 매매일지: 줄인 비중과 매도 금액 (사용자 요청 2026-08-15) ---


def test_journal_records_weight_reduced_and_sell_amount_on_partial_take_profit(monkeypatch, tmp_path):
    """부분 익절은 sell_fraction(잔량 대비 비율)만 봐서는 포트폴리오를 얼마나 줄였는지
    안 보인다 — 전체 대비 줄인 비중과 매도 금액이 일지에 같이 남아야 한다."""
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 120.0)  # +20%, 1차 익절
    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-1")

    position = _position(
        entry_price=100.0, peak_price=100.0, weight=0.12, entry_day=DAY.date(), quantity=30
    )
    portfolio = PortfolioState(positions=[position], cash_weight=0.88)
    trade_journal_log_path = tmp_path / "trade_journal.jsonl"

    asyncio.run(
        pipeline.evaluate_holdings(
            portfolio,
            DAY,
            sell.execute_sell_order,
            log_path=tmp_path / "sell.jsonl",
            trade_journal_log_path=trade_journal_log_path,
        )
    )

    entry = json.loads(trade_journal_log_path.read_text().strip())
    assert entry["reason"] == "take_profit_trail"
    assert entry["shares_sold"] == 10  # 30주의 1/3
    assert entry["sell_amount"] == pytest.approx(1200.0)  # 10주 x 120원
    # 사람이 읽는 값은 이 종목 보유분 기준이다(합 100%).
    assert entry["shares_before"] == 30
    assert entry["shares_after"] == 20
    assert entry["position_fraction_sold"] == pytest.approx(1 / 3)
    assert entry["position_fraction_remaining"] == pytest.approx(2 / 3)
    # 포트폴리오 대비 비중은 분석용으로 계속 남는다.
    assert entry["portfolio_weight_before"] == pytest.approx(0.12)
    assert entry["portfolio_weight_sold"] == pytest.approx(0.04)


def test_journal_records_full_position_on_stop_loss(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 89.0)  # -11%
    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-1")

    position = _position(
        entry_price=100.0, peak_price=100.0, weight=0.10, entry_day=DAY.date(), quantity=30
    )
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    trade_journal_log_path = tmp_path / "trade_journal.jsonl"

    asyncio.run(
        pipeline.evaluate_holdings(
            portfolio,
            DAY,
            sell.execute_sell_order,
            log_path=tmp_path / "sell.jsonl",
            trade_journal_log_path=trade_journal_log_path,
        )
    )

    entry = json.loads(trade_journal_log_path.read_text().strip())
    assert entry["reason"] == "stop_loss"
    assert entry["shares_sold"] == 30
    assert entry["sell_amount"] == pytest.approx(2670.0)  # 30주 x 89원
    assert entry["shares_after"] == 0
    assert entry["position_fraction_sold"] == pytest.approx(1.0)  # 전량
    assert entry["position_fraction_remaining"] == pytest.approx(0.0)
    assert entry["portfolio_weight_sold"] == pytest.approx(0.10)


def test_journal_records_the_exit_plan_that_actually_applied(monkeypatch, tmp_path):
    """LLM이 정한 계획으로 잘린 건지 고정 기본값으로 잘린 건지 나중에 갈라볼 수 있어야 한다."""
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 94.0)  # -6%
    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-1")

    plan = ExitPlan(
        stop_loss_pct=-0.06, take_profit_pct=0.12, take_profit_fraction=0.25, trail_pct=-0.04
    )
    position = _position(
        entry_price=100.0, peak_price=100.0, weight=0.10, entry_day=DAY.date(), quantity=30,
        exit_plan=plan,
    )
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    trade_journal_log_path = tmp_path / "trade_journal.jsonl"

    asyncio.run(
        pipeline.evaluate_holdings(
            portfolio,
            DAY,
            sell.execute_sell_order,
            log_path=tmp_path / "sell.jsonl",
            trade_journal_log_path=trade_journal_log_path,
        )
    )

    entry = json.loads(trade_journal_log_path.read_text().strip())
    assert entry["reason"] == "stop_loss"  # 고정값 -10%였다면 아직 안 잘렸을 것
    assert entry["exit_plan"]["stop_loss_pct"] == pytest.approx(-0.06)


def test_position_fractions_always_sum_to_one(monkeypatch, tmp_path):
    """한 행만 보고 해석되려면 두 값의 합이 100%여야 한다 — 이게 깨지면 '보유분의
    몇 %'라는 말 자체가 성립하지 않는다. 주식수 내림으로 33.3%를 정확히 못 파는
    경우(30주가 아닌 31주)에도 성립해야 한다."""
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 120.0)
    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-1")

    position = _position(entry_price=100.0, peak_price=100.0, weight=0.12, entry_day=DAY.date(), quantity=31)
    portfolio = PortfolioState(positions=[position], cash_weight=0.88)
    trade_journal_log_path = tmp_path / "trade_journal.jsonl"

    asyncio.run(
        pipeline.evaluate_holdings(
            portfolio, DAY, sell.execute_sell_order,
            log_path=tmp_path / "sell.jsonl", trade_journal_log_path=trade_journal_log_path,
        )
    )

    entry = json.loads(trade_journal_log_path.read_text().strip())
    assert entry["shares_sold"] == 10  # int(31/3)
    assert entry["shares_after"] == 21
    assert entry["position_fraction_sold"] == pytest.approx(10 / 31)
    assert (
        entry["position_fraction_sold"] + entry["position_fraction_remaining"] == pytest.approx(1.0)
    )


def test_journal_does_not_trust_a_fill_that_missed_part_of_the_order(monkeypatch, tmp_path):
    """2026-08-18 036570 재현. 31주 손절이 다 나갔는데 체결 조회가 19주만 잡았고,
    그 값을 그대로 믿는 바람에 일지에 "61.3% 매도 / 38.7% 잔여"와 "잔여 0주"가
    같은 행에 실렸다. 잔여를 1-매도로 되계산한 탓에 합만 1.0으로 맞아떨어져
    한 행만 봐서는 틀린 걸 알 수 없었다.

    덜 잡힌 체결(complete=False)이면 수량은 집행 전후 상태 차이에서 다시 뽑고,
    총액은 관측된 평균 단가로 되세운다."""
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 229_500.0)  # 판단 시점 호가
    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-1")

    # 주문 직전 0주 -> 조회 시점엔 19주만 잡힘(실제로는 31주가 다 체결됐다).
    totals = iter([(0, 0.0), (19, 4_364_500.0)])
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: next(totals))

    position = _position(
        ticker="036570", entry_price=255_500.0, peak_price=255_500.0, weight=0.08,
        entry_day=DAY.date(), quantity=31,
    )
    portfolio = PortfolioState(positions=[position], cash_weight=0.92)
    trade_journal_log_path = tmp_path / "trade_journal.jsonl"

    asyncio.run(
        pipeline.evaluate_holdings(
            portfolio, DAY, sell.execute_sell_order,
            log_path=tmp_path / "sell.jsonl", trade_journal_log_path=trade_journal_log_path,
        )
    )

    entry = json.loads(trade_journal_log_path.read_text().strip())
    assert entry["reason"] == "stop_loss"
    # 체결 조회가 19주만 잡았어도 실제로 나간 건 31주다.
    assert entry["shares_before"] == 31
    assert entry["shares_sold"] == 31
    assert entry["shares_after"] == 0
    assert entry["position_fraction_sold"] == pytest.approx(1.0)
    assert entry["position_fraction_remaining"] == pytest.approx(0.0)  # "38.7% 잔여"가 아니다
    # 총액은 19주분 4,364,500원이 아니라 단가 x 실제 수량으로 되세운 값.
    assert entry["sell_amount"] == pytest.approx(31 * (4_364_500 / 19))
    assert entry["sell_amount_source"] == "estimated"
    assert entry["exit_price_source"] == "fill_partial"


def test_alerts_once_when_no_position_price_could_be_fetched(monkeypatch, tmp_path):
    """보유 종목 시세를 하나도 못 받으면 이 회차는 손절·익절을 아무것도 판정하지
    않는다. 문턱을 안 넘어서 조용한 것과 눈을 감아서 조용한 것은 다른데, 지금까지
    둘 다 똑같이 조용했다 — 2026-08-20 15:17~15:27에 실제로 그랬다."""
    portfolio = PortfolioState(cash_weight=0.80, positions=[_position(), _position(ticker="000660")])

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: None)

    alerts = []
    monkeypatch.setattr(pipeline.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    result = asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert result == portfolio  # 아무것도 안 팔았고 상태도 그대로
    assert len(alerts) == 1
    assert "시세" in alerts[0]

    # 매분 도는 잡이라 두 번째 회차는 조용해야 한다(로그에는 계속 남는다).
    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))
    assert len(alerts) == 1


def test_no_all_prices_alert_when_at_least_one_price_arrives(monkeypatch, tmp_path):
    """일부만 실패하는 건 정상 장애 범위다 — 여기까지 알리면 매분 울린다."""
    portfolio = PortfolioState(cash_weight=0.80, positions=[_position(), _position(ticker="000660")])

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 100.0 if ticker == "005930" else None)

    alerts = []
    monkeypatch.setattr(pipeline.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert alerts == []


def test_per_minute_check_asks_kis_with_the_fast_fail_policy(monkeypatch, tmp_path):
    """매분 도는 이 경로만 빠른 실패 예산을 쓴다.

    크론이 곧 재시도 루프라 회차 안에서 버티는 건 공백만 늘린다. 2026-08-21에
    기존 예산으로 한 회차가 실측 75초 걸려, 회차 실패와 락 스킵이 교대로 이어지며
    15:07~15:29 23분간 손절/익절 판정이 한 번도 없었다.
    """
    seen = []

    def record(ticker, policy=None):
        seen.append(policy)
        return 105.0

    monkeypatch.setattr(kis, "fetch_current_price", record)

    portfolio = PortfolioState(positions=[_position()], cash_weight=0.90)

    asyncio.run(
        pipeline.evaluate_holdings(
            portfolio, DAY, sell.execute_sell_simulated, log_path=tmp_path / "sell.jsonl"
        )
    )

    assert seen == [kis.FAST_FAIL_POLICY]


# --- 공백이 길어지면 다시 알린다 (2026-08-21 오후 23분 공백 무알림) ---


def _blind(monkeypatch):
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: None)


def _frozen_clock(monkeypatch, tmp_path, start):
    """evaluate_holdings는 시계를 인자로 받지 않는다 — track_blackout을 감싸 시간만
    갈아끼운다. 원본을 먼저 잡아두지 않으면 감싼 함수가 자기 자신을 부른다."""
    real_track = pipeline.notify.track_blackout
    clock = [start]
    monkeypatch.setattr(
        pipeline.notify,
        "track_blackout",
        lambda ctx, blind, **kw: real_track(ctx, blind, now=clock[0], state_dir=tmp_path / "bo"),
    )
    return clock


def test_long_blackout_alerts_again_even_after_the_daily_one_was_spent(monkeypatch, tmp_path):
    """하루 1회 알림은 "시작됐다"까지만 알린다. 2026-08-21은 13:02에 그걸 써버려서
    15:07~15:29의 23분 공백(장 마감 직전, 안전장치가 통째로 눈을 감은 구간)이
    무알림으로 지나갔다."""
    portfolio = PortfolioState(cash_weight=0.80, positions=[_position(), _position(ticker="000660")])
    _blind(monkeypatch)

    alerts = []
    monkeypatch.setattr(pipeline.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    clock = _frozen_clock(monkeypatch, tmp_path, datetime(2026, 8, 21, 15, 7, tzinfo=pipeline.notify.KST))

    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))
    assert len(alerts) == 1  # 하루 1회 알림

    clock[0] = datetime(2026, 8, 21, 15, 13, tzinfo=pipeline.notify.KST)
    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert len(alerts) == 2
    assert "공백 지속" in alerts[1]
    assert "6분째" in alerts[1]


def test_recovery_reports_how_long_the_safety_net_was_blind(monkeypatch, tmp_path):
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])

    alerts = []
    monkeypatch.setattr(pipeline.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    clock = _frozen_clock(monkeypatch, tmp_path, datetime(2026, 8, 21, 13, 51, tzinfo=pipeline.notify.KST))

    _blind(monkeypatch)
    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))
    clock[0] = datetime(2026, 8, 21, 13, 57, tzinfo=pipeline.notify.KST)
    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 100.0)
    clock[0] = datetime(2026, 8, 21, 13, 58, tzinfo=pipeline.notify.KST)
    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert "공백 종료" in alerts[-1]
    assert "7분간" in alerts[-1]


def test_every_round_leaves_a_line_even_when_nothing_happens(monkeypatch, tmp_path, caplog):
    """성공 회차가 무로깅이면 "문턱을 안 넘어 조용한 회차"와 "아예 안 돈 회차"가
    로그에서 같은 모양이 된다 — 2026-08-21 장애를 사후 분석할 때 실패 로그의
    부재로 성공을 역추정해야 했다."""
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 100.0)

    with caplog.at_level("INFO"):
        asyncio.run(
            pipeline.evaluate_holdings(
                portfolio, DAY, sell.execute_sell_simulated, log_path=tmp_path / "sell.jsonl"
            )
        )

    assert "evaluate_holdings_done positions=1 priced=1 sells=0" in caplog.text


# --- 복구 직후 일봉으로 공백 구간을 사후 판정한다 (2026-08-27) ---


def _blackout_then_recover(monkeypatch, tmp_path, portfolio, alerts):
    """공백 2회차(승격까지) 후 시세가 돌아온 회차까지 돌린다."""
    monkeypatch.setattr(pipeline.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)
    clock = _frozen_clock(monkeypatch, tmp_path, datetime(2026, 8, 26, 14, 7, tzinfo=pipeline.notify.KST))

    _blind(monkeypatch)
    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))
    clock[0] = datetime(2026, 8, 26, 14, 13, tzinfo=pipeline.notify.KST)
    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 100.0)
    clock[0] = datetime(2026, 8, 26, 15, 25, tzinfo=pipeline.notify.KST)
    return clock


def test_recovery_alert_clears_the_window_with_the_daily_bar(monkeypatch, tmp_path):
    """2026-08-26의 실제 결과 — 77분 공백이었지만 어느 포지션도 문턱을 안 지났다.
    사람이 8/27에 손으로 확인한 것을 복구 알림이 스스로 하게 만든 것이다."""
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position(entry_price=100.0, peak_price=100.0)])
    alerts = []
    _blackout_then_recover(monkeypatch, tmp_path, portfolio, alerts)
    _daily_bars(monkeypatch, low=95.0, high=108.0)

    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert "공백 종료" in alerts[-1]
    assert "일봉 대조(1/1종목)" in alerts[-1]
    assert "문턱에 닿은 종목 없음" in alerts[-1]


def test_recovery_alert_flags_a_threshold_the_blind_window_hid(monkeypatch, tmp_path):
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position(entry_price=100.0, peak_price=100.0)])
    alerts = []
    _blackout_then_recover(monkeypatch, tmp_path, portfolio, alerts)
    _daily_bars(monkeypatch, low=88.0, high=101.0)  # 손절선(-10%) 아래를 찍었다

    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert "⚠️" in alerts[-1]
    assert "005930 손절" in alerts[-1]


def test_recovery_alert_does_not_claim_safety_when_the_bar_lookup_also_failed(monkeypatch, tmp_path):
    """일봉 조회도 같은 KIS다 — 복구 직후에 또 죽으면 "안 닿았다"고 말하면 안 된다."""
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])
    alerts = []
    _blackout_then_recover(monkeypatch, tmp_path, portfolio, alerts)
    # 오토유즈 픽스처의 기본값이 그대로 — fetch_daily_ohlcv는 None을 준다.

    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert "일봉 대조 실패" in alerts[-1]
    assert "문턱에 닿은 종목 없음" not in alerts[-1]


def test_recovery_alert_ignores_a_stale_bar(monkeypatch, tmp_path):
    """전날 봉으로 재면 오늘 공백과 무관한 답이 나온다 — 판정하지 않은 것으로 센다."""
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])
    alerts = []
    _blackout_then_recover(monkeypatch, tmp_path, portfolio, alerts)
    _daily_bars(monkeypatch, low=95.0, high=108.0, bar_date=date(2026, 8, 8))

    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert "일봉 대조 실패" in alerts[-1]


def test_recovery_alert_still_goes_out_when_the_audit_blows_up(monkeypatch, tmp_path):
    """감사는 부가 정보다 — 실패해도 복구 알림 자체를 막으면 안 된다."""
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])
    alerts = []
    _blackout_then_recover(monkeypatch, tmp_path, portfolio, alerts)

    def boom(ticker, lookback_days=60, **kw):
        raise RuntimeError("KIS 폭발")

    monkeypatch.setattr(kis, "fetch_daily_ohlcv", boom)

    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert "공백 종료" in alerts[-1]


def test_quiet_rounds_never_touch_the_daily_bar_api(monkeypatch, tmp_path):
    """공백이 없었으면 감사도 없다 — 매분 도는 회차에 조회를 하나도 더 얹지 않는다."""
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 100.0)

    def fail(*a, **k):
        raise AssertionError("공백이 없던 회차가 일봉을 조회했다")

    monkeypatch.setattr(kis, "fetch_daily_ohlcv", fail)

    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))


# --- 관측 범위 기록과 장 마감 후 대조 (2026-08-27) ---


def test_each_round_widens_the_observed_range(monkeypatch, tmp_path):
    """매분 본 가격의 최저/최고를 남긴다 — 이게 있어야 "안 넘어서 조용한 것"과
    "넘었는데 그 순간을 안 본 것"을 사후에 가릴 수 있다."""
    path = tmp_path / "observed_range.json"
    monkeypatch.setattr(pipeline, "DEFAULT_OBSERVED_RANGE_PATH", path)
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position()])

    for price in (100.0, 94.0, 108.0):
        monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None, p=price: p)
        asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    observed = pipeline.load_observed_range(path=path)
    assert observed["005930"]["min"] == 94.0
    assert observed["005930"]["max"] == 108.0
    assert observed["005930"]["rounds"] == 3


def test_observed_range_from_another_day_is_not_reused(monkeypatch, tmp_path):
    """어제 관측 범위를 오늘 일봉과 대조하면 아무 의미가 없다."""
    path = tmp_path / "observed_range.json"
    path.write_text(json.dumps({"day": "2020-01-01", "observed": {"005930": {"min": 1.0, "max": 2.0}}}))

    assert pipeline.load_observed_range(path=path) == {}


def test_audit_reports_a_threshold_the_sampling_walked_past(monkeypatch, tmp_path):
    """2026-08-27 192820의 모양 그대로 — 일봉 저가는 트리거 아래인데, 매분 샘플은
    한 번도 그 아래를 못 봤다. 회차가 다 돌아도 생기는 구멍이라 공백 알림은 못 잡는다."""
    path = tmp_path / "observed_range.json"
    path.write_text(
        json.dumps({"day": pipeline._kst_today().isoformat(), "observed": {"005930": {"min": 95.0, "max": 101.0}}})
    )
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position(entry_price=100.0, peak_price=100.0)])
    _daily_bars(monkeypatch, low=88.0, high=101.0)  # 손절선(-10%) 아래를 찍었다

    missed = asyncio.run(pipeline.audit_observation_gap(portfolio, DAY, path=path))

    assert missed == ["005930 손절"]


def test_audit_is_silent_when_we_actually_saw_the_threshold(monkeypatch, tmp_path):
    """관측 범위가 이미 문턱을 넘었다면 안전장치는 볼 기회가 있었던 것이다 —
    팔았든 안 팔았든 "못 본 문턱"으로 보고할 일이 아니다."""
    path = tmp_path / "observed_range.json"
    path.write_text(
        json.dumps({"day": pipeline._kst_today().isoformat(), "observed": {"005930": {"min": 88.0, "max": 101.0}}})
    )
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position(entry_price=100.0, peak_price=100.0)])
    _daily_bars(monkeypatch, low=88.0, high=101.0)

    assert asyncio.run(pipeline.audit_observation_gap(portfolio, DAY, path=path)) == []


def test_audit_says_nothing_about_a_ticker_it_never_observed(monkeypatch, tmp_path):
    """오늘 산 종목이나 상태 파일이 날아간 경우다 — "안 넘었다"가 아니라 "모른다"라,
    판정 자체를 하지 않는다."""
    path = tmp_path / "observed_range.json"
    path.write_text(json.dumps({"day": pipeline._kst_today().isoformat(), "observed": {"000660": {"min": 1.0, "max": 2.0}}}))
    portfolio = PortfolioState(cash_weight=0.90, positions=[_position(entry_price=100.0, peak_price=100.0)])
    _daily_bars(monkeypatch, low=1.0, high=2.0)

    assert asyncio.run(pipeline.audit_observation_gap(portfolio, DAY, path=path)) == []
