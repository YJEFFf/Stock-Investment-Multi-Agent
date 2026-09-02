from datetime import date

import pytest
from pydantic import ValidationError

from src import kis, sell
from src.schemas import ExitPlan, FillRecord, OHLCVBar, PortfolioState, Position, SellAction


@pytest.fixture(autouse=True)
def _no_real_fill_lookup(monkeypatch):
    """execute_sell_order가 주문 전후로 누적 체결 집계를 조회한다 — 목킹 안 하면
    테스트가 실제 KIS를 때린다. 기본값은 None(조회 불가)이라 체결 확인 실패 경로가
    돌고, 체결가 대신 호가로 폴백한다. 체결이 잡히는 경우는 _fill_totals로 따로 건다."""
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: None)


def _fill_totals(monkeypatch, before, after):
    """주문 전/후 누적 체결 집계를 순서대로 돌려주도록 건다."""
    calls = iter([before, after])
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: next(calls))


def _position(**overrides) -> Position:
    defaults = dict(ticker="005930", sector="반도체", weight=0.10, entry_price=100.0, peak_price=100.0)
    defaults.update(overrides)
    return Position(**defaults)


# --- evaluate_deterministic_sell ---


def test_returns_none_without_entry_price():
    position = _position(entry_price=None)
    assert sell.evaluate_deterministic_sell(position, current_price=50.0) is None


def test_stop_loss_triggers_at_threshold():
    position = _position(entry_price=100.0)
    action = sell.evaluate_deterministic_sell(position, current_price=90.0)  # 정확히 -10%

    assert action is not None
    assert action.reason == "stop_loss"
    assert action.sell_fraction == 1.0


def test_stop_loss_triggers_beyond_threshold():
    position = _position(entry_price=100.0)
    action = sell.evaluate_deterministic_sell(position, current_price=70.0)

    assert action.reason == "stop_loss"


def test_no_sell_when_within_normal_range():
    position = _position(entry_price=100.0)
    assert sell.evaluate_deterministic_sell(position, current_price=105.0) is None


def test_take_profit_stage0_triggers_at_threshold():
    position = _position(entry_price=100.0, take_profit_stage=0)
    action = sell.evaluate_deterministic_sell(position, current_price=120.0)  # 정확히 +20%

    assert action is not None
    assert action.reason == "take_profit_trail"
    assert action.sell_fraction == pytest.approx(1 / 3)


def test_take_profit_stage0_below_threshold_does_not_trigger():
    position = _position(entry_price=100.0, take_profit_stage=0)
    assert sell.evaluate_deterministic_sell(position, current_price=115.0) is None


def test_take_profit_after_first_stage_uses_trailing_from_peak_not_entry():
    """1단계 익절 이후엔 진입가가 아니라 고점 대비로 판단해야 한다 — 진입가 대비로
    보면 여전히 훨씬 위라서 트레일링이 절대 발동 안 하는 버그가 생긴다."""
    position = _position(entry_price=100.0, peak_price=150.0, take_profit_stage=1)

    # 진입가(100) 대비로는 +39%지만, 고점(150) 대비로는 -7% 조정 -> 발동해야 함
    action = sell.evaluate_deterministic_sell(position, current_price=139.5)

    assert action is not None
    assert action.reason == "take_profit_trail"


def test_take_profit_after_first_stage_no_trigger_without_enough_pullback():
    position = _position(entry_price=100.0, peak_price=150.0, take_profit_stage=1)
    assert sell.evaluate_deterministic_sell(position, current_price=145.0) is None  # -3.3%뿐


def test_take_profit_after_first_stage_returns_none_when_peak_missing():
    position = _position(entry_price=100.0, peak_price=None, take_profit_stage=1)
    # 손절선(-10%)에 안 걸리는 가격을 써야 고점 누락 분기를 실제로 검증한다.
    assert sell.evaluate_deterministic_sell(position, current_price=95.0) is None


def test_stop_loss_takes_priority_even_after_take_profit_stage():
    """부분 익절을 이미 했어도, 그 이후 진입가 대비 -10% 밑으로 급락하면 손절이
    우선한다 — 트레일링 익절 로직이 손절을 가로막지 않는다."""
    position = _position(entry_price=100.0, peak_price=150.0, take_profit_stage=1)
    action = sell.evaluate_deterministic_sell(position, current_price=85.0)

    assert action.reason == "stop_loss"
    assert action.sell_fraction == 1.0


# --- update_peak_price ---


