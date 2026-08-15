"""브로커 잔고 대조·교정 로직. 순수 함수라 실제 계좌 없이 전부 검증된다."""

import pytest

from src.reconcile import reconcile
from src.schemas import PortfolioState, Position


def _position(**overrides) -> Position:
    defaults = dict(
        ticker="192820", sector="화장품", weight=0.0237,
        entry_price=210_000.0, peak_price=250_500.0, quantity=12, take_profit_stage=3,
    )
    defaults.update(overrides)
    return Position(**defaults)


def _portfolio(*positions) -> PortfolioState:
    return PortfolioState(positions=list(positions), cash_weight=0.66)


def test_corrects_entry_price_to_broker_average():
    """실제로 겪은 케이스 — 호가 210,000이 기록됐지만 체결은 232,000이었다."""
    portfolio = _portfolio(_position())

    corrected, drifts = reconcile(portfolio, {"192820": (12, 232_000.0)})

    assert corrected.positions[0].entry_price == 232_000.0
    entry_drift = next(d for d in drifts if d.field == "entry_price")
    assert entry_drift.local == 210_000.0
    assert entry_drift.broker == 232_000.0
    assert entry_drift.relative_diff == pytest.approx(0.10476, rel=1e-3)
    assert entry_drift.corrected is True


def test_no_drift_when_values_already_match():
    portfolio = _portfolio(_position(entry_price=232_000.0))

    corrected, drifts = reconcile(portfolio, {"192820": (12, 232_000.0)})

    assert drifts == []
    assert corrected == portfolio


def test_float_representation_noise_is_not_treated_as_drift():
    """003550의 실제 값 — 113976.8116(기록) vs 113976.811(브로커)은 같은 값이다."""
    portfolio = _portfolio(_position(ticker="003550", entry_price=113_976.8116, peak_price=122_000.0))

    _corrected, drifts = reconcile(portfolio, {"003550": (12, 113_976.811)})

    assert drifts == []


def test_corrects_quantity_to_broker():
    portfolio = _portfolio(_position(quantity=18))

    corrected, drifts = reconcile(portfolio, {"192820": (12, 210_000.0)})

    assert corrected.positions[0].quantity == 12
    qty_drift = next(d for d in drifts if d.field == "quantity")
    assert (qty_drift.local, qty_drift.broker) == (18.0, 12.0)


def test_peak_price_is_raised_when_corrected_entry_exceeds_it():
    """진입가 교정이 고점을 추월하면 고점도 같이 올린다 — 진입가보다 낮은 고점은
    성립하지 않고, 그대로 두면 트레일링이 즉시 발동해버린다."""
    portfolio = _portfolio(_position(entry_price=210_000.0, peak_price=215_000.0))

    corrected, _drifts = reconcile(portfolio, {"192820": (12, 232_000.0)})

    assert corrected.positions[0].peak_price == 232_000.0


def test_peak_price_untouched_when_still_above_corrected_entry():
    portfolio = _portfolio(_position(entry_price=210_000.0, peak_price=250_500.0))

    corrected, _drifts = reconcile(portfolio, {"192820": (12, 232_000.0)})

    assert corrected.positions[0].peak_price == 250_500.0


def test_weight_and_cash_are_never_touched():
    """비중은 브로커에 없는 개념이라 교정 대상이 아니다 — 조용히 바꾸면 안 된다."""
    portfolio = _portfolio(_position(weight=0.0237))

    corrected, _drifts = reconcile(portfolio, {"192820": (12, 232_000.0)})

    assert corrected.positions[0].weight == 0.0237
    assert corrected.cash_weight == 0.66


def test_position_missing_at_broker_is_reported_but_not_removed():
    """조회가 부분 실패했을 수도 있고, 포지션을 자동으로 없애면 손절 대상이 조용히
    사라진다. 보고만 하고 사람이 판단하게 둔다."""
    portfolio = _portfolio(_position())

    corrected, drifts = reconcile(portfolio, {})

    assert len(corrected.positions) == 1
    drift = next(d for d in drifts if d.field == "missing_at_broker")
    assert drift.ticker == "192820"
    assert drift.corrected is False


def test_holding_missing_locally_is_reported():
    """브로커는 들고 있는데 상태 파일에 없는 경우 — 주문은 나갔는데 상태 저장이
    실패한 흔적일 수 있어 반드시 드러나야 한다."""
    portfolio = _portfolio(_position())

    _corrected, drifts = reconcile(portfolio, {"192820": (12, 210_000.0), "005930": (10, 70_000.0)})

    drift = next(d for d in drifts if d.field == "missing_locally")
    assert drift.ticker == "005930"
    assert drift.broker == 10.0
    assert drift.corrected is False


def test_entry_price_none_is_treated_as_drift_and_filled():
    """진입가가 없는 포지션은 매도 평가 자체가 스킵된다 — 브로커 값으로 채워주면
    리스크 관리 대상으로 복귀한다."""
    portfolio = _portfolio(_position(entry_price=None, peak_price=None))

    corrected, drifts = reconcile(portfolio, {"192820": (12, 232_000.0)})

    assert corrected.positions[0].entry_price == 232_000.0
    assert corrected.positions[0].peak_price == 232_000.0
    assert any(d.field == "entry_price" for d in drifts)


def test_reconcile_is_idempotent():
    portfolio = _portfolio(_position())
    holdings = {"192820": (12, 232_000.0)}

    once, _ = reconcile(portfolio, holdings)
    twice, drifts = reconcile(once, holdings)

    assert twice == once
    assert drifts == []
