"""개인 기기 인증·알림함·푸시 발송 상태. 거래 장부와 별도 SQLite 저장소."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from src.schemas import AppAlert

SESSION_SECONDS = 90 * 86400


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, expires REAL NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT UNIQUE NOT NULL,
                    endpoint TEXT UNIQUE NOT NULL, info TEXT NOT NULL, created REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, last_status TEXT);
                CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, created REAL NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries (
                    alert TEXT NOT NULL, subscription INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL, status TEXT,
                    PRIMARY KEY(alert, subscription));
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, window REAL NOT NULL, count INTEGER NOT NULL);
            """)
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def allow(self, key: str, *, limit: int, period: int = 60) -> bool:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
            if not row or now - row["window"] >= period:
                db.execute("INSERT OR REPLACE INTO rate_limits VALUES (?,?,1)", (key, now))
                return True
            if row["count"] >= limit:
                return False
            db.execute("UPDATE rate_limits SET count=count+1 WHERE key=?", (key,))
            return True

    def pairing_code(self, *, hours: int = 48) -> str:
        code = secrets.token_hex(8).upper()
        with self.connect() as db:
            db.execute("DELETE FROM pairing WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO pairing VALUES (?,?)", (digest(code), time.time() + hours * 3600))
        return "-".join(code[i:i + 4] for i in range(0, len(code), 4))

    def login(self, code: str) -> str | None:
        normalized = code.replace("-", "").replace(" ", "").upper().strip()
        if len(normalized) != 16:
            return None
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM pairing WHERE hash=? AND expires>?", (digest(normalized), now)).fetchone()
            if not row:
                return None
            db.execute("DELETE FROM pairing WHERE hash=?", (digest(normalized),))
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (digest(token), now + SESSION_SECONDS, now))
            return token

    def session(self, token: str | None) -> str | None:
        if not token or len(token) > 128:
            return None
        key = digest(token)
        with self.connect() as db:
            return key if db.execute("SELECT 1 FROM sessions WHERE hash=? AND expires>?", (key, time.time())).fetchone() else None

    def logout(self, session: str):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (session,))
            db.execute("UPDATE subscriptions SET active=0 WHERE session=?", (session,))
            db.execute("UPDATE deliveries SET state='cancelled' WHERE state='pending' AND subscription IN (SELECT id FROM subscriptions WHERE session=?)", (session,))

    def subscribe(self, session: str, info: dict):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            owner = db.execute("SELECT * FROM subscriptions WHERE endpoint=?", (info["endpoint"],)).fetchone()
            if owner and owner["session"] != session:
                live = db.execute("SELECT 1 FROM sessions WHERE hash=? AND expires>?", (owner["session"], time.time())).fetchone()
                if owner["active"] and live:
                    raise ValueError("subscription belongs to another active device")
                # iOS는 로그인 세션이 만료돼도 기존 push endpoint를 유지한다.
                db.execute("UPDATE deliveries SET state='cancelled' WHERE state='pending' AND subscription IN (SELECT id FROM subscriptions WHERE session=? OR id=?)", (session, owner["id"]))
                db.execute("DELETE FROM subscriptions WHERE session=?", (session,))
                db.execute("UPDATE subscriptions SET session=?,info=?,created=?,active=1,last_status=NULL WHERE id=?",
                           (session, json.dumps(info), time.time(), owner["id"]))
                return
            existing = db.execute("SELECT endpoint FROM subscriptions WHERE session=?", (session,)).fetchone()
            if existing and existing["endpoint"] != info["endpoint"]:
                db.execute("UPDATE deliveries SET state='cancelled' WHERE state='pending' AND subscription IN (SELECT id FROM subscriptions WHERE session=?)", (session,))
            db.execute("""INSERT INTO subscriptions (session,endpoint,info,created) VALUES (?,?,?,?)
                        ON CONFLICT(session) DO UPDATE SET
                        created=CASE WHEN subscriptions.active=0 OR subscriptions.endpoint<>excluded.endpoint THEN excluded.created ELSE subscriptions.created END,
                        endpoint=excluded.endpoint,info=excluded.info,
                        active=1,last_status=NULL""", (session, info["endpoint"], json.dumps(info), time.time()))

    def unsubscribe(self, session: str):
        with self.connect() as db:
            db.execute("UPDATE subscriptions SET active=0 WHERE session=?", (session,))
            db.execute("UPDATE deliveries SET state='cancelled' WHERE state='pending' AND subscription IN (SELECT id FROM subscriptions WHERE session=?)", (session,))

    def subscription_status(self, session: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT active,last_status FROM subscriptions WHERE session=?", (session,)).fetchone()
            pending = db.execute("SELECT COUNT(*) FROM deliveries d JOIN subscriptions s ON d.subscription=s.id WHERE s.session=? AND d.state='pending'", (session,)).fetchone()[0]
        return {"enabled": bool(row and row["active"]), "last_status": row["last_status"] if row else None,
                "pending": pending, "worker_seen": self.get_meta("worker_seen")}

    def add_alert(self, alert: AppAlert, *, only_session: str | None = None):
        created = alert.created_at.timestamp()
        with self.connect() as db:
            added = db.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,?)", (alert.id, created, alert.model_dump_json())).rowcount
            if not added:
                return
            subs = db.execute("SELECT p.* FROM subscriptions p JOIN sessions s ON p.session=s.hash WHERE p.active=1 AND s.expires>?", (time.time(),)).fetchall()
            for sub in subs:
                if only_session and sub["session"] != only_session:
                    continue
                if created < sub["created"] or time.time() - created > 86400:
                    continue
                db.execute("INSERT OR IGNORE INTO deliveries (alert,subscription,due) VALUES (?,?,?)", (alert.id, sub["id"], time.time()))

    def alerts(self, limit: int = 150) -> list[dict]:
        with self.connect() as db:
            return [json.loads(r["payload"]) for r in db.execute("SELECT payload FROM alerts ORDER BY created DESC LIMIT ?", (limit,))]

    def pending(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("""SELECT d.*,p.info,a.payload,a.created FROM deliveries d
                JOIN subscriptions p ON p.id=d.subscription JOIN alerts a ON a.id=d.alert
                JOIN sessions s ON s.hash=p.session
                WHERE d.state='pending' AND d.due<=? AND p.active=1 AND s.expires>?
                ORDER BY d.due LIMIT 30""", (time.time(), time.time()))]

    def delivery_result(self, delivery: dict, *, code: int | None):
        attempts = delivery["attempts"] + 1
        sent = code is not None and 200 <= code < 300
        expired = code in (404, 410)
        terminal = expired or attempts >= 8 or time.time() - delivery["created"] > 86400
        state = "sent" if sent else "failed" if terminal else "pending"
        status = "accepted" if sent else "expired" if expired else f"http_{code}" if code else "network_error"
        with self.connect() as db:
            db.execute("UPDATE deliveries SET state=?,attempts=?,due=?,status=? WHERE alert=? AND subscription=?",
                       (state, attempts, time.time() + min(3600, 15 * 2 ** attempts), status, delivery["alert"], delivery["subscription"]))
            db.execute("UPDATE subscriptions SET last_status=? WHERE id=?", (status, delivery["subscription"]))
            if expired:
                db.execute("UPDATE subscriptions SET active=0 WHERE id=?", (delivery["subscription"],))
                db.execute("UPDATE deliveries SET state='cancelled' WHERE state='pending' AND subscription=?", (delivery["subscription"],))

    def set_meta(self, key: str, value: str):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
