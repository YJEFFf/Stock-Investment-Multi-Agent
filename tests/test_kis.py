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
    monkeypatch.setattr(kis, "_kis_get", lambda path, tr_id, params, policy=None: FAKE_CHART_RESPONSE)

    bars = kis.fetch_daily_ohlcv("005930", lookback_days=60)

    assert bars is not None
    assert len(bars) == 2


def test_fetch_daily_ohlcv_returns_none_when_request_fails(monkeypatch):
    monkeypatch.setattr(kis, "_kis_get", lambda path, tr_id, params, policy=None: None)

    assert kis.fetch_daily_ohlcv("005930") is None


def test_fetch_daily_ohlcv_trims_to_lookback_days(monkeypatch):
    monkeypatch.setattr(kis, "_kis_get", lambda path, tr_id, params, policy=None: FAKE_CHART_RESPONSE)

    bars = kis.fetch_daily_ohlcv("005930", lookback_days=1)

    assert len(bars) == 1
    assert bars[0].date == date(2026, 1, 2)  # 최신 것만 남는다


# --- _kis_post / _kis_request (POST 경로) ---


def test_kis_post_sends_body_and_returns_data_on_success(monkeypatch):
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")
    captured = {}

    class FakeResponse:
        def json(self):
            return {"rt_cd": "0", "msg_cd": "0", "msg1": "ok", "output": {"ODNO": "123"}}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["headers"] = headers
        captured["data"] = data
        return FakeResponse()

    monkeypatch.setattr(kis.requests, "post", fake_post)

    result = kis._kis_post("/some/path", "TRID", {"A": "1"})

    assert result["output"]["ODNO"] == "123"
    assert captured["headers"]["custtype"] == "P"
    assert json.loads(captured["data"]) == {"A": "1"}


def test_kis_post_returns_none_on_api_error(monkeypatch):
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")

    class FakeResponse:
        def json(self):
            return {"rt_cd": "1", "msg_cd": "SOME_ERROR", "msg1": "실패"}

    monkeypatch.setattr(kis.requests, "post", lambda *a, **k: FakeResponse())

    assert kis._kis_post("/some/path", "TRID", {}) is None


# --- fetch_current_price ---


def test_fetch_current_price_success(monkeypatch):
    monkeypatch.setattr(
        kis,
        "_kis_get",
        lambda path, tr_id, params, policy=None: {"rt_cd": "0", "output": {"stck_prpr": "231000"}},
    )

    assert kis.fetch_current_price("005930") == 231000.0


def test_fetch_current_price_returns_none_when_request_fails(monkeypatch):
    monkeypatch.setattr(kis, "_kis_get", lambda path, tr_id, params, policy=None: None)

    assert kis.fetch_current_price("005930") is None


# --- fetch_account_balance ---


def test_fetch_account_balance_success(monkeypatch):
    monkeypatch.setattr(
        kis, "_kis_get", lambda path, tr_id, params, policy=None: {"output2": [{"tot_evlu_amt": "100000000"}]}
    )

    assert kis.fetch_account_balance() == 100000000.0


def test_fetch_account_balance_returns_none_when_output_empty(monkeypatch):
    monkeypatch.setattr(kis, "_kis_get", lambda path, tr_id, params, policy=None: {"output2": []})

    assert kis.fetch_account_balance() is None


# --- place_market_buy_order ---


def test_place_market_buy_order_returns_order_number(monkeypatch):
    captured = {}

    def fake_post(path, tr_id, body):
        captured["body"] = body
        captured["tr_id"] = tr_id
        return {"output": {"ODNO": "0000123456"}}

    monkeypatch.setattr(kis, "_kis_post", fake_post)

    order_no = kis.place_market_buy_order("005930", 10)

    assert order_no == "0000123456"
    assert captured["body"]["PDNO"] == "005930"
    assert captured["body"]["ORD_QTY"] == "10"
    assert captured["body"]["ORD_DVSN"] == "01"  # 시장가
    assert captured["tr_id"] == kis.ORDER_BUY_TR_ID


def test_place_market_buy_order_rejects_non_positive_quantity(monkeypatch):
    def fail_post(*a, **k):
        raise AssertionError("수량이 0 이하면 API를 호출하면 안 된다")

    monkeypatch.setattr(kis, "_kis_post", fail_post)

    assert kis.place_market_buy_order("005930", 0) is None


def test_place_market_buy_order_returns_none_when_request_fails(monkeypatch):
    monkeypatch.setattr(kis, "_kis_post", lambda path, tr_id, body: None)

    assert kis.place_market_buy_order("005930", 10) is None


