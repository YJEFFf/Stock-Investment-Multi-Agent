"""Read-only thesis experiment. No --apply or automatic order activation mode."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from src import kis, thesis_evaluation, thesis_shadow
from src.market_calendar import is_krx_trading_day


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("capture", "review", "measure"))
    parser.add_argument("--data-dir", type=Path, default=Path("logs"))
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    load_dotenv()
    root = args.output_dir or args.data_dir / "thesis_shadow"
    now = thesis_shadow.now_kst()
    if not is_krx_trading_day(now.date()):
        return 0
    try:
        if args.phase == "capture":
            result = asyncio.run(thesis_shadow.capture_entries(args.data_dir / "pending_buys.json", root=root))
        elif args.phase == "review":
            result = asyncio.run(thesis_shadow.review_holdings(
                args.data_dir / "portfolio_state.json", args.data_dir / "trade_journal.jsonl", root=root,
                fee_rate=kis.BROKERAGE_FEE_RATE, tax_rate=kis.SELL_TAX_RATE,
                unconfirmed_path=args.data_dir / "unconfirmed_orders.json",
            ))
        else:
            result = thesis_evaluation.measure(root, args.data_dir / "price_history",
                                               args.data_dir / "trade_journal.jsonl", as_of=now.date())
        print("thesis_shadow_" + args.phase, json.dumps({k: v for k, v in result.items()
              if k not in {"measurements", "results"}}, ensure_ascii=False))
        return 0
    except Exception:
        logging.exception("thesis_shadow_%s_failed", args.phase)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
