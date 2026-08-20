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
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

KST = ZoneInfo("Asia/Seoul")  # "하루"의 경계는 장이 도는 시간대 기준이어야 한다 (pipeline._kst_today와 같은 이유)

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
    "order_response_lost": "주문응답유실",
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


def format_buy_decision_alert(day: str, names: list[str]) -> str:
    """decide_buys.py(08:30)가 판단만 마쳤을 때 보내는 요약 — 0건이어도 보낸다.
    실제 매수 체결 알림(format_buy_alert)과 달리 "판단 단계가 살아서 끝까지
    돌았다"는 것 자체가 목적이라, 승인 0건도 조용히 넘기지 않는다(사용자 요청,
    2026-08-11 — 침묵이 "신호 없음"인지 "cron이 안 돎"인지 구분이 안 되는 문제)."""
    if not names:
        return f"🔎 [SIMA] 매수 판단 완료 ({day})\n승인 0건 — 오늘은 매수 후보 없음"
    return f"🔎 [SIMA] 매수 판단 완료 ({day})\n승인 {len(names)}건: {', '.join(names)}\n집행은 09:00 장 시작 직후"


def format_sell_decision_alert(day: str, names: list[str]) -> str:
    """decide_llm_sell.py(15:35)가 판단만 마쳤을 때 보내는 요약 — 0건이어도
    보낸다. format_buy_decision_alert와 같은 이유(사용자 요청, 2026-08-11)."""
    if not names:
        return f"🔎 [SIMA] 재량 매도 판단 완료 ({day})\n매도 결정 0건 — 보유 종목 유지"
    return (
        f"🔎 [SIMA] 재량 매도 판단 완료 ({day})\n매도 결정 {len(names)}건: {', '.join(names)}"
        "\n집행은 다음 거래일 09:00 장 시작 직후"
    )


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


# 매분 도는 잡(check_stop_loss)이 지속 장애를 만나면 텔레그램이 하루 수백 번
# 울린다 — 하루 첫 발생에만 보내고 나머지는 logs/cron.log로만 남긴다.
# 원래 scripts/check_stop_loss.py 안에 있던 로직을 여기로 올렸다. 이유는 두
# 가지다: (1) "알림을 얼마나 자주 보낼 것인가"는 알림 쪽 관심사고, (2) 원래
# 구현은 마커 파일이 하나뿐이라 사유가 여럿이면 서로를 묻어버렸다 — 그날
# 손절 체크가 한 번 예외로 죽어 알림이 나가면, 그 뒤 장 마감까지 시세가 통째로
# 안 나와도(안전장치가 눈을 감은 상태) 두 번째 알림이 안 나간다. context별로
# 따로 센다.
ALERT_MARKER_DIR = Path("logs/alert_markers")


def alert_once_per_day(context_key: str, message: str, today: date | None = None) -> bool:
    """오늘 이 context_key로 아직 안 보냈으면 보내고 True. 이미 보냈으면 False.

    마커 쓰기가 실패해도 알림 자체는 이미 나갔으므로 True를 돌려준다 — 다음
    호출에서 한 번 더 울릴 뿐이고, 그게 알림을 통째로 잃는 것보다 낫다.
    """
    today = today or datetime.now(KST).date()
    marker = ALERT_MARKER_DIR / f"{context_key}.txt"
    try:
        if marker.exists() and marker.read_text().strip() == today.isoformat():
            return False
    except OSError:
        pass  # 마커를 못 읽으면 "아직 안 보냈다"로 본다 — 알림은 놓치는 쪽이 더 위험하다

    send_telegram_alert(message)

    try:
        ALERT_MARKER_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(today.isoformat())
    except OSError as exc:
        logger.warning("alert_marker_write_failed context=%s error=%s", context_key, exc)
    return True
