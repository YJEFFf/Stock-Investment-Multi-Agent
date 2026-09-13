import asyncio
import json
from datetime import date

import pytest

from src import kis, pipeline
from src.schemas import Decision, ExitPlan, GateResult, PortfolioState, Position

TICKER = "005930"


@pytest.fixture(autouse=True)
def _no_real_notify_or_name_lookup(monkeypatch):
    # execute_buy_order가 성공/스킵마다 텔레그램 알림 + 종목명 조회(collectors 경유)를
    # 시도한다 — 목킹 안 하면 테스트가 실제 텔레그램 메시지를 보내고 실제 네이버를
    # 긁는다(2026-08-09, 실수로 한 번 겪음).
    monkeypatch.setattr(pipeline.notify, "send_telegram_alert", lambda message: True)
    monkeypatch.setattr(pipeline, "display_name", lambda ticker: ticker)

    # 텔레그램에 보이기 전 decision.reason을 한국어로 옮기는 단계(src/translate.py)가
    # 실제 Claude API를 타지 않게 항등 함수로 막는다 — 번역 자체 검증은 tests/test_translate.py.
    async def _identity(text, label="translate"):
        return text

    monkeypatch.setattr(pipeline.translate, "to_korean", _identity)

    # execute_buy_order가 주문 전후로 누적 체결 집계를 조회한다 — 막지 않으면 테스트가
    # 실제 KIS를 때리고 타임아웃까지 기다린다. 체결 브래킷을 실제로 검증하는
    # 테스트는 _wire_buy_with_prices로 이 목을 다시 덮어쓴다.
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: None)


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


def test_skips_when_gap_too_large(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 110.0)  # +10%, 문턱(3%) 초과

    def fail(*a, **k):
        raise AssertionError("갭이 크면 잔고 조회까지 가면 안 된다")

    monkeypatch.setattr(kis, "fetch_account_balance", fail)

    portfolio = PortfolioState()
    log_path = tmp_path / "trade_journal.jsonl"
    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08, log_path=log_path
        )
    )

    assert result == portfolio
    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert entries[0]["event"] == "buy_skipped"
    assert entries[0]["reason"] == "gap_too_large"
    assert entries[0]["gap_pct"] == pytest.approx(0.1)


def test_skips_when_price_data_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: None)
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 100.0)

    portfolio = PortfolioState()
    log_path = tmp_path / "trade_journal.jsonl"
    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08, log_path=log_path
        )
    )

    assert result == portfolio
    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert entries[0]["reason"] == "price_data_unavailable"


def test_skips_when_balance_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 101.0)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: None)

    portfolio = PortfolioState()
    log_path = tmp_path / "trade_journal.jsonl"
    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08, log_path=log_path
        )
    )

    assert result == portfolio
    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert entries[0]["reason"] == "balance_unavailable"


def test_skips_when_order_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 101.0)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: None)

    portfolio = PortfolioState()
    log_path = tmp_path / "trade_journal.jsonl"
    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08, log_path=log_path
        )
    )

    assert result == portfolio
    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert entries[0]["reason"] == "order_rejected"


def test_opens_new_position_with_fill_price(monkeypatch, tmp_path):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(230000.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 231000.0)  # 갭 ~0.4%, 문턱 이내
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
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 101.0)
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


def test_adds_to_existing_position_falls_back_to_weight_when_shares_unknown(monkeypatch, tmp_path):
    """주식수를 안 들고 있는 포지션(시뮬레이션 경로)에서는 비중 가중으로 물러선다.

    정확하진 않지만 수량 자체가 없어 달리 방법이 없다. 수량이 있으면 아래
    test_adds_to_existing_position_averages_by_shares 쪽 경로를 타야 한다.
    """
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(200.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 200.0)
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
    # 기존 포지션에 수량이 없었으므로(existing.quantity is None) 비중 폴백을 탄다.
    # 비중 가중: (100*0.08 + 200*0.08) / 0.16 = 150
    assert pos.entry_price == pytest.approx(150.0)
    assert pos.peak_price == 200.0  # 새 체결가가 기존 고점보다 높음
    assert pos.entry_day == date(2026, 1, 5)  # 추가매수해도 최초 진입일 유지


