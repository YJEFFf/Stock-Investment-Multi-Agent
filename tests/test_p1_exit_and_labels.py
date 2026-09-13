"""2026-09-14 P1: 변동성 출구 규칙(P1-8)과 사람이 읽는 매도 사유(P1-10)."""

import pytest

from src import notion_sync, sell
from src.schemas import ExitPlan, Position, SellAction


# --- volatility_exit_plan ---


@pytest.mark.parametrize(
    "sigma_pct, stop, trail",
    [
        (1.5, 0.083853, 0.067082),  # 2.5·1.5%·√5 / 2.0·1.5%·√5 — 범위 안
        (3.0, 0.15, 0.12),  # 상한에서 잘린다
        (0.5, 0.03, 0.03),  # 하한에서 잘린다
    ],
)
def test_volatility_plan_follows_the_decided_formula_and_bounds(sigma_pct, stop, trail):
    plan = sell.volatility_exit_plan(sigma_pct)

    assert plan.stop_loss_pct == pytest.approx(-stop, abs=2e-6)
    assert plan.take_profit_pct == pytest.approx(2 * stop, abs=4e-6)  # 2:1 고정
    assert plan.trail_pct == pytest.approx(-trail, abs=2e-6)
    assert plan.take_profit_fraction == pytest.approx(1 / 3)


@pytest.mark.parametrize("sigma_pct", [None, 0.0, -1.0])
def test_no_volatility_means_no_plan_not_a_tight_one(sigma_pct):
    """0으로 채우면 하한 3%가 박혀 "매우 조용한 종목"으로 오인된다."""
    assert sell.volatility_exit_plan(sigma_pct) is None


def test_every_sigma_produces_a_plan_the_schema_accepts():
    for tenths in range(1, 200):
        assert isinstance(sell.volatility_exit_plan(tenths / 10), ExitPlan)


def test_wider_volatility_never_gives_a_tighter_stop():
    stops = [abs(sell.volatility_exit_plan(s / 10).stop_loss_pct) for s in range(1, 60)]
    assert stops == sorted(stops)


# --- exit_trigger: 판정 사유는 그대로, 표시만 가른다 ---


def _position(stage):
    return Position(ticker="007660", sector="반도체", weight=0.08, entry_price=100.0, peak_price=120.0, take_profit_stage=stage)


def test_first_take_profit_is_labelled_apart_from_trailing():
    """2026-09-07 007660 1차 익절이 take_profit_trail로 찍혀 폐기한 룰이 산 것처럼 보였다."""
    action = SellAction(ticker="007660", reason="take_profit_trail", sell_fraction=0.35)

    assert sell.exit_trigger(action, _position(0)) == "take_profit_first"
    assert sell.exit_trigger(action, _position(1)) == "take_profit_trail"


def test_other_reasons_pass_through():
    stop = SellAction(ticker="007660", reason="stop_loss", sell_fraction=1.0)
    assert sell.exit_trigger(stop, _position(0)) == "stop_loss"


def test_the_decision_reason_itself_is_untouched_so_stage_logic_still_fires():
    """execute_sell은 reason 문자열로 익절 단계를 올린다. 라벨 분리가 그 문자열을 바꾸면
    단계가 안 올라가 매분 재발동한다 — 1차 익절 뒤에도 단계가 1로 올라가야 한다."""
    from src.schemas import PortfolioState

    portfolio = PortfolioState(positions=[_position(0).model_copy(update={"quantity": 30})], cash_weight=0.9)
    action = SellAction(ticker="007660", reason="take_profit_trail", sell_fraction=1 / 3)

    after = sell.execute_sell(portfolio, action, 120.0)

    assert after.positions[0].take_profit_stage == 1
    assert sell.exit_trigger(action, _position(0)) == "take_profit_first"


# --- 노션 라벨 ---


def test_notion_uses_the_trigger_label_when_the_row_has_one():
    assert notion_sync.sell_reason_label({"reason": "take_profit_trail", "exit_trigger": "take_profit_first", "realized_pnl_pct": 0.14}) == "1차익절"
    assert notion_sync.sell_reason_label({"reason": "take_profit_trail", "exit_trigger": "take_profit_trail", "realized_pnl_pct": 0.05}) == "트레일링익절"


def test_notion_keeps_the_breakeven_trailing_label():
    """트레일링은 진입가 아래에서도 팔린다 — 수익 없는 청산을 익절이라 부르지 않는 기존 규약."""
    assert notion_sync.sell_reason_label({"reason": "take_profit_trail", "exit_trigger": "take_profit_trail", "realized_pnl_pct": 0.0}) == "트레일링청산"


def test_notion_rows_before_2026_09_14_keep_their_old_label():
    assert notion_sync.sell_reason_label({"reason": "take_profit_trail", "realized_pnl_pct": 0.2}) == "익절"


def test_telegram_label_for_a_trailing_exit_matches_notion(monkeypatch, tmp_path):
    """독립 리뷰: REASON_LABELS의 옛 "익절"이 트리거 라벨을 덮어 텔레그램만 "익절"로 나갔다."""
    import asyncio
    from datetime import datetime

    from src import pipeline
    from src.schemas import PortfolioState

    sent = []
    monkeypatch.setattr(pipeline.notify, "send_telegram_alert", lambda m: sent.append(m) or True)
    monkeypatch.setattr(pipeline, "display_name", lambda t: t)
    position = _position(2).model_copy(update={"quantity": 30})
    portfolio = PortfolioState(positions=[position], cash_weight=0.9)
    action = SellAction(ticker="007660", reason="take_profit_trail", sell_fraction=1 / 3)

    asyncio.run(pipeline.finalize_sell(
        portfolio, action, position, 110.0, datetime(2026, 9, 15), sell.execute_sell_simulated,
        tmp_path / "sell.jsonl", tmp_path / "journal.jsonl",
    ))

    assert "매도 (트레일링익절)" in sent[0]
