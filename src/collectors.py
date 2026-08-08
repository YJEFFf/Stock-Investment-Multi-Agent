import html
import io
import json
import logging
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TypeVar

import requests
from dotenv import load_dotenv

from src import kis
from src.schemas import DisclosureItem, MarketContext, NewsItem, OHLCVBar

load_dotenv()

logger = logging.getLogger(__name__)

NAVER_CHART_URL = "https://fchart.stock.naver.com/sise.nhn"
NAVER_STOCK_NEWS_URL = "https://finance.naver.com/item/news_news.naver"
NAVER_NEWS_HUB_URL = "https://finance.naver.com/news/"
NAVER_KOSPI200_CONSTITUENTS_URL = "https://finance.naver.com/sise/entryJongmok.naver"
DART_CORP_CODE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

KOSPI200_INDEX_SYMBOL = "KPI200"
KOSPI200_CONSTITUENT_PAGES = 20  # 페이지당 10종목 x 20페이지 = 200종목

REQUEST_TIMEOUT_SECONDS = 5.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.0
USER_AGENT = "Mozilla/5.0 (compatible; SIMA-research-bot/0.1; personal use)"

# 동시 스크래핑 요청 수를 제한해 서버에 부담을 주지 않는다.
_REQUEST_SEMAPHORE = threading.Semaphore(5)

T = TypeVar("T")


def _fetch_with_retries(
    url: str,
    parse: Callable[[str | bytes], T],
    *,
    params: dict | None = None,
    headers: dict | None = None,
    binary: bool = False,
) -> T | None:
    """공통 HTTP GET + 파싱 재시도 로직.

    네트워크·파싱 실패에만 재시도한다 (규칙 4). 분석 결과가 마음에 안 들어서
    재시도하는 경로는 이 함수 안에 존재하지 않는다 — 재시도 소진 시 조용히 None을
    반환해 "판단 불가" 상태로 이어지게 한다.
    """
    merged_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with _REQUEST_SEMAPHORE:
                response = requests.get(
                    url, params=params, headers=merged_headers, timeout=REQUEST_TIMEOUT_SECONDS
                )
            response.raise_for_status()
            payload = response.content if binary else response.text
            return parse(payload)
        except (requests.RequestException, ET.ParseError, ValueError, zipfile.BadZipFile) as exc:
            last_error = exc
            logger.warning("fetch_failed url=%s attempt=%d error=%s", url, attempt, exc)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    logger.error("fetch_exhausted url=%s error=%s", url, last_error)
    return None


def _parse_bars(xml_text: str) -> list[OHLCVBar]:
    root = ET.fromstring(xml_text)
    bars = []
    for item in root.iter("item"):
        raw = item.get("data")
        if not raw:
            continue
        parts = raw.split("|")
        if len(parts) != 6:
            continue
        d, o, h, low, c, v = parts
        bars.append(
            OHLCVBar(
                date=datetime.strptime(d, "%Y%m%d").date(),
                open=float(o),
                high=float(h),
                low=float(low),
                close=float(c),
                volume=int(v),
            )
        )
    bars.sort(key=lambda b: b.date)
    return bars


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0  # 데이터 부족 시 중립값

    gains, losses = [], []
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        (gains if change >= 0 else losses).append(abs(change))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_indicators(bars: list[OHLCVBar]) -> dict[str, float]:
    """추세·모멘텀·거래량·지지저항 지표. 텍스트로 차트를 본 것과 동등한 효과를
    노린다 (docs/PLAN.md §5). bars는 오래된 순으로 정렬돼 있어야 한다."""
    if not bars:
        return {}

    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    latest_close = closes[-1]
    indicators: dict[str, float] = {}

    for n in (5, 20, 60):
        if len(closes) >= n:
            sma = sum(closes[-n:]) / n
            indicators[f"sma{n}"] = sma
            indicators[f"close_vs_sma{n}_pct"] = (latest_close - sma) / sma * 100

    indicators["rsi14"] = _rsi(closes, period=14)

    window = bars[-20:] if len(bars) >= 20 else bars
    # 거래정지 등 무거래일은 네이버가 O/H/L/V를 전부 0으로 내려준다 — 지지/저항
    # 계산에 넣으면 왜곡되거나(0이 최저가로 잡힘) 0으로 나누기 오류가 난다.
    traded_window = [b for b in window if b.volume > 0]
    if traded_window:
        recent_high = max(b.high for b in traded_window)
        recent_low = min(b.low for b in traded_window)
        indicators["recent_high_20d"] = recent_high
        indicators["recent_low_20d"] = recent_low
        indicators["close_vs_recent_high_pct"] = (latest_close - recent_high) / recent_high * 100
        indicators["close_vs_recent_low_pct"] = (latest_close - recent_low) / recent_low * 100

    for n in (5, 20, 60):
        if len(closes) > n:
            past = closes[-(n + 1)]
            indicators[f"return_{n}d_pct"] = (latest_close - past) / past * 100

    if len(volumes) >= 20:
        avg_volume_20 = sum(volumes[-20:]) / 20
        indicators["volume_vs_20d_avg_ratio"] = volumes[-1] / avg_volume_20 if avg_volume_20 else 0.0

    return indicators


