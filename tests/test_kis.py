import json
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from src import kis

FAKE_CHART_RESPONSE = {
    "rt_cd": "0",
    "msg_cd": "MCA00000",
    "msg1": "정상처리 되었습니다.",
    "output1": {},
    "output2": [
        {
            "stck_bsop_date": "20260102",
            "stck_oprc": "70000",
            "stck_hgpr": "71000",
            "stck_lwpr": "69500",
            "stck_clpr": "70500",
            "acml_vol": "1000000",
        },
        {
            "stck_bsop_date": "20260101",
            "stck_oprc": "69000",
            "stck_hgpr": "70000",
            "stck_lwpr": "68500",
            "stck_clpr": "69500",
            "acml_vol": "900000",
        },
    ],
}

RATE_LIMITED_RESPONSE = {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."}
BAD_TICKER_RESPONSE = {"rt_cd": "1", "msg_cd": "SOME_OTHER_ERROR", "msg1": "존재하지 않는 종목코드입니다."}


@pytest.fixture(autouse=True)
def _isolate_token_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(kis, "TOKEN_CACHE_PATH", tmp_path / "token_cache.json")
    monkeypatch.setattr(kis, "_last_request_at", 0.0)
    monkeypatch.setattr(kis.time, "sleep", lambda *_: None)


def _fake_token_response(access_token="tok123", expires_in=86400):
    class FakeResponse:
        def json(self):
            return {"access_token": access_token, "expires_in": expires_in}

    return FakeResponse()


def test_get_access_token_fetches_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        return _fake_token_response()

    monkeypatch.setattr(kis.requests, "post", fake_post)

    token = kis.get_access_token()

    assert token == "tok123"
    assert calls["n"] == 1
    assert json.loads(kis.TOKEN_CACHE_PATH.read_text())["access_token"] == "tok123"


def test_get_access_token_reuses_valid_cache(monkeypatch):
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    kis.TOKEN_CACHE_PATH.write_text(json.dumps({"access_token": "cached-tok", "expires_at": expires_at.isoformat()}))

    def fail_post(*a, **k):
        raise AssertionError("should not re-fetch a still-valid cached token")

    monkeypatch.setattr(kis.requests, "post", fail_post)

    token = kis.get_access_token()

    assert token == "cached-tok"


def test_get_access_token_refetches_when_cache_expired(monkeypatch):
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    kis.TOKEN_CACHE_PATH.write_text(json.dumps({"access_token": "stale-tok", "expires_at": expired_at.isoformat()}))

    monkeypatch.setattr(kis.requests, "post", lambda *a, **k: _fake_token_response("fresh-tok"))

    token = kis.get_access_token()

    assert token == "fresh-tok"


def test_get_access_token_returns_none_on_network_failure(monkeypatch):
    monkeypatch.setattr(
        kis.requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("down"))
    )

    assert kis.get_access_token() is None


def test_kis_get_retries_on_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    responses = [FakeResponse(RATE_LIMITED_RESPONSE), FakeResponse(FAKE_CHART_RESPONSE)]

    def fake_get(*a, **k):
        return responses.pop(0)

    monkeypatch.setattr(kis.requests, "get", fake_get)

    data = kis._kis_get("/some/path", "TRID", {})

    assert data == FAKE_CHART_RESPONSE


def test_kis_get_returns_none_after_rate_limit_exhausts_retries(monkeypatch):
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")

    class FakeResponse:
        def json(self):
            return RATE_LIMITED_RESPONSE

    monkeypatch.setattr(kis.requests, "get", lambda *a, **k: FakeResponse())

    assert kis._kis_get("/some/path", "TRID", {}) is None


def test_kis_get_returns_none_immediately_on_non_rate_limit_api_error(monkeypatch):
    """일반 API 오류(잘못된 종목코드 등)는 재시도해도 결과가 바뀌지 않으므로
    즉시 실패 처리하고 재시도하지 않는다."""
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")

    calls = {"n": 0}

    class FakeResponse:
        def json(self):
            return BAD_TICKER_RESPONSE

    def fake_get(*a, **k):
        calls["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(kis.requests, "get", fake_get)

    result = kis._kis_get("/some/path", "TRID", {})

    assert result is None
    assert calls["n"] == 1


def test_kis_get_returns_none_when_token_unavailable(monkeypatch):
    monkeypatch.setattr(kis, "get_access_token", lambda: None)

    assert kis._kis_get("/some/path", "TRID", {}) is None


def test_parse_daily_chart_extracts_bars_oldest_first():
    bars = kis._parse_daily_chart(FAKE_CHART_RESPONSE)

    assert len(bars) == 2
    assert bars[0].date == date(2026, 1, 1)  # 오래된 순 정렬 확인 (원본은 최신이 먼저 옴)
    assert bars[1].date == date(2026, 1, 2)
    assert bars[1].close == 70500
    assert bars[1].volume == 1000000


def test_fetch_daily_ohlcv_success(monkeypatch):
    monkeypatch.setattr(kis, "_kis_get", lambda path, tr_id, params: FAKE_CHART_RESPONSE)

    bars = kis.fetch_daily_ohlcv("005930", lookback_days=60)

    assert bars is not None
    assert len(bars) == 2


def test_fetch_daily_ohlcv_returns_none_when_request_fails(monkeypatch):
    monkeypatch.setattr(kis, "_kis_get", lambda path, tr_id, params: None)

    assert kis.fetch_daily_ohlcv("005930") is None


def test_fetch_daily_ohlcv_trims_to_lookback_days(monkeypatch):
    monkeypatch.setattr(kis, "_kis_get", lambda path, tr_id, params: FAKE_CHART_RESPONSE)

    bars = kis.fetch_daily_ohlcv("005930", lookback_days=1)

    assert len(bars) == 1
    assert bars[0].date == date(2026, 1, 2)  # 최신 것만 남는다
