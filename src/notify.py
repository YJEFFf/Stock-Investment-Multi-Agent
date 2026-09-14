"""매수·매도·오류를 SSH 없이 바로 알기 위한 텔레그램 알림.

AGENTS.md의 재시도 규칙(규칙 4)과는 무관한 영역이다 — 이건 실패를 감추거나
우회하는 코드가 아니라 이미 일어난 일을 사람에게 알리는 코드라 재시도를 하지
않는다. 알림 전송 자체가 실패해도 예외를 삼킨다 — 알림 실패 때문에 원래 이벤트의
로그(logs/cron.log)가 안 남으면 SSH로 원인 파악할 방법마저 사라진다.

메시지가 하루에도 여러 건 나갈 수 있어(매수/매도/스킵/오류) 전부 같은 뼈대로
통일했다(사용자 요청, 2026-08-09): 이모지+[SIMA]+종류 한 줄 → 종목·핵심수치
한 줄 → 있으면 사유. `format_*` 함수들이 이 뼈대를 강제하고, 호출부는 절대
직접 문자열을 조립하지 않는다 — 그래야 나중에 양식을 한 곳만 고치면 전체가
같이 바뀐다.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

KST = ZoneInfo("Asia/Seoul")  # "하루"의 경계는 장이 도는 시간대 기준이어야 한다 (pipeline._kst_today와 같은 이유)

# notion_sync.REASON_LABELS와 값은 같지만 독립적으로 든다 — 이 파일과 notion_sync.py는
# "노션에 뭘 쓰나"/"텔레그램에 뭘 보내나"로 고치는 이유가 다르다.
# 매도 기록의 exit_trigger(sell.exit_trigger)용. REASON_LABELS의 take_profit_trail("익절")은
# 1차 익절과 트레일링을 가르지 못해 2026-09-14 이전 기록을 읽을 때만 쓴다.
TRIGGER_LABELS = {
    "stop_loss": "손절",
    "take_profit_first": "1차익절",
    "take_profit_trail": "트레일링익절",
    "llm_discretionary": "LLM재량매도",
}

REASON_LABELS = {
    **TRIGGER_LABELS,
    "stop_loss": "손절",
    "take_profit_trail": "익절",
    "llm_discretionary": "LLM재량매도",
    "price_data_unavailable": "시세조회실패",
    "gap_too_large": "갭초과",
    "balance_unavailable": "잔고조회실패",
    "quantity_zero": "수량0",
    "pre_open_quote": "개장전시세(체결없음)",
    "order_rejected": "주문거부",
    "order_response_lost": "주문응답유실",
}


def _truncate(text: str | None, limit: int = 200) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


def format_buy_alert(ticker: str, price: float, quantity: int, reason: str) -> str:
    return f"🟢 [SIMA] 매수\n{ticker} · {price:,.0f}원 · {quantity}주\n사유: {_truncate(reason)}"


def format_ic_alert(day: str, segments: dict) -> str:
    """scripts/measure_ic.py(16:00)의 결과. 숫자가 없으면 없다고 보낸다 — 0으로 채우지 않는다.

    구간마다 한 덩어리다: 전체와, 2026-09-14 차트 프롬프트 변경 이후(사전 등록 가설).
    """
    lines = [f"📐 [SIMA] 신호 IC ({day})"]
    for name, seg in segments.items():
        label = "전체" if seg["since"] is None else f"{seg['since']} 이후"
        lines.append(f"— {label} (판단 {seg['decisions']}건)")
        for k, s in sorted(seg["horizons"].items(), key=lambda kv: int(kv[0])):
            if s["mean_ic"] is None:
                lines.append(f"  k={k}: 측정 가능한 날 없음")
                continue
            t_part = f" t={s['t_stat']:+.2f}" if s["t_stat"] is not None else ""
            lines.append(f"  k={k}: IC {s['mean_ic']:+.3f}{t_part} ({s['days_measured']}일, 양 {s['positive_days']}일)")
        rho = next((s["rho_score_momentum_20d"] for s in seg["horizons"].values()), None)
        if rho is not None:
            lines.append(f"  ρ(점수, 직전20일수익률) {rho:+.2f}")
    lines.append("판단에 되먹이지 않음 · 60거래일 후 실거래 판단(IC≥0.03, t≥2)")
    return "\n".join(lines)


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


def format_blackout_escalation_alert(minutes: float, positions: int) -> str:
    return (
        f"🔴 [SIMA] 시세 공백 지속\n"
        f"{minutes:.0f}분째 보유 {positions}종목 시세 전멸 — 손절/익절 판정이 그동안 한 번도 없었습니다\n"
        f"사유: KIS 시세 조회 연속 실패. 복구되면 총 공백 시간을 다시 알립니다."
    )


def format_blackout_recovery_alert(
    minutes: float, total: int = 0, checked: int = 0, crossed: list[str] | None = None
) -> str:
    """복구 알림. 공백 동안 문턱을 넘은 종목이 있었는지 **일봉으로 사후 판정한
    결과**를 함께 싣는다(사용자 확정, 2026-08-27).

    원래 이 알림은 "확인이 불가능합니다"라고 말했는데, 그건 사실이 아니었다 —
    2026-08-26 장애를 사람이 8/27에 일봉 저가/고가로 확인했고, 그 방법이 그대로
    자동화된다. 장중 어느 시각에 닿았는지는 일봉으로 알 수 없지만 "닿았는가"는
    답이 나오고, 이 알림이 답해야 하는 질문이 정확히 그것이다.

    `checked`를 `total`과 따로 받는 이유: 일봉 조회도 같은 KIS를 쓰므로 복구
    직후에 또 실패할 수 있다. 그때 "문턱 넘은 종목 없음"이라고 말하면 안전하다는
    거짓 확신을 준다 — 몇 종목을 실제로 확인했는지 숫자로 드러낸다.
    """
    lines = [
        "🟡 [SIMA] 시세 공백 종료",
        f"총 {minutes:.0f}분간 손절/익절 판정 없음 — 지금은 시세가 정상입니다",
    ]
    crossed = crossed or []
    if checked == 0:
        lines.append("일봉 대조 실패 — 그 구간에 문턱을 넘은 종목이 있었는지 확인하지 못했습니다.")
        return "\n".join(lines)

    unchecked = f" ({total - checked}종목은 일봉을 못 받아 확인 못 함)" if checked < total else ""
    if crossed:
        lines.append(f"일봉 대조({checked}/{total}종목): ⚠️ {', '.join(crossed)}{unchecked}")
        lines.append("오늘 안에 문턱을 지났습니다 — 장중 시각은 일봉으로 알 수 없으니 다음 회차 판정을 확인하세요.")
    else:
        lines.append(f"일봉 대조({checked}/{total}종목): 문턱에 닿은 종목 없음{unchecked}")
    return "\n".join(lines)


def format_blackout_unresolved_alert(minutes: float) -> str:
    return (
        f"🔴 [SIMA] 시세 공백인 채로 장 마감\n"
        f"마감까지 {minutes:.0f}분간 손절/익절 판정 없음 — 복구를 못 보고 끝났습니다\n"
        f"사유: 장 마감 직전 구간은 KIS 모의투자 지연이 가장 심한 시간대입니다."
    )


def format_buy_decision_alert(day: str, names: list[str]) -> str:
    """decide_buys.py(08:30)가 판단만 마쳤을 때 보내는 요약 — 0건이어도 보낸다.
    실제 매수 체결 알림(format_buy_alert)과 달리 "판단 단계가 살아서 끝까지
    돌았다"는 것 자체가 목적이라, 승인 0건도 조용히 넘기지 않는다(사용자 요청,
    2026-08-11 — 침묵이 "신호 없음"인지 "cron이 안 돎"인지 구분이 안 되는 문제)."""
    if not names:
        return f"🔎 [SIMA] 매수 판단 완료 ({day})\n승인 0건 — 오늘은 매수 후보 없음"
    return f"🔎 [SIMA] 매수 판단 완료 ({day})\n승인 {len(names)}건: {', '.join(names)}\n집행은 09:00 장 시작 직후"


def format_gate_headroom_broken_alert(headroom: dict) -> str:
    return (
        "🔴 [SIMA] 게이트 점검 실패\n"
        f"가상 신규 매수를 {headroom['new_buys_until_reject']}건 넣어도 총노출 한도가 거부하지 않았습니다 "
        f"(거부 룰: {headroom['rejected_by']}). 게이트 경로나 설정을 확인하세요.\n"
        "주문은 내지 않은 점검입니다."
    )


def format_gate_zero_rejection_alert(days: int, headroom: dict) -> str:
    addon = (
        f"\n보유 최대 종목 추가매수 시: {headroom['addon_rejected_by'] or '승인'}"
        if headroom.get("addon_ticker")
        else ""
    )
    return (
        f"🧱 [SIMA] 게이트 주간 점검\n최근 {days}거래일 게이트 거부 0건\n"
        f"지금 투자비중 {headroom['invested_weight']:.1%} — 신규 매수 {headroom['new_buys_until_reject']}건 뒤 "
        f"{headroom['rejected_by']} 한도가 거부합니다(가상 점검, 주문 없음){addon}"
    )


def format_blind_minutes_budget_alert(day: str, blind: dict, budget: int) -> str:
    return (
        f"🟠 [SIMA] 안전장치 공백 예산 초과 ({day})\n"
        f"오늘 09:00~15:30 중 손절·익절 판정이 없던 분: {blind['blind']}분 / 예산 {budget}분\n"
        f"최장 연속 {blind['longest_run']}분. 짧은 공백이 쌓인 날도 잡으려는 알림입니다."
    )


def format_buy_decision_skipped_alert(day: str) -> str:
    """Claude API 스위치가 꺼져 매수 판단을 건너뛴 날. 조용히 넘기지 않는다 — 스위치를
    켜는 걸 잊으면 매수 판단이 소리 없이 영영 멈춘다."""
    return (
        f"⏸ [SIMA] 매수 판단 건너뜀 ({day})\n"
        "Claude API 정지 중(llm.CLAUDE_API_ENABLED=False) — 신규 매수 없음\n"
        "보유 종목 손절·익절 감시와 IC 측정은 정상 동작"
    )


def format_sell_decision_alert(day: str, names: list[str]) -> str:
    """decide_llm_sell.py(15:35)가 판단만 마쳤을 때 보내는 요약 — 0건이어도
    보낸다. format_buy_decision_alert와 같은 이유(사용자 요청, 2026-08-11)."""
    if not names:
        return f"🔎 [SIMA] 재량 매도 판단 완료 ({day})\n매도 결정 0건 — 보유 종목 유지"
    return (
        f"🔎 [SIMA] 재량 매도 판단 완료 ({day})\n매도 결정 {len(names)}건: {', '.join(names)}"
        "\n집행은 다음 거래일 09:00 장 시작 직후"
    )


def format_reconcile_drift_alert(day: str, drifts: list) -> str:
    """장 마감 후 브로커 대조에서 어긋난 곳이 나왔을 때. **어긋남이 있을 때만 보낸다.**

    매수·매도 판단 알림(format_*_decision_alert)과 달리 "이상 없음"은 안 보낸다 —
    저쪽은 크론이 돌았는지 자체가 궁금해서 0건도 보내지만, 이 대조는 이상 없는 날이
    정상이자 대다수라 매일 보내면 알림이 배경 소음이 되고 진짜 드리프트가 묻힌다.
    크론이 살아있는지는 08:30·15:35 알림이 이미 답한다.

    교정은 하지 않는다(드라이런). 진입가는 손절·익절 판정의 유일한 기준점이라
    상태 파일을 코드가 조용히 고치면 그 판정이 어느 값 기준이었는지 되짚기 어렵다 —
    사람이 보고 `--apply`를 결정한다.
    """
    lines = [f"🟠 [SIMA] 브로커 대조 — 어긋난 곳 {len(drifts)}건 ({day})"]
    for d in drifts:
        if d.field == "missing_at_broker":
            lines.append(f"⚠️ {d.ticker}: 상태 파일엔 있는데 브로커 잔고엔 없음")
        elif d.field == "missing_locally":
            lines.append(f"⚠️ {d.ticker}: 브로커는 {d.broker:.0f}주 보유 중인데 상태 파일엔 없음")
        else:
            diff = d.relative_diff
            diff_str = f" ({diff:+.2%})" if diff is not None else ""
            lines.append(f"• {d.ticker} {d.field}: {d.local} → {d.broker}{diff_str}")
    lines.append("교정 안 함(드라이런). 필요하면 scripts/reconcile_portfolio.py --apply — 장중 금지.")
    return "\n".join(lines)


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
        # 성공도 남긴다. 실패만 로그가 있으면 "안 보냈다"와 "보냈는데 기록이 없다"가
        # 로그에서 같은 모양이라, 2026-08-26 장애 때 알림 도착 여부를 사용자에게
        # 물어서야 확인할 수 있었다(CHANGELOG 2026-08-27). 본문 첫 줄만 남긴다 —
        # 어떤 알림인지 식별하는 데 그거면 충분하고, 전문은 길다.
        logger.info("telegram_alert_sent title=%s", message.splitlines()[0] if message else "")
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


def alert_once(context_key: str, message: str) -> bool:
    """이 context_key로 **한 번도** 안 보냈으면 보내고 True. 주 1회 같은 주기는 키에 주차를
    넣어 만든다(alert_once_per_day는 마커의 날짜를 비교하므로 매일 다시 울린다)."""
    marker = ALERT_MARKER_DIR / f"{context_key}.once.txt"
    if marker.exists():
        return False
    if not send_telegram_alert(message):
        return False  # 못 보냈으면 마커를 안 남긴다 — 다음에 다시 시도한다
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now(KST).isoformat())
    except OSError as exc:
        logger.warning("alert_once_marker_write_failed key=%s error=%s", context_key, exc)
    return True


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


# --- 공백(전멸) 구간 추적 ---
#
# alert_once_per_day만으로는 "장애가 시작됐다"까지만 알 수 있고 "얼마나 길어지고
# 있나"는 못 알린다. 2026-08-21이 정확히 그랬다: 13:02에 하루치 알림을 써버려서
# 15:07~15:29의 23분 공백(장 마감 직전, 안전장치가 통째로 눈을 감은 구간)이
# 무알림으로 지나갔다. 같은 날 오전에 이미 울렸다는 이유로 더 심한 오후를 못 알리는
# 건 알림이 아니라 소음 억제일 뿐이다.
#
# 그래서 여기서는 연속 구간을 상태 파일로 들고 간다. 매분 새 프로세스가 뜨는
# 구조(check_stop_loss)라 메모리에 둘 수 없다.
BLACKOUT_STATE_DIR = Path("logs/alert_markers")
BLACKOUT_ESCALATE_AFTER = timedelta(minutes=5)


@dataclass(frozen=True)
class BlackoutEvent:
    """알릴 만한 일이 생겼을 때만 나온다. 조용한 회차는 None이다."""

    kind: Literal["escalated", "recovered"]
    minutes: float


def _blackout_state_path(context_key: str, state_dir: Path | None = None) -> Path:
    return (state_dir or BLACKOUT_STATE_DIR) / f"{context_key}.blackout.json"


def _read_blackout(path: Path, now: datetime) -> tuple[datetime, bool] | None:
    try:
        raw = json.loads(path.read_text())
        since = datetime.fromisoformat(raw["since"])
    except (OSError, ValueError, KeyError):
        return None
    # 날짜가 넘어갔으면 이어붙이지 않는다 — 장 마감 시점에 공백인 채로 끝나면
    # 파일이 남는데, 그걸 다음 날 아침에 이어 세면 "밤새 15시간 공백"이 된다.
    if since.date() != now.date():
        return None
    return since, bool(raw.get("escalated"))


def track_blackout(
    context_key: str,
    blind: bool,
    *,
    now: datetime | None = None,
    state_dir: Path | None = None,
    escalate_after: timedelta = BLACKOUT_ESCALATE_AFTER,
) -> BlackoutEvent | None:
    """매 회차 호출한다. blind=True면 이번 회차가 아무것도 관측 못 했다는 뜻.

    돌려주는 것은 "지금 알려야 할 일"뿐이다:
      - 공백이 escalate_after를 넘긴 첫 회차 -> BlackoutEvent("escalated")
      - 알림까지 갔던 공백이 끝난 회차       -> BlackoutEvent("recovered")
      - 그 외(공백 시작, 짧은 공백, 평상시)  -> None

    짧은 공백에 복구 알림을 안 보내는 이유: 1~2분짜리 blip은 정상 장애 범위라
    (kis 재시도가 흡수한다) 매번 울리면 그 알림을 안 보게 된다. 시작 알림은
    기존대로 alert_once_per_day가 하루 한 번 맡는다 — 여기서 다시 보내지 않는다.
    """
    now = now or datetime.now(KST)
    path = _blackout_state_path(context_key, state_dir)
    state = _read_blackout(path, now)

    if blind:
        if state is None:
            _write_blackout(path, now, escalated=False)
            return None
        since, escalated = state
        elapsed = now - since
        if not escalated and elapsed >= escalate_after:
            _write_blackout(path, since, escalated=True)
            return BlackoutEvent("escalated", elapsed.total_seconds() / 60)
        return None

    if state is None:
        return None
    since, escalated = state
    _clear_blackout(path)
    if not escalated:
        return None
    # 여기서 재는 길이는 "공백 시작 ~ 시세가 돌아온 회차"라 실제보다 최대 한 회차
    # 길다. 사람이 로그를 찾아볼 구간을 좁혀주는 게 목적이라 이 정도면 충분하다.
    return BlackoutEvent("recovered", (now - since).total_seconds() / 60)


def close_blackout(context_key: str, *, now: datetime | None = None, state_dir: Path | None = None) -> float | None:
    """열려 있는 공백 구간을 강제로 닫고 그 길이(분)를 돌려준다. 없으면 None.

    장 마감 후에 한 번 부른다(scripts/decide_llm_sell.py). track_blackout의 복구
    알림은 "시세가 돌아온 회차"에서만 나오는데, 2026-08-21처럼 공백인 채로 장이
    끝나면 그 회차가 영영 안 온다 — 하필 가장 심한 공백이 마감 직전에 몰리므로
    이 경로가 없으면 최악의 사례만 조용히 지나간다.
    """
    now = now or datetime.now(KST)
    path = _blackout_state_path(context_key, state_dir)
    state = _read_blackout(path, now)
    _clear_blackout(path)
    if state is None:
        return None
    since, _escalated = state
    return (now - since).total_seconds() / 60


def _write_blackout(path: Path, since: datetime, *, escalated: bool) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"since": since.isoformat(), "escalated": escalated}))
    except OSError as exc:
        # 못 써도 다음 회차가 새 구간으로 다시 시작할 뿐이다 — 알림이 늦어지지
        # 알림 자체가 사라지진 않는다.
        logger.warning("blackout_state_write_failed path=%s error=%s", path, exc)


def _clear_blackout(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("blackout_state_clear_failed path=%s error=%s", path, exc)
