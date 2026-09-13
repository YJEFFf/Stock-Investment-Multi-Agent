"""2026-09-14 P1-5 게이트 여유 점검, P1-9 안전장치 공백 예산."""

import logging
from datetime import date, datetime

import pytest

from src import notify, pipeline
from src.schemas import PortfolioState, Position, RiskGateConfig


def _pos(ticker, weight):
    return Position(ticker=ticker, sector="x", weight=weight, entry_price=100.0, peak_price=100.0)


# --- gate_headroom ---


def test_headroom_counts_new_buys_until_total_exposure_rejects():
    # 투자 45.26% → 8%씩 6건(93.26%) 승인, 7번째(101.26%)에서 거부 — 첫 평가의 "하루 7건"
    portfolio = PortfolioState(cash_weight=0.5474, positions=[_pos("A", 0.08), _pos("B", 0.3726)])

    h = pipeline.gate_headroom(portfolio)

    assert h["new_buys_until_reject"] == 6
    assert h["rejected_by"] == "total_exposure"


def test_headroom_exercises_the_position_limit_on_the_largest_holding():
    portfolio = PortfolioState(cash_weight=0.84, positions=[_pos("A", 0.08), _pos("B", 0.08)])
    h = pipeline.gate_headroom(portfolio)
    assert h["addon_ticker"] in {"A", "B"} and h["addon_rejected_by"] == "position_limit"


def test_headroom_reports_a_broken_gate_when_nothing_rejects():
    loose = RiskGateConfig(position_limit=1.0, total_exposure_limit=100.0)
    h = pipeline.gate_headroom(PortfolioState(cash_weight=1.0), config=loose)
    assert h["rejected_by"] is None and h["new_buys_until_reject"] == pipeline.GATE_HEADROOM_MAX_PROBES


def test_headroom_never_touches_the_decision_log():
    pipeline.gate_headroom(PortfolioState(cash_weight=1.0))
    assert not pipeline.DEFAULT_LOG_PATH.exists()


# --- summarize_blind_minutes ---


DAY = date(2026, 9, 15)


def _line(hh, mm, positions=6, priced=6, ss=13):
    return f"2026-09-15 {hh:02d}:{mm:02d}:{ss:02d},112 INFO evaluate_holdings_done positions={positions} priced={priced} sells=0\n"


def _log(tmp_path, lines):
    path = tmp_path / "cron.log"
    path.write_text("".join(lines))
    return path


def test_every_minute_covered_means_zero_blind(tmp_path):
    lines = [_line(h, m) for h in range(9, 16) for m in range(60) if (h, m) <= (15, 30)]
    s = pipeline.summarize_blind_minutes(_log(tmp_path, lines), DAY, had_positions=True)
    assert s == {"expected": 391, "blind": 0, "longest_run": 0}


def test_rounds_that_priced_nothing_are_blind_and_the_longest_run_is_reported(tmp_path):
    lines = [_line(h, m, priced=0 if (h, m) >= (14, 32) and (h, m) < (14, 44) else 6)
             for h in range(9, 16) for m in range(60) if (h, m) <= (15, 30)]
    s = pipeline.summarize_blind_minutes(_log(tmp_path, lines), DAY, had_positions=True)
    assert s["blind"] == 12 and s["longest_run"] == 12  # 2026-09-03 14:32~14:43과 같은 모양


def test_a_minute_skipped_on_the_lock_is_covered_if_another_round_judged_it(tmp_path):
    """09:01은 매분 크론이 락에 막히지만 execute_open이 같은 분에 판정한다."""
    lines = [_line(h, m) for h in range(9, 16) for m in range(60) if (h, m) <= (15, 30)]
    s = pipeline.summarize_blind_minutes(_log(tmp_path, lines + ["2026-09-15 09:01:03,083 INFO check_stop_loss_skipped reason=previous_run_still_holding_lock\n"]), DAY, had_positions=True)
    assert s["blind"] == 0


def test_minutes_after_the_last_position_was_sold_are_not_counted(tmp_path):
    lines = [_line(9, m) for m in range(0, 10)] + [_line(9, 10, positions=0, priced=1)]
    s = pipeline.summarize_blind_minutes(_log(tmp_path, lines), DAY, had_positions=False)
    assert s["expected"] == 11 and s["blind"] == 0


def test_no_rounds_with_holdings_is_the_whole_day_blind(tmp_path):
    """크론이 아예 안 돈 날이 가장 나쁘다 — 0분이 아니라 391분이다."""
    s = pipeline.summarize_blind_minutes(_log(tmp_path, []), DAY, had_positions=True)
    assert s["blind"] == 391