def test_update_peak_price_raises_peak_on_new_high():
    position = _position(entry_price=100.0, peak_price=100.0)
    updated = sell.update_peak_price(position, current_price=110.0)

    assert updated.peak_price == 110.0


def test_update_peak_price_does_not_lower_peak():
    position = _position(entry_price=100.0, peak_price=120.0)
    updated = sell.update_peak_price(position, current_price=110.0)

    assert updated.peak_price == 120.0


def test_update_peak_price_initializes_from_entry_when_peak_missing():
    position = _position(entry_price=100.0, peak_price=None)
    updated = sell.update_peak_price(position, current_price=90.0)

    assert updated.peak_price == 100.0  # 90은 진입가보다 낮으므로 진입가가 고점


def test_update_peak_price_noop_without_entry_price():
    position = _position(entry_price=None, peak_price=None)
    updated = sell.update_peak_price(position, current_price=999.0)

    assert updated.peak_price is None


# --- execute_sell ---


def test_execute_sell_stop_loss_removes_position_entirely():
    from src.schemas import SellAction

    portfolio = PortfolioState(positions=[_position(weight=0.10)], cash_weight=0.90)
    action = SellAction(ticker="005930", reason="stop_loss", sell_fraction=1.0)

    updated = sell.execute_sell(portfolio, action, current_price=90.0)

    assert updated.positions == []
    assert updated.cash_weight == pytest.approx(1.0)


def test_execute_sell_partial_take_profit_reduces_weight_and_resets_peak():
    from src.schemas import SellAction

    position = _position(weight=0.09, entry_price=100.0, peak_price=150.0, take_profit_stage=1)
    portfolio = PortfolioState(positions=[position], cash_weight=0.91)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)

    updated = sell.execute_sell(portfolio, action, current_price=139.5)

    assert len(updated.positions) == 1
    remaining = updated.positions[0]
    assert remaining.weight == pytest.approx(0.06)
    assert remaining.take_profit_stage == 2
    assert remaining.peak_price == 139.5  # 다음 단계는 여기서부터 새로 고점을 추적
    assert updated.cash_weight == pytest.approx(0.94)


def test_execute_sell_unknown_ticker_returns_portfolio_unchanged():
    from src.schemas import SellAction

    portfolio = PortfolioState(positions=[_position(ticker="005930")], cash_weight=0.90)
    action = SellAction(ticker="999999", reason="stop_loss", sell_fraction=1.0)

    updated = sell.execute_sell(portfolio, action, current_price=100.0)

    assert updated == portfolio


def test_execute_sell_removes_position_when_remaining_weight_negligible():
    from src.schemas import SellAction

    position = _position(weight=1e-10, entry_price=100.0, peak_price=150.0, take_profit_stage=3)
    portfolio = PortfolioState(positions=[position], cash_weight=0.999)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1.0)

    updated = sell.execute_sell(portfolio, action, current_price=139.5)

    assert updated.positions == []


def test_execute_sell_reduces_quantity_proportionally():
    from src.schemas import SellAction

    position = _position(weight=0.09, entry_price=100.0, peak_price=150.0, take_profit_stage=1, quantity=30)
    portfolio = PortfolioState(positions=[position], cash_weight=0.91)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)

    updated = sell.execute_sell(portfolio, action, current_price=139.5)

    assert updated.positions[0].quantity == 20  # 30 - int(30 * 1/3)


# --- execute_sell_simulated / execute_sell_order ---


def test_execute_sell_simulated_matches_execute_sell():
    import asyncio

    from src.schemas import SellAction

    portfolio = PortfolioState(positions=[_position(weight=0.10)], cash_weight=0.90)
    action = SellAction(ticker="005930", reason="stop_loss", sell_fraction=1.0)

    updated, _fill = asyncio.run(sell.execute_sell_simulated(portfolio, action, current_price=90.0))

    assert updated.positions == []
    assert updated.cash_weight == pytest.approx(1.0)


def test_execute_sell_order_falls_back_to_simulated_without_tracked_quantity(monkeypatch):
    import asyncio

    from src.schemas import SellAction

    def fail(*a, **k):
        raise AssertionError("실제 수량을 모르면 KIS 주문을 내면 안 된다")

    monkeypatch.setattr(kis, "place_market_sell_order", fail)

    portfolio = PortfolioState(positions=[_position(weight=0.10, quantity=None)], cash_weight=0.90)
    action = SellAction(ticker="005930", reason="stop_loss", sell_fraction=1.0)

    updated, _fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=90.0))

    assert updated.positions == []  # execute_sell()과 동일한 결과