def test_adds_to_existing_position_averages_by_shares(monkeypatch, tmp_path):
    """평균 원가는 주식수 가중이다 — 비중 가중은 단가를 제곱으로 넣는 셈이라 틀린다.

    `weight`는 매수 시점 원가 기준 비중이라 `weight ∝ 수량 x 단가`다. 그걸로 단가를
    가중하면 비싼 쪽 매수로 평균이 끌려간다: 100주 @1,000원 + 50주 @2,000원이
    정답 1,333.33원 대신 1,500원(+12.5%)이 됐다(2026-08-28 발견).

    진입가가 부풀면 손절은 일찍, 익절은 늦게 발동한다 — 2026-08-15 192820 오익절과
    같은 부류다. `position_limit`(0.15)이 온전한 0.08 포지션의 추가매수는 막지만
    **부분 익절로 비중이 준 종목은 뚫리므로** 죽은 경로가 아니다.
    """
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(2000.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 2000.0)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 1_250_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: "ODNO789")
    monkeypatch.setattr(kis, "fetch_fill_price", lambda ticker, order_date: 2000.0)

    # 트림돼서 비중이 낮아진 포지션 — 실제로 게이트를 통과할 수 있는 모양이다.
    existing = Position(
        ticker=TICKER, sector="반도체", weight=0.02, entry_day=date(2026, 1, 5),
        entry_price=1000.0, peak_price=1200.0, quantity=100,
    )
    portfolio = PortfolioState(positions=[existing], cash_weight=0.9)

    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), portfolio,
            "반도체", 0.08, log_path=tmp_path / "trade_journal.jsonl",
        )
    )

    pos = result.positions[0]
    assert pos.quantity == 150  # 1,250,000 * 0.08 // 2,000 = 50주 추가
    # 주식수 가중: (100*1000 + 50*2000) / 150 = 1333.33  (비중 가중이면 1,600원)
    assert pos.entry_price == pytest.approx(1000 * 100 / 150 + 2000 * 50 / 150)
    assert pos.entry_price == pytest.approx(1333.333333, rel=1e-6)


# --- 출구 규칙은 진입 시점에 코드가 변동성으로 정한다 (2026-09-14, 매니저 결정에서 변경) ---


def _plan(stop_loss_pct=-0.06) -> ExitPlan:
    return ExitPlan(
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=abs(stop_loss_pct) * 2,
        take_profit_fraction=0.25,
        trail_pct=-0.04,
    )


def _volatile_bars(daily_move=0.02, n=30):
    """종가가 +2%, −2%를 번갈아 움직이는 30거래일. 20일 일간 수익률 표준편차가 약 2%다."""
    from datetime import timedelta

    from src.schemas import OHLCVBar

    bars, px, d = [], 100.0, date(2026, 7, 1)
    for i in range(n):
        px = px * (1 + daily_move) if i % 2 else px / (1 + daily_move)
        bars.append(OHLCVBar(date=d + timedelta(days=i), open=px, high=px, low=px, close=px, volume=1000))
    return bars


def _wire_successful_buy(monkeypatch, bars=None):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: bars or _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: (bars[-1].close if bars else 101.0))
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: "order-1")
    monkeypatch.setattr(kis, "fetch_fill_price", lambda ticker, day: (bars[-1].close if bars else 101.0))
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: None)


def _buy(decision, portfolio, log_path):
    return asyncio.run(
        pipeline.execute_buy_order(
            decision, GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08, log_path=log_path
        )
    )


def test_new_position_uses_the_volatility_plan_not_the_managers(monkeypatch, tmp_path):
    """2026-09-14 뒤집음. 매니저 출구 규칙은 829건 중 77%가 같은 값이었고 변동성과 상관이
    없었다. 손절폭은 리스크 한도라 코드가 정한다(규칙 6) — 매니저 값은 포지션에 박지 않는다."""
    from src import collectors, sell

    bars = _volatile_bars()
    _wire_successful_buy(monkeypatch, bars)
    decision = _decision()
    decision.exit_plan = _plan()

    updated = _buy(decision, PortfolioState(positions=[], cash_weight=1.0), tmp_path / "journal.jsonl")

    sigma = collectors.compute_indicators(bars)["daily_return_stdev_20d"]
    expected = sell.volatility_exit_plan(sigma)
    assert updated.positions[0].exit_plan == expected
    assert updated.positions[0].exit_plan != decision.exit_plan