def fetch_market_context(ticker: str, lookback_days: int = 60) -> MarketContext | None:
    """개별 종목 일봉 데이터를 한국투자증권(KIS) API에서 가져온다.

    네이버 스크래핑 대신 KIS를 쓰는 이유는 실시간성이다 — 모의투자 계좌로도
    실제 매매 판단에 쓸 시세이니 지연이 적은 공식 API 쪽을 신뢰한다. 종목
    리스트(fetch_kospi200_universe)와 지수 시계열(fetch_kospi200_index_bars)은
    개별 종목 시세가 아니라서 계속 네이버를 쓴다.
    """
    bars = kis.fetch_daily_ohlcv(ticker, lookback_days)
    if bars is None:
        return None

    return MarketContext(
        ticker=ticker,
        as_of=datetime.now(timezone.utc),
        bars=bars,
        indicators=compute_indicators(bars),
    )


def fetch_kospi200_index_bars(lookback_days: int = 60) -> list[OHLCVBar] | None:
    """코스피200 지수 자체의 일봉 시계열. 정량 필터에서 개별 종목의 초과수익률
    (시장 전체 움직임 대비)을 계산하는 데 쓴다. 지수는 '종목'이 아니라 네이버
    차트 API를 그대로 재사용한다 — 이미 검증된 동일 엔드포인트가 심볼만 다르게
    받으면 그대로 동작한다."""
    params = {"symbol": KOSPI200_INDEX_SYMBOL, "timeframe": "day", "count": lookback_days, "requestType": 0}

    def _parse(xml_text: str) -> list[OHLCVBar]:
        bars = _parse_bars(xml_text)
        if not bars:
            raise ValueError("no bars parsed for KOSPI200 index")
        return bars

    return _fetch_with_retries(NAVER_CHART_URL, _parse, params=params)


_KOSPI200_CONSTITUENT_ROW_PATTERN = re.compile(
    r'<a href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]*)</a>'
)


def _parse_kospi200_constituent_page(html_text: str) -> list[tuple[str, str]]:
    return [
        (code, html.unescape(name.strip()))
        for code, name in _KOSPI200_CONSTITUENT_ROW_PATTERN.findall(html_text)
    ]


def fetch_kospi200_universe() -> list[tuple[str, str]] | None:
    """네이버 금융에서 코스피200 편입종목 전체를 스크래핑한다.

    반환값은 (종목코드, 종목명)이다 — 업종(섹터)이 아니다. 이 페이지엔 업종 정보가
    없어 뉴스 분석가가 쓰는 sector는 별도로 채워야 한다 (아직 미구현, 알려진 갭).

    페이지 하나라도 수집 실패하면 전체를 None으로 실패 처리한다. 유니버스가
    일부만 채워진 채로 조용히 넘어가면 이후 전체 판단이 왜곡되므로, 개별 종목
    조회 실패(빈 리스트로 넘어감)보다 엄격하게 다룬다.
    """
    constituents: list[tuple[str, str]] = []
    seen: set[str] = set()

    for page in range(1, KOSPI200_CONSTITUENT_PAGES + 1):
        page_items = _fetch_with_retries(
            NAVER_KOSPI200_CONSTITUENTS_URL,
            _parse_kospi200_constituent_page,
            params={"type": "KPI200", "page": page},
        )
        if page_items is None:
            logger.error("kospi200_universe_fetch_failed page=%d", page)
            return None

        for code, name in page_items:
            if code not in seen:
                seen.add(code)
                constituents.append((code, name))

    return constituents


_COMPANY_NEWS_ROW_PATTERN = re.compile(
    r'<td class="title">\s*<a href="([^"]+)"[^>]*>([^<]*)</a>.*?'
    r'<td class="info">([^<]*)</td>\s*<td class="date">\s*([^<]*?)\s*</td>',
    re.DOTALL,
)


