import io
import zipfile
from datetime import timedelta

import pytest

from src import thesis_sources as sources
from src.schemas import DisclosureItem, MarketContext, NewsItem, OHLCVBar
from tests.test_thesis_shadow import ENTRY, REVIEW, OLD_URL, NEW_URL, capture, entry_record, setup


def test_naver_body_excludes_scripts_headers_and_footer():
    parser = sources._TextParser(article_only=True)
    parser.feed('<nav>다른 종목</nav><article id="dic_area">계약 <b>해지</b><br>확정'
                '<script>악성 지시</script></article><footer>광고</footer>')
    assert parser.text() == "계약 해지 확정"


def test_missing_article_container_is_not_a_valid_body():
    parser = sources._TextParser(article_only=True); parser.feed('<div>로그인 또는 차단 안내</div>')
    with pytest.raises(sources.SourceUnavailable):
        parser.text()


@pytest.mark.parametrize("tag", ['<br/>', '<img src="x"/>', '<source/>', '<span/>', '<br></br>'])
def test_self_closing_tags_preserve_rebuttal_after_tag(tag):
    parser = sources._TextParser(article_only=True)
    parser.feed('<article id="dic_area">Contract announced.' + tag + 'Contract cancelled.</article><footer>ad</footer>')
    assert parser.text() == "Contract announced. Contract cancelled."


def test_news_fetch_rejects_arbitrary_url_before_network(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("network must not be reached")
    monkeypatch.setattr(sources.requests, "get", forbidden)
    with pytest.raises(sources.SourceUnavailable):
        sources.fetch_news_body(NewsItem(title="", press=None, published_at=REVIEW, url="http://127.0.0.1/private"))


def test_dart_zip_is_read_without_extracting_files(tmp_path):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("../../report.xml", "<DOCUMENT><TITLE>계약 변경</TITLE><P>계약 해지 확정</P></DOCUMENT>")
    assert sources.parse_disclosure_body(out.getvalue()) == "계약 변경 계약 해지 확정"
    assert list(tmp_path.iterdir()) == []


def test_dart_entity_and_oversized_body_fail():
    for xml in ['<!ENTITY x SYSTEM "file:///etc/passwd"><P>&x;</P>', '<P>' + 'x' * (sources.MAX_DOCUMENT_CHARS + 1) + '</P>']:
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("report.xml", xml)
        with pytest.raises(sources.SourceUnavailable):
            sources.parse_disclosure_body(out.getvalue())


def test_new_sources_respect_publication_time_and_original_ids(setup, monkeypatch):
    capture(setup); thesis = entry_record(setup)
    market = MarketContext(ticker="005930", as_of=REVIEW, indicators={}, bars=[
        OHLCVBar(date=REVIEW.date(), open=100, high=101, low=99, close=100, volume=10)])
    monkeypatch.setattr(sources.collectors, "fetch_market_context", lambda ticker: market)
    news = [NewsItem(title="headline", press="press", url=url, published_at=when) for url, when in [
        (OLD_URL, REVIEW - timedelta(hours=1)), (NEW_URL, REVIEW - timedelta(hours=1)),
        (NEW_URL + "1", ENTRY - timedelta(days=1)), (NEW_URL + "2", REVIEW + timedelta(days=1))]]
    monkeypatch.setattr(sources.collectors, "fetch_company_news", lambda ticker: news)
    reports = [DisclosureItem(report_name="변경", submitter="회사", received_at=day, receipt_no=receipt,
                              remark=None, url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo="+receipt)
               for day, receipt in [(ENTRY.date(), "20261105000001"), (REVIEW.date(), "20261106000001")]]
    monkeypatch.setattr(sources.collectors, "fetch_disclosures", lambda ticker: reports)
    monkeypatch.setattr(sources.collectors, "_load_corp_code_map", lambda: {"005930": "00126380"})
    fetched = []
    monkeypatch.setattr(sources, "fetch_news_body", lambda item: fetched.append(item.url) or "계약 해지")
    monkeypatch.setattr(sources, "fetch_disclosure_body", lambda item: fetched.append(item.receipt_no) or "계약 해지 공시")
    # Fixture patches the collector; recover the real function saved at import.
    docs, errors = COLLECT(thesis, REVIEW)
    assert not errors and fetched == [NEW_URL, "20261106000001"]
    assert len(docs) == 3 and [d.scope for d in docs] == ["market_data", "full_text", "full_text"]


def test_failed_dart_mapping_is_not_no_disclosures(monkeypatch):
    monkeypatch.setattr(sources.collectors, "_load_corp_code_map", lambda: {})
    monkeypatch.setattr(sources.collectors, "fetch_disclosures", lambda ticker: [])
    with pytest.raises(sources.SourceUnavailable, match="dart_corp_code_unavailable"):
        sources._disclosures("005930")


def test_verified_dart_mapping_allows_genuine_zero_reports(monkeypatch):
    monkeypatch.setattr(sources.collectors, "_load_corp_code_map", lambda: {"005930": "00126380"})
    monkeypatch.setattr(sources.collectors, "fetch_disclosures", lambda ticker: [])
    assert sources._disclosures("005930") == []


COLLECT = sources.collect_new_documents