def test_new_position_without_enough_history_falls_back_to_the_default(monkeypatch, tmp_path):
    """20일 변동성을 못 구하면(상장 직후·조회 결과 부족) exit_plan=None이고, sell.plan_for가
    고정 기본값으로 떨어뜨린다. 매니저 값으로 폴백하지 않는다."""
    _wire_successful_buy(monkeypatch)  # 봉 1개뿐
    decision = _decision()
    decision.exit_plan = _plan()

    updated = _buy(decision, PortfolioState(positions=[], cash_weight=1.0), tmp_path / "journal.jsonl")

    assert updated.positions[0].exit_plan is None


def test_adding_to_existing_position_keeps_the_original_exit_plan(monkeypatch, tmp_path):
    """물타기하면서 손절선도 같이 넓히는 경로를 열지 않는다 — 기존 규칙이 이긴다."""
    _wire_successful_buy(monkeypatch, _volatile_bars(0.05))  # 새로 계산하면 훨씬 넓은 규칙이 나온다
    original = _plan(stop_loss_pct=-0.05)
    existing = Position(
        ticker=TICKER, sector="반도체", weight=0.05, entry_price=90.0, peak_price=90.0, quantity=10,
        exit_plan=original,
    )
    log_path = tmp_path / "journal.jsonl"

    updated = _buy(_decision(), PortfolioState(positions=[existing], cash_weight=0.95), log_path)

    assert updated.positions[0].exit_plan == original
    entry = json.loads(log_path.read_text().strip())
    assert entry["exit_plan_source"] == "existing_position"
    assert entry["exit_plan"]["stop_loss_pct"] == pytest.approx(-0.05)


def test_buy_journal_records_applied_plan_source_sigma_and_the_managers_plan(monkeypatch, tmp_path):
    bars = _volatile_bars()
    _wire_successful_buy(monkeypatch, bars)
    log_path = tmp_path / "journal.jsonl"
    decision = _decision()
    decision.exit_plan = _plan()

    updated = _buy(decision, PortfolioState(positions=[], cash_weight=1.0), log_path)

    entry = json.loads(log_path.read_text().strip())
    assert entry["exit_plan"] == updated.positions[0].exit_plan.model_dump(mode="json")
    assert entry["exit_plan_source"] == "volatility"
    assert entry["exit_plan_sigma_20d_pct"] == pytest.approx(2.0, abs=0.1)
    assert entry["manager_exit_plan"]["stop_loss_pct"] == pytest.approx(-0.06)  # 비교용으로만 남는다


# --- 진입가 출처 체인 (2026-08-15, 192820 오익절 건 이후) ---


def _wire_buy_with_prices(monkeypatch, fill_price, position_avg, bracket=None, fill=None):
    """진입가 폴백 체인을 단계별로 확인한다.

    체인: 전후 집계 브래킷 -> 그날 매수 집계 평균 -> 잔고 매입평균가 -> 호가.
    `bracket`은 (주문전, 주문후) 누적 체결 집계 튜플이며 None이면 조회 불가로 둔다.
    `fill`은 (단가, 주문수량 대비 체결 비율) — 주문 수량은 잔고에서 계산돼 테스트가
    미리 알 수 없으므로, "전량 체결"을 수량을 안 박고 표현하려면 이쪽을 쓴다.
    """
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 101.0)  # 주문 직전 호가
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "fetch_fill_price", lambda ticker, day: fill_price)
    monkeypatch.setattr(kis, "fetch_position_avg_price", lambda ticker: position_avg)

    ordered: list[int] = []

    def _place(ticker, qty):
        ordered.append(qty)
        return "order-1"

    monkeypatch.setattr(kis, "place_market_buy_order", _place)

    if fill is not None:
        unit_price, filled_ratio = fill
        before = (100, 9_000.0)

        def _totals(ticker, day, side):
            if not ordered:  # 주문 직전 조회
                return before
            filled = int(ordered[0] * filled_ratio)
            return (before[0] + filled, before[1] + filled * unit_price)

        monkeypatch.setattr(kis, "fetch_daily_fill_totals", _totals)
        return

    totals = iter(bracket if bracket is not None else [None, None])
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: next(totals))


def _run_buy(tmp_path):
    log_path = tmp_path / "journal.jsonl"
    portfolio = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None),
            PortfolioState(positions=[], cash_weight=1.0), "반도체", 0.08, log_path=log_path,
        )
    )
    return portfolio, json.loads(log_path.read_text().strip())


