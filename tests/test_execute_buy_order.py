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


def test_adds_to_existing_position_with_weighted_average_entry_price(monkeypatch, tmp_path):
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
    # 가중평균: (100*0.08 + 200*0.08) / 0.16 = 150
    assert pos.entry_price == pytest.approx(150.0)
    assert pos.peak_price == 200.0  # 새 체결가가 기존 고점보다 높음
    assert pos.entry_day == date(2026, 1, 5)  # 추가매수해도 최초 진입일 유지


# --- ExitPlan이 진입 시점에 포지션에 박히는가 (사용자 확정 2026-08-15) ---


def _plan(stop_loss_pct=-0.06) -> ExitPlan:
    return ExitPlan(
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=abs(stop_loss_pct) * 2,
        take_profit_fraction=0.25,
        trail_pct=-0.04,
    )


def _wire_successful_buy(monkeypatch):
    monkeypatch.setattr(kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: _prev_bars(100.0))
    monkeypatch.setattr(kis, "fetch_current_price", lambda ticker, policy=None: 101.0)
    monkeypatch.setattr(kis, "fetch_account_balance", lambda: 100_000_000.0)
    monkeypatch.setattr(kis, "place_market_buy_order", lambda ticker, qty: "order-1")
    monkeypatch.setattr(kis, "fetch_fill_price", lambda ticker, day: 101.0)
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: None)


def test_new_position_carries_the_decisions_exit_plan(monkeypatch, tmp_path):
    _wire_successful_buy(monkeypatch)
    plan = _plan()
    decision = _decision()
    decision.exit_plan = plan
    portfolio = PortfolioState(positions=[], cash_weight=1.0)

    updated = asyncio.run(
        pipeline.execute_buy_order(
            decision,
            GateResult(approved=True, rejected_by=None),
            portfolio,
            "반도체",
            0.08,
            log_path=tmp_path / "journal.jsonl",
        )
    )

    assert updated.positions[0].exit_plan == plan


def test_new_position_without_exit_plan_stays_none(monkeypatch, tmp_path):
    """degraded 판단은 exit_plan이 None이고, sell.plan_for가 고정 기본값으로 떨어뜨린다."""
    _wire_successful_buy(monkeypatch)
    portfolio = PortfolioState(positions=[], cash_weight=1.0)

    updated = asyncio.run(
        pipeline.execute_buy_order(
            _decision(),
            GateResult(approved=True, rejected_by=None),
            portfolio,
            "반도체",
            0.08,
            log_path=tmp_path / "journal.jsonl",
        )
    )

    assert updated.positions[0].exit_plan is None


def test_adding_to_existing_position_keeps_the_original_exit_plan(monkeypatch, tmp_path):
    """물타기하면서 손절선도 같이 넓히는 경로를 열지 않는다 — 기존 규칙이 이긴다."""
    _wire_successful_buy(monkeypatch)
    original = _plan(stop_loss_pct=-0.05)
    existing = Position(
        ticker=TICKER, sector="반도체", weight=0.05, entry_price=90.0, peak_price=90.0, quantity=10,
        exit_plan=original,
    )
    decision = _decision()
    decision.exit_plan = _plan(stop_loss_pct=-0.14)  # 훨씬 느슨한 새 계획
    portfolio = PortfolioState(positions=[existing], cash_weight=0.95)

    updated = asyncio.run(
        pipeline.execute_buy_order(
            decision,
            GateResult(approved=True, rejected_by=None),
            portfolio,
            "반도체",
            0.08,
            log_path=tmp_path / "journal.jsonl",
        )
    )

    assert updated.positions[0].exit_plan == original


def test_buy_journal_records_the_exit_plan(monkeypatch, tmp_path):
    _wire_successful_buy(monkeypatch)
    log_path = tmp_path / "journal.jsonl"
    decision = _decision()
    decision.exit_plan = _plan()

    asyncio.run(
        pipeline.execute_buy_order(
            decision, GateResult(approved=True, rejected_by=None),
            PortfolioState(positions=[], cash_weight=1.0), "반도체", 0.08, log_path=log_path,
        )
    )

    entry = json.loads(log_path.read_text().strip())
    assert entry["exit_plan"]["stop_loss_pct"] == pytest.approx(-0.06)
    assert entry["exit_plan"]["take_profit_pct"] == pytest.approx(0.12)


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
