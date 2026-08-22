from datetime import datetime

import pytest

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


def test_alert_once_per_day_sends_only_first_time(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    sent = []
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: sent.append(m) or True)

    assert notify.alert_once_per_day("ctx", "첫 번째") is True
    assert notify.alert_once_per_day("ctx", "두 번째") is False
    assert sent == ["첫 번째"]


def test_alert_once_per_day_counts_each_context_separately(monkeypatch, tmp_path):
    """마커가 하나뿐이던 옛 구현에서는 사유 A가 울리면 그날 사유 B가 통째로
    묻혔다 — 손절 체크가 죽어 알림이 나간 날엔 시세 전면 장애를 못 듣는다."""
    monkeypatch.chdir(tmp_path)
    sent = []
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: sent.append(m) or True)

    assert notify.alert_once_per_day("check_stop_loss_failed", "A") is True
    assert notify.alert_once_per_day("holdings_all_prices_unavailable", "B") is True
    assert sent == ["A", "B"]


def test_alert_once_per_day_resets_on_a_new_day(monkeypatch, tmp_path):
    from datetime import date

    monkeypatch.chdir(tmp_path)
    sent = []
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: sent.append(m) or True)

    assert notify.alert_once_per_day("ctx", "어제", today=date(2026, 8, 20)) is True
    assert notify.alert_once_per_day("ctx", "어제 또", today=date(2026, 8, 20)) is False
    assert notify.alert_once_per_day("ctx", "오늘", today=date(2026, 8, 21)) is True
    assert sent == ["어제", "오늘"]


# --- 시세 공백 구간 추적 (2026-08-21 오후 23분 공백이 무알림이었던 것) ---

CTX = "holdings_all_prices_unavailable"


def _at(hh, mm):
    return datetime(2026, 8, 21, hh, mm, tzinfo=notify.KST)


def test_blackout_stays_quiet_until_it_gets_long(tmp_path):
    """짧은 blip은 조용하다 — 1~2분짜리는 정상 장애 범위라 매번 울리면 알림을 안 보게 된다."""
    assert notify.track_blackout(CTX, True, now=_at(15, 7), state_dir=tmp_path) is None
    assert notify.track_blackout(CTX, True, now=_at(15, 9), state_dir=tmp_path) is None
    assert notify.track_blackout(CTX, True, now=_at(15, 11), state_dir=tmp_path) is None


def test_blackout_escalates_once_after_five_minutes(tmp_path):
    notify.track_blackout(CTX, True, now=_at(15, 7), state_dir=tmp_path)

    event = notify.track_blackout(CTX, True, now=_at(15, 12), state_dir=tmp_path)
    assert event is not None
    assert event.kind == "escalated"
    assert event.minutes == pytest.approx(5.0)

    # 그 뒤로는 같은 공백에 대해 계속 조용하다 — 23분이면 매분 울릴 뻔했다.
    for minute in range(13, 30):
        assert notify.track_blackout(CTX, True, now=_at(15, minute), state_dir=tmp_path) is None


def test_blackout_reports_total_length_when_prices_come_back(tmp_path):
    notify.track_blackout(CTX, True, now=_at(13, 51), state_dir=tmp_path)
    notify.track_blackout(CTX, True, now=_at(13, 57), state_dir=tmp_path)  # escalated

    event = notify.track_blackout(CTX, False, now=_at(13, 58), state_dir=tmp_path)
    assert event is not None
    assert event.kind == "recovered"
    assert event.minutes == pytest.approx(7.0)

    # 구간이 닫혔으니 다음 정상 회차는 조용하다.
    assert notify.track_blackout(CTX, False, now=_at(13, 59), state_dir=tmp_path) is None


def test_short_blackout_recovers_without_an_alert(tmp_path):
    """알림까지 가지 않은 공백은 복구도 조용히 한다 — 안 그러면 blip마다 두 번 울린다."""
    notify.track_blackout(CTX, True, now=_at(13, 1), state_dir=tmp_path)
    assert notify.track_blackout(CTX, False, now=_at(13, 3), state_dir=tmp_path) is None


def test_blackout_does_not_carry_over_to_the_next_day(tmp_path):
    """공백인 채로 장이 끝나면 상태 파일이 남는다. 다음 날 아침에 이어 세면
    "밤새 15시간 공백"이 된다."""
    notify.track_blackout(CTX, True, now=_at(15, 29), state_dir=tmp_path)

    next_morning = datetime(2026, 8, 24, 9, 1, tzinfo=notify.KST)
    assert notify.track_blackout(CTX, True, now=next_morning, state_dir=tmp_path) is None

    event = notify.track_blackout(
        CTX, True, now=datetime(2026, 8, 24, 9, 7, tzinfo=notify.KST), state_dir=tmp_path
    )
    assert event is not None and event.minutes == pytest.approx(6.0)


def test_close_blackout_reports_a_window_that_never_recovered(tmp_path):
    """2026-08-21의 15:07~15:29가 정확히 이 모양이다 — 복구 회차가 영영 안 온다."""
    notify.track_blackout(CTX, True, now=_at(15, 7), state_dir=tmp_path)
    notify.track_blackout(CTX, True, now=_at(15, 29), state_dir=tmp_path)

    minutes = notify.close_blackout(CTX, now=_at(15, 35), state_dir=tmp_path)
    assert minutes == pytest.approx(28.0)

    # 한 번 닫으면 끝이다 — 다음 날 15:35에 또 보고하면 안 된다.
    assert notify.close_blackout(CTX, now=_at(15, 36), state_dir=tmp_path) is None


def test_close_blackout_is_silent_when_nothing_was_open(tmp_path):
    assert notify.close_blackout(CTX, now=_at(15, 35), state_dir=tmp_path) is None