def test_execute_sell_order_places_real_order_and_updates_quantity(monkeypatch):
    import asyncio

    from src.schemas import SellAction

    captured = {}

    def fake_sell(ticker, quantity):
        captured["ticker"] = ticker
        captured["quantity"] = quantity
        return "ODNO789"

    monkeypatch.setattr(kis, "place_market_sell_order", fake_sell)

    position = _position(weight=0.09, entry_price=100.0, peak_price=150.0, take_profit_stage=1, quantity=30)
    portfolio = PortfolioState(positions=[position], cash_weight=0.91)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)

    updated, _fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=139.5))

    assert captured["ticker"] == "005930"
    assert captured["quantity"] == 10  # int(30 * 1/3)
    assert updated.positions[0].quantity == 20


def test_execute_sell_order_full_exit_sells_all_shares(monkeypatch):
    import asyncio

    from src.schemas import SellAction

    captured = {}
    monkeypatch.setattr(
        kis, "place_market_sell_order", lambda ticker, qty: captured.setdefault("qty", qty) or "ODNO1"
    )

    position = _position(weight=0.10, entry_price=100.0, quantity=30)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    action = SellAction(ticker="005930", reason="stop_loss", sell_fraction=1.0)

    updated, _fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=90.0))

    assert captured["qty"] == 30
    assert updated.positions == []


def test_execute_sell_order_liquidates_dust_when_fraction_rounds_to_zero(monkeypatch):
    """부분 익절이 0주로 내림되면 남은 걸 전량 판다.

    예전엔 여기서 그냥 돌아갔는데, 그러면 이 포지션은 영원히 청산되지 않는다 —
    트리거는 계속 걸리지만 매번 0주고, 비중도 1/3씩만 줄어드니 제거 조건(잔여 ≈ 0)에
    닿지 않는다. 192820이 실제로 4단계까지 내려와 8주가 됐고, 2주에서 굳을 참이었다
    (2026-08-20).
    """
    import asyncio

    from src.schemas import SellAction

    captured = {}

    def fake_order(ticker, qty):
        captured["qty"] = qty
        return "ORDER-1"

    monkeypatch.setattr(kis, "place_market_sell_order", fake_order)

    position = _position(weight=0.09, entry_price=100.0, peak_price=150.0, take_profit_stage=1, quantity=2)
    portfolio = PortfolioState(positions=[position], cash_weight=0.91)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)  # int(2/3) = 0

    updated, _fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=139.5))

    assert captured["qty"] == 2  # 남은 전량
    assert updated.positions == []  # 포지션이 실제로 닫힌다
    assert updated.cash_weight == pytest.approx(1.0)


def test_execute_sell_order_liquidates_single_share_position(monkeypatch):
    """자투리의 종점 — 1주 남은 포지션에 부분 익절이 걸려도 닫힌다."""
    import asyncio

    from src.schemas import SellAction

    captured = {}
    monkeypatch.setattr(
        kis, "place_market_sell_order", lambda ticker, qty: captured.setdefault("qty", qty) and "ORDER-1"
    )

    position = _position(weight=0.01, entry_price=100.0, peak_price=150.0, take_profit_stage=3, quantity=1)
    portfolio = PortfolioState(positions=[position], cash_weight=0.99)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)

    updated, _fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=139.5))

    assert captured["qty"] == 1
    assert updated.positions == []


def test_execute_sell_order_returns_unchanged_when_order_rejected(monkeypatch):
    import asyncio

    from src.schemas import SellAction

    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: None)

    position = _position(weight=0.10, entry_price=100.0, quantity=30)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    action = SellAction(ticker="005930", reason="stop_loss", sell_fraction=1.0)

    updated, _fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=90.0))

    assert updated == portfolio


# --- ExitPlan: 진입 시 확정된 종목별 출구 규칙 (사용자 확정 2026-08-15) ---


def _plan(**overrides) -> ExitPlan:
    defaults = dict(stop_loss_pct=-0.06, take_profit_pct=0.12, take_profit_fraction=0.25, trail_pct=-0.04)
    defaults.update(overrides)
    return ExitPlan(**defaults)


def test_plan_for_falls_back_to_default_when_position_has_none():
    """이 기능 이전에 열린 포지션은 exit_plan이 없다 — 고정 기본값으로 계속 돌아야 한다."""
    assert sell.plan_for(_position(exit_plan=None)) == sell.DEFAULT_EXIT_PLAN


