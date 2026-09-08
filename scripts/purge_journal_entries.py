"""매매 기록에서 특정 종목·날짜의 행을 제거한다. 기본은 드라이런.

**왜 도구로 두는가**: 테스트가 운영 `logs/`를 오염시킨 사고가 2026-08-20, 08-27,
09-08까지 네 번 났다. 재발 자체는 `tests/test_log_isolation.py`가 막지만, 이미
들어간 가짜 행을 걷어내는 일은 그때마다 임시 코드로 했다. 매매 기록을 지우는
작업이라 임시 코드로 할 일이 아니다 — `reconcile_*.py`와 같은 규약으로 둔다.

**장중에는 --apply를 거부한다.** 1분 크론이 같은 파일에 append하는 동안
읽기->쓰기로 덮으면 그 사이 들어온 **진짜 매도 기록이 사라진다.** reconcile 계열이
장중 --apply를 막는 것과 같은 이유이고, 이쪽이 더 위험하다(저쪽은 교정, 이쪽은 삭제).

실행:
    uv run python scripts/purge_journal_entries.py --ticker 005930 --day 2026-08-09
    uv run python scripts/purge_journal_entries.py --ticker 005930 --day 2026-08-09 --apply
"""

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import notion_sync  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("purge_journal_entries")

KST = ZoneInfo("Asia/Seoul")
JOURNAL_PATHS = [Path("logs/sell.jsonl"), Path("logs/trade_journal.jsonl")]
SYNC_STATE_PATH = Path("logs/notion_sync_state.json")

# 크론이 이 파일들에 쓸 수 있는 구간. 15:50 reconcile은 읽기 전용이지만 그 뒤로
# 물러선다(deploy/crontab의 15:50 주석과 같은 여유).
CRON_WRITE_WINDOW = (time(8, 25), time(15, 55))


def _market_hours_now() -> bool:
    from src.market_calendar import is_krx_trading_day

    now = datetime.now(KST)
    if not is_krx_trading_day(now.date()):
        return False
    return CRON_WRITE_WINDOW[0] <= now.time() < CRON_WRITE_WINDOW[1]


def _matches(row: dict, ticker: str, day: str) -> bool:
    return row.get("ticker") == ticker and row.get("day") == day


def purge_journals(ticker: str, day: str, *, apply: bool) -> int:
    removed_total = 0
    for path in JOURNAL_PATHS:
        if not path.exists():
            logger.warning("journal_missing path=%s", path)
            continue
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        keep = [r for r in rows if not _matches(r, ticker, day)]
        removed = len(rows) - len(keep)
        removed_total += removed
        logger.info("journal path=%s rows=%d remove=%d keep=%d", path, len(rows), removed, len(keep))
        if not removed or not apply:
            continue
        backup = path.with_suffix(path.suffix + f".bak-{datetime.now(KST):%Y%m%d-%H%M%S}-purge")
        shutil.copy2(path, backup)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))
        logger.info("journal_rewritten path=%s backup=%s", path, backup)
    return removed_total


def purge_notion(ticker: str, day: str, *, apply: bool) -> int:
    """노션 페이지를 아카이브하고 동기화 상태에서 키를 지운다.

    상태에서만 지우고 페이지를 두면 노션에 고아 행이 남고, 페이지만 지우고 상태를
    두면 다음 동기화가 "이미 보냈다"고 판단해 되살리지 않는다 — 둘 다 해야 한다.
    """
    if not SYNC_STATE_PATH.exists():
        logger.warning("sync_state_missing path=%s", SYNC_STATE_PATH)
        return 0

    state = json.loads(SYNC_STATE_PATH.read_text())
    entries = state.get("entries", {})
    # 키 형태: "sell:005930:2026-08-09", 같은 날 두 번째부터는 ":1", ":2"가 붙는다.
    prefixes = tuple(f"{event}:{ticker}:{day}" for event in ("buy", "sell"))
    targets = {k: v for k, v in entries.items() if k.startswith(prefixes)}
    logger.info("notion targets=%d keys=%s", len(targets), sorted(targets))
    if not targets or not apply:
        return len(targets)

    archived = 0
    for key, meta in sorted(targets.items()):
        page_id = meta.get("page_id")
        if not page_id:
            continue
        if notion_sync._notion_request("PATCH", f"/pages/{page_id}", {"archived": True}) is None:
            logger.error("notion_archive_failed key=%s page_id=%s", key, page_id)
            continue
        archived += 1
        logger.info("notion_archived key=%s page_id=%s", key, page_id)

    backup = SYNC_STATE_PATH.with_suffix(f".json.bak-{datetime.now(KST):%Y%m%d-%H%M%S}-purge")
    shutil.copy2(SYNC_STATE_PATH, backup)
    state["entries"] = {k: v for k, v in entries.items() if k not in targets}
    state["synced_keys"] = [k for k in state.get("synced_keys", []) if k not in targets]
    SYNC_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    logger.info("sync_state_rewritten backup=%s archived=%d", backup, archived)
    return archived


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--day", required=True, help="YYYY-MM-DD")
    parser.add_argument("--apply", action="store_true", help="실제로 지운다 (기본은 드라이런)")
    args = parser.parse_args()

    if args.apply and _market_hours_now():
        logger.error(
            "장중에는 --apply를 하지 않는다 — 1분 크론이 같은 파일에 append하는 동안 "
            "덮어쓰면 그 사이 들어온 진짜 매도 기록이 사라진다. 15:55 이후에 다시 실행할 것."
        )
        return 2

    rows = purge_journals(args.ticker, args.day, apply=args.apply)
    pages = purge_notion(args.ticker, args.day, apply=args.apply)
    verb = "제거함" if args.apply else "제거 예정 (드라이런)"
    logger.info("purge_done ticker=%s day=%s 로그행=%d 노션=%d %s", args.ticker, args.day, rows, pages, verb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
