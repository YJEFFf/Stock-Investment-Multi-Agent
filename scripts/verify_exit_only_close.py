"""One-time, off-hours verification of legacy closing-quote timestamps.

Only held-stock minute quotes are queried. No sale, NAV rewrite, account query,
or model call occurs. Every stored price must equal the provider's closing bar.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import exit_only, kis, operating_mode
from src.market_calendar import KST, deployment_blocked, monitor_due, market_session
from src.order_guard import _atomic_bytes
from src.portfolio_store import portfolio_lock


def verified_state(state, quotes, at):
    updated = state.model_copy(deep=True)
    day = monitor_due(at).date()
    close = market_session(day).close
    if state.basis_warnings or state.share_basis:
        raise ValueError("only a legacy ledger without basis checkpoints can be migrated")
    for position in state.portfolio.positions:
        ticker = position.ticker
        previous, quote = state.quotes[ticker], quotes.get(ticker)
        if (quote is None or quote.provider_at != close or quote.market_status != "CLOSE"
                or not exit_only.valid_quote(quote, at=close)
                or previous.price != quote.price or previous.source != "kis"
                or previous.observed_at.astimezone(KST).date() != day
                or previous.observed_at < close or previous.provider_at is not None):
            raise ValueError(f"unverified stored closing quote: {ticker}")
        updated.quotes[ticker] = previous.model_copy(update={"provider_at": close, "observed_at": at})
    updated.updated_at = at
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    at = datetime.now(KST)
    if not operating_mode.EXIT_ONLY or deployment_blocked(at):
        raise RuntimeError("requires EXIT_ONLY and an off-hours maintenance window")
    path = exit_only.STATE_PATH
    with portfolio_lock(path.with_suffix(".lock"), blocking=False):
        before = path.read_bytes()
        state = exit_only.load(path)
        day = monitor_due(at).date()
        quotes = {p.ticker: kis.fetch_closing_quote(p.ticker, day, policy=kis.FAST_FAIL_POLICY)
                  for p in state.portfolio.positions}
        result = verified_state(state, quotes, at)
        proof = {"at": at.isoformat(), "day": day.isoformat(),
                 "before_sha256": hashlib.sha256(before).hexdigest(),
                 "quotes": {t: {"price": q.price, "provider_at": q.provider_at.isoformat()} for t, q in quotes.items()}}
        if args.apply:
            if path.read_bytes() != before:
                raise RuntimeError("ledger changed during verification")
            archive = path.parent / "audits" / "2026-11-11-close-verification" / at.strftime("%H%M%S%f")
            _atomic_bytes(archive / "before.json", before)
            _atomic_bytes(archive / "proof.json", json.dumps(proof, indent=2).encode())
            exit_only.save(result, path)
            exit_only.load(path)
            proof["after_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            _atomic_bytes(archive / "proof.json", json.dumps(proof, indent=2).encode())
        print(json.dumps({"applied": args.apply, **proof}, indent=2))


if __name__ == "__main__":
    main()