def test_default_plan_matches_the_original_fixed_constants():
    assert sell.DEFAULT_EXIT_PLAN.stop_loss_pct == -0.10
    assert sell.DEFAULT_EXIT_PLAN.take_profit_pct == 0.20
    assert sell.DEFAULT_EXIT_PLAN.take_profit_fraction == pytest.approx(1 / 3)
    assert sell.DEFAULT_EXIT_PLAN.trail_pct == -0.07


def test_position_plan_overrides_default_stop_loss():
    """-6% 손절 계획이면 고정값 -10%에 닿기 전에 잘려야 한다."""
    position = _position(entry_price=100.0, exit_plan=_plan(stop_loss_pct=-0.06))

    assert sell.evaluate_deterministic_sell(position, current_price=95.0) is None
    action = sell.evaluate_deterministic_sell(position, current_price=94.0)
    assert action.reason == "stop_loss"
    assert action.sell_fraction == 1.0


def test_position_plan_can_be_looser_than_default():
    """양방향 허용(사용자 확정) — 고정값보다 느슨한 손절선도 그대로 집행된다."""
    position = _position(entry_price=100.0, exit_plan=_plan(stop_loss_pct=-0.13, take_profit_pct=0.26))

    assert sell.evaluate_deterministic_sell(position, current_price=89.0) is None  # -11%, 고정값이면 잘렸을 것
    assert sell.evaluate_deterministic_sell(position, current_price=87.0).reason == "stop_loss"


def test_position_plan_overrides_take_profit_trigger_and_fraction():
    position = _position(entry_price=100.0, exit_plan=_plan(take_profit_pct=0.12, take_profit_fraction=0.25))

    assert sell.evaluate_deterministic_sell(position, current_price=111.0) is None
    action = sell.evaluate_deterministic_sell(position, current_price=112.0)
    assert action.reason == "take_profit_trail"
    assert action.sell_fraction == 0.25


def test_position_plan_overrides_trailing_stop():
    position = _position(
        entry_price=100.0, peak_price=150.0, take_profit_stage=1, exit_plan=_plan(trail_pct=-0.04)
    )

    assert sell.evaluate_deterministic_sell(position, current_price=145.0) is None  # -3.3%
    action = sell.evaluate_deterministic_sell(position, current_price=144.0)  # -4%
    assert action.reason == "take_profit_trail"


def test_exit_plan_survives_partial_take_profit():
    """부분 익절 후에도 계획은 포지션에 그대로 남아야 한다 — 재결정 경로가 없기 때문에
    여기서 계획이 날아가면 남은 잔량이 조용히 고정 기본값으로 넘어가버린다."""
    plan = _plan()
    position = _position(weight=0.12, entry_price=100.0, exit_plan=plan)
    portfolio = PortfolioState(positions=[position], cash_weight=0.88)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=0.25)

    updated = sell.execute_sell(portfolio, action, current_price=112.0)

    assert updated.positions[0].exit_plan == plan


def test_exit_plan_rejects_out_of_range_values():
    """바운드는 스키마가 강제한다 — 손절선이 사라지는 방향으로 조용히 통과하면 안 된다."""
    with pytest.raises(ValidationError):
        _plan(stop_loss_pct=-0.40)
    with pytest.raises(ValidationError):
        _plan(take_profit_fraction=1.0)  # 전량이면 (1-f)^n 잔량 보존이 깨진다
    with pytest.raises(ValidationError):
        _plan(trail_pct=-0.50)


# --- 주식수 내림과 비중 정합성 ---


def test_execute_sell_order_reduces_weight_by_actual_shares_sold(monkeypatch):
    """4주 중 1/3 익절이면 실제로 나가는 건 1주(25%)다. 비중도 25%만 줄어야 매매일지의
    '줄인 비중'이 실제와 맞는다 — 요청값 33.3%로 깎으면 장부가 실제보다 작아진다."""
    import asyncio

    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-1")

    position = _position(weight=0.12, entry_price=100.0, quantity=4)
    portfolio = PortfolioState(positions=[position], cash_weight=0.88)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)

    updated, _fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=120.0))

    assert updated.positions[0].quantity == 3
    assert updated.positions[0].weight == pytest.approx(0.09)  # 0.12 * 0.75
    assert updated.cash_weight == pytest.approx(0.91)


def test_execute_sell_order_survives_float_rounding_on_three_shares(monkeypatch):
    """3주 중 1주: int(3 * (1/3))은 부동소수 오차로 0이 된다. 주식수는 재계산이 아니라
    실제 체결 수량으로 덮어써야 한다."""
    import asyncio

    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-1")

    position = _position(weight=0.12, entry_price=100.0, quantity=3)
    portfolio = PortfolioState(positions=[position], cash_weight=0.88)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)

    updated, _fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=120.0))

    assert updated.positions[0].quantity == 2


