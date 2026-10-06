import base64
import fcntl
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src import app_notifications, mobile_app, mobile_push, notify
from src.mobile_app import create_app, valid_subscription
from src.mobile_data import snapshot
from src.mobile_store import Store

ORIGIN = "https://sima.test"
HEADERS = {"Origin": ORIGIN, "X-SIMA-Request": "1"}
KST = ZoneInfo("Asia/Seoul")


def keyfile(path):
    key = ec.generate_private_key(ec.SECP256R1())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


def subscription(endpoint="https://web.push.apple.com/Qtest"):
    key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return {"endpoint": endpoint, "expirationTime": None, "keys": {"p256dh": encode(key), "auth": encode(b"1234567890abcdef")}}


@pytest.fixture
def web(tmp_path):
    keyfile(tmp_path / "state/vapid.pem")
    app = create_app({"TESTING": True, "ORIGIN": ORIGIN, "STATE_DIR": tmp_path / "state", "DATA_DIR": tmp_path / "data"})
    client = app.test_client()
    return app, client, app.extensions["sima_store"]


def request(client, method, path, **kwargs):
    return client.open(path, method=method, base_url=ORIGIN, **kwargs)


def login(web):
    _, client, store = web
    return request(client, "POST", "/api/login", json={"code": store.pairing_code()}, headers=HEADERS)


def write_rows(root, name, rows):
    root.mkdir(exist_ok=True, parents=True)
    (root / name).write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.mark.parametrize("path", ["/api/snapshot", "/api/snapshot?refresh=1", "/api/alerts", "/api/push"])
def test_private_routes_require_a_device_session(web, path):
    assert request(web[1], "GET", path).status_code == 401


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.test", "X-SIMA-Request": "1"}, {"Origin": ORIGIN}])
def test_login_rejects_cross_site_requests(web, headers):
    assert request(web[1], "POST", "/api/login", json={"code": web[2].pairing_code()}, headers=headers).status_code == 403


def test_pairing_single_use_secure_cookie_and_logout(web):
    _, client, store = web
    code = store.pairing_code()
    response = request(client, "POST", "/api/login", json={"code": code}, headers=HEADERS)
    assert response.status_code == 200
    assert all(flag in response.headers["Set-Cookie"] for flag in ["__Host-sima=", "Secure", "HttpOnly", "SameSite=Strict", "Path=/"])
    assert store.login(code) is None
    result = request(client, "GET", "/api/snapshot")
    assert result.status_code == 200
    assert result.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in result.headers["Content-Security-Policy"]
    assert request(client, "POST", "/api/logout", json={}, headers=HEADERS).status_code == 200
    assert request(client, "GET", "/api/snapshot").status_code == 401


def test_expired_pairing_code_cannot_authenticate(web):
    code = web[2].pairing_code(hours=-1)
    assert web[2].login(code) is None


def test_pairing_attempts_are_rate_limited(web):
    for _ in range(10):
        assert request(web[1], "POST", "/api/login", json={"code": "0000"}, headers=HEADERS).status_code == 401
    assert request(web[1], "POST", "/api/login", json={"code": web[2].pairing_code()}, headers=HEADERS).status_code == 429


@pytest.mark.parametrize("path", ["/.env", "/logs/account_nav.jsonl", "/src/kis.py", "/assets/../../.env"])
def test_app_does_not_serve_operational_files(web, path):
    assert request(web[1], "GET", path).status_code == 404


@pytest.mark.parametrize("url", ["http://web.push.apple.com/token", "https://127.0.0.1/token", "https://169.254.169.254/latest", "https://web.push.apple.com.evil.test/token", "https://user@web.push.apple.com/token", "https://web.push.apple.com:8443/token"])
def test_push_subscription_cannot_be_used_for_ssrf(url):
    with pytest.raises(ValueError):
        valid_subscription(subscription(url))


def test_valid_push_subscription_and_keys():
    assert valid_subscription(subscription())["endpoint"].startswith("https://web.push.apple.com/")
    bad = subscription()
    bad["keys"]["p256dh"] = "A" * 87
    with pytest.raises(ValueError):
        valid_subscription(bad)


def test_push_registration_test_notification_and_logout_cancel(web):
    _, client, store = web
    login(web)
    assert request(client, "POST", "/api/push", json=subscription(), headers=HEADERS).status_code == 200
    assert request(client, "GET", "/api/push").json["enabled"] is True
    response = request(client, "POST", "/api/push/test", json={}, headers=HEADERS)
    assert response.json["queued"] is True
    assert len(store.pending()) == 1
    assert request(client, "POST", "/api/logout", json={}, headers=HEADERS).status_code == 200
    assert store.pending() == []


