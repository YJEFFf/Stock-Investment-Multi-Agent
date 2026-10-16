"""저장한 원장 증거와 상태 사본으로 복구를 재생한다. 네트워크·운영 파일 쓰기 없음.

python scripts/replay_order_recovery.py logs/audits/2026-10-16
입력: portfolio_state.json, trade_journal.jsonl, unconfirmed_orders.json,
      broker_evidence.json (holdings, fills[ticker][buy|sell], observed_at).
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import kis, order_guard, order_recovery, portfolio_store


def replay(source: Path) -> dict:
    files = ["portfolio_state.json", "trade_journal.jsonl", "unconfirmed_orders.json", "broker_evidence.json"]
    hashes = {name: hashlib.sha256((source/name).read_bytes()).hexdigest() for name in files}
    evidence = json.loads((source/"broker_evidence.json").read_text())
    now = datetime.fromisoformat(evidence["observed_at"])
    before = [json.loads(l) for l in (source/"trade_journal.jsonl").read_text().splitlines()]
    orders = json.loads((source/"unconfirmed_orders.json").read_text())
    def fills(ticker, day, side, **kwargs):
        # 원장 파일의 일자는 마커의 주문일과 함께 고정한다.
        order_day = datetime.fromisoformat(orders[ticker]["created_at"]).astimezone(order_recovery.KST).date()
        if day != order_day:
            raise AssertionError("replay requested wrong order date")
        value = evidence["fills"][ticker][side]
        return tuple(value) if value is not None else None
    def forbidden(*a, **kw):
        raise AssertionError("replay cannot access network or submit orders")
    with tempfile.TemporaryDirectory(prefix="sima-recovery-replay-") as directory:
        tmp = Path(directory)
        for name in files[:3]:shutil.copyfile(source/name, tmp/name)
        with (patch.dict(os.environ, {"SIMA_EXTENDED_ORDER_RECOVERY": "1"}),
              patch.object(order_guard,"STATE_PATH",tmp/"unconfirmed_orders.json"),
              patch.object(order_recovery,"JOURNAL_PATH",tmp/"trade_journal.jsonl"),
              patch.object(order_recovery,"AUDIT_DIR",tmp/"evidence"),
              patch.object(portfolio_store,"PORTFOLIO_STATE_PATH",tmp/"portfolio_state.json"),
              patch.object(portfolio_store,"PORTFOLIO_LOCK_PATH",tmp/"portfolio.lock"),
              patch.object(kis,"fetch_daily_fill_totals",fills),
              patch.object(kis,"fetch_holdings",lambda **kw:evidence["holdings"]),
              patch.object(kis.requests,"get",forbidden),patch.object(kis.requests,"post",forbidden)):
            for i in range(len(orders)*2):
                order_recovery.recover_pending_order(now+timedelta(minutes=i))
            remaining = order_guard.load_orders()
            after = [json.loads(l) for l in (tmp/"trade_journal.jsonl").read_text().splitlines()]
            changes = []
            for old,new in zip(before,after):
                if old!=new:
                    changes.append({"ticker":new['ticker'],"event":new['event'],"day":new['day'],
                                    "fields":{k:{"before":old.get(k),"after":v} for k,v in new.items()
                                              if k!='fill_recovery' and old.get(k)!=v}})
            state_before = {name:(tmp/name).read_bytes() for name in files[:3]}
            order_recovery.recover_pending_order(now+timedelta(minutes=10))
            idempotent = all((tmp/name).read_bytes()==data for name,data in state_before.items())
    assert all(hashlib.sha256((source/name).read_bytes()).hexdigest()==h for name,h in hashes.items())
    return {"source_sha256":hashes,"changes":changes,"remaining":list(remaining),
            "second_pass_unchanged":idempotent,"broker_orders":0}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    args=parser.parse_args()
    print(json.dumps(replay(args.source),ensure_ascii=False,indent=2))
