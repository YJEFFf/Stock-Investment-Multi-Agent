"""한국투자증권(KIS) Open API 클라이언트 — 시세 조회 + 모의투자 주문 집행.

collectors.py(Naver 스크래핑)와 분리한 이유: 인증 토큰 수명 관리·초당 거래건수
제한 대응은 HTML 파싱과 완전히 다른 종류의 문제라 고치는 이유가 다르다
(CLAUDE.md "쪼개는 시점" 원칙).

CLAUDE.md 규칙 7: 모의투자 도메인(openapivts)만 호출한다. 실전투자 도메인
(openapi.koreainvestment.com)은 이 파일 어디에도 등장하지 않는다.

실측으로 확인한 것 (2026-08-08, 모의투자 계좌):
- 토큰 발급은 1분당 1회로 제한된다 (EGW00133) — 그래서 파일 캐시가 필요하다.
- 시세 조회는 초당 거래건수 제한이 있고, 걸리면 HTTP 500 + rt_cd="1" +
  msg_cd="EGW00201"로 응답한다.
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

실측으로 확인한 것 (2026-08-19, 유량 제한):
- 모의투자 유량 한도는 **초당 2건**이다. 잔고조회를 간격 없이 5연타하면 1·2번은
  정상, 3번부터 EGW00201이 온다.
- 용량 거부에는 **층이 둘 있다.** 같은 잔고조회 엔드포인트인데 게이트웨이 한도는
  msg_cd="EGW00201" + "초당 거래건수를 초과하였습니다"이고, 원장 쪽은 "원장에서
  허용 가능한 초당 거래건수를 초과하였습니다"라는 다른 문구로 온다(msg_cd는 당시
  로그에 안 남아 미상 — 그래서 kis_api_error가 이제 msg_cd를 찍는다).
- 원장 거부는 **우리 호출 속도와 무관하다.** 09:00:07에 초당 1건으로, 그 시점
  원장을 건드린 유일한 호출이 거부됐다 — 호출 한 건이 "초당 건수 초과"로 막혔다는
  건 그 카운터가 계좌 단위일 수 없다는 뜻이다(전체 모의투자 이용자 공유). 장 마감
  후 20여 회를 일부러 한도 넘겨가며 때려도 이 문구는 재현되지 않았다.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from src.schemas import FillRecord, OHLCVBar

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
ORDER_SELL_TR_ID = "VTTC0801U"  # 모의투자 현금 매도 주문
DAILY_CCLD_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
DAILY_CCLD_TR_ID = "VTTC8001R"  # 모의투자 주식일별주문체결조회
BALANCE_PATH = "/uapi/domestic-stock/v1/trading/inquire-balance"
BALANCE_TR_ID = "VTTC8434R"  # 모의투자 주식잔고조회
ACCOUNT_PRODUCT_CODE = "01"  # 실측으로 확인됨 — 계좌번호 뒤 2자리

TOKEN_CACHE_PATH = Path(__file__).resolve().parent.parent / ".kis_token_cache.json"


@dataclass(frozen=True)
class RetryPolicy:
    """한 번의 KIS 요청에 쓸 재시도 예산.

    호출처마다 다른 이유: 같은 시세 조회라도 하루 한 번 도는 집행 경로와 매분
    도는 손절 체크는 실패를 견디는 방식이 정반대다. 전자는 다음 기회가 없으니
    회차 안에서 버텨야 하고, 후자는 1분 뒤 크론이 어차피 다시 부르므로 회차를
    짧게 끝내는 쪽이 공백이 짧다.
    """

    timeout_seconds: float
    max_attempts: int
    backoff_seconds: tuple[float, ...]

    def backoff_for(self, attempt: int) -> float:
        """attempt번째 시도 직후 쉴 시간. 표를 넘어가면 마지막 값을 계속 쓴다."""
        return self.backoff_seconds[min(attempt, len(self.backoff_seconds)) - 1]


# 1.5 → 3 → 6 → 10초 (누적 20.5초). 09:00 장 시작 직후 모의투자 원장이 순간적으로
# 유량을 막는 구간을 넘기려면 이 정도는 버텨야 한다 — 2026-08-19에 09:00:07의
# 0.06초짜리 거부 한 번이 재시도 없이 그날 유일한 승인 매수를 날렸다.
# 하루 한 번짜리 경로(08:30 판단·09:01 집행·15:35 판단)는 전부 이걸 쓴다.
DEFAULT_POLICY = RetryPolicy(
    timeout_seconds=10.0,
    max_attempts=5,
    backoff_seconds=(1.5, 3.0, 6.0, 10.0),
)

# 매분 도는 손절/익절 체크 전용(pipeline.evaluate_holdings).
# 크론이 곧 재시도 루프라 회차 안에서 오래 버티지 않는다 — 5종목 기준 최악 약 38초로
# 60초 주기 안에 들어와, 다음 분이 락에 막혀 통째로 스킵되는 일이 없다.
#
# 38초의 내역: 15초(1차 타임아웃) + 1.5초(백오프) + 15초(2차) + 종목 수만큼의 스태거.
# _throttle이 요청을 1초 간격으로 벌리므로 **보유 종목이 늘면 그만큼 길어진다**
# (대략 31.5 + N초). 20종목을 넘으면 다시 60초를 넘기니 그때 이 값을 다시 봐야 한다.
#
# 2026-08-21 장애가 근거다. 기존 예산(5시도)으로 한 회차가 **실측 75초**(15:08:02
# 시작 → 15:09:17 exhausted) 걸려, 짝수 분 회차가 실패하고 홀수 분은 락에 막히는
# 교대 패턴이 15:07~15:29 23분간 이어졌다. 타임아웃을 10→15초로 오히려 **늘린** 건
# 그 장애가 전부 read timeout이었기 때문이다(당시 실측 응답 6초, 한계 10초라 여유 4초).
# 시도 횟수를 줄여 확보한 시간을 회당 인내심으로 옮긴 것이다.
#
# 다만 이 정책은 공백의 **간격**을 2분에서 1분으로 좁힐 뿐, 서버가 응답을 아예 안 주는
# 구간 자체를 없애지는 못한다. 그 구간을 사람이 알게 하는 건 notify.track_blackout이다.
FAST_FAIL_POLICY = RetryPolicy(
    timeout_seconds=15.0,
    max_attempts=2,
    backoff_seconds=(1.5,),
)

MIN_REQUEST_SPACING_SECONDS = 1.0  # 실측: 0.3초 간격은 부족했다. 모의투자 한도는 초당 2건(2026-08-19)

# 용량 거부(= 다시 보내면 통과할 수 있는 "지금 바빠서 안 됨")를 가리는 기준.
# msg_cd가 1순위다. msg1 문구 매칭은 원장 거부의 msg_cd를 아직 실측하지 못해서
# 두는 한시적 보조 수단이고, kis_api_error 로그가 이제 msg_cd를 남기므로 실제
# 코드가 관측되는 즉시 CAPACITY_MSG_CODES로 옮기고 이 문구 매칭은 걷어낸다.
CAPACITY_MSG_CODES = frozenset({"EGW00201"})
CAPACITY_MSG1_MARKERS = ("초당 거래건수",)

TOKEN_EXPIRY_SAFETY_MARGIN = timedelta(minutes=5)


class OrderResponseLost(Exception):
    """주문을 보냈지만 응답을 받지 못했다 — 접수됐는지 알 수 없는 상태.

    주문 POST는 멱등이 아니라 재전송할 수 없다(같은 종목을 두 번 산다). 그런데
    "응답이 없다"와 "접수가 거부됐다"는 전혀 다른 사건이다 — 전자는 브로커에
    포지션이 생겼는데 우리 상태엔 없는 경우를 포함한다. 둘을 None으로 뭉뚱그리면
    호출부가 구분할 수 없으므로 예외로 올린다. 실제 접수 여부를 원장(체결 조회)으로
    확인하는 건 호출부 책임이다.
    """


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
                timeout=DEFAULT_POLICY.timeout_seconds,
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


def _is_capacity_rejection(data: dict) -> bool:
    """다시 보내면 통과할 수 있는 용량 거부인가.

    같은 잔고조회 엔드포인트가 층에 따라 다른 문구를 돌려준다(2026-08-19 실측):
    게이트웨이 한도는 msg_cd=EGW00201 "초당 거래건수를 초과하였습니다",
    원장 쪽은 "원장에서 허용 가능한 초당 거래건수를 초과하였습니다"(msg_cd 미상).
    코드 하나만 화이트리스트로 두면 후자가 영구 실패로 오분류된다 — 실제로
    그 오분류가 2026-08-19 승인 매수를 통째로 날렸다.
    """
    if data.get("msg_cd") in CAPACITY_MSG_CODES:
        return True
    msg1 = data.get("msg1") or ""
    return any(marker in msg1 for marker in CAPACITY_MSG1_MARKERS)


def _kis_request(
    method: str,
    path: str,
    tr_id: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    idempotent: bool = True,
    policy: RetryPolicy = DEFAULT_POLICY,
) -> dict | None:
    """공통 KIS 요청(GET/POST).

    재시도 대상은 "다시 보내면 통과할 수 있는 실패"뿐이다 — 네트워크 예외와
    용량 거부(_is_capacity_rejection). 비즈니스 오류(rt_cd != "0", 예: 잘못된
    종목코드·잔고 부족)는 몇 번을 보내도 같은 답이라 즉시 실패로 처리한다.
    CLAUDE.md 규칙 4의 "수집 실패는 재시도 OK, 결과가 마음에 안 들어 재시도는
    금지"와 같은 구분선이다.

    **idempotent=False는 주문 전용이다(_kis_post).** 주문 POST는 재전송하면 같은
    종목을 두 번 사므로 한 번만 보낸다. 이때 용량 거부·비즈니스 오류는 브로커가
    "안 받았다"고 답한 것이라 그대로 None을 돌려주면 되지만, 네트워크 예외는
    접수 여부를 알 수 없는 상태라 OrderResponseLost로 올린다 — 호출부가 원장에서
    확인해야 한다.
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

    max_attempts = policy.max_attempts if idempotent else 1
    last_error: Exception | str | None = None
    for attempt in range(1, max_attempts + 1):
        _throttle()
        try:
            if method == "GET":
                response = requests.get(
                    f"{BASE_URL}{path}", headers=headers, params=params, timeout=policy.timeout_seconds
                )
            else:
                response = requests.post(
                    f"{BASE_URL}{path}", headers=headers, data=json.dumps(body), timeout=policy.timeout_seconds
                )
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            if not idempotent:
                logger.error("kis_order_response_lost path=%s error=%s", path, exc)
                raise OrderResponseLost(str(exc)) from exc
            last_error = exc
            logger.warning("kis_fetch_failed path=%s attempt=%d error=%s", path, attempt, exc)
            if attempt < max_attempts:
                time.sleep(policy.backoff_for(attempt))
            continue

        if data.get("rt_cd") == "0":
            return data

        msg_cd, msg1 = data.get("msg_cd"), (data.get("msg1") or "").strip()

        if _is_capacity_rejection(data):
            last_error = f"capacity_rejected msg_cd={msg_cd}"
            logger.warning(
                "kis_rate_limited path=%s attempt=%d msg_cd=%s msg=%s", path, attempt, msg_cd, msg1
            )
            if attempt < max_attempts:
                time.sleep(policy.backoff_for(attempt))
            continue

        # msg_cd를 반드시 함께 남긴다 — 이게 없으면 새로운 용량 거부가 나타나도
        # 어떤 코드를 화이트리스트에 넣어야 하는지 사후에 알 방법이 없다.
        logger.error("kis_api_error path=%s msg_cd=%s msg=%s", path, msg_cd, msg1)
        return None

    logger.error("kis_fetch_exhausted path=%s error=%s", path, last_error)
    return None