# --- 실제 체결 내역 반환 (사용자 확정 2026-08-15) ---


def test_execute_sell_order_returns_actual_fill_not_quote(monkeypatch):
    """시장가라 판단 시점 호가(120)와 체결가(118)는 다르다. 매매일지에 적혀야 하는
    건 체결가이므로 execute_sell_order가 그 사실을 돌려줘야 한다."""
    import asyncio

    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-1")
    _fill_totals(monkeypatch, before=(0, 0.0), after=(10, 1180.0))

    position = _position(weight=0.12, entry_price=100.0, quantity=30)
    portfolio = PortfolioState(positions=[position], cash_weight=0.88)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)

    _updated, fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=120.0))

    assert fill == FillRecord(quantity=10, amount=1180.0)
    assert fill.price == pytest.approx(118.0)


def test_execute_sell_order_fill_excludes_earlier_same_day_sells(monkeypatch):
    """같은 날 이미 12주를 팔았어도 이번 8주분만 잡혀야 한다 — 집계를 사후에 한 번만
    조회하면 두 건이 섞인다(192820이 실제로 그렇게 20주/4,946,000원으로 합산됐다)."""
    import asyncio

    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-2")
    _fill_totals(monkeypatch, before=(12, 3_024_000.0), after=(20, 4_946_000.0))

    position = _position(weight=0.0533, entry_price=210_000.0, quantity=26, take_profit_stage=1,
                         peak_price=258_000.0)
    portfolio = PortfolioState(positions=[position], cash_weight=0.9467)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)

    _updated, fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=240_500.0))

    assert fill.quantity == 8
    assert fill.amount == pytest.approx(1_922_000.0)


def test_execute_sell_order_peak_resets_to_fill_price_not_quote(monkeypatch):
    """다음 트레일링 구간의 기준 고점도 체결가로 잡혀야 한다 — 호가로 잡으면
    이후 모든 단계가 실제와 어긋난 기준으로 판정된다."""
    import asyncio

    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-1")
    _fill_totals(monkeypatch, before=(0, 0.0), after=(10, 1180.0))

    position = _position(weight=0.12, entry_price=100.0, quantity=30, peak_price=150.0)
    portfolio = PortfolioState(positions=[position], cash_weight=0.88)
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)

    updated, _fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=120.0))

    assert updated.positions[0].peak_price == pytest.approx(118.0)


def test_execute_sell_order_returns_no_fill_when_totals_unavailable(monkeypatch, caplog):
    """체결 확인이 안 되면 체결을 지어내지 않고 None을 돌려준다 — 호출부가 호가로
    폴백하되 그 사실을 일지에 남길 수 있어야 한다."""
    import asyncio

    monkeypatch.setattr(kis, "place_market_sell_order", lambda ticker, qty: "order-1")
    # autouse 픽스처가 이미 조회 불가(None)로 걸어둔 상태다.

    position = _position(weight=0.12, entry_price=100.0, quantity=30)
    portfolio = PortfolioState(positions=[position], cash_weight=0.88)
    action = SellAction(ticker="005930", reason="stop_loss", sell_fraction=1.0)

    with caplog.at_level("WARNING"):
        _updated, fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=90.0))

    assert fill is None
    assert "fill_unverified" in caplog.text


def test_execute_sell_simulated_never_reports_a_fill():
    """실주문을 낸 적이 없으니 체결이라는 사실 자체가 없다."""
    import asyncio

    portfolio = PortfolioState(positions=[_position(weight=0.10)], cash_weight=0.90)
    action = SellAction(ticker="005930", reason="stop_loss", sell_fraction=1.0)

    _updated, fill = asyncio.run(sell.execute_sell_simulated(portfolio, action, current_price=90.0))

    assert fill is None


# --- 매도 주문 응답 유실 (2026-08-19) ---


