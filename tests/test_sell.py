import pytest

from src import sell
from src.schemas import PortfolioState, Position


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
