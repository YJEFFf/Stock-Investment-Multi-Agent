"""Adversarial DART responses must not become apparently healthy opinions."""
import asyncio
import io
import json
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from src import analysts, collectors, pipeline, run_history
from src.schemas import DailyRunRecord, DataCollectionUnavailable, DisclosureContext

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 10, 7, tzinfo=KST)


@pytest.fixture(autouse=True)
def isolated_dart(monkeypatch):
    def blocked(*a, **kw):
        raise AssertionError("unmocked HTTP in DART test")
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(collectors, "_corp_code_cache", {"005930": "00126380"})
    monkeypatch.setattr(collectors.time, "sleep", lambda *_: None)
    monkeypatch.setenv("DART_API_KEY", "SYNTHETIC_DART_KEY")


def row(n=1, **changes):
    return {"corp_code": "00126380", "stock_code": "005930", "corp_name": "삼성전자",
            "report_nm": "주요사항보고서(자기주식취득결정)", "flr_nm": "삼성전자",
            "rcept_dt": "20261109", "rcept_no": f"20261109{n:06d}", "rm": "유", **changes}


def page(rows=None, **changes):
    rows = [row()] if rows is None else rows
    return {"status": "000", "page_no": 1, "page_count": 100,
            "total_count": len(rows), "total_page": 1, "list": rows, **changes}


def serve(monkeypatch, pages):
    calls = []
    class Response:
        def __init__(self, payload):
            self.text = json.dumps(payload)
        def raise_for_status(self):
            pass
    def get(url, **kwargs):
        assert url == collectors.DART_LIST_URL
        params = kwargs["params"]
        calls.append(dict(params))
        return Response(pages[params["page_no"]])
    monkeypatch.setattr(collectors.requests, "get", get)
    return calls


def collect(**kw):
    return collectors.fetch_disclosures("005930", as_of=NOW, **kw)


def test_every_page_is_read_and_old_tenth_item_boundary_is_removed(monkeypatch):
    tail = row(101, report_nm="주요사항보고서(영업정지)")
    calls = serve(monkeypatch, {
        1: page([row(i) for i in range(1, 101)], total_count=101, total_page=2),
        2: page([tail], page_no=2, total_count=101, total_page=2),
    })
    items = collect()
    assert len(items) == 101 and items[-1].report_name == tail["report_nm"]
    assert [c["page_no"] for c in calls] == [1, 2]
    assert all(c["last_reprt_at"] == "Y" and c["corp_code"] == "00126380" for c in calls)
    assert items[0].corp_code == "00126380" and items[0].stock_code == "005930"


@pytest.mark.parametrize("second", [
    {"status": "013"},
    page([row(101)], page_no=2, total_count=102, total_page=2),
    page([row(1)], page_no=2, total_count=101, total_page=2),
    page([], page_no=2, total_count=101, total_page=2),
    page([row(101, rcept_dt="20261110")], page_no=2, total_count=101, total_page=2),
    {"status": "020"},
])
def test_later_page_failure_never_returns_partial_success(monkeypatch, second):
    serve(monkeypatch, {1: page([row(i) for i in range(1, 101)], total_count=101, total_page=2), 2: second})
    assert collect() is None


def test_resource_limit_fails_instead_of_slicing(monkeypatch):
    serve(monkeypatch, {1: page([row(i) for i in range(11)])})
    assert collect(limit=10) is None


def test_global_resource_guard_stops_before_loading_more_pages(monkeypatch):
    calls = serve(monkeypatch, {1: page([row(i) for i in range(100)], total_count=10001, total_page=101)})
    assert collect() is None and len(calls) == 1


@pytest.mark.parametrize("mapping", [None, {}, {"005930": ""}, {"005930": "123"}, {"005930": 126380}])
def test_unverified_mapping_is_failure_without_list_request(monkeypatch, mapping):
    monkeypatch.setattr(collectors, "_load_corp_code_map", lambda: mapping)
    assert collect() is None


def test_mapping_failure_propagates_into_daily_health_without_model_call(monkeypatch):
    monkeypatch.setattr(collectors, "_corp_code_cache", None)
    monkeypatch.setattr(collectors, "_fetch_with_retries", lambda *a, **kw: None)
    async def forbidden(**kw):
        pytest.fail("No model should see missing collection")
    monkeypatch.setattr(analysts.llm, "call_structured", forbidden)
    record = DailyRunRecord(run_id="test", day="2026-11-10", started_at=NOW,
                            git_commit="test", provider="mock", model="mock")
    token = run_history.CURRENT.set(record)
    try:
        assert asyncio.run(pipeline.make_disclosure_analyst_fn()("005930", "", NOW)) == []
    finally:
        run_history.CURRENT.reset(token)
    assert record.sources[0].status == "failed" and run_history.input_failed(record)


@pytest.mark.parametrize("payload", [
    {"status": "000"}, {"status": "000", "total_count": 2}, [],
    page(None, page_no=True), page(None, total_page=2), page(None, page_count=10),
    page(None, total_count=2), page(None, list=None), page([]),
    {"status": "013", "total_count": 1}, {"status": "013", "list": [row()]},
    {"status": "013", "total_count": False}, {"status": "013", "total_count": 0.0},
    {"status": "013", "page_no": 2},
    page([row(), row()]), page([row(corp_code="00999999")]),
    page([row(stock_code="000660")]), page([row(rcept_no="bad")]),
    page([row(rcept_dt="20261011")]), page([row(rcept_dt="20261111")]),
    page([row(rcept_dt="20261301")]), page([row(report_nm=" ")]),
    page([row(flr_nm=None)]), page([row(rm="未知")]), page([row(rm=0)]),
])
def test_bad_api_response_is_not_success_or_empty(monkeypatch, payload):
    serve(monkeypatch, {1: payload})
    assert collect() is None


