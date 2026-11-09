"""Monitor existing holdings, or explicitly initialize their separate simulation ledger."""
import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import exit_only, notify, operating_mode
from src.portfolio_store import PORTFOLIO_STATE_PATH, portfolio_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize-from", type=Path)
    args = parser.parse_args()
    if not operating_mode.EXIT_ONLY:
        raise RuntimeError("exit-only mode is not enabled")
    if args.initialize_from:
        with portfolio_lock():
            unconfirmed = Path("logs/unconfirmed_orders.json")
            if not unconfirmed.exists() or json.loads(unconfirmed.read_text()):
                raise RuntimeError("unconfirmed order state must be present and empty")
            state = exit_only.initialize(PORTFOLIO_STATE_PATH.read_bytes(),
                                         json.loads(args.initialize_from.read_text()), at=datetime.now(exit_only.KST))
            print(f"Initialized {len(state.portfolio.positions)} holdings; broker portfolio unchanged")
    else:
        asyncio.run(exit_only.monitor())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        main()
    except Exception as exc:
        logging.exception("exit_only_failed")
        notify.alert_once_per_day("exit_only_failed", notify.format_error_alert(
            "가상 매도 추적 실패", f"{type(exc).__name__}: 이번 회차를 중단했습니다. 기존 장부는 초기화하지 않습니다."))
        raise