def test_execute_sell_order_records_sell_when_response_lost_but_fill_appears(monkeypatch):
    """응답만 유실되고 매도는 체결된 경우 — 판 것으로 기록해야 한다.

    안 팔린 걸로 두면 실제로는 없는 주식을 계속 보유로 들고 있게 되고,
    다음 손절 평가가 존재하지 않는 포지션을 상대로 돈다.
    """
    import asyncio

    from src.schemas import SellAction

    order_calls = {"n": 0}

    def lost(ticker, qty):
        order_calls["n"] += 1
        raise kis.OrderResponseLost("read timeout")

    monkeypatch.setattr(kis, "place_market_sell_order", lost)
    totals = iter([(0, 0.0), (30, 2700.0)])
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: next(totals, (30, 2700.0)))

    position = _position(weight=0.10, entry_price=100.0, quantity=30)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    action = SellAction(ticker="005930", reason="stop_loss", sell_fraction=1.0)

    updated, fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=90.0))

    assert order_calls["n"] == 1  # 재전송 금지
    assert updated.positions == []
    assert fill is not None and fill.quantity == 30


def test_execute_sell_order_keeps_position_when_response_lost_and_no_fill(monkeypatch):
    """응답 유실 + 체결 흔적 없음 = 주문이 안 나갔다. 포지션을 그대로 둔다."""
    import asyncio

    from src.schemas import SellAction

    monkeypatch.setattr(kis, "place_market_sell_order", lambda t, q: (_ for _ in ()).throw(
        kis.OrderResponseLost("read timeout")
    ))
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", lambda ticker, day, side: (0, 0.0))

    position = _position(weight=0.10, entry_price=100.0, quantity=30)
    portfolio = PortfolioState(positions=[position], cash_weight=0.90)
    action = SellAction(ticker="005930", reason="stop_loss", sell_fraction=1.0)

    updated, fill = asyncio.run(sell.execute_sell_order(portfolio, action, current_price=90.0))

    assert updated == portfolio
    assert fill is None


# --- 시세 공백 사후 판정 (일봉 대조, 2026-08-27) ---


def _bar(low: float, high: float) -> OHLCVBar:
    return OHLCVBar(date=date(2026, 8, 26), open=high, high=high, low=low, close=low, volume=1)


def test_bar_audit_reports_nothing_when_the_day_stayed_inside_the_thresholds():
    position = _position(entry_price=100.0, peak_price=100.0)
    assert sell.threshold_crossed_in_bar(position, _bar(low=95.0, high=115.0)) is None


def test_bar_audit_catches_a_stop_loss_the_blind_window_could_have_hidden():
    """이 판정이 존재하는 이유 — 2026-08-26에 77분간 시세를 하나도 못 받았고,
    그 구간에 문턱을 지났는지를 사람이 일봉으로 확인해야 했다."""
    position = _position(entry_price=100.0, peak_price=100.0)
    assert sell.threshold_crossed_in_bar(position, _bar(low=89.0, high=101.0)) == "stop_loss"


def test_bar_audit_catches_a_take_profit_trigger():
    position = _position(entry_price=100.0, peak_price=100.0)
    assert sell.threshold_crossed_in_bar(position, _bar(low=99.0, high=121.0)) == "take_profit"


def test_bar_audit_no_longer_claims_to_see_trailing(): 
    """2026-09-02 뒤집음. 옛 판정은 저가 185가 고점 200 대비 -7.5%라며
    "take_profit_trail"을 돌려줬다 — 일봉은 저가와 고점의 시간 순서를 안 주는데도.
    안전장치가 구간으로 트레일링을 안 보게 됐으므로 사후 판정도 안 본다. 둘이
    어긋나면 사후 판정이 실제로는 나가지 않았을 매도를 "놓쳤다"고 보고한다."""
    position = _position(entry_price=100.0, peak_price=200.0, take_profit_stage=1)

    assert sell.threshold_crossed_in_bar(position, _bar(low=185.0, high=200.0)) is None
    # 손절은 진입가 기준이라 순서와 무관 — 그대로 잡힌다.
    assert sell.threshold_crossed_in_bar(position, _bar(low=89.0, high=200.0)) == "stop_loss"


def test_bar_audit_prefers_stop_loss_when_both_ends_crossed():
    """손절 우선 — evaluate_deterministic_sell과 같은 순서."""
    position = _position(entry_price=100.0, peak_price=100.0)
    assert sell.threshold_crossed_in_bar(position, _bar(low=85.0, high=125.0)) == "stop_loss"


def test_bar_audit_declines_to_judge_a_position_without_an_entry_price():
    """진입가가 없으면 판정 자체를 안 한다 — "안 닿았다"로 답하면 확인하지 않은 것을
    안전하다고 말하는 게 된다(AnalystOpinion | None과 같은 구분)."""
    position = _position(entry_price=None, peak_price=None)
    assert sell.threshold_crossed_in_bar(position, _bar(low=1.0, high=999.0)) is None