# --- place_market_sell_order ---


def test_place_market_sell_order_returns_order_number(monkeypatch):
    captured = {}

    def fake_post(path, tr_id, body):
        captured["body"] = body
        captured["tr_id"] = tr_id
        return {"output": {"ODNO": "0000654321"}}

    monkeypatch.setattr(kis, "_kis_post", fake_post)

    order_no = kis.place_market_sell_order("005930", 10)

    assert order_no == "0000654321"
    assert captured["body"]["PDNO"] == "005930"
    assert captured["body"]["ORD_QTY"] == "10"
    assert captured["tr_id"] == kis.ORDER_SELL_TR_ID


def test_place_market_sell_order_rejects_non_positive_quantity(monkeypatch):
    def fail_post(*a, **k):
        raise AssertionError("수량이 0 이하면 API를 호출하면 안 된다")

    monkeypatch.setattr(kis, "_kis_post", fail_post)

    assert kis.place_market_sell_order("005930", 0) is None


def test_place_market_sell_order_returns_none_when_request_fails(monkeypatch):
    monkeypatch.setattr(kis, "_kis_post", lambda path, tr_id, body: None)

    assert kis.place_market_sell_order("005930", 10) is None


# --- fetch_fill_price ---


def test_fetch_fill_price_success(monkeypatch):
    monkeypatch.setattr(
        kis, "_kis_get", lambda path, tr_id, params, policy=None: {"output2": {"pchs_avg_pric": "231500.0000"}}
    )

    price = kis.fetch_fill_price("005930", date(2026, 8, 9))

    assert price == pytest.approx(231500.0)


def test_fetch_fill_price_returns_none_when_no_fills(monkeypatch):
    monkeypatch.setattr(
        kis, "_kis_get", lambda path, tr_id, params, policy=None: {"output2": {"pchs_avg_pric": "0"}}
    )

    assert kis.fetch_fill_price("005930", date(2026, 8, 9)) is None


def test_fetch_fill_price_returns_none_when_request_fails(monkeypatch):
    monkeypatch.setattr(kis, "_kis_get", lambda path, tr_id, params, policy=None: None)

    assert kis.fetch_fill_price("005930", date(2026, 8, 9)) is None


# --- 용량 거부 분류 / 주문 재시도 금지 (2026-08-19) ---

LEDGER_LIMITED_RESPONSE = {
    "rt_cd": "1",
    "msg_cd": "UNKNOWN_LEDGER_CODE",
    "msg1": "원장에서 허용 가능한 초당 거래건수를 초과하였습니다.",
}


def test_is_capacity_rejection_matches_both_layers():
    """게이트웨이 한도(EGW00201)와 원장 한도는 문구가 다르다 — 둘 다 잡아야 한다."""
    assert kis._is_capacity_rejection(RATE_LIMITED_RESPONSE)
    assert kis._is_capacity_rejection(LEDGER_LIMITED_RESPONSE)
    assert not kis._is_capacity_rejection(BAD_TICKER_RESPONSE)


def test_kis_get_retries_on_ledger_capacity_rejection(monkeypatch):
    """msg_cd를 모르는 원장 거부도 재시도 대상이다.

    2026-08-19에 이게 rt_cd != "0" 분기로 떨어져 재시도 0회로 실패했고,
    그날 유일한 승인 매수(012450)가 집행되지 못했다.
    """
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    responses = [FakeResponse(LEDGER_LIMITED_RESPONSE), FakeResponse(FAKE_CHART_RESPONSE)]
    monkeypatch.setattr(kis.requests, "get", lambda *a, **k: responses.pop(0))

    assert kis._kis_get("/some/path", "TRID", {}) == FAKE_CHART_RESPONSE