def _kis_get(path: str, tr_id: str, params: dict, *, policy: RetryPolicy = DEFAULT_POLICY) -> dict | None:
    return _kis_request("GET", path, tr_id, params=params, policy=policy)


def _kis_post(path: str, tr_id: str, body: dict) -> dict | None:
    """주문 접수 전용. 재시도하지 않는다 — _kis_request docstring 참고."""
    return _kis_request("POST", path, tr_id, body=body, idempotent=False)


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


def fetch_daily_ohlcv(
    ticker: str, lookback_days: int = 60, *, policy: RetryPolicy = DEFAULT_POLICY
) -> list[OHLCVBar] | None:
    """KIS 일별시세 API에서 최근 lookback_days 거래일치 일봉을 가져온다.

    조회 기간을 얼마나 넓게 잡아도 KIS가 최근 100거래일로 캡핑하는 걸 실측으로
    확인했다 — 그래서 넉넉한 고정폭 창을 요청하고 뒤에서 lookback_days만큼 자른다.

    policy를 받는 이유는 fetch_current_price와 같다: 시세 공백 복구 직후의 사후
    판정(pipeline._audit_blackout_window)은 매분 도는 회차 안에서 도는 조회라
    FAST_FAIL_POLICY로 짧게 끝내야 다음 분이 락에 막히지 않는다.
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

    data = _kis_get(DAILY_CHART_PATH, DAILY_CHART_TR_ID, params, policy=policy)
    if data is None:
        return None

    bars = _parse_daily_chart(data)
    if not bars:
        return None
    return bars[-lookback_days:]


@dataclass(frozen=True)
class Quote:
    """시세 한 건. 현재가 + **당일 고가/저가** + 신선도 판별용 시가/전일 종가.

    당일 고가/저가는 원래부터 `inquire-price` 응답에 같이 들어 있었는데
    (`stck_hgpr`/`stck_lwpr`) 2026-08-27까지 버리고 `stck_prpr`만 썼다. 매분
    390번씩 받아놓고 안 쓴 셈이다 — 그 두 숫자가 "분당 1회 샘플링이 스쳐 지나간
    가격"을 복원하는 유일한 무료 단서다(sell.evaluate_deterministic_sell).

    open_price/prev_close도 같은 응답의 다른 필드다(2026-09-02 추가, 추가 호출 없음).
    **이 응답에는 "언제 찍힌 시세인가"를 말해주는 필드가 없다** — 그래서 개장 직후
    받은 값이 당일 것인지 전일 것인지 구분할 방법이 이 둘뿐이다. prev_close는
    `stck_prpr - prdy_vrss`로 역산한다: 직전 거래일 종가가 나오면 당일 시세고,
    그 전 거래일 종가가 나오면 전일 스냅샷을 받은 것이다
    (pipeline._log_quote_freshness_at_open).
    """

    price: float
    day_high: float | None = None
    day_low: float | None = None
    open_price: float | None = None
    prev_close: float | None = None


def fetch_quote(ticker: str, *, policy: RetryPolicy = DEFAULT_POLICY) -> Quote | None:
    """현재가와 당일 고가/저가를 한 번에. 추가 호출이 아니라 같은 응답의 다른 필드다."""
    data = _kis_get(CURRENT_PRICE_PATH, CURRENT_PRICE_TR_ID, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}, policy=policy)
    if data is None:
        return None

    output = data.get("output") or {}
    price_str = output.get("stck_prpr")
    if not price_str:
        logger.warning("kis_price_missing ticker=%s", ticker)
        return None

    def _optional(key: str) -> float | None:
        raw = output.get(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        # 장 시작 전에는 0으로 온다 — 0을 저가로 믿으면 모든 손절이 발동한다.
        return value if value > 0 else None

    try:
        price = float(price_str)
    except ValueError:
        logger.warning("kis_price_unparseable ticker=%s raw=%s", ticker, price_str)
        return None

    # 전일 대비는 부호가 있고 보합이면 0이라 _optional(>0만 통과)로는 못 읽는다.
    # 역산한 전일 종가가 양수일 때만 채운다 — 못 읽으면 None으로 남긴다("모른다"와
    # "0원"을 같은 모양으로 만들지 않는다).
    try:
        prev_close = price - float(output.get("prdy_vrss"))
    except (TypeError, ValueError):
        prev_close = None
    if prev_close is not None and prev_close <= 0:
        prev_close = None

    return Quote(
        price=price,
        day_high=_optional("stck_hgpr"),
        day_low=_optional("stck_lwpr"),
        open_price=_optional("stck_oprc"),
        prev_close=prev_close,
    )


def fetch_current_price(ticker: str, *, policy: RetryPolicy = DEFAULT_POLICY) -> float | None:
    """실시간 현재가. 갭 체크(장 시작가 vs 전일 종가)와 주문 수량 계산에 쓴다.

    매분 도는 손절 체크만 policy=FAST_FAIL_POLICY로 부른다 — 이유는 그 상수 주석 참고.
    """
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
    data = _kis_get(CURRENT_PRICE_PATH, CURRENT_PRICE_TR_ID, params, policy=policy)
    if data is None:
        return None

    price_str = (data.get("output") or {}).get("stck_prpr")
    if not price_str:
        return None
    return float(price_str)


@dataclass(frozen=True)
class AccountSnapshot:
    """계좌를 금액으로 본 한 장면. 전부 브로커가 준 값이다 — 계산이 없다.

    왜 필요한가 (2026-09-01): 노션 일일 리포트의 "총정리"가 현금을
    `총평가금액 x PortfolioState.cash_weight`로 만들고 있었다. `cash_weight`는
    **매수 시점 원가 기준 장부 비중**이고 총평가금액은 **실시간 시장가치**라,
    둘을 곱하면 현금도 투자금도 아닌 값이 나온다. 9/1 실측으로 현금이 823,891원
    (+1.64%) 부풀어 있었고, 평가이익이 쌓일수록 더 벌어지는 한쪽 방향 오차였다
    (cash_weight는 고정인데 총평가금액만 오르므로).

    세 값 다 원래부터 같은 응답에 들어 있었다 — `tot_evlu_amt` 하나만 꺼내고
    나머지를 버리고 있었다. 2026-08-27에 `stck_hgpr`/`stck_lwpr`을 매분 받아놓고
    버리던 것과 같은 모양이라, 추가 호출이 전혀 없다.
    """

    total: float  # tot_evlu_amt — 총평가금액(예수금 + 유가증권 평가)
    cash: float  # dnca_tot_amt — 예수금 총금액
    securities: float  # scts_evlu_amt — 유가증권 평가금액


def _balance_params() -> dict:
    return {
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


def fetch_account_snapshot() -> AccountSnapshot | None:
    """계좌 총평가금액·예수금·유가증권 평가금액을 한 번에 (AccountSnapshot).

    셋 중 하나라도 못 읽으면 통째로 None이다 — 일부만 돌려주면 호출부가 나머지를
    빼기로 만들어내게 되고, 그게 지금 걷어내는 그 계산이다.
    """
    data = _kis_get(BALANCE_PATH, BALANCE_TR_ID, _balance_params())
    if data is None:
        return None

    rows = data.get("output2") or []
    if not rows:
        return None
    row = rows[0]
    try:
        return AccountSnapshot(
            total=float(row["tot_evlu_amt"]),
            cash=float(row["dnca_tot_amt"]),
            securities=float(row["scts_evlu_amt"]),
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("kis_account_snapshot_unparseable row_keys=%s", sorted(row))
        return None


def fetch_account_balance() -> float | None:
    """계좌 총평가금액(tot_evlu_amt). 비중(weight) 기반 주문 수량을 절대
    원화·주수로 환산하려면 이 값이 필요하다 — PortfolioState는 비중만 들고
    있고 절대 금액은 추적하지 않으므로, 매번 브로커(KIS)를 실제 잔고의
    출처로 삼는다(자체 상태와 동기화 문제를 피하기 위해).

    주문 수량 계산에는 총평가금액만 있으면 되므로 이 얇은 창구를 남겨둔다 —
    금액 세 개가 다 필요한 쪽(노션 일일 리포트)은 fetch_account_snapshot을 쓴다."""
    snapshot = fetch_account_snapshot()
    return snapshot.total if snapshot is not None else None


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


def place_market_sell_order(ticker: str, quantity: int) -> str | None:
    """모의투자 계좌로 시장가 매도 주문을 접수한다. place_market_buy_order와
    완전히 대칭 — 같은 엔드포인트를 tr_id만 바꿔서 쓴다(KIS의 매수/매도 공통
    설계). 성공하면 주문번호(ODNO)를 반환한다.

    구조 검증은 매수와 별개로 라이브로 다시 확인했다(2026-08-09) — 같은
    "모의투자 영업일이 아닙니다" 응답으로 계좌·파라미터가 정상 처리됨을
    확인했다. 실제 체결은 매수와 마찬가지로 장중 재검증이 필요하다.
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
    data = _kis_post(ORDER_CASH_PATH, ORDER_SELL_TR_ID, body)
    if data is None:
        return None

    return (data.get("output") or {}).get("ODNO")