def _parse_company_news(html_text: str) -> list[NewsItem]:
    items = []
    for url_path, title, press, date_str in _COMPANY_NEWS_ROW_PATTERN.findall(html_text):
        published_at = None
        try:
            published_at = datetime.strptime(date_str.strip(), "%Y.%m.%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass

        items.append(
            NewsItem(
                title=html.unescape(title.strip()),
                press=html.unescape(press.strip()) or None,
                published_at=published_at,
                url=f"https://finance.naver.com{url_path}",
            )
        )
    return items


def fetch_company_news(ticker: str, limit: int = 10) -> list[NewsItem] | None:
    """종목별 뉴스 탭을 스크래핑한다.

    Referer 헤더가 없으면 빈 결과만 온다 (실제로 확인됨) — 부모 페이지를 거쳐 온
    요청처럼 보이게 한다. 뉴스가 0건인 것과 수집 자체가 실패한 것은 다르게 취급한다
    (0건은 정상 상태로 빈 리스트, 수집 실패만 None).
    """
    params = {"code": ticker, "page": 1, "clusterId": ""}
    headers = {"Referer": f"https://finance.naver.com/item/news.naver?code={ticker}"}

    items = _fetch_with_retries(NAVER_STOCK_NEWS_URL, _parse_company_news, params=params, headers=headers)
    if items is None:
        return None
    return items[:limit]


_SECTOR_HEADLINE_PATTERN = re.compile(r'href="(/news/news_read\.naver\?[^"]+)"[^>]*title="([^"]*)"')


def _parse_sector_news(html_text: str, sector: str) -> list[NewsItem]:
    items = []
    seen_urls: set[str] = set()

    for url_path, title in _SECTOR_HEADLINE_PATTERN.findall(html_text):
        headline = html.unescape(title.strip())
        if sector not in headline:
            continue

        url = f"https://finance.naver.com{url_path}"
        if url in seen_urls:
            continue
        seen_urls.add(url)

        items.append(NewsItem(title=headline, press=None, published_at=None, url=url))

    return items


def fetch_sector_news(sector: str, limit: int = 10) -> list[NewsItem] | None:
    """업종 전용 뉴스 탭이 네이버에 따로 없어, 전체 시황 뉴스 허브를 긁어 제목에
    업종명이 들어간 헤드라인만 골라낸다 (docs/PLAN.md §5 — 키워드 매칭이지만 업종
    단위라 회사명 매칭보다 오탐 허용 범위가 넓다). 개별 기사 타임스탬프는 이 페이지에
    없어 published_at은 항상 None이다.
    """
    items = _fetch_with_retries(NAVER_NEWS_HUB_URL, lambda text: _parse_sector_news(text, sector))
    if items is None:
        return None
    return items[:limit]


_corp_code_cache: dict[str, str] | None = None


def _parse_corp_code_zip(zip_bytes: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])

    root = ET.fromstring(xml_bytes)
    mapping: dict[str, str] = {}
    for item in root.iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if stock_code:
            mapping[stock_code] = corp_code
    return mapping


def _load_corp_code_map() -> dict[str, str]:
    """DART 고유번호(corp_code) 매핑을 받아 프로세스 생애주기 동안 캐시한다.

    자주 바뀌지 않는 데이터라 파일 캐시까지는 필요 없다. 수집 실패 시에는 캐시하지
    않아 다음 호출에서 다시 시도한다.
    """
    global _corp_code_cache
    if _corp_code_cache is not None:
        return _corp_code_cache

    api_key = os.getenv("DART_API_KEY", "")
    mapping = _fetch_with_retries(
        DART_CORP_CODE_URL, _parse_corp_code_zip, params={"crtfc_key": api_key}, binary=True
    )
    if mapping is None:
        return {}

    _corp_code_cache = mapping
    return _corp_code_cache


def _parse_disclosure_list(json_text: str) -> list[DisclosureItem]:
    data = json.loads(json_text)
    status = data.get("status")

    if status == "013":  # 조회된 데이터가 없습니다 — 정상적인 0건 상태
        return []
    if status != "000":
        raise ValueError(f"DART API error status={status} message={data.get('message')}")

    items = []
    for row in data.get("list", []):
        items.append(
            DisclosureItem(
                report_name=row["report_nm"],
                submitter=row["flr_nm"],
                received_at=datetime.strptime(row["rcept_dt"], "%Y%m%d").date(),
                receipt_no=row["rcept_no"],
                remark=row.get("rm") or None,
                url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row['rcept_no']}",
            )
        )
    return items


def fetch_disclosures(ticker: str, lookback_days: int = 30, limit: int = 10) -> list[DisclosureItem] | None:
    """DART 공시검색 API에서 종목별 최근 공시 목록을 가져온다.

    corp_code 없이 조회하면 전체 법인 대상 검색이 되어 종목당 독립 호출 원칙(규칙 5)이
    깨지므로, corp_code를 항상 채워서 요청한다. 우리 유니버스에 없는 종목이면(매핑에
    없으면) 수집 실패가 아니라 빈 리스트를 반환한다.
    """
    corp_code = _load_corp_code_map().get(ticker)
    if corp_code is None:
        return []

    end_date = datetime.now(timezone.utc).date()
    begin_date = end_date - timedelta(days=lookback_days)
    params = {
        "crtfc_key": os.getenv("DART_API_KEY", ""),
        "corp_code": corp_code,
        "bgn_de": begin_date.strftime("%Y%m%d"),
        "end_de": end_date.strftime("%Y%m%d"),
        "page_count": limit,
    }

    items = _fetch_with_retries(DART_LIST_URL, _parse_disclosure_list, params=params)
    if items is None:
        return None
    return items[:limit]
