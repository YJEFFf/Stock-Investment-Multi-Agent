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
