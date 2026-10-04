"""SIMA 개인용 PWA HTTP API. 브로커/API 키와 연결하지 않는다."""

import base64
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask, abort, g, jsonify, request, send_from_directory
from pydantic import ValidationError

from src.app_notifications import make_alert
from src.mobile_data import snapshot
from src.mobile_store import SESSION_SECONDS, Store
from src.schemas import WebPushSubscription

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def valid_subscription(raw: dict) -> dict:
    sub = WebPushSubscription.model_validate(raw)
    url = urlsplit(sub.endpoint)
    host = url.hostname or ""
    allowed = host == "web.push.apple.com" or host.endswith(".push.apple.com") or host in {
        "fcm.googleapis.com", "updates.push.services.mozilla.com"}
    if url.scheme != "https" or not allowed or url.port not in (None, 443) or url.username or url.password or url.fragment:
        raise ValueError("unsupported push endpoint")
    key = b64decode(sub.keys.p256dh)
    if len(key) != 65 or len(b64decode(sub.keys.auth)) != 16:
        raise ValueError("invalid push keys")
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key)
    return sub.model_dump()


def public_key(private_path: Path) -> str:
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config.update(
        DATA_DIR=os.environ.get("SIMA_WEB_DATA_DIR", "/home/ubuntu/sima/logs"),
        STATE_DIR=os.environ.get("SIMA_WEB_STATE_DIR", "/var/lib/sima-pwa"),
        ORIGIN=os.environ.get("SIMA_WEB_ORIGIN", "http://localhost:8765").rstrip("/"),
        INITIAL_CAPITAL=float(os.environ.get("SIMA_INITIAL_CAPITAL", "100000000")),
        MAX_CONTENT_LENGTH=12_000,
    )
    if config:
        app.config.update(config)
    origin = app.config["ORIGIN"]
    parsed_origin = urlsplit(origin)
    secure = parsed_origin.scheme == "https"
    if not secure and parsed_origin.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("HTTPS is required")
    app.config["TRUSTED_HOSTS"] = [parsed_origin.netloc, parsed_origin.hostname]
    cookie = "__Host-sima" if secure else "sima_session"
    store = Store(Path(app.config["STATE_DIR"]) / "app.sqlite3")
    app.extensions["sima_store"] = store
    key_path = Path(app.config["STATE_DIR"]) / "vapid.pem"
    vapid = public_key(key_path) if key_path.exists() else None
    cached = {"at": 0.0, "data": None}

    @app.before_request
    def protect():
        g.session = store.session(request.cookies.get(cookie))
        if request.method in {"POST", "DELETE", "PUT", "PATCH"}:
            if request.headers.get("Origin") != origin or request.headers.get("X-SIMA-Request") != "1":
                abort(403)
            if request.method == "POST" and not request.is_json:
                abort(415)
        if request.path.startswith("/api/") and request.path not in {"/api/session", "/api/login"} and not g.session:
            abort(401)

    @app.after_request
    def headers(response):
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        })
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(415)
    @app.errorhandler(429)
    def client_error(error):
        return jsonify(error=error.name), error.code

    @app.get("/")
    def index():
        return send_from_directory(WEB_ROOT, "index.html")

    @app.get("/assets/<path:name>")
    def assets(name):
        return send_from_directory(WEB_ROOT / "assets", name)

    @app.get("/manifest.webmanifest")
    def manifest():
        return send_from_directory(WEB_ROOT, "manifest.webmanifest", mimetype="application/manifest+json")

    @app.get("/service-worker.js")
    def worker():
        response = send_from_directory(WEB_ROOT, "service-worker.js", mimetype="text/javascript")
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.get("/healthz")
    def health():
        return jsonify(ok=True)

    @app.get("/api/session")
    def session():
        return jsonify(authenticated=bool(g.session), vapid_public_key=vapid if g.session else None)

    @app.post("/api/login")
    def login():
        if not store.allow("login", limit=10):
            abort(429)
        body = request.get_json()
        if not isinstance(body, dict) or not isinstance(body.get("code"), str):
            abort(400)
        token = store.login(body["code"])
        if not token:
            abort(401)
        response = jsonify(authenticated=True, vapid_public_key=vapid)
        response.set_cookie(cookie, token, max_age=SESSION_SECONDS, secure=secure, httponly=True, samesite="Strict", path="/")
        return response

    @app.post("/api/logout")
    def logout():
        store.logout(g.session)
        response = jsonify(ok=True)
        response.delete_cookie(cookie, secure=secure, httponly=True, samesite="Strict", path="/")
        return response

    @app.get("/api/snapshot")
    def dashboard():
        if not store.allow("snapshot:" + g.session, limit=30):
            abort(429)
        # 수동 새로고침은 저장 기록을 즉시 다시 읽는다. 브로커/분석을 호출하지 않는다.
        if request.args.get("refresh") == "1" or time.monotonic() - cached["at"] > 10 or cached["data"] is None:
            cached.update(at=time.monotonic(), data=snapshot(Path(app.config["DATA_DIR"]),
                          initial_capital=app.config["INITIAL_CAPITAL"]).model_dump(mode="json"))
        return jsonify(cached["data"])

    @app.get("/api/alerts")
    def alerts():
        return jsonify(alerts=store.alerts())

    @app.get("/api/push")
    def push_state():
        return jsonify(**store.subscription_status(g.session), available=bool(vapid))

    @app.post("/api/push")
    def subscribe():
        if not vapid:
            return jsonify(error="Push is not configured"), 503
        try:
            info = valid_subscription(request.get_json())
            store.subscribe(g.session, info)
        except (ValidationError, ValueError, TypeError):
            abort(400)
        return jsonify(ok=True)

    @app.delete("/api/push")
    def unsubscribe():
        store.unsubscribe(g.session)
        return jsonify(ok=True)

    @app.post("/api/push/test")
    def test_push():
        if not store.allow("push_test:" + g.session, limit=2):
            abort(429)
        if not store.subscription_status(g.session)["enabled"]:
            return jsonify(error="Enable notifications first"), 409
        alert = make_alert("[SIMA] 테스트 알림\n아이폰 알림 연결을 확인합니다. 이 알림을 누르면 SIMA 알림함이 열립니다.")
        store.add_alert(alert, only_session=g.session)
        return jsonify(queued=True, id=alert.id)

    return app
