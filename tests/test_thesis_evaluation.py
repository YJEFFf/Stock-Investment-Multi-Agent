import json
from datetime import timedelta

import pytest

from src import thesis_evaluation as evaluation
from src.evaluation import PriceHistory
from src.schemas import ExitPlan, HoldingThesisRecord, OHLCVBar, Position
from tests.test_thesis_shadow import ENTRY, REVIEW


def record():
    return HoldingThesisRecord(
        day=REVIEW.date(), ticker="005930", started_at=REVIEW, completed_at=REVIEW,
        status="invalidated", thesis_hash="frozen", prompt_version="prompt:holding_thesis@123456", model="mock",
        position=Position(ticker="005930", sector="반도체", weight=.08, quantity=30,
                          entry_day=ENTRY.date(), entry_price=100, peak_price=100,
                          exit_plan=ExitPlan(stop_loss_pct=-.1, take_profit_pct=.2,
                                             take_profit_fraction=1/3, trail_pct=-.07)),
        hypothetical_sell_fee_rate=.000142, hypothetical_sell_tax_rate=.002,
    )


def bar(day, price):
    return OHLCVBar(date=day, open=price, high=price, low=price, close=price, volume=100)


def market(r):
    opening = evaluation.advance(r.day, 1)
    end = evaluation.advance(opening, 5)
    return {opening: bar(opening, 100), end: bar(end, 80)}, end


def sale(r, day, quantity=30, **updates):
    return {"event": "sell", "ticker": r.ticker, "day": day.isoformat(), "shares_before": 30,
            "shares_sold": quantity, "exit_price": 90, "exit_price_source": "fill",
            "reason": "stop_loss", "exit_plan": r.position.exit_plan.model_dump(mode="json"), **updates}


def test_forward_window_skips_weekend_and_refuses_immature_outcome():
    r = record(); prices, end = market(r)
    assert evaluation.advance(r.day, 1).isoformat() == "2026-11-09"
    result = evaluation.measure_one(r, prices, [], horizon=5, as_of=end - timedelta(days=1))
    assert result["status"] == "immature" and "scenarios" not in result
    assert result["horizon_end"] == "2026-11-16"


def test_net_opportunity_cost_and_slippage_use_same_cost_assumptions():
    r = record(); prices, end = market(r)
    prices[end] = bar(end, 110)
    result = evaluation.measure_one(r, prices, [], horizon=5, as_of=end)
    zero, ten, twentyfive = result["scenarios"]
    cost_factor = 1 - .000142 - .002
    assert result["baseline_gross"] == 3300
    assert zero["opportunity_cost_vs_baseline"] == pytest.approx(300 * cost_factor)
    assert zero["avoided_loss_vs_baseline"] == 0
    assert ten["hypothetical_exit_net"] == pytest.approx(3000 * .999 * cost_factor)
    assert twentyfive["hypothetical_exit_net"] < ten["hypothetical_exit_net"]


def test_partial_baseline_sale_and_residual_are_both_counted():
    r = record(); prices, end = market(r)
    rows = [sale(r, evaluation.advance(r.day, 2), quantity=10)]
    observed = r.model_copy(update={"day": end, "position": r.position.model_copy(update={"quantity": 20})})
    result = evaluation.measure_one(r, prices, rows, horizon=5, as_of=end,
                                    observations={(end, r.ticker): observed})
    assert result["status"] == "measured"
    assert result["baseline_gross"] == 10 * 90 + 20 * 80
    assert result["scenarios"][0]["avoided_loss_vs_baseline"] == pytest.approx(500 * .997858)


@pytest.mark.parametrize("updates,status", [
    ({"exit_price_source": "quote_fallback"}, "baseline_fill_unavailable"),
    ({"exit_price_source": "fill_partial"}, "baseline_fill_unavailable"),
    ({"shares_before": 40}, "baseline_fill_unavailable"),
    ({"exit_price": float("nan")}, "baseline_fill_unavailable"),
    ({"reason": "llm_discretionary"}, "baseline_policy_changed"),
    ({"exit_plan": None}, "baseline_policy_changed"),
])
def test_uncertain_or_changed_full_exit_is_excluded(updates, status):
    r = record(); prices, end = market(r)
    rows = [sale(r, evaluation.advance(r.day, 1), **updates)]
    assert evaluation.measure_one(r, prices, rows, horizon=5, as_of=end)["status"] == status


def test_new_buy_is_excluded_until_original_position_is_fully_closed():
    r = record(); prices, end = market(r)
    buy = {"event": "buy", "ticker": r.ticker, "day": evaluation.advance(r.day, 2).isoformat()}
    assert evaluation.measure_one(r, prices, [buy], horizon=5, as_of=end)["status"] == "baseline_additional_buy"
    rows = [sale(r, evaluation.advance(r.day, 1)), buy]
    result = evaluation.measure_one(r, prices, rows, horizon=5, as_of=end, observations={})
    assert result["status"] == "measured" and result["baseline_gross"] == 2700


def test_missing_final_snapshot_or_changed_quantity_prevents_baseline_inference():
    r = record(); prices, end = market(r)
    for observations in [{}, {(end, r.ticker): r.model_copy(update={
            "position": r.position.model_copy(update={"quantity": 60})})},
            {(end, r.ticker): r.model_copy(update={"unconfirmed_order": True})},
            {(end, r.ticker): r.model_copy(update={"status": "started"})}]:
        result = evaluation.measure_one(r, prices, [], horizon=5, as_of=end, observations=observations)
        assert result["status"] == "baseline_position_unverified"


def test_daily_report_counts_first_invalidation_only_and_does_not_mutate_inputs(tmp_path):
    r = record(); prices, end = market(r)
    root = tmp_path / "shadow"; reviews = root / "reviews"; reviews.mkdir(parents=True)
    for row in [r, r.model_copy(update={"day": evaluation.advance(r.day, 1)}),
                r.model_copy(update={"day": end, "status": "intact"})]:
        (reviews / f"{row.day}_{row.ticker}.json").write_text(row.model_dump_json())
    (reviews / "broken.json").write_text("interrupted write")
    future = r.model_copy(update={"day": evaluation.advance(end, 1), "thesis_hash": "future"})
    (reviews / f"{future.day}_{future.ticker}.json").write_text(future.model_dump_json())
    journal = tmp_path / "journal.jsonl"; journal.write_text("")
    price_dir = tmp_path / "prices"; PriceHistory(price_dir).merge(r.ticker, list(prices.values()))
    inputs = [journal, *reviews.iterdir(), *price_dir.iterdir()]
    before = {path: path.read_bytes() for path in inputs}
    report = evaluation.measure(root, price_dir, journal, as_of=end)
    assert report["first_invalidation_episodes"] == 1
    assert report["measurement_counts"] == {"measured": 1, "immature": 2}
    assert report["invalid_record_files"] == ["broken.json"]
    assert before == {path: path.read_bytes() for path in inputs}
    assert json.loads((root / "measurement.json").read_text()) == report