SIDE_CODES = {"buy": "02", "sell": "01"}

# 매도 시 붙는 세금(증권거래세+농특세) 요율. **브로커가 안 주는 유일한 비용이라
# 여기서만 계산값이다.**
#
# 2026-09-01 실측으로 뽑았다: 배포 이후 전 거래의 매도 체결액 22,250,550원과
# 브로커가 준 추정제비용 13,300원을 계좌 실제 증감과 맞춰보니 44,501원이 남았고,
# 그건 매도 체결액의 정확히 0.2000%였다. (1억 시작 - 실현손익 1,109,100 + 평가손익
# 1,791,240 = 100,682,140 예상, 실제 100,624,343, 차이 57,797 ≈ 13,300 + 44,501.)
#
# **이건 KIS 모의투자가 적용하는 요율이다.** 실계좌(2026년 코스피 0.15%)와 다르므로
# 실거래 전환 시 반드시 다시 잰다 — 규칙 7 해제 시 확인할 항목.
SELL_TAX_RATE = 0.0020

# 위탁수수료 요율. 정상 경로에서는 이 상수를 안 쓴다 — 브로커의 추정제비용
# (prsm_tlex_smtl)을 주문 전후 차로 재는 게 1순위고(FillRecord.fee), 이 값은 그
# 조회가 실패했거나 옛 포지션이라 기록이 없을 때의 근사용이다.
# 2026-09-01 실측: 매수·매도 15건 전부 체결액의 0.0141~0.0142%로 같았다.
BROKERAGE_FEE_RATE = 0.000142


def fetch_daily_fill_totals(ticker: str, order_date: date, side: str) -> tuple[int, float, float] | None:
    """그날 그 종목의 **누적** 체결 수량·금액·위탁수수료 (side: "buy" | "sell").

    주문 직전·직후로 두 번 불러 차를 내면 그 주문 하나의 정확한 체결 수량·금액이
    나온다(`fill_between`). 집계값만 보면 안 되는 이유: output2는 그날 전체를
    합산하므로 같은 종목을 같은 날 두 번 매도하면 두 건이 섞인다 — 실제로
    192820이 2026-08-12에 12주·8주로 두 번 익절돼 20주/4,946,000원으로 합산
    보고됐고, 건별 금액을 사후에 분리할 수 없었다(docs/PLAN.md).

    output1(주문별 명세)은 모의투자 계좌가 빈 배열로 돌려줘서 쓸 수 없다(2026-08-15 실측).
    """
    if side not in SIDE_CODES:
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    date_str = order_date.strftime("%Y%m%d")
    params = {
        "CANO": os.getenv("KIS_ACCOUNT_NO", ""),
        "ACNT_PRDT_CD": ACCOUNT_PRODUCT_CODE,
        "INQR_STRT_DT": date_str,
        "INQR_END_DT": date_str,
        "SLL_BUY_DVSN_CD": SIDE_CODES[side],
        "INQR_DVSN": "00",
        "PDNO": ticker,
        "CCLD_DVSN": "01",  # 체결분만
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

    output2 = data.get("output2") or {}
    qty_str, amount_str = output2.get("tot_ccld_qty"), output2.get("tot_ccld_amt")
    if qty_str is None or amount_str is None:
        return None
    try:
        # 추정제비용(prsm_tlex_smtl)도 같은 응답에 들어 있다 — 추가 호출이 아니다.
        # 이것도 누적값이라 수량·금액과 똑같이 주문 전후 차로 재야 이 주문 몫이 나온다.
        # 없으면 0.0으로 둔다: 아직 아무 비용도 안 붙은 상태(전 값)와 같은 의미라
        # 차를 내면 어차피 0이 되고, 진짜 "모른다"는 fill_between이 None으로 표현한다.
        fee = float(output2.get("prsm_tlex_smtl") or 0.0)
        return int(qty_str), float(amount_str), fee
    except ValueError:
        logger.warning("kis_fill_totals_unparseable ticker=%s qty=%r amt=%r", ticker, qty_str, amount_str)
        return None


def _fee_of(totals: tuple | None) -> float | None:
    """누적 집계에서 수수료 항목만. 원소가 2개뿐인 옛 형태(수수료 이전에 만들어진
    테스트 픽스처 등)면 None — "수수료 0원"이 아니라 "안 재봤다"는 뜻이다."""
    if totals is None or len(totals) < 3:
        return None
    return totals[2]


def fill_between(
    before: tuple | None, after: tuple | None
) -> FillRecord | None:
    """주문 전후 누적 체결 집계의 차 = 그 주문 하나의 체결 내역.

    어느 쪽이든 조회에 실패했거나(None) 수량이 안 늘었으면 None이다 — 후자는
    "주문이 체결되지 않았다"와 "애초에 실주문을 안 냈다(시뮬레이션 경로)"를 모두
    포함하며, 둘 다 체결 사실을 지어내면 안 되는 상황이라 같게 다룬다.

    수수료도 같은 차로 뽑는다. 양쪽 중 하나라도 수수료를 안 들고 있으면 None으로
    남긴다 — 0.0으로 채우면 "수수료가 0원이었다"가 되어 순손익이 조용히 부풀어 오른다.
    """
    if before is None or after is None:
        return None
    qty = after[0] - before[0]
    amount = after[1] - before[1]
    if qty <= 0 or amount <= 0:
        return None
    before_fee, after_fee = _fee_of(before), _fee_of(after)
    fee = None if before_fee is None or after_fee is None else max(after_fee - before_fee, 0.0)
    return FillRecord(quantity=qty, amount=amount, fee=fee)


FILL_POLL_TIMEOUT_S = 12.0
FILL_POLL_INTERVAL_S = 1.5


def fill_after_order(
    ticker: str,
    order_date: date,
    side: str,
    before: tuple | None,
    expected_quantity: int,
    timeout_s: float | None = None,
    poll_interval_s: float | None = None,
) -> FillRecord | None:
    """주문 직후, 주문 수량이 다 체결될 때까지 기다렸다가 그 주문의 체결 내역을 낸다.

    주문 직후 **한 번만** 조회하면 안 되는 이유 (2026-08-18 실측): 036570 손절
    31주가 여러 번에 나뉘어 체결되는 사이에 스냅샷이 찍혀 19주/4,364,500원만
    잡혔다. 브로커 실제 체결은 31주/7,121,000원이었고, 매매일지에서 12주·
    2,756,500원이 통째로 빠졌다. 같은 날 4주짜리 익절은 한 번에 체결돼 멀쩡했다 —
    주문이 클수록 걸리고, 작은 주문만 보고 있으면 안 드러난다.

    expected_quantity에 도달하면 즉시 반환한다(대부분 첫 조회에서 끝난다).
    타임아웃까지 못 채우면 그때까지 잡힌 만큼을 complete=False로 반환한다 —
    체결량을 지어내지 않되, "덜 잡혔다"는 사실은 남겨야 호출부가 상태 차이로
    되짚을 수 있다.
    """
    if before is None:
        return None

    # 기본값을 인자 기본값으로 박지 않고 여기서 읽는다 — 그래야 테스트가 모듈 상수만
    # 바꿔서 대기 시간을 줄일 수 있다(안 그러면 목킹된 테스트가 매번 실제로 기다린다).
    timeout_s = FILL_POLL_TIMEOUT_S if timeout_s is None else timeout_s
    poll_interval_s = FILL_POLL_INTERVAL_S if poll_interval_s is None else poll_interval_s

    deadline = time.monotonic() + timeout_s
    best: FillRecord | None = None
    while True:
        after = fetch_daily_fill_totals(ticker, order_date, side)
        fill = fill_between(before, after)
        if fill is not None:
            best = fill
            if fill.quantity >= expected_quantity:
                return fill

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval_s, remaining))

    if best is None:
        return None
    logger.warning(
        "kis_fill_incomplete ticker=%s side=%s expected=%d observed=%d timeout_s=%.1f",
        ticker,
        side,
        expected_quantity,
        best.quantity,
        timeout_s,
    )
    return best.model_copy(update={"complete": False})


