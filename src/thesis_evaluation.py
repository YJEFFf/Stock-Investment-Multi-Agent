"""Forward-only measurement of the first invalidation in each holding episode.

No LLM, price fetching, order placement or portfolio mutation. Prices are the cache
already accumulated by the regular IC job. Baseline = observed deterministic sales
plus remaining holdings marked to the horizon close. Additional buys or uncertain
fills make the baseline unavailable instead of fabricating an allocation.
"""

import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
import math

from src.evaluation import PriceHistory
from src.market_calendar import is_krx_trading_day
from src.schemas import HoldingThesisRecord

HORIZONS = (5, 10, 20)
SLIPPAGE_BPS = (0, 10, 25)  # sensitivity scenarios, not trading parameters


def advance(day, sessions):
    for _ in range(sessions):
        day += timedelta(days=1)
        while not is_krx_trading_day(day):
            day += timedelta(days=1)
    return day


def measure_one(record: HoldingThesisRecord, prices, journal, *, horizon, as_of, observations=None):
    exit_day = advance(record.day, 1)
    end_day = advance(exit_day, horizon)
    result = {"ticker": record.ticker, "review_day": record.day.isoformat(),
              "entry_day": str(record.position.entry_day), "thesis_hash": record.thesis_hash,
              "hypothetical_exit_day": exit_day.isoformat(), "horizon_end": end_day.isoformat(),
              "horizon": horizon, "status": "immature"}
    if as_of < end_day:
        return result
    qty = record.position.quantity
    start, end = prices.get(exit_day), prices.get(end_day)
    if qty is None or qty <= 0 or start is None or end is None or start.open <= 0 or start.volume <= 0 or end.close <= 0:
        return {**result, "status": "missing_price_or_quantity"}
    remaining, proceeds = qty, 0.0
    for row in journal:
        if row.get("ticker") != record.ticker or not record.day.isoformat() < row.get("day", "") <= end_day.isoformat():
            continue
        if not remaining:
            break  # later re-entry belongs to a different episode
        if row.get("event") == "buy":
            return {**result, "status": "baseline_additional_buy"}
        if row.get("event") != "sell":
            continue
        expected_plan = record.position.exit_plan.model_dump(mode="json") if record.position.exit_plan else None
        if row.get("reason") not in {"stop_loss", "take_profit_trail"} or row.get("exit_plan") != expected_plan:
            return {**result, "status": "baseline_policy_changed"}
        sold, price = row.get("shares_sold"), row.get("exit_price")
        if (row.get("shares_before") != remaining or not isinstance(sold, int) or not 0 < sold <= remaining
                or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0
                or row.get("exit_price_source") != "fill"):
            return {**result, "status": "baseline_fill_unavailable"}
        remaining -= sold
        proceeds += sold * price
    if observations is not None and remaining:
        observed = observations.get((end_day, record.ticker))
        if (observed is None or observed.unconfirmed_order or observed.status == "started"
                or observed.position.entry_day != record.position.entry_day
                or observed.position.quantity != remaining or observed.position.exit_plan != record.position.exit_plan):
            return {**result, "status": "baseline_position_unverified"}
    baseline = proceeds + remaining * end.close
    rate = record.hypothetical_sell_fee_rate + record.hypothetical_sell_tax_rate
    gross_exit = qty * start.open
    scenarios = []
    for bps in SLIPPAGE_BPS:
        hypothetical = gross_exit * (1 - bps / 10_000) * (1 - rate)
        # Apply equal assumed sell costs; baseline residual valuation also includes
        # a hypothetical liquidation cost at the horizon, not a claimed real sale.
        baseline_net = baseline * (1 - rate)
        advantage = hypothetical - baseline_net
        scenarios.append({"exit_slippage_bps": bps, "hypothetical_exit_net": hypothetical,
                          "baseline_net": baseline_net, "exit_advantage": advantage,
                          "avoided_loss_vs_baseline": max(advantage, 0),
                          "opportunity_cost_vs_baseline": max(-advantage, 0)})
    return {**result, "status": "measured", "quantity": qty, "baseline_remaining": remaining,
            "baseline_gross": baseline, "hypothetical_exit_gross": gross_exit,
            "assumed_sell_fee_rate": record.hypothetical_sell_fee_rate,
            "assumed_sell_tax_rate": record.hypothetical_sell_tax_rate, "scenarios": scenarios}


def measure(root: Path, price_dir: Path, journal_path: Path, *, as_of: date):
    journal = [json.loads(line) for line in journal_path.read_text().splitlines() if line.strip()]
    first, states, invalid_files, observations = {}, Counter(), [], {}
    for path in sorted((root / "reviews").glob("*.json")):
        try:
            record = HoldingThesisRecord.model_validate_json(path.read_text())
        except ValueError:
            invalid_files.append(path.name)
            continue
        if record.day > as_of:
            continue
        states[record.status] += 1
        observations[(record.day, record.ticker)] = record
        if record.status == "invalidated" and record.completed_at and record.thesis_hash:
            key = (record.ticker, record.position.entry_day, record.thesis_hash)
            first.setdefault(key, record)
    history = PriceHistory(price_dir)
    measurements = [measure_one(record, history.load(record.ticker), journal, horizon=k, as_of=as_of,
                                observations=observations)
                    for record in first.values() for k in HORIZONS]
    summary = {"as_of": as_of.isoformat(), "shadow_only": True,
               "convention": "first invalidation at D close; hypothetical full exit at open(D+1); "
                             "actual baseline sales + residual close(D+1+k), k=5/10/20 KRX sessions",
               "cost_note": "Configured fee/tax estimates for both alternatives; exit slippage scenarios "
                            "0/10/25 bps. Hypothetical fills, not executable-price guarantees. "
                            "Cash interest and dividends excluded. Overlapping windows are not independent.",
               "review_counts": dict(states), "invalid_record_files": invalid_files,
               "first_invalidation_episodes": len(first),
               "measurement_counts": dict(Counter(x["status"] for x in measurements)),
               "measurements": measurements}
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "measurement.tmp"
    temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(root / "measurement.json")
    with (root / "measurement_history.jsonl").open("a") as file:
        file.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary
