#!/usr/bin/env python3
"""개인용 PWA 초기화·기기 연결·푸시 작업. 주문/LLM 호출 없음."""

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src.mobile_store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "pair", "worker", "status", "revoke-all"])
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("SIMA_WEB_STATE_DIR", "/var/lib/sima-pwa")))
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("SIMA_WEB_DATA_DIR", "/var/lib/sima-pwa/data")))
    parser.add_argument("--origin", default=os.environ.get("SIMA_WEB_ORIGIN", "http://localhost:8765"))
    args = parser.parse_args()
    os.umask(0o077)
    store = Store(args.state_dir / "app.sqlite3")
    key_path = args.state_dir / "vapid.pem"
    if args.command == "init":
        if not key_path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            key_path.chmod(0o600)
        print("PWA state initialized; existing keys preserved.")
    elif args.command == "pair":
        print(store.pairing_code())
    elif args.command == "worker":
        from src.mobile_push import run_worker
        logging.basicConfig(level=logging.INFO)
        run_worker(store, args.data_dir, key_path, args.origin)
    elif args.command == "revoke-all":
        with store.connect() as db:
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM pairing")
            db.execute("UPDATE subscriptions SET active=0")
            db.execute("UPDATE deliveries SET state='cancelled' WHERE state='pending'")
        print("All devices and pairing codes revoked.")
    else:
        with store.connect() as db:
            for table in ["sessions", "subscriptions", "alerts"]:
                print(table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            print("deliveries", [dict(row) for row in db.execute("SELECT state,COUNT(*) AS count FROM deliveries GROUP BY state")])
        print("worker_seen", store.get_meta("worker_seen"))


if __name__ == "__main__":
    main()
