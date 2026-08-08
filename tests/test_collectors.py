import io
import json
import zipfile
from datetime import date

import pytest
import requests

from src import collectors
from src.schemas import OHLCVBar

FAKE_XML = """<protocol title='' description=''>
<chartdata symbol='005930' name='삼성전자' count='3'>
<item data='20260101|70000|71000|69500|70500|1000000'/>
<item data='20260102|70500|72000|70000|71800|1200000'/>
<item data='20260105|71800|73000|71500|72900|1500000'/>
</chartdata>
</protocol>"""


def _bars(n: int) -> list[OHLCVBar]:
    return [
        OHLCVBar(date=date(2026, 1, 1 + i), open=100 + i, high=101 + i, low=99 + i, close=100 + i, volume=1000)
        for i in range(n)
    ]


def test_parse_bars_extracts_all_items():
    bars = collectors._parse_bars(FAKE_XML)
    assert len(bars) == 3
    assert bars[0] == OHLCVBar(
        date=date(2026, 1, 1), open=70000, high=71000, low=69500, close=70500, volume=1000000
    )


def test_compute_indicators_empty_bars_returns_empty_dict():
    assert collectors.compute_indicators([]) == {}


def test_compute_indicators_sma_and_returns():
    bars = _bars(25)
    indicators = collectors.compute_indicators(bars)

    assert indicators["sma5"] == pytest.approx(sum(b.close for b in bars[-5:]) / 5)
    assert indicators["sma20"] == pytest.approx(sum(b.close for b in bars[-20:]) / 20)
    assert "sma60" not in indicators  # 60일치가 없으면 계산하지 않는다
    assert indicators["return_5d_pct"] == pytest.approx(
        (bars[-1].close - bars[-6].close) / bars[-6].close * 100
    )
    assert indicators["volume_vs_20d_avg_ratio"] == pytest.approx(1.0)


def test_compute_indicators_skips_halted_days_for_support_resistance():
    """거래정지일은 네이버가 O/H/L/V를 전부 0으로 내려준다 — 실제 코스피200 199종목
    전수 조회 중 한화(000880)에서 발견된 케이스. 지지/저항 계산에 들어가면 0으로
    나누기 오류가 나거나 값이 왜곡된다."""
    traded = _bars(5)  # close 100..104, high 101..105, low 99..103
    halted = [
        OHLCVBar(date=date(2026, 1, 10 + i), open=0, high=0, low=0, close=104, volume=0) for i in range(3)
    ]
    bars = traded + halted

    indicators = collectors.compute_indicators(bars)

    # 무거래일을 제외한 traded 구간에서만 고가/저가가 나와야 한다.
    assert indicators["recent_high_20d"] == pytest.approx(105.0)
    assert indicators["recent_low_20d"] == pytest.approx(99.0)


def test_compute_indicators_all_halted_omits_support_resistance():
    bars = [OHLCVBar(date=date(2026, 1, 1 + i), open=0, high=0, low=0, close=100, volume=0) for i in range(3)]

    indicators = collectors.compute_indicators(bars)

    assert "recent_high_20d" not in indicators
    assert "recent_low_20d" not in indicators
    assert "close_vs_recent_high_pct" not in indicators


def test_fetch_market_context_success(monkeypatch):
    """fetch_market_context는 이제 KIS API(kis.fetch_daily_ohlcv)에서 일봉을 받는다
    — 네이버는 종목 리스트/지수 시계열에만 남아있다."""
    bars = _bars(3)
    monkeypatch.setattr(collectors.kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: bars)

    context = collectors.fetch_market_context("005930", lookback_days=3)

    assert context is not None
    assert context.ticker == "005930"
    assert len(context.bars) == 3
    assert "sma5" not in context.indicators  # 3일치뿐이라 5일 이동평균은 계산되지 않는다


def test_fetch_market_context_returns_none_when_kis_fetch_fails(monkeypatch):
    monkeypatch.setattr(collectors.kis, "fetch_daily_ohlcv", lambda ticker, lookback_days: None)

    context = collectors.fetch_market_context("005930", lookback_days=3)

    assert context is None


def test_fetch_kospi200_index_bars_success(monkeypatch):
    class FakeResponse:
        text = FAKE_XML

        def raise_for_status(self):
            pass

    monkeypatch.setattr(collectors.requests, "get", lambda *a, **k: FakeResponse())

    bars = collectors.fetch_kospi200_index_bars(lookback_days=3)

    assert bars is not None
    assert len(bars) == 3


def test_fetch_kospi200_index_bars_returns_none_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(collectors.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        collectors.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )

    bars = collectors.fetch_kospi200_index_bars(lookback_days=3)

    assert bars is None


