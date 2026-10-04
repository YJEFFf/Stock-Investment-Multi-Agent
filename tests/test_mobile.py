import base64
import fcntl
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from src import app_notifications, mobile_push, notify
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


@pytest.mark.parametrize("path", ["/api/snapshot", "/api/alerts", "/api/push"])
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


def test_app_outbox_is_independent_of_telegram_failure(monkeypatch):
    monkeypatch.setenv("SIMA_APP_ALERTS_ENABLED", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert notify.send_telegram_alert("⚠️ [SIMA] 오류 — 데이터 수집 실패\n오늘 신규 판단 중지") is True
    row = json.loads(app_notifications.DEFAULT_APP_ALERTS_PATH.read_text())
    assert row["kind"] == "error"
    assert "수집 실패" in row["title"]


def test_app_outbox_failure_does_not_stop_telegram(monkeypatch):
    monkeypatch.setenv("SIMA_APP_ALERTS_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test")
    monkeypatch.setattr(app_notifications, "DEFAULT_APP_ALERTS_PATH", Path("/dev/null/not-a-dir"))
    sent = []
    monkeypatch.setattr(notify.requests, "post", lambda *a, **kw: sent.append(kw) or SimpleNamespace(raise_for_status=lambda: None))
    assert notify.send_telegram_alert("test") is True
    assert len(sent) == 1


def test_telegram_disabled_only_queues_app(monkeypatch):
    monkeypatch.setenv("SIMA_APP_ALERTS_ENABLED", "1")
    monkeypatch.setenv("SIMA_TELEGRAM_ENABLED", "0")
    monkeypatch.setattr(notify.requests, "post", lambda *a, **kw: pytest.fail("telegram called"))
    assert notify.send_telegram_alert("[SIMA] 매수\n삼성전자")


def test_locked_app_outbox_does_not_delay_existing_telegram(monkeypatch):
    monkeypatch.setenv("SIMA_APP_ALERTS_ENABLED", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test")
    sent = []
    monkeypatch.setattr(notify.requests, "post", lambda *a, **kw: sent.append(kw) or SimpleNamespace(raise_for_status=lambda: None))
    with app_notifications.DEFAULT_APP_ALERTS_PATH.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert notify.send_telegram_alert("test")
    assert len(sent) == 1


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


def test_nav_uses_broker_cash_not_book_weights_and_marks_old_values(tmp_path):
    write_rows(tmp_path, "account_nav.jsonl", [{"day":"2026-10-02","status":"ok","total":9500,"cash":5500,"securities":4000,"holdings":[],"observed_at":"2026-10-05T03:40:00+09:00"}])
    (tmp_path / "portfolio_state.json").write_text(json.dumps({"cash_weight":0.01}))
    result = snapshot(tmp_path, initial_capital=10000, now=datetime(2026,10,6,16,0,tzinfo=KST))
    assert result.account["cash"] == 5500
    assert result.account["pnl"] == -500
    assert result.account["cash_ratio"] == 5500/9500
    assert any("갱신되지" in x for x in result.warnings)


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