def fetch_holdings() -> dict[str, tuple[int, float]] | None:
    """브로커가 보고하는 보유 종목 전체: {종목코드: (보유수량, 매입평균가)}.

    **브로커가 사실의 출처다.** 자체 상태 파일(logs/portfolio_state.json)의 값과
    다르면 항상 이쪽이 맞다 — 실제로 192820이 호가 210,000에 주문돼 232,000에
    체결됐는데 상태 파일엔 210,000이 남아, 실제 +6.6% 지점에서 익절이 +20%로
    오판돼 발동했다(2026-08-15, docs/PLAN.md). scripts/reconcile_portfolio.py가
    이 함수로 그 어긋남을 잡는다.

    보유 수량이 0인 종목(전량 매도 등)은 애초에 행이 없어 결과에도 안 들어간다.
    """
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

    holdings: dict[str, tuple[int, float]] = {}
    for row in data.get("output1") or []:
        ticker = row.get("pdno")
        qty_str, price_str = row.get("hldg_qty"), row.get("pchs_avg_pric")
        if not ticker or not qty_str or not price_str:
            continue
        try:
            quantity, price = int(qty_str), float(price_str)
        except ValueError:
            logger.warning("kis_holdings_unparseable ticker=%s qty=%r price=%r", ticker, qty_str, price_str)
            continue
        if quantity > 0 and price > 0:
            holdings[ticker] = (quantity, price)
    return holdings