FAKE_KOSPI200_PAGE_HTML = """
<table>
<tr><td class="ctg"><a href="/item/main.naver?code=005930" target="_parent">삼성전자</a></td></tr>
<tr><td class="ctg"><a href="/item/main.naver?code=000660" target="_parent">SK하이닉스</a></td></tr>
</table>
"""

FAKE_KOSPI200_EMPTY_PAGE_HTML = "<table></table>"


def test_parse_kospi200_constituent_page_extracts_code_and_name():
    items = collectors._parse_kospi200_constituent_page(FAKE_KOSPI200_PAGE_HTML)

    assert items == [("005930", "삼성전자"), ("000660", "SK하이닉스")]


def test_fetch_kospi200_universe_paginates_and_dedupes(monkeypatch):
    monkeypatch.setattr(collectors, "KOSPI200_CONSTITUENT_PAGES", 2)

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    # 페이지 1엔 종목 2개, 페이지 2엔 그중 하나가 다시 나온다(중복 제거 확인용).
    responses = [
        FakeResponse(FAKE_KOSPI200_PAGE_HTML),
        FakeResponse(
            '<table><tr><td class="ctg"><a href="/item/main.naver?code=005930" '
            'target="_parent">삼성전자</a></td></tr></table>'
        ),
    ]

    def fake_get(*args, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr(collectors.requests, "get", fake_get)

    universe = collectors.fetch_kospi200_universe()

    assert universe == [("005930", "삼성전자"), ("000660", "SK하이닉스")]


def test_fetch_kospi200_universe_returns_none_if_any_page_fails(monkeypatch):
    monkeypatch.setattr(collectors, "KOSPI200_CONSTITUENT_PAGES", 2)
    monkeypatch.setattr(collectors.time, "sleep", lambda *_: None)

    class FakeResponse:
        text = FAKE_KOSPI200_PAGE_HTML

        def raise_for_status(self):
            pass

    calls = {"n": 0}

    def fake_get(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= collectors.MAX_RETRIES:  # 페이지 1은 계속 실패
            raise requests.ConnectionError("down")
        return FakeResponse()

    monkeypatch.setattr(collectors.requests, "get", fake_get)

    universe = collectors.fetch_kospi200_universe()

    assert universe is None  # 페이지 하나라도 실패하면 전체 실패


FAKE_SECTOR_GROUP_LIST_HTML = """
<a href="/sise/sise_group_detail.naver?type=upjong&no=307">전자제품</a>
<a href="/sise/sise_group_detail.naver?type=upjong&no=272">화학</a>
"""

FAKE_SECTOR_MEMBERS_307_HTML = """
<a href="/item/main.naver?code=066570">LG전자</a>
<a href="/item/main.naver?code=009150">삼성전기</a>
"""

FAKE_SECTOR_MEMBERS_272_HTML = """
<a href="/item/main.naver?code=051910">LG화학</a>
"""


@pytest.fixture(autouse=True)
def _isolate_sector_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(collectors, "SECTOR_CACHE_PATH", tmp_path / "sector_cache.json")


def test_parse_sector_groups_extracts_no_and_name():
    groups = collectors._parse_sector_groups(FAKE_SECTOR_GROUP_LIST_HTML)

    assert groups == [("307", "전자제품"), ("272", "화학")]


def test_parse_sector_members_extracts_codes():
    codes = collectors._parse_sector_members(FAKE_SECTOR_MEMBERS_307_HTML)

    assert codes == ["066570", "009150"]


def test_fetch_kospi200_sector_map_builds_map_from_all_groups(monkeypatch):
    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    responses = {
        ("sise_group.naver", None): FakeResponse(FAKE_SECTOR_GROUP_LIST_HTML),
        ("sise_group_detail.naver", "307"): FakeResponse(FAKE_SECTOR_MEMBERS_307_HTML),
        ("sise_group_detail.naver", "272"): FakeResponse(FAKE_SECTOR_MEMBERS_272_HTML),
    }

    def fake_get(url, params=None, headers=None, timeout=None):
        key = (url.rsplit("/", 1)[-1], (params or {}).get("no"))
        return responses[key]

    monkeypatch.setattr(collectors.requests, "get", fake_get)

    sector_map = collectors.fetch_kospi200_sector_map()

    assert sector_map == {"066570": "전자제품", "009150": "전자제품", "051910": "화학"}


def test_fetch_kospi200_sector_map_uses_fresh_cache_without_network_call(monkeypatch):
    from datetime import datetime, timezone

    collectors.SECTOR_CACHE_PATH.write_text(
        json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "sector_map": {"005930": "반도체"}})
    )

    def fail_get(*a, **k):
        raise AssertionError("신선한 캐시가 있으면 네트워크 요청을 하면 안 된다")

    monkeypatch.setattr(collectors.requests, "get", fail_get)

    sector_map = collectors.fetch_kospi200_sector_map()

    assert sector_map == {"005930": "반도체"}


