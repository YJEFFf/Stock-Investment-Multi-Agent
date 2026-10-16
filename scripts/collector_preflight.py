"""Read-only preflight, bounded repair, or independent completion watchdog."""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from src import collector_repair, notify
from src.portfolio_store import PortfolioLockBusy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--watchdog", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.watchdog:
            collector_repair.watchdog()
        else:
            result = collector_repair.check(allow_repair=args.repair, full=args.full)
            print({name: value["status"] for name, value in result["sources"].items()})
    except PortfolioLockBusy:
        logging.info("collector worker already running")
    except Exception as exc:
        logging.exception("collector controller failed")
        notify.alert_once_per_day("collector_controller_failed", notify.format_error_alert("수집 자동화 실행 실패", type(exc).__name__))
        raise


if __name__ == "__main__":
    main()
