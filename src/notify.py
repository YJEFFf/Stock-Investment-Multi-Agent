"""cron 실행 실패를 SSH 없이 바로 알기 위한 텔레그램 알림.

CLAUDE.md의 재시도 규칙(규칙 4)과는 무관한 영역이다 — 이건 실패를 감추거나
우회하는 코드가 아니라 이미 실패한 뒤에 사람에게 알리는 코드라 재시도를 하지
않는다. 알림 전송 자체가 실패해도 예외를 삼킨다 — 알림 실패 때문에 원래 실패의
로그(logs/cron.log)가 안 남으면 SSH로 원인 파악할 방법마저 사라진다.
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram_alert(message: str) -> bool:
    """성공하면 True, 토큰/chat_id 미설정이거나 전송 실패면 False (예외를 던지지 않는다)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("telegram_alert_skipped reason=missing_token_or_chat_id")
        return False

    try:
        response = requests.post(
            TELEGRAM_API_URL.format(token=token),
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except Exception:
        logger.exception("telegram_alert_failed")
        return False