def test_fetch_kospi200_sector_map_refetches_when_cache_stale(monkeypatch):
    from datetime import datetime, timedelta, timezone

    stale_time = datetime.now(timezone.utc) - timedelta(days=collectors.SECTOR_CACHE_TTL_DAYS + 1)
    collectors.SECTOR_CACHE_PATH.write_text(
        json.dumps({"fetched_at": stale_time.isoformat(), "sector_map": {"005930": "옛날업종"}})
    )

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def raise_for_status(self):
            pass

    responses = {
        ("sise_group.naver", None): FakeResponse(FAKE_SECTOR_GROUP_LIST_HTML),
        ("sise_group_detail.naver", "307"): FakeResponse(FAKE_SECTOR_MEMBERS_307_HTML),
        ("sise_group_detail.naver", "272"): FakeResponse(FAKE_SECTOR_MEMBERS_272_HTML),
    }

    def fake_get(url, params=None, headers=None, timeout=None):
        key = (url.rsplit("/", 1)[-1], (params or {}).get("no"))
        return responses[key]

    monkeypatch.setattr(collectors.requests, "get", fake_get)

    sector_map = collectors.fetch_kospi200_sector_map()

    assert sector_map == {"066570": "전자제품", "009150": "전자제품", "051910": "화학"}