def test_alert_lands_in_app_outbox(monkeypatch):
    monkeypatch.setenv("SIMA_APP_ALERTS_ENABLED", "1")
    assert notify.send_telegram_alert("⚠️ [SIMA] 오류 — 데이터 수집 실패\n오늘 신규 판단 중지") is True
    row = json.loads(app_notifications.DEFAULT_APP_ALERTS_PATH.read_text())
    assert row["kind"] == "error"
    assert "수집 실패" in row["title"]


def test_app_outbox_write_failure_is_reported(monkeypatch):
    # 텔레그램 폴백이 없어졌으니 실패는 False로 드러나야 한다(alert_once가 마커를 안 남긴다).
    monkeypatch.setenv("SIMA_APP_ALERTS_ENABLED", "1")
    monkeypatch.setattr(app_notifications, "DEFAULT_APP_ALERTS_PATH", Path("/dev/null/not-a-dir"))
    assert notify.send_telegram_alert("test") is False


def test_stuck_app_outbox_writer_delays_the_caller_only_briefly(monkeypatch):
    # 매매 호출자(주문·손절 경로)가 알림 한 건에 묶이는 상한이다. 늘리려면 그 경로 지연부터 볼 것.
    assert app_notifications.LOCK_WAIT_SECONDS <= 2
    monkeypatch.setenv("SIMA_APP_ALERTS_ENABLED", "1")
    monkeypatch.setattr(app_notifications, "LOCK_WAIT_SECONDS", 0.2)
    with app_notifications.DEFAULT_APP_ALERTS_PATH.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.monotonic()
        assert notify.send_telegram_alert("test") is False
        assert time.monotonic() - started < 1


