"""매수·매도·오류를 SSH 없이 바로 알기 위한 텔레그램 알림.

CLAUDE.md의 재시도 규칙(규칙 4)과는 무관한 영역이다 — 이건 실패를 감추거나
우회하는 코드가 아니라 이미 일어난 일을 사람에게 알리는 코드라 재시도를 하지
않는다. 알림 전송 자체가 실패해도 예외를 삼킨다 — 알림 실패 때문에 원래 이벤트의
로그(logs/cron.log)가 안 남으면 SSH로 원인 파악할 방법마저 사라진다.

메시지가 하루에도 여러 건 나갈 수 있어(매수/매도/스킵/오류) 전부 같은 뼈대로
통일했다(사용자 요청, 2026-08-09): 이모지+[SIMA]+종류 한 줄 → 종목·핵심수치
한 줄 → 있으면 사유. `format_*` 함수들이 이 뼈대를 강제하고, 호출부는 절대
직접 문자열을 조립하지 않는다 — 그래야 나중에 양식을 한 곳만 고치면 전체가
같이 바뀐다.
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# notion_sync.REASON_LABELS와 값은 같지만 독립적으로 든다 — 이 파일과 notion_sync.py는
# "노션에 뭘 쓰나"/"텔레그램에 뭘 보내나"로 고치는 이유가 다르다.
REASON_LABELS = {
    "stop_loss": "손절",
    "take_profit_trail": "익절",
    "llm_discretionary": "LLM재량매도",
    "price_data_unavailable": "시세조회실패",
    "gap_too_large": "갭초과",
    "balance_unavailable": "잔고조회실패",
    "quantity_zero": "수량0",
    "order_rejected": "주문거부",
}


def _truncate(text: str | None, limit: int = 200) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


def format_buy_alert(ticker: str, price: float, quantity: int, reason: str) -> str:
    return f"🟢 [SIMA] 매수\n{ticker} · {price:,.0f}원 · {quantity}주\n사유: {_truncate(reason)}"


def format_buy_skipped_alert(ticker: str, reason_label: str, detail: str = "") -> str:
    suffix = f" ({detail})" if detail else ""
    return f"⛔ [SIMA] 매수 스킵\n{ticker} · 사유: {reason_label}{suffix}\n게이트는 승인했지만 체결 직전에 스킵됨"


def format_sell_alert(
    ticker: str, reason_label: str, price: float, realized_pnl_pct: float | None, reasoning: str | None = None
) -> str:
    pnl_text = f"{realized_pnl_pct:+.1%}" if realized_pnl_pct is not None else "-"
    lines = [f"🔴 [SIMA] 매도 ({reason_label})", f"{ticker} · {price:,.0f}원 · 실현손익 {pnl_text}"]
    if reasoning:
        lines.append(f"사유: {_truncate(reasoning)}")
    return "\n".join(lines)


def format_error_alert(context: str, error: str) -> str:
    return f"⚠️ [SIMA] 오류 — {context}\n{_truncate(error, 500)}"


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
