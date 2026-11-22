"""Off-hours release of one latched share-basis warning after explicit review.

The operator states the reviewed listed-share count. A fresh KIS quote must report
exactly that count with the stored par value, and the latched checkpoint must differ
only in share count. Quantity, entry price, frozen ExitPlan, quotes, sales and NAV
history are never changed. No order, account query or model call occurs.
"""
import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import exit_only, kis, operating_mode
from src.market_calendar import KST, deployment_blocked
from src.order_guard import _atomic_bytes
from src.portfolio_store import portfolio_lock


def released_state(state, ticker, quote, *, reviewed_listed_shares, at):
    updated = state.model_copy(deep=True)
    old = state.share_basis.get(ticker)
    if ticker not in state.basis_warnings:
        raise ValueError(f"no latched basis warning: {ticker}")
    if ticker not in {p.ticker for p in state.portfolio.positions}:
        raise ValueError(f"not a remaining holding: {ticker}")
    if old is None:
        raise ValueError("a warning without a prior checkpoint needs a closing-price review, not a share-count release")
    if quote is None or quote.listed_shares != reviewed_listed_shares:
        raise ValueError("fresh quote does not report the reviewed listed-share count")
    if quote.par_value != old["par_value"]:
        raise ValueError("par value changed; this is a price-basis event, not a share-count release")
    if old["listed_shares"] == reviewed_listed_shares:
        raise ValueError("share count is unchanged; the warning came from a price mismatch")
    updated.basis_warnings = [t for t in state.basis_warnings if t != ticker]
    updated.share_basis[ticker] = {"listed_shares": reviewed_listed_shares, "par_value": quote.par_value,
                                   "day": at.date().isoformat()}
    updated.updated_at = at
    exit_only.notice(updated, at, f"가상 매도 추적 재개 · {state.names[ticker]}",
                     f"상장주식수 {old['listed_shares']:,}주 → {reviewed_listed_shares:,}주 변경을 확인했습니다. "
                     "가격 조정 이벤트가 아니어서 기존 수량·매입가·매도 기준을 그대로 두고 추적을 재개합니다.")
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--listed-shares", type=int, required=True, help="reviewed new listed-share count")
    parser.add_argument("--note", required=True, help="review evidence recorded in the audit proof")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    at = datetime.now(KST)
    if not operating_mode.EXIT_ONLY or deployment_blocked(at):
        raise RuntimeError("requires EXIT_ONLY and an off-hours maintenance window")
    path = exit_only.STATE_PATH
    with portfolio_lock(path.with_suffix(".lock"), blocking=False):
        before = path.read_bytes()
        state = exit_only.load(path)
        quote = kis.fetch_quote(args.ticker, policy=kis.FAST_FAIL_POLICY)
        result = released_state(state, args.ticker, quote, reviewed_listed_shares=args.listed_shares, at=at)
        proof = {"at": at.isoformat(), "ticker": args.ticker, "note": args.note,
                 "before_sha256": hashlib.sha256(before).hexdigest(),
                 "old_basis": state.share_basis[args.ticker], "new_basis": result.share_basis[args.ticker],
                 "quote": {"price": quote.price, "prev_close": quote.prev_close,
                           "listed_shares": quote.listed_shares, "par_value": quote.par_value}}
        if args.apply:
            if path.read_bytes() != before:
                raise RuntimeError("ledger changed during review")
            archive = path.parent / "audits" / f"{at:%Y-%m-%d}-basis-release" / f"{args.ticker}-{at:%H%M%S%f}"
            _atomic_bytes(archive / "before.json", before)
            _atomic_bytes(archive / "proof.json", json.dumps(proof, indent=2, ensure_ascii=False).encode())
            exit_only.save(result, path)
            exit_only.load(path)
            proof["after_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            _atomic_bytes(archive / "proof.json", json.dumps(proof, indent=2, ensure_ascii=False).encode())
        print(json.dumps({"applied": args.apply, **proof}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
