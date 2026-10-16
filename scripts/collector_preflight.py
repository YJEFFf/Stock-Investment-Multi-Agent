"""Read-only preflight, bounded repair, or independent completion watchdog."""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from src import collector_repair
from src.portfolio_store import PortfolioLockBusy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--watchdog", action="store_true")
    parser.add_argument("--monitor", action="store_true", help="24시간 점검 및 장외 자동 수리")
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.watchdog:
            collector_repair.watchdog()
        else:
            result = collector_repair.check(allow_repair=args.repair or args.monitor, full=args.full, monitoring=args.monitor)
            print({name: value["status"] for name, value in result["sources"].items()})
        collector_repair.controller_status("watchdog_process" if args.watchdog else "worker_process")
    except PortfolioLockBusy:
        logging.info("collector worker already running")
    except Exception as exc:
        logging.exception("collector controller failed")
        collector_repair.controller_status("watchdog_process" if args.watchdog else "worker_process", f"수집 자동화 실행 실패: {type(exc).__name__}. 다음 점검에서 재시도합니다.")
        raise


if __name__ == "__main__":
    main()
