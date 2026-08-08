"""한국투자증권(KIS) Open API 클라이언트 — 시세 데이터 수집 전용, 모의투자만.

collectors.py(Naver 스크래핑)와 분리한 이유: 인증 토큰 수명 관리·초당 거래건수
제한 대응은 HTML 파싱과 완전히 다른 종류의 문제라 고치는 이유가 다르다
(CLAUDE.md "쪼개는 시점" 원칙).

CLAUDE.md 규칙 7: 모의투자 도메인(openapivts)만 호출한다. 실전투자 도메인
(openapi.koreainvestment.com)은 이 파일 어디에도 등장하지 않는다.

실측으로 확인한 것 (2026-08-08, 모의투자 계좌):
- 토큰 발급은 1분당 1회로 제한된다 (EGW00133) — 그래서 파일 캐시가 필요하다.
- 시세 조회는 초당 거래건수 제한이 있고, 걸리면 HTTP 500 + rt_cd="1" +
  msg_cd="EGW00201"로 응답한다 (msg1 텍스트가 아니라 msg_cd로 판별하는 게 안전).
- inquire-daily-itemchartprice는 조회 기간을 얼마나 넓게 잡아도 최근 100
  거래일로 캡핑된다.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from src.schemas import OHLCVBar

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://openapivts.koreainvestment.com:29443"  # 모의투자 전용 도메인
TOKEN_PATH = "/oauth2/tokenP"
DAILY_CHART_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
DAILY_CHART_TR_ID = "FHKST03010100"

TOKEN_CACHE_PATH = Path(__file__).resolve().parent.parent / ".kis_token_cache.json"

REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 3
RATE_LIMIT_BACKOFF_SECONDS = 1.5
MIN_REQUEST_SPACING_SECONDS = 1.0  # 실측: 0.3초 간격은 부족했다
RATE_LIMIT_MSG_CODE = "EGW00201"
TOKEN_EXPIRY_SAFETY_MARGIN = timedelta(minutes=5)

_token_lock = threading.Lock()
_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _read_token_cache() -> dict | None:
    try:
        return json.loads(TOKEN_CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_token_cache(token: str, expires_at: datetime) -> None:
    try:
        TOKEN_CACHE_PATH.write_text(json.dumps({"access_token": token, "expires_at": expires_at.isoformat()}))
    except OSError as exc:
        logger.warning("kis_token_cache_write_failed error=%s", exc)


def get_access_token() -> str | None:
    """토큰을 파일에 캐시해 1분당 1회 발급 제한을 피한다.

    프로세스가 새로 뜰 때마다(예: 매일 파이프라인 실행, 반복 테스트) 캐시가 아직
    유효하면 재발급 없이 재사용한다.
    """
    with _token_lock:
        cached = _read_token_cache()
        if cached:
            try:
                expires_at = datetime.fromisoformat(cached["expires_at"])
                if datetime.now(timezone.utc) < expires_at - TOKEN_EXPIRY_SAFETY_MARGIN:
                    return cached["access_token"]
            except (KeyError, ValueError):
                pass

        try:
            response = requests.post(
                f"{BASE_URL}{TOKEN_PATH}",
                json={
                    "grant_type": "client_credentials",
                    "appkey": os.getenv("KIS_APP_KEY", ""),
                    "appsecret": os.getenv("KIS_APP_SECRET", ""),
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("kis_token_fetch_failed error=%s", exc)
            return None

        token = data.get("access_token")
        if not token:
            logger.error("kis_token_fetch_failed response=%s", data)
            return None

        expires_in = int(data.get("expires_in") or 86400)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        _write_token_cache(token, expires_at)
        return token


def _throttle() -> None:
    global _last_request_at
    with _throttle_lock:
        wait = _last_request_at + MIN_REQUEST_SPACING_SECONDS - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _kis_get(path: str, tr_id: str, params: dict) -> dict | None:
    """공통 KIS GET 요청. 초당 거래건수 제한(EGW00201)만 재시도 대상이다.

    네트워크 예외도 재시도 대상(규칙 4)이지만, 그 외 API 비즈니스 오류
    (rt_cd != "0", 예: 잘못된 종목코드)는 재시도해도 같은 응답이 나오므로
    즉시 실패로 처리한다 — "결과가 마음에 안 들어서 재시도"와 구분되는,
    "재시도해도 바뀌지 않는 실패"다.
    """
    token = get_access_token()
    if token is None:
        return None

    headers = {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": os.getenv("KIS_APP_KEY", ""),
        "appsecret": os.getenv("KIS_APP_SECRET", ""),
        "tr_id": tr_id,
    }

    last_error: Exception | str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            response = requests.get(
                f"{BASE_URL}{path}", headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
            )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning("kis_fetch_failed path=%s attempt=%d error=%s", path, attempt, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS * attempt)
            continue

        if data.get("msg_cd") == RATE_LIMIT_MSG_CODE:
            last_error = "rate_limited"
            logger.warning("kis_rate_limited path=%s attempt=%d", path, attempt)
            if attempt < MAX_RETRIES:
                time.sleep(RATE_LIMIT_BACKOFF_SECONDS * attempt)
            continue

        if data.get("rt_cd") != "0":
            logger.error("kis_api_error path=%s msg=%s", path, data.get("msg1"))
            return None

        return data

    logger.error("kis_fetch_exhausted path=%s error=%s", path, last_error)
    return None


def _parse_daily_chart(data: dict) -> list[OHLCVBar]:
    bars = []
    for row in data.get("output2") or []:
        date_str = row.get("stck_bsop_date")
        if not date_str:
            continue
        bars.append(
            OHLCVBar(
                date=datetime.strptime(date_str, "%Y%m%d").date(),
                open=float(row["stck_oprc"]),
                high=float(row["stck_hgpr"]),
                low=float(row["stck_lwpr"]),
                close=float(row["stck_clpr"]),
                volume=int(row["acml_vol"]),
            )
        )
    bars.sort(key=lambda b: b.date)
    return bars


def fetch_daily_ohlcv(ticker: str, lookback_days: int = 60) -> list[OHLCVBar] | None:
    """KIS 일별시세 API에서 최근 lookback_days 거래일치 일봉을 가져온다.

    조회 기간을 얼마나 넓게 잡아도 KIS가 최근 100거래일로 캡핑하는 걸 실측으로
    확인했다 — 그래서 넉넉한 고정폭 창을 요청하고 뒤에서 lookback_days만큼 자른다.
    """
    end_date = datetime.now(timezone.utc).date()
    begin_date = end_date - timedelta(days=400)

    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": ticker,
        "FID_INPUT_DATE_1": begin_date.strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D",
        "FID_ORG_ADJ_PRC": "1",
    }

    data = _kis_get(DAILY_CHART_PATH, DAILY_CHART_TR_ID, params)
    if data is None:
        return None

    bars = _parse_daily_chart(data)
    if not bars:
        return None
    return bars[-lookback_days:]