def test_fetch_kospi200_sector_map_falls_back_to_stale_cache_on_refresh_failure(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(collectors.time, "sleep", lambda *_: None)
    stale_time = datetime.now(timezone.utc) - timedelta(days=collectors.SECTOR_CACHE_TTL_DAYS + 1)
    collectors.SECTOR_CACHE_PATH.write_text(
        json.dumps({"fetched_at": stale_time.isoformat(), "sector_map": {"005930": "반도체"}})
    )

    monkeypatch.setattr(
        collectors.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )

    sector_map = collectors.fetch_kospi200_sector_map()

    assert sector_map == {"005930": "반도체"}  # 갱신 실패 -> 오래된 캐시라도 사용


def test_fetch_kospi200_sector_map_returns_none_when_no_cache_and_fetch_fails(monkeypatch):
    monkeypatch.setattr(collectors.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        collectors.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )

    assert collectors.fetch_kospi200_sector_map() is None


FAKE_COMPANY_NEWS_HTML = """
<table class="type5">
<tbody>
<tr>
<td class="title"><a href="/item/news_read.naver?article_id=123&office_id=001&code=005930&page=1&sm=" class="tit">테스트 뉴스 제목</a></td>
<td class="info">테스트언론사</td>
<td class="date"> 2026.01.05 10:00</td>
</tr>
<tr>
<td class="title"><a href="/item/news_read.naver?article_id=124&office_id=002&code=005930&page=1&sm=" class="tit">두 번째 뉴스 &amp; 특수문자</a></td>
<td class="info">다른언론사</td>
<td class="date"> 2026.01.04 09:30</td>
</tr>
</tbody>
</table>
"""

FAKE_COMPANY_NEWS_EMPTY_HTML = """
<table class="type5">
<tbody>
<tr><td colspan="3">최근 1년 내 검색된 뉴스가 없습니다.</td></tr>
</tbody>
</table>
"""

FAKE_SECTOR_HUB_HTML = """
<ul>
<li><a href="/news/news_read.naver?article_id=1&office_id=1&mode=LSS3D" title="반도체 업황 개선 기대감 확산">반도체 업황..</a></li>
<li><a href="/news/news_read.naver?article_id=2&office_id=1&mode=LSS3D" title="완전히 상관없는 정치 뉴스">상관없는..</a></li>
<li><a href="/news/news_read.naver?article_id=3&office_id=1&mode=LSS3D" title="반도체 수출 역대 최대">반도체 수출..</a></li>
</ul>
"""


def test_parse_company_news_extracts_title_press_date():
    items = collectors._parse_company_news(FAKE_COMPANY_NEWS_HTML)

    assert len(items) == 2
    assert items[0].title == "테스트 뉴스 제목"
    assert items[0].press == "테스트언론사"
    assert items[0].published_at is not None
    assert items[0].published_at.year == 2026
    assert items[1].title == "두 번째 뉴스 & 특수문자"  # &amp; 언이스케이프 확인


def test_parse_sector_news_filters_by_keyword():
    items = collectors._parse_sector_news(FAKE_SECTOR_HUB_HTML, "반도체")

    assert len(items) == 2
    assert all("반도체" in item.title for item in items)


def test_fetch_company_news_sends_referer_header(monkeypatch):
    captured = {}

    class FakeResponse:
        text = FAKE_COMPANY_NEWS_HTML

        def raise_for_status(self):
            pass

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(collectors.requests, "get", fake_get)

    items = collectors.fetch_company_news("005930")

    assert items is not None
    assert len(items) == 2
    assert captured["headers"]["Referer"] == "https://finance.naver.com/item/news.naver?code=005930"


def test_fetch_company_news_no_articles_returns_empty_list_not_none(monkeypatch):
    class FakeResponse:
        text = FAKE_COMPANY_NEWS_EMPTY_HTML

        def raise_for_status(self):
            pass

    monkeypatch.setattr(collectors.requests, "get", lambda *a, **k: FakeResponse())

    items = collectors.fetch_company_news("005930")

    assert items == []  # 수집은 성공했고 그냥 뉴스가 없는 것 — None과 다름


def test_fetch_sector_news_success(monkeypatch):
    class FakeResponse:
        text = FAKE_SECTOR_HUB_HTML

        def raise_for_status(self):
            pass

    monkeypatch.setattr(collectors.requests, "get", lambda *a, **k: FakeResponse())

    items = collectors.fetch_sector_news("반도체")

    assert items is not None
    assert len(items) == 2


def test_fetch_company_news_returns_none_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(collectors.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        collectors.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )

    items = collectors.fetch_company_news("005930")

    assert items is None


def _fake_corp_code_zip() -> bytes:
    xml_content = (
        "<?xml version='1.0' encoding='UTF-8'?><result>"
        "<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
        "<stock_code>005930</stock_code><modify_date>20260101</modify_date></list>"
        "<list><corp_code>00164779</corp_code><corp_name>비상장</corp_name>"
        "<stock_code> </stock_code><modify_date>20260101</modify_date></list>"
        "</result>"
    ).encode("utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml_content)
    return buf.getvalue()


FAKE_DISCLOSURE_LIST_JSON = json.dumps(
    {
        "status": "000",
        "message": "정상",
        "list": [
            {
                "corp_code": "00126380",
                "corp_name": "삼성전자",
                "stock_code": "005930",
                "report_nm": "주요사항보고서(자기주식취득결정)",
                "rcept_no": "20260101000001",
                "flr_nm": "삼성전자",
                "rcept_dt": "20260101",
                "rm": "유",
            }
        ],
    }
)

FAKE_DISCLOSURE_NO_DATA_JSON = json.dumps({"status": "013", "message": "조회된 데이타가 없습니다."})
FAKE_DISCLOSURE_BAD_KEY_JSON = json.dumps({"status": "010", "message": "등록되지 않은 키입니다."})


def test_parse_corp_code_zip_skips_blank_stock_code():
    mapping = collectors._parse_corp_code_zip(_fake_corp_code_zip())

    assert mapping == {"005930": "00126380"}


def test_load_corp_code_map_caches_after_first_call(monkeypatch):
    monkeypatch.setattr(collectors, "_corp_code_cache", None)
    calls = {"n": 0}

    class FakeResponse:
        content = _fake_corp_code_zip()

        def raise_for_status(self):
            pass

    def fake_get(*a, **k):
        calls["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(collectors.requests, "get", fake_get)

    first = collectors._load_corp_code_map()
    second = collectors._load_corp_code_map()

    assert first == {"005930": "00126380"}
    assert second == first
    assert calls["n"] == 1  # 두 번째 호출은 캐시에서 나옴


def test_parse_disclosure_list_extracts_items():
    items = collectors._parse_disclosure_list(FAKE_DISCLOSURE_LIST_JSON)

    assert len(items) == 1
    assert items[0].report_name == "주요사항보고서(자기주식취득결정)"
    assert items[0].received_at == date(2026, 1, 1)
    assert items[0].remark == "유"


def test_parse_disclosure_list_status_013_returns_empty_list():
    assert collectors._parse_disclosure_list(FAKE_DISCLOSURE_NO_DATA_JSON) == []


def test_parse_disclosure_list_bad_status_raises():
    with pytest.raises(ValueError):
        collectors._parse_disclosure_list(FAKE_DISCLOSURE_BAD_KEY_JSON)


def test_fetch_disclosures_success(monkeypatch):
    monkeypatch.setattr(collectors, "_corp_code_cache", {"005930": "00126380"})

    class FakeResponse:
        text = FAKE_DISCLOSURE_LIST_JSON

        def raise_for_status(self):
            pass

    monkeypatch.setattr(collectors.requests, "get", lambda *a, **k: FakeResponse())

    items = collectors.fetch_disclosures("005930")

    assert items is not None
    assert len(items) == 1


def test_fetch_disclosures_ticker_not_in_map_returns_empty_list(monkeypatch):
    monkeypatch.setattr(collectors, "_corp_code_cache", {})

    items = collectors.fetch_disclosures("999999")

    assert items == []


def test_fetch_disclosures_returns_none_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(collectors, "_corp_code_cache", {"005930": "00126380"})
    monkeypatch.setattr(collectors.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        collectors.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down")),
    )

    items = collectors.fetch_disclosures("005930")

    assert items is None