def test_entry_price_prefers_this_orders_own_fill(monkeypatch, tmp_path):
    """주문 전후 누적 체결 집계의 차 = 이 주문 하나의 체결가. 같은 날 이미 다른
    체결(100주/9,000원)이 있어도 섞이지 않고 이번 주문분(주당 112원)만 잡혀야 한다."""
    _wire_buy_with_prices(
        monkeypatch,
        fill_price=999.0,  # 그날 집계 평균 — 섞인 값이라 쓰면 안 된다
        position_avg=888.0,
        fill=(112.0, 1.0),  # 주문 수량 전부 체결
    )

    portfolio, entry = _run_buy(tmp_path)

    assert portfolio.positions[0].entry_price == pytest.approx(112.0)
    assert entry["entry_price_source"] == "fill"


def test_entry_price_marks_partial_fill_distinctly(monkeypatch, tmp_path):
    """체결 조회가 주문 수량을 다 못 따라잡았으면 "fill"이 아니라 "fill_partial"이다.

    2026-08-28 300720: 488주 주문 중 12초 안에 95주(19.5%)만 잡힌 채 타임아웃됐는데
    매매일지엔 "fill"로 남아, 그 진입가가 전량 평균인지 앞부분 19%짜리인지 일지만
    보고는 알 수 없었다(실제로 전량 평균과 -0.04% 어긋나 있었다). 값은 그대로 쓰되
    — 부분 체결 평균가도 호가보다는 훨씬 낫다 — 근사치라는 사실을 라벨로 남긴다.
    매도 경로(finalize_sell)는 처음부터 이렇게 구분하고 있었다.
    """
    _wire_buy_with_prices(
        monkeypatch,
        fill_price=999.0,
        position_avg=888.0,
        fill=(112.0, 0.195),  # 주문 수량의 19.5%만 잡힘
    )

    portfolio, entry = _run_buy(tmp_path)

    # 폴백으로 넘어가지 않는다 — 부분 체결 평균가가 여전히 최선의 출처다.
    assert portfolio.positions[0].entry_price == pytest.approx(112.0)
    assert entry["entry_price_source"] == "fill_partial"


def test_entry_price_falls_back_to_daily_average_when_bracket_unavailable(monkeypatch, tmp_path):
    _wire_buy_with_prices(monkeypatch, fill_price=112.0, position_avg=999.0)

    portfolio, entry = _run_buy(tmp_path)

    assert portfolio.positions[0].entry_price == 112.0
    assert entry["entry_price_source"] == "daily_avg"


def test_entry_price_falls_back_to_broker_position_average(monkeypatch, tmp_path):
    """체결 직후라 일별체결 집계에 아직 안 잡힌 경우 — 호가(101)가 아니라 브로커가
    보고하는 매입평균가(112)를 써야 한다."""
    _wire_buy_with_prices(monkeypatch, fill_price=None, position_avg=112.0)

    portfolio, entry = _run_buy(tmp_path)

    assert portfolio.positions[0].entry_price == 112.0
    assert entry["entry_price_source"] == "position_avg"


def test_entry_price_last_resort_is_quote_and_is_flagged(monkeypatch, tmp_path, caplog):
    """둘 다 실패하면 호가로 밀되, 손절·익절 기준이 실제 원가와 다를 수 있다는 걸
    로그와 매매일지 양쪽에 남긴다 — 조용히 넘어가면 192820처럼 오익절이 난다."""
    _wire_buy_with_prices(monkeypatch, fill_price=None, position_avg=None)

    with caplog.at_level("ERROR"):
        portfolio, entry = _run_buy(tmp_path)

    assert portfolio.positions[0].entry_price == 101.0  # 호가
    assert entry["entry_price_source"] == "quote_fallback"
    assert "entry_price_unverified" in caplog.text


def test_later_sources_are_not_queried_once_a_price_is_found(monkeypatch, tmp_path):
    """체인이 앞단에서 끝나면 뒤쪽 조회는 아예 안 나가야 한다 — 불필요한 KIS 호출은
    초당 거래건수 제한을 갉아먹는다."""
    _wire_buy_with_prices(
        monkeypatch, fill_price=112.0, position_avg=None, bracket=[(0, 0.0), (20, 2_240_000.0)]
    )
    daily_calls, balance_calls = [], []
    monkeypatch.setattr(kis, "fetch_fill_price", lambda ticker, day: daily_calls.append(ticker))
    monkeypatch.setattr(kis, "fetch_position_avg_price", lambda ticker: balance_calls.append(ticker))

    _run_buy(tmp_path)

    assert daily_calls == []
    assert balance_calls == []