def fetch_position_avg_price(ticker: str) -> float | None:
    """그 종목 보유분의 매입평균가. 체결가 조회가 실패했을 때 진입가의 2차 출처다."""
    holdings = fetch_holdings()
    if not holdings or ticker not in holdings:
        return None
    return holdings[ticker][1]


def fetch_fill_price(ticker: str, order_date: date) -> float | None:
    """그날 그 종목의 매수 평균 체결가. inquire-daily-ccld의 output2(집계)에서
    구매평균가격(pchs_avg_pric)을 쓴다 — 종목·날짜를 좁혀서 조회하므로 이 주문
    하나의 평균 체결가와 사실상 같다(같은 날 같은 종목을 두 번 매수하지 않는 한).

    output1(주문별 체결 내역)의 필드명은 아직 실측으로 확인 못 했다(2026-08-09,
    실제 체결 건이 없어서) — 그래서 필드명이 이미 확인된 output2 집계 쪽을 쓴다.

    `SLL_BUY_DVSN_CD="02"`(매수만)인 게 중요하다. 원래 "00"(전체)이었는데, 그러면
    같은 날 같은 종목을 매수한 뒤 부분 익절까지 나간 경우 output2가 **매수와 매도를
    한데 섞어** 평균을 낸다 — 실제로 192820이 2026-08-12에 매수 38주 + 매도 20주라
    "00"으로는 237,711원(= 전체 체결금액 / 전체 체결수량)이 나온다. 진입가로 쓰면
    안 되는 값이다 (2026-08-15 실측).
    """
    date_str = order_date.strftime("%Y%m%d")
    params = {
        "CANO": os.getenv("KIS_ACCOUNT_NO", ""),
        "ACNT_PRDT_CD": ACCOUNT_PRODUCT_CODE,
        "INQR_STRT_DT": date_str,
        "INQR_END_DT": date_str,
        "SLL_BUY_DVSN_CD": "02",  # 매수만 — "00"(전체)이면 같은 날 매도와 섞인다(위 docstring)
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
