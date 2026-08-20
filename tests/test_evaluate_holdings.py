import asyncio
import json
from datetime import datetime, timezone

import pytest

from src import kis, pipeline, sell
from src.schemas import AnalystOpinion, ExitPlan, PortfolioState, Position, SellAction


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


# --- 매매일지: 줄인 비중과 매도 금액 (사용자 요청 2026-08-15) ---


def test_journal_records_weight_reduced_and_sell_amount_on_partial_take_profit(monkeypatch, tmp_path):
    """부분 익절은 sell_fraction(잔량 대비 비율)만 봐서는 포트폴리오를 얼마나 줄였는지
    안 보인다 — 전체 대비 줄인 비중과 매도 금액이 일지에 같이 남아야 한다."""
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 120.0)  # +20%, 1차 익절
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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 89.0)  # -11%
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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 94.0)  # -6%
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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 120.0)
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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 229_500.0)  # 판단 시점 호가
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

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: None)

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

    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker: 100.0 if ticker == "005930" else None)

    alerts = []
    monkeypatch.setattr(pipeline.notify, "send_telegram_alert", lambda message: alerts.append(message) or True)

    asyncio.run(pipeline.evaluate_holdings(portfolio, DAY, sell.execute_sell_simulated))

    assert alerts == []