def test_bar_audit_respects_the_frozen_exit_plan_not_the_default():
    plan = ExitPlan(stop_loss_pct=-0.05, take_profit_pct=0.10, take_profit_fraction=0.3, trail_pct=-0.04)
    position = _position(entry_price=100.0, peak_price=100.0, exit_plan=plan)

    # 기본값(-10%)이었다면 안 걸릴 저가인데, 이 포지션의 문턱은 -5%다.
    assert sell.threshold_crossed_in_bar(position, _bar(low=94.0, high=101.0)) == "stop_loss"


# --- 당일 저가/고가로 놓친 문턱 되짚기 (2026-08-27) ---


TODAY = date(2026, 8, 27)


def _quote(price, low=None, high=None):
    return kis.Quote(price=price, day_low=low, day_high=high)


def test_day_range_no_longer_judges_trailing():
    """2026-09-02 뒤집음. 8/27 192820 시나리오(저가 271,000이 트레일 라인 275,745를
    지났다)는 예전엔 구간 판정으로 팔았지만 이제 안 판다 — 저가가 고점보다 먼저
    찍혔는지를 알 수 없기 때문이다(_threshold_crossed docstring)."""
    position = _position(entry_price=232000.0, peak_price=296500.0, take_profit_stage=4)

    assert sell.evaluate_with_day_range(
        position, _quote(278000.0, low=271000.0, high=280000.0), today=TODAY
    ) == (None, False)


def test_the_rally_that_sold_a_position_on_2026_09_02():
    """실제로 나간 오발동. 192820의 당일 저가 271,000은 09:00에, 고가 292,000은
    13:28에 찍혔다. 저가가 4시간 반 먼저였는데도 13:29에 "고점 대비 -7.19%"로 팔렸다
    — 가격이 올라가면서 이미 지나간 저가가 트레일 라인 아래로 들어간 것이다.
    이제는 안 판다."""
    position = _position(entry_price=232000.0, peak_price=292000.0, take_profit_stage=6)

    assert sell.evaluate_with_day_range(
        position, _quote(291500.0, low=271000.0, high=292000.0), today=TODAY
    ) == (None, False)


def test_day_range_trigger_executes_at_the_current_price_not_the_low():
    """꼬리 바닥에 파는 게 아니라 "닿았으니 지금 정리"다 — SellAction은 가격을 담지
    않고, 집행부가 넘겨받은 현재가로 판다(pipeline.finalize_sell). 트레일링이 빠진
    뒤에도 이 규약은 손절·첫 익절에 그대로 남는다."""
    position = _position(entry_price=100.0, peak_price=100.0)

    action, from_range = sell.evaluate_with_day_range(position, _quote(95.0, low=88.0), today=TODAY)

    assert from_range is True
    assert action.reason == "stop_loss" and action.sell_fraction == 1.0


def test_a_point_trigger_is_not_reported_as_a_range_trigger():
    """지금 가격이 이미 문턱 아래면 예전 경로 그대로다 — 하루 1회 제한이 붙으면 안 된다."""
    position = _position(entry_price=100.0, peak_price=100.0)

    action, from_range = sell.evaluate_with_day_range(position, _quote(85.0, low=85.0), today=TODAY)

    assert from_range is False
    assert action.reason == "stop_loss"


def test_the_once_a_day_range_guard_is_gone_field_and_all():
    """하루 1회 제한(range_trigger_day)은 구간 트레일링 때문에 있었다. 트레일링이
    빠지면서 걸릴 일이 없어졌으므로 필드째 걷어냈다 — 값만 남겨두면 이 경로가
    실제보다 촘촘해 보인다(sector_concentration_limit 폐기 때와 같은 처리)."""
    assert "range_trigger_day" not in Position.model_fields


def test_range_stop_loss_repeats_until_the_position_is_gone():
    """손절은 전량 매도라 포지션이 사라진다 — 반복될 수가 없어서 제한이 필요 없다."""
    position = _position(entry_price=100.0, peak_price=100.0)

    action, from_range = sell.evaluate_with_day_range(position, _quote(95.0, low=88.0), today=TODAY)

    assert from_range is True
    assert action.reason == "stop_loss"


def test_no_day_range_means_the_old_point_only_behaviour():
    position = _position(entry_price=232000.0, peak_price=296500.0, take_profit_stage=4)

    assert sell.evaluate_with_day_range(position, _quote(278000.0), today=TODAY) == (None, False)


# --- 트레일링 매도 후의 고점 리셋 (2026-09-02) ---


