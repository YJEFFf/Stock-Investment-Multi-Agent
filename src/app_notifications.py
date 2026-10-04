"""앱 알림 발송함. 매매 프로세스에서는 파일 저장만 하고 네트워크 전송하지 않는다."""

import fcntl
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.schemas import AppAlert

DEFAULT_APP_ALERTS_PATH = Path("logs/app_alerts.jsonl")
logger = logging.getLogger(__name__)


def make_alert(message: str) -> AppAlert:
    lines = message.strip().splitlines() or ["SIMA 알림"]
    title = lines[0].split("[SIMA]", 1)[-1].strip()[:240]
    if any(word in title for word in ("오류", "실패", "어긋", "미확인", "공백")):
        kind = "error"
    elif "판단" in title:
        kind = "analysis"
    elif any(word in title for word in ("스킵", "중지", "건너뜀")):
        kind = "warning"
    elif "매수" in title:
        kind = "buy"
    elif "매도" in title:
        kind = "sell"
    elif "IC" in title:
        kind = "measurement"
    elif any(word in title for word in ("마감", "일일", "요약")):
        kind = "summary"
    else:
        kind = "info"
    return AppAlert(id=uuid.uuid4().hex, created_at=datetime.now(ZoneInfo("Asia/Seoul")),
                    title=title, body="\n".join(lines[1:])[:6000], kind=kind)


def enqueue(message: str) -> bool:
    if os.environ.get("SIMA_APP_ALERTS_ENABLED") != "1":
        return False
    try:
        payload = (make_alert(message).model_dump_json() + "\n").encode()
        DEFAULT_APP_ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(DEFAULT_APP_ALERTS_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            # 다른 기록자가 정지돼도 매매 호출자와 기존 Telegram을 기다리게 하지 않는다.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("incomplete app alert write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except Exception:
        logger.exception("app_alert_enqueue_failed")
        return False