# --- 주문 응답 유실 (2026-08-19) ---


def _raise_response_lost(ticker, qty):
    raise kis.OrderResponseLost("read timeout")


def test_records_position_when_order_response_lost_but_fill_appears(monkeypatch, tmp_path):
    """응답만 유실되고 주문은 살아 있던 경우 — 포지션으로 기록해야 한다.

    여기서 그냥 포기하면 브로커엔 주식이 있는데 우리 상태엔 없어서, 그 보유가
    손절·익절 평가 대상에서 통째로 빠진다.
    """
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 101.0)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", _raise_response_lost)

    totals = iter([(0, 0.0), (10, 1020.0)])
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: next(totals, (10, 1020.0)))

    portfolio = PortfolioState(cash_weight=1.0)
    log_path = tmp_path / "trade_journal.jsonl"
    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08, log_path=log_path
        )
    )

    assert len(result.positions) == 1
    assert result.positions[0].ticker == TICKER
    assert result.positions[0].entry_price == pytest.approx(102.0)  # 1020.0 / 10주

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert entries[0]["event"] == "buy"


def test_skips_when_order_response_lost_and_no_fill(monkeypatch, tmp_path):
    """응답 유실 + 원장에 체결 흔적 없음 = 주문이 안 나갔다고 본다. 재전송하지 않는다."""
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 101.0)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)

    order_calls = {"n": 0}

    def order(ticker, qty):
        order_calls["n"] += 1
        raise kis.OrderResponseLost("read timeout")

    monkeypatch.setattr(kis, "place_market_buy_order", order)
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: (0, 0.0))

    portfolio = PortfolioState()
    log_path = tmp_path / "trade_journal.jsonl"
    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), portfolio, "반도체", 0.08, log_path=log_path
        )
    )

    assert result == portfolio
    assert order_calls["n"] == 1  # 재전송 금지
    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert entries[0]["reason"] == "order_response_lost"


# --- 개장 전 기준가 가드 (2026-09-14, 첫 1개월 평가 P0) ---


def _wire_happy_path(monkeypatch, orders):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "fetch_fill_price", lambda ticker, order_date: 101.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: orders.append(qty) or "ODNO1")
    monkeypatch.setattr(pipeline, "PRE_OPEN_QUOTE_WAIT_S", 0.0)


def test_skips_when_the_quote_never_shows_a_fill_today(monkeypatch, tmp_path):
    """2026-08-12 매수 4건이 전부 gap 0.0·진입가=전일 종가였다. 첫 체결 전에는 현재가
    자리에 기준가가 와서 갭이 항상 0으로 재지고, 체결 조회가 실패하면 그 값이 진입가가
    됐다 — 192820 오익절 캐스케이드(−79만원)의 출발점이다."""
    orders: list[int] = []
    _wire_happy_path(monkeypatch, orders)
    calls = []

    def pre_open(ticker, policy=None):
        calls.append(ticker)
        return kis.Quote(price=100.0, day_high=None, day_low=None, prev_close=100.0)

    monkeypatch.setattr(kis, "fetch_quote", pre_open)
    log_path = tmp_path / "trade_journal.jsonl"

    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), PortfolioState(cash_weight=1.0),
            "반도체", 0.08, log_path=log_path,
        )
    )

    assert result.positions == [] and orders == []
    assert len(calls) == pipeline.PRE_OPEN_QUOTE_ATTEMPTS  # 데이터 수집 재시도는 끝까지 한다(규칙 4)
    entry = json.loads(log_path.read_text().splitlines()[0])
    assert (entry["event"], entry["reason"]) == ("buy_skipped", "pre_open_quote")


