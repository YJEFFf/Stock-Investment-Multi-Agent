import json
from datetime import datetime, timedelta, timezone

from src.pipeline import load_decision_entries, summarize_llm_calls, summarize_recent_trading_days


def _write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_summarize_recent_trading_days_only_counts_the_window(tmp_path):
    log_path = tmp_path / "pipeline.jsonl"
    # 3일치: 오래된 날 하루(신호 발생) + 최근 2일(신호 없음) — window=2면 오래된 날은 제외돼야 함
    _write_jsonl(
        log_path,
        [
            {"day": "2026-08-01", "ticker": "AAA", "action": "BUY", "approved": True, "rejected_by": None},
            {"day": "2026-08-05", "ticker": "BBB", "action": "HOLD", "approved": True, "rejected_by": None},
            {
                "day": "2026-08-06",
                "ticker": "CCC",
                "action": "BUY",
                "approved": False,
                "rejected_by": "position_limit",
            },
        ],
    )

    summary = summarize_recent_trading_days(log_path, n_days=2)

    assert summary["total_days"] == 2
    assert summary["signal_days"] == 0
    assert summary["rejected_by_counts"] == {"position_limit": 1}


def test_summarize_recent_trading_days_handles_fewer_days_than_window(tmp_path):
    log_path = tmp_path / "pipeline.jsonl"
    _write_jsonl(
        log_path,
        [{"day": "2026-08-06", "ticker": "AAA", "action": "BUY", "approved": True, "rejected_by": None}],
    )

    summary = summarize_recent_trading_days(log_path, n_days=20)

    assert summary["total_days"] == 1
    assert summary["signal_day_ratio"] == 1.0


def test_summarize_recent_trading_days_missing_file_returns_empty(tmp_path):
    summary = summarize_recent_trading_days(tmp_path / "does_not_exist.jsonl", n_days=20)
    assert summary["total_days"] == 0
    assert summary["signal_day_ratio"] == 0.0


def test_summarize_llm_calls_aggregates_per_label(tmp_path):
    log_path = tmp_path / "llm_calls.jsonl"
    now = datetime.now(timezone.utc)
    _write_jsonl(
        log_path,
        [
            {
                "timestamp": now.isoformat(),
                "label": "chart",
                "success": True,
                "input_tokens": 100,
                "output_tokens": 20,
            },
            {
                "timestamp": now.isoformat(),
                "label": "chart",
                "success": False,
                "input_tokens": 50,
                "output_tokens": 0,
            },
            {
                "timestamp": now.isoformat(),
                "label": "news",
                "success": True,
                "input_tokens": 200,
                "output_tokens": 40,
            },
        ],
    )

    summary = summarize_llm_calls(log_path)

    assert summary["chart"]["calls"] == 2
    assert summary["chart"]["failures"] == 1
    assert summary["chart"]["failure_rate"] == 0.5
    assert summary["chart"]["input_tokens"] == 150
    assert summary["news"]["calls"] == 1
    assert summary["news"]["failure_rate"] == 0.0


def test_summarize_llm_calls_filters_by_since(tmp_path):
    log_path = tmp_path / "llm_calls.jsonl"
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=40)
    _write_jsonl(
        log_path,
        [
            {"timestamp": old.isoformat(), "label": "chart", "success": True, "input_tokens": 1, "output_tokens": 1},
            {"timestamp": now.isoformat(), "label": "chart", "success": True, "input_tokens": 2, "output_tokens": 2},
        ],
    )

    summary = summarize_llm_calls(log_path, since=now - timedelta(days=1))

    assert summary["chart"]["calls"] == 1
    assert summary["chart"]["input_tokens"] == 2


def test_summarize_llm_calls_missing_file_returns_empty_dict(tmp_path):
    assert summarize_llm_calls(tmp_path / "does_not_exist.jsonl") == {}


def test_decision_entries_deduped_by_day_and_ticker_keeping_the_later_record(tmp_path):
    """같은 날 같은 종목이 두 번 판단되면 나중 기록만 센다.

    2026-08-12에 같은 37종목 유니버스가 두 번 완주해 74개 레코드가 남았고, 두 실행은
    승인 종목까지 달랐다(298050 vs 282330). decide_buys_done과 매매일지는 뒤쪽 실행과
    일치한다 — 실제로 집행된 쪽이다. 원본 로그는 그대로 두고 읽는 쪽에서 거른다.
    """
    log_path = tmp_path / "pipeline.jsonl"
    _write_jsonl(
        log_path,
        [
            {"day": "2026-08-12", "ticker": "AAA", "action": "BUY", "approved": True, "rejected_by": None},
            {"day": "2026-08-12", "ticker": "BBB", "action": "HOLD", "approved": False, "rejected_by": None},
            # 두 번째 완주 — AAA 판단이 뒤집혔다
            {"day": "2026-08-12", "ticker": "AAA", "action": "HOLD", "approved": False, "rejected_by": None},
            {"day": "2026-08-12", "ticker": "BBB", "action": "HOLD", "approved": False, "rejected_by": None},
        ],
    )

    entries = load_decision_entries(log_path)

    assert len(entries) == 2, "종목당 한 건만 남아야 한다"
    aaa = next(e for e in entries if e["ticker"] == "AAA")
    assert aaa["action"] == "HOLD" and aaa["approved"] is False, "나중 기록이 이겨야 한다"


def test_rejected_by_counts_are_not_double_counted_on_duplicate_runs(tmp_path):
    """중복 완주가 게이트 거부 건수를 부풀리면 안 된다 — 감시 지표가 왜곡된다."""
    log_path = tmp_path / "pipeline.jsonl"
    entry = {
        "day": "2026-08-12", "ticker": "AAA", "action": "BUY",
        "approved": False, "rejected_by": "position_limit",
    }
    _write_jsonl(log_path, [entry, dict(entry)])

    summary = summarize_recent_trading_days(log_path, n_days=20)

    assert summary["rejected_by_counts"] == {"position_limit": 1}


def test_summarize_llm_calls_excludes_non_production_labels(tmp_path):
    """일회성 진단·실험 호출은 감시 지표에서 빠져야 한다.

    2026-08-24 reasoning A/B 90건이 분석가 호출수·토큰 집계에 섞였다. 원본 로그에는
    남기되(무슨 일이 있었는지의 기록이다) 지표는 오염시키지 않는다.
    """
    log_path = tmp_path / "llm_calls.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write_jsonl(
        log_path,
        [
            {"timestamp": now, "label": "chart", "success": True, "input_tokens": 100, "output_tokens": 20},
            {"timestamp": now, "label": "ab_chart_A1", "success": True, "input_tokens": 999, "output_tokens": 999},
            {"timestamp": now, "label": "ab_chart_B", "success": True, "input_tokens": 999, "output_tokens": 999},
        ],
    )

    summary = summarize_llm_calls(log_path)

    assert set(summary) == {"chart"}
    assert summary["chart"]["calls"] == 1
    assert summary["chart"]["input_tokens"] == 100