def test_kis_post_does_not_retry_capacity_rejection(monkeypatch):
    """주문은 재전송하면 두 번 체결될 수 있어 한 번만 보낸다.

    용량 거부는 브로커가 "안 받았다"고 답한 것이라 그대로 실패로 돌린다.
    """
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")
    calls = {"n": 0}

    class FakeResponse:
        def json(self):
            return RATE_LIMITED_RESPONSE

    def fake_post(*a, **k):
        calls["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(kis.requests, "post", fake_post)

    assert kis._kis_post("/some/path", "TRID", {}) is None
    assert calls["n"] == 1


def test_kis_post_raises_order_response_lost_without_resending(monkeypatch):
    """응답을 못 받은 주문은 접수 여부를 알 수 없다 — 재전송 금지, 예외로 올린다."""
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")
    calls = {"n": 0}

    def fake_post(*a, **k):
        calls["n"] += 1
        raise requests.ConnectionError("read timeout")

    monkeypatch.setattr(kis.requests, "post", fake_post)

    with pytest.raises(kis.OrderResponseLost):
        kis._kis_post("/some/path", "TRID", {})
    assert calls["n"] == 1


def test_kis_get_still_retries_network_errors(monkeypatch):
    """조회는 멱등이라 네트워크 예외를 계속 재시도한다(규칙 4)."""
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")
    calls = {"n": 0}

    class FakeResponse:
        def json(self):
            return FAKE_CHART_RESPONSE

    def fake_get(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("down")
        return FakeResponse()

    monkeypatch.setattr(kis.requests, "get", fake_get)

    assert kis._kis_get("/some/path", "TRID", {}) == FAKE_CHART_RESPONSE
    assert calls["n"] == 3


# --- RetryPolicy (2026-08-21 장애 대응) ---


def test_backoff_clamps_to_last_value_past_the_table():
    policy = kis.RetryPolicy(timeout_seconds=1.0, max_attempts=5, backoff_seconds=(1.5, 3.0))

    assert policy.backoff_for(1) == 1.5
    assert policy.backoff_for(2) == 3.0
    assert policy.backoff_for(3) == 3.0  # 표를 넘어가면 마지막 값을 계속 쓴다


def test_fast_fail_policy_worst_case_fits_inside_the_one_minute_cron():
    """이 정책의 존재 이유 자체다 — 숫자가 아니라 제약을 잠근다.

    손절 체크는 매분 도는데, 한 회차가 60초를 넘기면 다음 분이 락에 막혀 통째로
    스킵된다(portfolio_store.portfolio_lock, blocking=False). 2026-08-21 13:01에
    기존 예산(5시도 x 10초 + 백오프 20.5초 = 실측 64초)이 정확히 그래서 13:02를
    날렸고, 보유 5종목의 손절 판정이 2분간 비었다.
    """
    policy = kis.FAST_FAIL_POLICY
    worst_case = policy.max_attempts * policy.timeout_seconds + sum(
        policy.backoff_for(i) for i in range(1, policy.max_attempts)
    )

    assert worst_case < 60.0


def test_default_policy_still_rides_out_the_ledger_capacity_window():
    """하루 한 번짜리 경로는 예산을 줄이지 않는다 — 2026-08-19 회귀 방지.

    09:00:07 원장 용량 거부 한 번에 그날 유일한 승인 매수가 날아간 뒤 넣은 게
    이 누적 백오프다. 손절 체크가 빨라져야 한다는 이유로 여기까지 같이 깎으면
    같은 사고가 재발한다.
    """
    policy = kis.DEFAULT_POLICY
    cumulative_backoff = sum(policy.backoff_for(i) for i in range(1, policy.max_attempts))

    assert policy.max_attempts == 5
    assert cumulative_backoff >= 20.0


def test_kis_get_stops_at_the_policy_attempt_limit(monkeypatch):
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")
    monkeypatch.setattr(kis.time, "sleep", lambda seconds: None)

    class FakeResponse:
        def json(self):
            return RATE_LIMITED_RESPONSE

    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(kis.requests, "get", fake_get)

    assert kis._kis_get("/some/path", "TRID", {}, policy=kis.FAST_FAIL_POLICY) is None
    assert calls["n"] == kis.FAST_FAIL_POLICY.max_attempts  # 기본값 5가 아니라 2


def test_kis_get_uses_the_policy_timeout(monkeypatch):
    monkeypatch.setattr(kis, "get_access_token", lambda: "tok")
    seen = {}

    class FakeResponse:
        def json(self):
            return FAKE_CHART_RESPONSE

    def fake_get(*a, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        return FakeResponse()

    monkeypatch.setattr(kis.requests, "get", fake_get)

    kis._kis_get("/some/path", "TRID", {}, policy=kis.FAST_FAIL_POLICY)

    assert seen["timeout"] == kis.FAST_FAIL_POLICY.timeout_seconds


def test_fetch_current_price_defaults_to_the_patient_policy(monkeypatch):
    """정책을 명시하지 않은 호출처는 전부 기존 예산 그대로여야 한다."""
    seen = {}

    def fake_get(path, tr_id, params, policy=None):
        seen["policy"] = policy
        return {"rt_cd": "0", "output": {"stck_prpr": "231000"}}

    monkeypatch.setattr(kis, "_kis_get", fake_get)

    kis.fetch_current_price("005930")

    assert seen["policy"] == kis.DEFAULT_POLICY
