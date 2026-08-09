from src import notify


class _FakeResponse:
    def raise_for_status(self):
        pass


def test_send_telegram_alert_success(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse()

    monkeypatch.setattr(notify.requests, "post", fake_post)

    assert notify.send_telegram_alert("test message") is True
    assert captured["url"] == "https://api.telegram.org/bottest-token/sendMessage"
    assert captured["json"] == {"chat_id": "12345", "text": "test message"}


def test_send_telegram_alert_skips_when_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert notify.send_telegram_alert("test message") is False


def test_send_telegram_alert_swallows_network_failure(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    def fake_post(url, json, timeout):
        raise ConnectionError("network down")

    monkeypatch.setattr(notify.requests, "post", fake_post)

    assert notify.send_telegram_alert("test message") is False


def test_format_buy_alert():
    msg = notify.format_buy_alert("005930", 231200.0, 34, "반도체 업황 개선")
    assert msg.startswith("🟢 [SIMA] 매수\n")
    assert "005930 · 231,200원 · 34주" in msg
    assert "사유: 반도체 업황 개선" in msg


def test_format_buy_alert_truncates_long_reason():
    msg = notify.format_buy_alert("005930", 100.0, 1, "가" * 500)
    reason_line = [line for line in msg.splitlines() if line.startswith("사유:")][0]
    assert len(reason_line) < 500
    assert reason_line.endswith("…")


def test_format_buy_skipped_alert():
    msg = notify.format_buy_skipped_alert("005930", "갭초과", "4.2%")
    assert msg.startswith("⛔ [SIMA] 매수 스킵\n")
    assert "갭초과 (4.2%)" in msg


def test_format_buy_skipped_alert_without_detail():
    msg = notify.format_buy_skipped_alert("005930", "잔고조회실패")
    assert "잔고조회실패\n" in msg


def test_format_sell_alert_with_pnl_and_no_reasoning():
    msg = notify.format_sell_alert("005930", "손절", 200000.0, -0.135)
    assert msg.startswith("🔴 [SIMA] 매도 (손절)\n")
    assert "005930 · 200,000원 · 실현손익 -13.5%" in msg
    assert "사유" not in msg


def test_format_sell_alert_with_reasoning():
    msg = notify.format_sell_alert("005930", "LLM재량매도", 200000.0, -0.05, reasoning="근거가 무너짐")
    assert "사유: 근거가 무너짐" in msg


def test_format_sell_alert_missing_pnl():
    msg = notify.format_sell_alert("005930", "손절", 200000.0, None)
    assert "실현손익 -" in msg


def test_format_error_alert():
    msg = notify.format_error_alert("evaluate_holdings 실패", "RuntimeError: boom")
    assert msg == "⚠️ [SIMA] 오류 — evaluate_holdings 실패\nRuntimeError: boom"