def test_waits_for_the_first_fill_and_then_buys_on_the_real_price(monkeypatch, tmp_path):
    orders: list[int] = []
    _wire_happy_path(monkeypatch, orders)
    quotes = iter([
        kis.Quote(price=100.0, day_high=None, day_low=None, prev_close=100.0),  # 09:01:00 기준가
        kis.Quote(price=101.0, day_high=101.5, day_low=100.5, prev_close=100.0),  # 첫 체결 잡힘
    ])
    monkeypatch.setattr(kis, "fetch_quote", lambda ticker, policy=None: next(quotes))

    result = asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), PortfolioState(cash_weight=1.0),
            "반도체", 0.08, log_path=tmp_path / "trade_journal.jsonl",
        )
    )

    assert len(result.positions) == 1
    assert orders == [int((100_000_000 * 0.08) // 101.0)]  # 수량도 기준가가 아니라 실제 가격으로


def test_a_real_gap_is_still_caught_once_the_quote_is_live(monkeypatch, tmp_path):
    """가드가 갭 체크를 대체하지 않는다 — 체결이 잡힌 뒤의 진짜 갭은 그대로 거른다."""
    orders: list[int] = []
    _wire_happy_path(monkeypatch, orders)
    monkeypatch.setattr(
        kis, "fetch_quote",
        lambda ticker, policy=None: kis.Quote(price=110.0, day_high=110.0, day_low=109.0, prev_close=100.0),
    )
    log_path = tmp_path / "trade_journal.jsonl"

    asyncio.run(
        pipeline.execute_buy_order(
            _decision(), GateResult(approved=True, rejected_by=None), PortfolioState(cash_weight=1.0),
            "반도체", 0.08, log_path=log_path,
        )
    )

    assert orders == []
    assert json.loads(log_path.read_text().splitlines()[0])["reason"] == "gap_too_large"


# --- 독립 리뷰 지적 (2026-09-14) ---


def _bars_with_today(prev_close=100.0, today_close=110.0):
    from datetime import timedelta

    from src.schemas import OHLCVBar

    today = pipeline._kst_today()
    bars = [OHLCVBar(date=today - timedelta(days=30 - i), open=prev_close, high=prev_close, low=prev_close, close=prev_close, volume=1) for i in range(29)]
    bars.append(OHLCVBar(date=today, open=today_close, high=today_close, low=today_close, close=today_close, volume=1))
    return bars


def test_the_gap_is_measured_against_the_previous_close_not_todays_forming_bar(monkeypatch, tmp_path):
    """KIS 일봉은 장중에 오늘 봉을 포함한다. 그 봉의 종가를 "전일 종가"로 쓰면 갭이 0으로 눌린다 —
    2026-08-12 192820은 실제 시가 갭 +10.48%에 gap_pct=0.0으로 기록되고 매수됐다."""
    orders = []
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _bars_with_today(100.0, 110.0))
    monkeypatch.setattr(
        kis, "fetch_quote", lambda ticker, policy=None: kis.Quote(price=110.0, day_high=110.0, day_low=109.0, prev_close=None)
    )
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: orders.append(qty) or "o")
    log_path = tmp_path / "journal.jsonl"

    asyncio.run(pipeline.execute_buy_order(_decision(), GateResult(approved=True, rejected_by=None), PortfolioState(cash_weight=1.0), "반도체", 0.08, log_path=log_path))

    assert orders == []
    entry = json.loads(log_path.read_text().splitlines()[0])
    assert entry["reason"] == "gap_too_large" and entry["gap_pct"] == pytest.approx(0.10)


def test_the_brokers_previous_close_is_preferred_when_present(monkeypatch, tmp_path):
    orders = []
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _bars_with_today(104.0, 110.0))
    monkeypatch.setattr(
        kis, "fetch_quote", lambda ticker, policy=None: kis.Quote(price=110.0, day_high=110.0, day_low=109.0, prev_close=100.0)
    )
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: orders.append(qty) or "o")
    log_path = tmp_path / "journal.jsonl"

    asyncio.run(pipeline.execute_buy_order(_decision(), GateResult(approved=True, rejected_by=None), PortfolioState(cash_weight=1.0), "반도체", 0.08, log_path=log_path))

    assert json.loads(log_path.read_text().splitlines()[0])["gap_pct"] == pytest.approx(0.10)


def test_a_corrupt_bar_cannot_crash_the_buy_after_the_order_filled(monkeypatch, tmp_path):
    """독립 리뷰 재현: 종가 0인 봉 하나로 변동성 계산이 ZeroDivisionError를 냈고, 그 계산이 주문 뒤라
    브로커엔 체결된 주식이 상태 파일엔 없는 보유가 생길 수 있었다. 이제 주문 전에, 예외 없이 계산한다."""
    bars = _volatile_bars()
    bars[10] = bars[10].model_copy(update={"close": 0.0})
    _wire_successful_buy(monkeypatch, bars)

    updated = _buy(_decision(), PortfolioState(positions=[], cash_weight=1.0), tmp_path / "journal.jsonl")

    assert len(updated.positions) == 1 and updated.positions[0].exit_plan is None
    assert json.loads((tmp_path / "journal.jsonl").read_text().strip())["exit_plan_source"] == "default"