def test_the_peak_reset_survives_the_days_high():
    """리셋의 요점은 "다음 구간을 새 고점부터 추적한다"인데, day_high >= 현재가는
    항상 참이라 update_peak_price가 다음 회차에 반드시 되돌렸다. 그래서 리셋한
    날에는 day_high를 안 본다."""
    portfolio = PortfolioState(
        cash_weight=0.5,
        positions=[_position(entry_price=232000.0, peak_price=292000.0, take_profit_stage=5, quantity=9)],
    )
    action = SellAction(ticker="005930", reason="take_profit_trail", sell_fraction=1 / 3)

    after = sell.execute_sell(portfolio, action, current_price=271000.0, today=TODAY)
    position = after.positions[0]
    assert position.peak_price == 271000.0 and position.peak_reset_day == TODAY

    # 같은 날 그날의 고가가 다시 들어와도 리셋은 유지된다.
    assert sell.update_peak_price(position, 271000.0, day_high=292000.0, today=TODAY) is position


def test_the_peak_reset_expires_the_next_day():
    """리셋은 그날 한정이다 — 다음 날의 고가는 리셋 이후 구간이라 정상적으로 잡아야
    한다. 안 그러면 고점이 영원히 리셋 값에 묶여 트레일 라인이 안 올라간다."""
    position = _position(
        entry_price=232000.0, peak_price=271000.0, take_profit_stage=6, peak_reset_day=TODAY
    )

    tomorrow = date(2026, 8, 28)
    updated = sell.update_peak_price(position, 285000.0, day_high=292000.0, today=tomorrow)

    assert updated.peak_price == 292000.0


def test_one_intraday_reversal_does_not_liquidate_the_whole_position():
    """리셋이 매분 무효화되던 시절엔 -7% 반락 한 번에 9주가 5분 만에 전량 나갔다
    (2026-09-02 발견). 설계 의도는 트리거당 1/3이다."""
    portfolio = PortfolioState(
        cash_weight=0.5,
        positions=[_position(entry_price=232000.0, peak_price=292000.0, take_profit_stage=5, quantity=9)],
    )
    price, day_high = 271000.0, 292000.0  # 고가 292,000에서 반락한 뒤 그대로 횡보

    sells = 0
    for _ in range(5):
        position = portfolio.positions[0]
        position = sell.update_peak_price(position, price, day_high=day_high, today=TODAY)
        portfolio = PortfolioState(positions=[position], cash_weight=portfolio.cash_weight)
        action = sell.evaluate_deterministic_sell(position, price)
        if action is None:
            continue
        sells += 1
        portfolio = sell.execute_sell(portfolio, action, price, today=TODAY)

    assert sells == 1
    assert portfolio.positions[0].quantity == 6


def test_peak_uses_the_days_high_not_just_the_sample():
    """2026-08-26에 192820 실제 고가는 297,000인데 기록된 고점은 296,500이었다 —
    고점이 낮게 남으면 트레일 라인도 낮아져 매도가 늦어진다."""
    position = _position(entry_price=232000.0, peak_price=296500.0)

    updated = sell.update_peak_price(position, 283000.0, day_high=297000.0)

    assert updated.peak_price == 297000.0


def test_entry_day_positions_are_exempt_from_range_evaluation():
    """당일 저가/고가는 우리가 사기 *전* 구간을 포함한다. 오늘 고가 근처에서 산 종목이
    오늘 아침 저가만으로 손절되면, 보유한 적도 없는 구간을 근거로 청산하는 것이다."""
    position = _position(entry_day=TODAY, entry_price=300000.0, peak_price=300000.0)

    action, from_range = sell.evaluate_with_day_range(
        position, _quote(295000.0, low=265000.0, high=302000.0), today=TODAY
    )

    assert action is None and from_range is False


def test_a_position_held_since_yesterday_is_evaluated_on_the_range():
    position = _position(entry_day=date(2026, 8, 26), entry_price=300000.0, peak_price=300000.0)

    action, from_range = sell.evaluate_with_day_range(
        position, _quote(295000.0, low=265000.0, high=302000.0), today=TODAY
    )

    assert from_range is True
    assert action.reason == "stop_loss"


def test_entry_day_high_does_not_raise_the_peak():
    """진입 전 고가를 고점으로 잡으면 트레일 라인이 보유한 적 없는 가격 위에 선다."""
    position = _position(entry_day=TODAY, entry_price=280000.0, peak_price=280000.0)

    updated = sell.update_peak_price(position, 281000.0, day_high=297000.0, today=TODAY)

    assert updated.peak_price == 281000.0
