"""한국투자증권(KIS) Open API 클라이언트 — 시세 조회 + 모의투자 주문 집행.

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

실측으로 확인한 것 (2026-08-09, 주문 관련):
- 계좌번호(CANO)는 .env의 KIS_ACCOUNT_NO(8자리), 상품코드(ACNT_PRDT_CD)는 "01"
  — 실제 주문·잔고조회 API로 검증됨(정상 응답, "01"이 아니면 계좌 인증 자체가
  거부됐을 것).
- 잔고조회(inquire-balance)는 라이브로 완전히 검증됨 — 총평가금액
  tot_evlu_amt가 사용자가 설정한 초기자금 1억(100000000)과 정확히 일치.
- **주문 접수(order-cash)는 구조만 검증됐다** — 계좌·파라미터는 정상 처리됐지만
  응답이 "모의투자 영업일이 아닙니다"(장이 닫혀 있음)라 실제 체결까지는 못
  봤다. 체결가 조회(inquire-daily-ccld)의 output1(주문별 체결 내역) 필드명은
  실제 체결 건이 없어 확인 못 했고, 대신 output2(집계, pchs_avg_pric 등 필드명
  확인됨)로 우회한다. 장중에 실제 매수가 한 번 나가면 이 부분을 재검증할 것.
"""

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
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
CURRENT_PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"
CURRENT_PRICE_TR_ID = "FHKST01010100"
ORDER_CASH_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
ORDER_BUY_TR_ID = "VTTC0802U"  # 모의투자 현금 매수 주문
DAILY_CCLD_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
DAILY_CCLD_TR_ID = "VTTC8001R"  # 모의투자 주식일별주문체결조회
BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
BALANCE_TR_ID = "VTTC8434R"  # 모의투자 주식잔고조회
ACCOUNT_PRODUCT_CODE = "01"  # 실측으로 확인됨 — 계좌번호 뒤 2자리

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


def _kis_request(
    method: str, path: str, tr_id: str, *, params: dict | None = None, body: dict | None = None
) -> dict | None:
    """공통 KIS 요청(GET/POST). 초당 거래건수 제한(EGW00201)만 재시도 대상이다.

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
    if body is not None:
        headers["custtype"] = "P"

    last_error: Exception | str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            if method == "GET":
                response = requests.get(
                    f"{BASE_URL}{path}", headers=headers, params=params, timeout=REQUEST_TIMEOUT_SECONDS
                )
            else:
                response = requests.post(
                    f"{BASE_URL}{path}", headers=headers, data=json.dumps(body), timeout=REQUEST_TIMEOUT_SECONDS
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


def _kis_get(path: str, tr_id: str, params: dict) -> dict | None:
    return _kis_request("GET", path, tr_id, params=params)


def _kis_post(path: str, tr_id: str, body: dict) -> dict | None:
    return _kis_request("POST", path, tr_id, body=body)


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


def fetch_current_price(ticker: str) -> float | None:
    """실시간 현재가. 갭 체크(장 시작가 vs 전일 종가)와 주문 수량 계산에 쓴다."""
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
    data = _kis_get(CURRENT_PRICE_PATH, CURRENT_PRICE_TR_ID, params)
    if data is None:
        return None

    price_str = (data.get("output") or {}).get("stck_prpr")
    if not price_str:
        return None
    return float(price_str)


def fetch_account_balance() -> float | None:
    """계좌 총평가금액(tot_evlu_amt). 비중(weight) 기반 주문 수량을 절대
    원화·주수로 환산하려면 이 값이 필요하다 — PortfolioState는 비중만 들고
    있고 절대 금액은 추적하지 않으므로, 매번 브로커(KIS)를 실제 잔고의
    출처로 삼는다(자체 상태와 동기화 문제를 피하기 위해)."""
    params = {
        "CANO": os.getenv("KIS_ACCOUNT_NO", ""),
        "ACNT_PRDT_CD": ACCOUNT_PRODUCT_CODE,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    data = _kis_get(BALANCE_PATH, BALANCE_TR_ID, params)
    if data is None:
        return None

    rows = data.get("output2") or []
    if not rows:
        return None
    amount_str = rows[0].get("tot_evlu_amt")
    if not amount_str:
        return None
    return float(amount_str)


def place_market_buy_order(ticker: str, quantity: int) -> str | None:
    """모의투자 계좌로 시장가 매수 주문을 접수한다. 성공하면 주문번호(ODNO)를
    반환한다 — 주문 접수와 체결은 별개 이벤트라 체결가는 fetch_fill_price로
    따로 조회해야 한다.

    실거래시간 종단 검증은 아직 못 했다(2026-08-09, 장이 닫혀 있어 "모의투자
    영업일이 아닙니다" 응답만 확인) — 계좌·파라미터 형태는 정상 처리됐다.
    """
    if quantity <= 0:
        return None

    body = {
        "CANO": os.getenv("KIS_ACCOUNT_NO", ""),
        "ACNT_PRDT_CD": ACCOUNT_PRODUCT_CODE,
        "PDNO": ticker,
        "ORD_DVSN": "01",  # 시장가
        "ORD_QTY": str(quantity),
        "ORD_UNPR": "0",
    }
    data = _kis_post(ORDER_CASH_PATH, ORDER_BUY_TR_ID, body)
    if data is None:
        return None

    return (data.get("output") or {}).get("ODNO")


def fetch_fill_price(ticker: str, order_date: date) -> float | None:
    """그날 그 종목의 매수 평균 체결가. inquire-daily-ccld의 output2(집계)에서
    구매평균가격(pchs_avg_pric)을 쓴다 — 종목·날짜를 좁혀서 조회하므로 이 주문
    하나의 평균 체결가와 사실상 같다(같은 날 같은 종목을 두 번 매수하지 않는 한).

    output1(주문별 체결 내역)의 필드명은 아직 실측으로 확인 못 했다(2026-08-09,
    실제 체결 건이 없어서) — 그래서 필드명이 이미 확인된 output2 집계 쪽을 쓴다.
    """
    date_str = order_date.strftime("%Y%m%d")
    params = {
        "CANO": os.getenv("KIS_ACCOUNT_NO", ""),
        "ACNT_PRDT_CD": ACCOUNT_PRODUCT_CODE,
        "INQR_STRT_DT": date_str,
        "INQR_END_DT": date_str,
        "SLL_BUY_DVSN_CD": "00",
        "INQR_DVSN": "00",
        "PDNO": ticker,
        "CCLD_DVSN": "00",
        "ORD_GNO_BRNO": "",
        "ODNO": "",
        "INQR_DVSN_3": "00",
        "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    data = _kis_get(DAILY_CCLD_PATH, DAILY_CCLD_TR_ID, params)
    if data is None:
        return None

    price_str = (data.get("output2") or {}).get("pchs_avg_pric")
    if not price_str:
        return None
    price = float(price_str)
    return price if price > 0 else None
