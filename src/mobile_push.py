"""독립 Web Push 발송 작업. 재시도는 알림 전달에만 적용한다."""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from pywebpush import WebPushException, webpush

from src.mobile_app import valid_subscription
from src.mobile_store import Store
from src.schemas import AppAlert

logger = logging.getLogger(__name__)


class NoRedirectSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


def import_outbox(store: Store, path: Path) -> int:
    if not path.exists():
        return 0
    stat = path.stat()
    identity = f"{stat.st_dev}:{stat.st_ino}"
    saved = json.loads(store.get_meta("outbox_cursor") or "{}")
    offset = saved.get("offset", 0) if saved.get("identity") == identity else 0
    if offset > stat.st_size:
        offset = 0
    imported = 0
    with path.open("rb") as stream:
        stream.seek(offset)
        for _ in range(1000):
            line = stream.readline(65537)
            if not line or not line.endswith(b"\n"):
                if len(line) > 65536:
                    raise ValueError("app outbox line exceeds limit")
                break
            try:
                alert = AppAlert.model_validate_json(line)
            except ValueError:
                logger.error("invalid_app_outbox_record offset=%d", offset)
                store.set_meta("outbox_error", f"invalid_record_at_{offset}")
            else:
                store.add_alert(alert)
                imported += 1
            offset = stream.tell()
    # add_alert는 id로 멱등이다. 커서 저장 전 종료돼도 같은 알림을 재발송하지 않는다.
    store.set_meta("outbox_cursor", json.dumps({"identity": identity, "offset": offset}))
    return imported


def send_pending(store: Store, private_key: Path, origin: str) -> int:
    delivered = 0
    for item in store.pending():
        code = None
        if time.time() - item["created"] > 86400:
            store.delivery_result(item, code=None)
            continue
        try:
            sub = valid_subscription(json.loads(item["info"]))
            alert = AppAlert.model_validate_json(item["payload"])
            payload = {"id": alert.id, "title": alert.title, "body": alert.body[:220],
                       "url": f"/#alerts/{alert.id}"}
            with NoRedirectSession() as session:
                response = webpush(subscription_info=sub, data=json.dumps(payload, ensure_ascii=False),
                                   vapid_private_key=str(private_key), vapid_claims={"sub": origin},
                                   ttl=86400, timeout=10, requests_session=session,
                                   headers={"Urgency": "high" if alert.kind == "error" else "normal"})
                code = response.status_code
        except WebPushException as exc:
            code = exc.response.status_code if exc.response is not None else None
        except Exception:
            # 예외 문자열에는 비밀인 구독 endpoint가 포함될 수 있어 기록하지 않는다.
            logger.warning("web_push_failed subscription=%s", item["subscription"])
        store.delivery_result(item, code=code)
        if code is not None and 200 <= code < 300:
            delivered += 1
    return delivered


def run_once(store: Store, data_dir: Path, private_key: Path, origin: str):
    import_outbox(store, data_dir / "app_alerts.jsonl")
    send_pending(store, private_key, origin)
    store.set_meta("worker_seen", datetime.now(timezone.utc).isoformat())


def run_worker(store: Store, data_dir: Path, private_key: Path, origin: str):
    while True:
        try:
            run_once(store, data_dir, private_key, origin)
        except Exception:
            logger.exception("app_worker_iteration_failed")
        time.sleep(5)