def test_verified_no_data_is_empty(monkeypatch):
    serve(monkeypatch, {1: {"status": "013", "message": "조회된 데이터가 없습니다."}})
    assert collect() == []


@pytest.mark.parametrize("hour", [7, 9, 16])
def test_same_kst_day_uses_same_explicit_thirty_calendar_day_window(monkeypatch, hour):
    calls = serve(monkeypatch, {1: page()})
    assert collectors.fetch_disclosures("005930", as_of=NOW.replace(hour=hour))
    assert calls[0]["bgn_de"] == "20261012" and calls[0]["end_de"] == "20261110"
    assert collectors.disclosure_window(NOW.astimezone(ZoneInfo("UTC"))) == collectors.disclosure_window(NOW)


@pytest.mark.parametrize("remark", ["정", "유정", "철", "공정"])
def test_unresolved_final_report_is_unavailable_but_history_preserves_marker(monkeypatch, remark):
    calls = serve(monkeypatch, {1: page([row(rm=remark)])})
    assert collect() is None
    history = collect(final_only=False)
    assert history[0].remark == remark
    assert [c["last_reprt_at"] for c in calls] == ["Y", "N"]


def test_final_correction_title_is_allowed_and_prompt_is_explicit(monkeypatch):
    serve(monkeypatch, {1: page([row(report_nm="[기재정정]주요사항보고서(자기주식취득결정)")])})
    calls = []
    async def fake(**kwargs):
        calls.append(kwargs["user"])
        return analysts._DisclosureAnalysisResponse(score=0.0, confidence=0.2, reasoning="terms unknown")
    monkeypatch.setattr(analysts.llm, "call_structured", fake)
    opinion = asyncio.run(pipeline.make_disclosure_analyst_fn()("005930", "", NOW))[0]
    assert opinion.source_context.latest_received_at.isoformat() == "2026-11-09"
    assert opinion.source_context.query_end == NOW.date()
    assert opinion.as_of.isoformat() == "2026-11-09T00:00:00+09:00"
    assert opinion.source_context.as_of == NOW
    assert "2026-10-12 ~ 2026-11-10" in calls[0]
    assert "20261109000001" in calls[0] and "not report bodies" in calls[0]
    assert opinion.evidence[0].startswith("prompt:disclosure@")


@pytest.mark.parametrize("changes", [dict(rm="유정"), dict(rm="철"), dict(stock_code="000660"), dict(rcept_dt="20261111")])
def test_direct_analyst_context_cannot_bypass_integrity_checks(changes):
    items = collectors._parse_disclosure_list(json.dumps(page([row(**changes)])))
    with pytest.raises(DataCollectionUnavailable):
        asyncio.run(analysts.disclosure_analyst(DisclosureContext(ticker="005930", as_of=NOW, disclosures=items)))


def zipped(xml):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("CORPCODE.xml", xml)
    return output.getvalue()


@pytest.mark.parametrize("xml", [
    "<result/>", "<unexpected/>",
    "<result><list><stock_code>005930</stock_code><corp_code/></list></result>",
    "<result>" + "<list><stock_code>005930</stock_code><corp_code>00126380</corp_code></list>" * 2 + "</result>",
])
def test_bad_corporation_file_is_not_cacheable(xml):
    with pytest.raises(ValueError):
        collectors._parse_corp_code_zip(zipped(xml))


def test_empty_mapping_is_never_cached(monkeypatch):
    monkeypatch.setattr(collectors, "_corp_code_cache", None)
    responses = iter([{}, {"005930": "00126380"}])
    monkeypatch.setattr(collectors, "_fetch_with_retries", lambda *a, **kw: next(responses))
    assert collectors._load_corp_code_map() is None
    assert collectors._load_corp_code_map() == {"005930": "00126380"}


def test_dart_error_logs_and_repair_requests_never_contain_key(monkeypatch, caplog):
    secret = "SYNTHETIC_DART_KEY"
    def fail(*a, **kw):
        raise requests.HTTPError(f"503: {collectors.DART_LIST_URL}?crtfc_key={secret}")
    monkeypatch.setattr(collectors.requests, "get", fail)
    repairs = []
    monkeypatch.setattr(collectors, "_queue_collection_failure", lambda *a, **kw: repairs.append((a, kw)))
    assert collectors._fetch_with_retries(collectors.DART_LIST_URL, lambda s: s,
        params={"crtfc_key": secret}, source="audit_test") is None
    assert secret not in caplog.text and "crtfc_key=" not in caplog.text
    assert "HTTPError" in caplog.text and secret not in str(repairs)


def test_legacy_context_roundtrip_preserves_original_hash():
    from src.thesis_shadow import digest
    original = {
        "ticker": "005930", "as_of": "2026-11-10T07:00:00+09:00",
        "disclosures": [{"report_name": "공시", "submitter": "회사", "received_at": "2026-11-09",
                         "receipt_no": "20261109000001", "remark": None,
                         "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261109000001"}],
    }
    parsed = DisclosureContext.model_validate(original)
    assert parsed.model_dump(mode="json") == original
    assert digest(parsed) == digest(original)
    assert parsed.model_dump(include={"ticker"}) == {"ticker": "005930"}
    assert "query_begin" not in parsed.model_dump(exclude_none=True)
