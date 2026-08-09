import json
from datetime import datetime, timedelta, timezone

from src.pipeline import summarize_llm_calls, summarize_recent_trading_days


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