def test_no_rounds_and_no_holdings_is_nothing_to_count(tmp_path):
    assert pipeline.summarize_blind_minutes(_log(tmp_path, []), DAY, had_positions=False) is None


# --- log_monitoring_summary 연결 ---


def _decision_log(tmp_path, days):
    import json

    path = tmp_path / "pipeline.jsonl"
    path.write_text("".join(json.dumps({"day": d, "ticker": "A", "action": "HOLD", "approved": False, "rejected_by": None}) + "\n" for d in days))
    return path


def test_weekly_zero_rejection_alert_is_sent_once_per_week(monkeypatch, tmp_path):
    alerts = []
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: alerts.append(m) or True)
    monkeypatch.setattr(pipeline, "_kst_today", lambda: DAY)
    days = [f"2026-08-{d:02d}" for d in range(10, 31)] + ["2026-09-15"]  # 오늘도 판단이 났다
    kwargs = dict(
        decision_log_path=_decision_log(tmp_path, days), llm_log_path=tmp_path / "llm.jsonl",
        portfolio=PortfolioState(cash_weight=0.6, positions=[_pos("A", 0.4)]), cron_log_path=_log(tmp_path, []),
    )
    (tmp_path / "llm.jsonl").write_text("")

    pipeline.log_monitoring_summary(**kwargs)
    pipeline.log_monitoring_summary(**kwargs)

    weekly = [a for a in alerts if "게이트 주간 점검" in a]
    assert len(weekly) == 1 and "신규 매수 7건 뒤 total_exposure" in weekly[0]  # 40% + 8%×7 = 96%


def test_blind_budget_alert_fires_when_the_day_exceeds_it(monkeypatch, tmp_path, caplog):
    alerts = []
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: alerts.append(m) or True)
    monkeypatch.setattr(pipeline, "_kst_today", lambda: DAY)
    (tmp_path / "llm.jsonl").write_text("")
    lines = [_line(h, m, priced=0 if h == 10 else 6) for h in range(9, 16) for m in range(60) if (h, m) <= (15, 30)]

    with caplog.at_level(logging.INFO, logger="src.pipeline"):
        pipeline.log_monitoring_summary(
            decision_log_path=_decision_log(tmp_path, ["2026-09-15"]), llm_log_path=tmp_path / "llm.jsonl",
            portfolio=PortfolioState(cash_weight=0.9, positions=[_pos("A", 0.1)]), cron_log_path=_log(tmp_path, lines),
        )

    assert any("monitoring_blind_minutes" in r.getMessage() and "blind=60" in r.getMessage() for r in caplog.records)
    assert any("공백 예산 초과" in a for a in alerts)


def test_alert_once_is_once_per_key(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: sent.append(m) or True)
    assert notify.alert_once("k1", "a") is True
    assert notify.alert_once("k1", "a") is False
    assert notify.alert_once("k2", "b") is True
    assert sent == ["a", "b"]


def test_weekly_alert_is_not_repeated_on_stale_data_while_decisions_are_paused(monkeypatch, tmp_path):
    """Claude API 정지로 판단을 건너뛴 날엔 최근 20일 창이 옛 데이터 그대로다(독립 리뷰)."""
    alerts = []
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: alerts.append(m) or True)
    monkeypatch.setattr(pipeline, "_kst_today", lambda: DAY)
    (tmp_path / "llm.jsonl").write_text("")

    pipeline.log_monitoring_summary(
        decision_log_path=_decision_log(tmp_path, [f"2026-08-{d:02d}" for d in range(10, 31)]),
        llm_log_path=tmp_path / "llm.jsonl",
        portfolio=PortfolioState(cash_weight=0.6, positions=[_pos("A", 0.4)]), cron_log_path=_log(tmp_path, []),
    )

    assert not any("게이트 주간 점검" in a for a in alerts)


def test_an_outage_after_selling_out_and_buying_again_is_still_counted(tmp_path):
    """독립 리뷰 재현: 09:00 전량 청산(positions=0) → 09:01 재매수 → 60분 장애가 공백 0으로 나왔다."""
    lines = [_line(9, 0, positions=0, priced=1)]
    lines += [_line(h, m, priced=0 if (h, m) >= (9, 2) and (h, m) < (10, 2) else 6)
              for h in range(9, 16) for m in range(60) if (9, 1) <= (h, m) <= (15, 30)]
    s = pipeline.summarize_blind_minutes(_log(tmp_path, lines), DAY, had_positions=True)
    assert s["expected"] == 391 and s["blind"] == 60


def test_alert_once_retries_when_the_send_failed(monkeypatch):
    results = iter([False, True])
    monkeypatch.setattr(notify, "send_telegram_alert", lambda m: next(results))
    assert notify.alert_once("k", "a") is False
    assert notify.alert_once("k", "a") is True