def test_concurrent_app_outbox_writer_does_not_drop_the_alert(monkeypatch):
    # 두 크론이 같은 순간에 쓰는 경우. 예전엔 즉시 포기해도 텔레그램이 메웠다.
    monkeypatch.setenv("SIMA_APP_ALERTS_ENABLED", "1")
    lock = app_notifications.DEFAULT_APP_ALERTS_PATH.open("w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    releaser = threading.Timer(0.2, lock.close)
    started = time.monotonic()
    releaser.start()
    try:
        assert notify.send_telegram_alert("🟢 [SIMA] 매수\n삼성전자") is True
        assert time.monotonic() - started >= 0.15  # 실제로 기다린 뒤 썼다
    finally:
        releaser.join()
    row = json.loads(app_notifications.DEFAULT_APP_ALERTS_PATH.read_text())
    assert row["title"] == "매수"


def configured_store(tmp_path):
    store = Store(tmp_path / "app.sqlite3")
    token = store.login(store.pairing_code())
    session = store.session(token)
    store.subscribe(session, valid_subscription(subscription()))
    return store, session


def test_outbox_partial_line_and_reimport_do_not_duplicate_push(tmp_path):
    store, _ = configured_store(tmp_path)
    path = tmp_path / "outbox.jsonl"
    alert = app_notifications.make_alert("[SIMA] 매수\n1주")
    payload = alert.model_dump_json().encode()
    path.write_bytes(payload[:20])
    mobile_push.import_outbox(store, path)
    assert store.alerts() == []
    with path.open("ab") as out:
        out.write(payload[20:] + b"\n")
    mobile_push.import_outbox(store, path)
    assert len(store.alerts()) == len(store.pending()) == 1
    store.set_meta("outbox_cursor", "{}")
    mobile_push.import_outbox(store, path)
    assert len(store.alerts()) == len(store.pending()) == 1


def test_existing_notifications_are_not_pushed_when_device_is_added(tmp_path):
    store, _ = configured_store(tmp_path)
    alert = app_notifications.make_alert("[SIMA] 과거 알림")
    alert.created_at -= timedelta(days=1)
    store.add_alert(alert)
    assert len(store.alerts()) == 1
    assert store.pending() == []


def test_expired_device_can_reconnect_its_existing_ios_subscription(tmp_path):
    store, old_session = configured_store(tmp_path)
    store.add_alert(app_notifications.make_alert("[SIMA] 매수\n1주"))
    with store.connect() as db:
        db.execute("UPDATE sessions SET expires=0 WHERE hash=?", (old_session,))
    new_session = store.session(store.login(store.pairing_code()))
    store.subscribe(new_session, valid_subscription(subscription()))
    assert store.subscription_status(new_session)["enabled"]
    assert store.pending() == []
    store.add_alert(app_notifications.make_alert("[SIMA] 새 알림"))
    assert len(store.pending()) == 1


@pytest.mark.parametrize("code,expected_state,active", [(201,"sent",1),(503,"pending",1),(410,"failed",0)])
def test_push_outcomes_are_persisted_without_leaking_endpoints(tmp_path, monkeypatch, code, expected_state, active):
    store, _ = configured_store(tmp_path)
    store.add_alert(app_notifications.make_alert("[SIMA] 매수\n1주"))
    captured = []
    monkeypatch.setattr(mobile_push, "webpush", lambda **kwargs: captured.append(kwargs) or SimpleNamespace(status_code=code))
    mobile_push.send_pending(store, tmp_path / "key.pem", ORIGIN)
    with store.connect() as db:
        assert db.execute("SELECT state FROM deliveries").fetchone()[0] == expected_state
        assert db.execute("SELECT active FROM subscriptions").fetchone()[0] == active
    assert captured[0]["timeout"] == 10
    assert json.loads(captured[0]["data"])["url"].startswith("/#alerts/")


def test_missing_nav_is_not_a_zero_balance_and_missing_run_is_not_hold(tmp_path):
    result = snapshot(tmp_path, now=datetime(2026,10,6,16,0,tzinfo=KST))
    assert result.account is None
    assert result.operation["status"] == "missing"
    assert result.measurements["normal_days"] == 0
    assert all(day["status"] == "unknown" for day in result.monitoring["days"])


def test_manual_refresh_reads_new_saved_nav_before_cache_expires(web, monkeypatch):
    app, client, _ = web
    monkeypatch.setattr(mobile_app, "time", SimpleNamespace(monotonic=lambda: 123.0))
    login(web)
    root = app.config["DATA_DIR"]
    nav = {"day":"2026-10-02", "status":"ok", "total":9500, "cash":5500, "securities":4000, "holdings":[]}
    write_rows(root, "account_nav.jsonl", [nav])
    assert request(client, "GET", "/api/snapshot").json["account"]["total"] == 9500
    write_rows(root, "account_nav.jsonl", [{**nav, "total":9700, "securities":4200}])
    assert request(client, "GET", "/api/snapshot").json["account"]["total"] == 9500
    response = request(client, "GET", "/api/snapshot?refresh=1")
    assert response.status_code == 200
    assert response.json["account"]["total"] == 9700
    assert response.headers["Cache-Control"] == "no-store"


def test_nav_uses_broker_cash_not_book_weights_and_marks_old_values(tmp_path):
    write_rows(tmp_path, "account_nav.jsonl", [{"day":"2026-10-02","status":"ok","total":9500,"cash":5500,"securities":4000,"holdings":[],"observed_at":"2026-10-05T03:40:00+09:00"}])
    (tmp_path / "portfolio_state.json").write_text(json.dumps({"cash_weight":0.01}))
    result = snapshot(tmp_path, initial_capital=10000, now=datetime(2026,10,6,16,0,tzinfo=KST))
    assert result.account["cash"] == 5500
    assert result.account["pnl"] == -500
    assert result.account["cash_ratio"] == 5500/9500
    assert any("갱신되지" in x for x in result.warnings)


@pytest.mark.parametrize("holdings,securities,pnl,rate", [
    ([{"evlu_amt":1200,"evlu_pfls_amt":200,"evlu_pfls_rt":20},
      {"evlu_amt":7200,"evlu_pfls_amt":-800,"evlu_pfls_rt":-10}], 8400, -600, -600/9000*100),
    ([{"evlu_amt":1100,"evlu_pfls_amt":100}], 1100, 100, 10),
    ([{"evlu_amt":1100}], 1100, None, None),
    ([{"evlu_amt":1100,"evlu_pfls_amt":100}], 2200, None, None),
    ([], 0, 0, None),
    ([], 1100, None, None),
])
def test_stock_pnl_uses_complete_same_snapshot_and_weighted_cost(tmp_path, holdings, securities, pnl, rate):
    write_rows(tmp_path, "account_nav.jsonl", [{"day":"2026-10-02", "status":"ok",
        "total":10000, "cash":10000-securities, "securities":securities, "holdings":holdings}])
    result = snapshot(tmp_path, now=datetime(2026,10,5,16,0,tzinfo=KST))
    assert result.account["securities_pnl"] == pnl
    if rate is None:
        assert result.account["securities_return_pct"] is None
    else:
        assert result.account["securities_return_pct"] == pytest.approx(rate)


def test_analysis_failure_is_not_counted_as_signal_or_normal_sample(tmp_path):
    write_rows(tmp_path, "daily_runs.jsonl", [
        {"day":"2026-10-06","status":"failed","cohort":"kis_master_naver_json_20261005"},
        {"day":"2026-10-07","status":"completed","cohort":"kis_master_naver_json_20261005","pending":0}])
    write_rows(tmp_path, "pipeline.jsonl", [{"day":"2026-10-06","ticker":"005930","action":"BUY","approved":True}])
    result = snapshot(tmp_path, now=datetime(2026,10,7,16,0,tzinfo=KST))
    assert result.measurements["normal_days"] == 1
    assert result.monitoring["signal_days"] == 0
    assert result.operation["label"] == "정상 관망"


def test_recent_failed_nav_retains_prior_value_with_warning(tmp_path):
    write_rows(tmp_path, "account_nav.jsonl", [
        {"day":"2026-10-02","status":"ok","total":9500,"cash":5500,"securities":4000},
        {"day":"2026-10-06","status":"unavailable"}])
    result = snapshot(tmp_path, now=datetime(2026,10,6,16,0,tzinfo=KST))
    assert result.account["day"] == "2026-10-02"
    assert any("최근 계좌 조회가 실패" in x for x in result.warnings)


def test_private_fields_never_appear_in_snapshot(tmp_path):
    write_rows(tmp_path, "trade_journal.jsonl", [{"day":"2026-10-02","event":"buy","ticker":"005930","account_no":"secret","order_no":"private","quantity":1}])
    result = snapshot(tmp_path, now=datetime(2026,10,5,16,0,tzinfo=KST)).model_dump_json()
    assert "secret" not in result and "private" not in result


def test_logout_during_push_stops_the_remaining_batch(tmp_path, monkeypatch):
    store, session = configured_store(tmp_path)
    store.add_alert(app_notifications.make_alert("[SIMA] 알림 1"))
    store.add_alert(app_notifications.make_alert("[SIMA] 알림 2"))
    sent = []
    def send(**kwargs):
        sent.append(kwargs)
        store.logout(session)
        return SimpleNamespace(status_code=201)
    monkeypatch.setattr(mobile_push, "webpush", send)
    mobile_push.send_pending(store, tmp_path / "key.pem", ORIGIN)
    assert len(sent) == 1
    assert not store.subscription_status(session)["enabled"]


def test_old_push_expiration_cannot_disable_a_new_subscription(tmp_path, monkeypatch):
    store, session = configured_store(tmp_path)
    store.add_alert(app_notifications.make_alert("[SIMA] 알림"))
    def send(**kwargs):
        store.subscribe(session, valid_subscription(subscription("https://web.push.apple.com/Qnew")))
        return SimpleNamespace(status_code=410)
    monkeypatch.setattr(mobile_push, "webpush", send)
    mobile_push.send_pending(store, tmp_path / "key.pem", ORIGIN)
    assert store.subscription_status(session)["enabled"]
    store.add_alert(app_notifications.make_alert("[SIMA] 새 구독 알림"))
    assert json.loads(store.pending()[0]["info"])["endpoint"].endswith("Qnew")


def test_holiday_status_does_not_require_a_fake_analysis_record(tmp_path):
    result = snapshot(tmp_path, now=datetime(2026,10,5,16,0,tzinfo=KST))
    assert result.operation["status"] == "holiday"
    assert result.operation["next_trading_day"] == "2026-10-06"
    assert result.measurements["normal_days"] == 0


@pytest.mark.parametrize("extra", [{}, {"day":"2026-10-06"}])
def test_analysis_usage_is_grouped_by_korean_trading_day(tmp_path, extra):
    write_rows(tmp_path, "llm_calls.jsonl", [{"timestamp":"2026-10-05T23:30:00Z","label":"chart","success":True,
                                             "input_tokens":100,"output_tokens":20, **extra}])
    result = snapshot(tmp_path, now=datetime(2026,10,6,16,0,tzinfo=KST))
    assert result.monitoring["analysts"]["chart"]["calls"] == 1


def test_manual_account_does_not_change_daily_nav(tmp_path):
    from src.kis import AccountSnapshot
    from src.mobile_balance import save_observation
    nav = {"day": "2026-10-02", "observed_at": "2026-10-02T15:35:00+09:00", "status": "ok", "total": 100, "cash": 100, "securities": 0, "holdings": []}
    write_rows(tmp_path, "account_nav.jsonl", [nav])
    before = (tmp_path / "account_nav.jsonl").read_bytes()
    target = tmp_path / "manual.json"
    assert save_observation(lambda: AccountSnapshot(110, 110, 0), target)
    result = snapshot(tmp_path, account_path=target)
    assert result.account["total"] == 110
    assert result.account["observed_at"] != nav["observed_at"]
    assert (tmp_path / "account_nav.jsonl").read_bytes() == before
    saved = target.read_bytes()
    assert not save_observation(lambda: None, target)
    assert target.read_bytes() == saved
    with pytest.raises(ValueError):
        save_observation(lambda: AccountSnapshot(float("nan"), 0, 0), target)
    assert target.read_bytes() == saved


def test_newer_daily_nav_wins_over_manual_observation(tmp_path):
    manual = {"day": "2026-10-01", "observed_at": "2026-10-01T15:35:00+09:00", "status": "ok", "total": 80, "cash": 80, "securities": 0, "holdings": []}
    target = tmp_path / "manual.json"
    target.write_text(json.dumps(manual))
    write_rows(tmp_path, "account_nav.jsonl", [{**manual, "day": "2026-10-02", "observed_at": "2026-10-02T15:35:00+09:00", "total": 100}])
    assert snapshot(tmp_path, account_path=target).account["total"] == 100


def test_broker_refresh_requires_authentication_and_csrf(web):
    assert request(web[1], "POST", "/api/account/refresh", json={}, headers=HEADERS).status_code == 401
    login(web)
    assert request(web[1], "POST", "/api/account/refresh", json={}).status_code == 403


@pytest.mark.parametrize("reply,expected", [(b"ok\n", 200), (b"failed\n", 503), (b"busy\n", 429)])
def test_broker_refresh_fixed_socket_request(web, monkeypatch, tmp_path, reply, expected):
    import io
    from src.kis import AccountSnapshot
    from src.mobile_balance import save_observation
    target = tmp_path / "manual.json"
    web[0].config["ACCOUNT_PATH"] = target
    assert save_observation(lambda: AccountSnapshot(110, 110, 0), target)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def settimeout(self, timeout): assert timeout == 23
        def connect(self, path): assert path == web[0].config["BALANCE_SOCKET"]
        def sendall(self, data): assert data == b"refresh\n"
        def makefile(self, mode): return io.BytesIO(reply)
    monkeypatch.setattr(mobile_app.socket, "socket", lambda *_: Connection())
    login(web)
    response = request(web[1], "POST", "/api/account/refresh", json={"command": "ignored"}, headers=HEADERS)
    assert response.status_code == expected
    if expected == 200:
        assert response.json["account"]["total"] == 110
    for _ in range(2):
        request(web[1], "POST", "/api/account/refresh", json={}, headers=HEADERS)
    assert request(web[1], "POST", "/api/account/refresh", json={}, headers=HEADERS).status_code == 429


def test_unavailable_balance_service_returns_failure(web):
    login(web)
    web[0].config["BALANCE_SOCKET"] = "/nonexistent/sima.sock"
    assert request(web[1], "POST", "/api/account/refresh", json={}, headers=HEADERS).status_code == 503


def test_trade_name_survives_full_sale_and_missing_name_in_latest_holdings(tmp_path):
    write_rows(tmp_path, "account_nav.jsonl", [
        {"day":"2026-10-05", "status":"ok", "total":10000, "holdings":[
            {"pdno":"0126Z0", "prdt_name":"삼성에피스홀딩스"},
            {"pdno":"NEW001", "prdt_name":"새종목"}]},
        {"day":"2026-10-06", "status":"ok", "total":10000, "holdings":[
            {"pdno":"NEW001", "prdt_name":"", "hldg_qty":1}]},
    ])
    write_rows(tmp_path, "trade_journal.jsonl", [
        {"day":"2026-10-06", "event":"sell", "ticker":"0126Z0"}])
    result = snapshot(tmp_path, now=datetime(2026,10,7,3,0,tzinfo=KST))
    assert result.trades[0]["name"] == "삼성에피스홀딩스"
    assert result.holdings[0]["name"] == "새종목"
