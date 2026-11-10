"""KRX(한국거래소) 거래일 판정.

cron이 매일 같은 시각에 돌지만 주말·공휴일엔 애초에 시장이 안 열려서 정량 필터도,
LLM 분석가도, 매수/매도 판단도 전부 낭비 호출이 된다 — 규칙 4(재시도는 데이터 수집
실패에만)와는 다른 문제지만 같은 정신: 성공해도 의미 없는 실행을 굳이 돌리지 않는다.

KRX 실시간 시세로 "오늘 거래일인지" 역산하는 방법도 검토했지만 버렸다 — cron이
09:10 KST에 도는데, 그 시각에 당일 일봉이 이미 채워져 있는지 타이밍이 불확실해서
판정 자체가 불안정해진다. 대신 KRX가 연초에 미리 공표하는 휴장일을 정적으로
박아둔다. 이 값은 매년 갱신이 필요하지만("결과가 마음에 안 들어서 재시도"와 달리)
알려진 스케줄을 옮겨적는 작업이라 AGENTS.md가 경계하는 종류의 우회가 아니다.

출처: 2026년 KRX 개장일/휴장일 공지 (요일 계산으로 대체공휴일 적용 여부 교차검증함,
2026-08-09). 매년 초 다음 해 일정으로 갱신할 것.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
SUPPORTED_YEARS = frozenset({2026})

KRX_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),  # 신정
        date(2026, 2, 16),  # 설날 연휴
        date(2026, 2, 17),  # 설날
        date(2026, 2, 18),  # 설날 연휴
        date(2026, 3, 2),  # 삼일절 대체공휴일 (3/1이 일요일)
        date(2026, 5, 1),  # 근로자의 날
        date(2026, 5, 5),  # 어린이날
        date(2026, 5, 25),  # 부처님오신날 대체공휴일 (5/24가 일요일)
        date(2026, 6, 3),  # 전국동시지방선거 (KRX 2026-05-20 공지)
        date(2026, 7, 17),  # 제헌절 (동일 KRX 공지)
        date(2026, 8, 17),  # 광복절 대체공휴일 (8/15가 토요일)
        date(2026, 9, 24),  # 추석 연휴
        date(2026, 9, 25),  # 추석
        date(2026, 10, 5),  # 개천절 대체공휴일 (10/3이 토요일)
        date(2026, 10, 9),  # 한글날
        date(2026, 12, 25),  # 성탄절
        date(2026, 12, 31),  # 연말 휴장일
    }
)


def is_krx_trading_day(day: date) -> bool:
    """주말이거나 KRX 휴장일이면 False. 그 외엔 True — 장중 임시 휴장 등은 못 잡는다."""
    if day.weekday() >= 5:  # 5=토, 6=일
        return False
    if day.year != 2026:
        raise ValueError(
            f"KRX_HOLIDAYS_{day.year}가 없다 — 새해 일정으로 market_calendar.py를 갱신할 것"
        )
    return day not in KRX_HOLIDAYS_2026


@dataclass(frozen=True)
class MarketSession:
    open: datetime
    close: datetime

    @property
    def valuation(self):
        return self.close + timedelta(minutes=5)


def market_session(day: date) -> MarketSession | None:
    """Unknown calendars raise; callers must not assume an ordinary session."""
    if day.year not in SUPPORTED_YEARS:
        raise ValueError(f"KRX calendar unavailable for {day.year}")
    if not is_krx_trading_day(day):
        return None
    # KRX first session / CSAT special sessions. Sources in audit fix report.
    opening = time(10) if day in {date(2026, 1, 2), date(2026, 11, 19)} else time(9)
    closing = time(16, 30) if day == date(2026, 11, 19) else time(15, 30)
    return MarketSession(datetime.combine(day, opening, KST), datetime.combine(day, closing, KST))


def previous_session_day(day: date) -> date:
    day -= timedelta(days=1)
    while market_session(day) is None:
        day -= timedelta(days=1)
    return day


def session_clock(day: date, hour: int, minute: int = 0) -> time:
    """Map a regular-session schedule to this day's announced session.

    Morning jobs retain their offset from open; afternoon jobs from close.
    Holidays retain wall time, while unknown calendars fail closed.
    """
    session = market_session(day)
    if session is None:
        return time(hour, minute)
    anchor = session.open if hour < 12 else session.close
    normal = 9 * 60 if hour < 12 else 15 * 60 + 30
    return (anchor + timedelta(minutes=hour * 60 + minute - normal)).time()


def deployment_blocked(at: datetime) -> bool:
    at = at.astimezone(KST)
    try:
        session = market_session(at.date())
    except ValueError:
        # Allow calendar repair overnight, without assuming an unknown trading day.
        return at.weekday() < 5 and time(6, 50) <= at.time() < time(18)
    return bool(session and time(6, 50) <= at.time()
                and at < session.close + timedelta(minutes=40))


def monitor_due(at: datetime) -> datetime | None:
    """Last minute that should have completed, including the final valuation.

    Remains due overnight/weekends, so a missed close cannot vanish from the UI.
    """
    at = at.astimezone(KST)
    session = market_session(at.date())
    if session and at >= session.open:
        due = session.valuation if at >= session.valuation else min(at, session.close)
        return due.replace(second=0, microsecond=0)
    return market_session(previous_session_day(at.date())).valuation
