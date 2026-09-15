import asyncio
import json
import time
from io import StringIO
from pathlib import Path

import pytest
from src import codex_plan


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_structured_call_uses_chatgpt_login_and_strips_api_keys(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    monkeypatch.setenv("KIS_APP_SECRET", "must-not-leak")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "CODEX_API_KEY" not in kwargs["env"]
        assert "KIS_APP_SECRET" not in kwargs["env"]
        if "/usr/bin/test" in command:
            return _Completed()
        if command[-2:] == ["login", "status"]:
            return _Completed(stdout="Logged in using ChatGPT\n")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"status":"ok"}')
        usage = {
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "cached_input_tokens": 4, "output_tokens": 3},
        }
        return _Completed(stdout=json.dumps(usage) + "\n")

    monkeypatch.setattr(codex_plan.subprocess, "run", fake_run)
    log_path = tmp_path / "calls.jsonl"

    result = asyncio.run(
        codex_plan.check_health(log_path=log_path)
    )

    assert result == codex_plan.HealthResponse(status="ok")
    assert calls[1][0][:6] == [
        "/usr/bin/sudo", "-n", "-H", "-u", "sima-codex", "/home/sima-codex/.local/bin/codex"
    ]
    assert calls[2][0][-1] == "-"
    assert "Do not inspect files" in calls[2][1]["input"]
    entry = json.loads(log_path.read_text())
    assert entry["success"] is True
    assert entry["input_tokens"] == 12
    assert entry["cached_input_tokens"] == 4
    assert entry["output_tokens"] == 3


def test_refuses_to_fall_back_to_api_authentication(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        if "/usr/bin/test" in command:
            return _Completed()
        return _Completed(stdout="Logged in using an API key\n")

    monkeypatch.setattr(codex_plan.subprocess, "run", fake_run)
    log_path = tmp_path / "calls.jsonl"

    with pytest.raises(codex_plan.CodexPlanUnavailable, match="not logged in using ChatGPT"):
        asyncio.run(
            codex_plan.check_health(log_path=log_path)
        )

    entry = json.loads(log_path.read_text())
    assert entry["success"] is False


def test_refuses_to_run_when_the_isolated_user_can_traverse_the_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_plan.subprocess, "run", lambda *args, **kwargs: _Completed(returncode=1))
    log_path = tmp_path / "calls.jsonl"

    with pytest.raises(codex_plan.CodexPlanUnavailable, match="can access the production repository"):
        asyncio.run(codex_plan.check_health(log_path=log_path))

    assert json.loads(log_path.read_text())["success"] is False


def test_buy_call_uses_sol_and_the_shared_llm_monitoring_log(monkeypatch, tmp_path):
    captured = {}

    async def fake_private(**kwargs):
        captured.update(kwargs)
        return codex_plan.HealthResponse(status="ok")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(codex_plan, "_call_structured", fake_private)

    result = asyncio.run(
        codex_plan.call_structured(
            system="s",
            user="u",
            response_model=codex_plan.HealthResponse,
            json_schema={},
            label="chart",
        )
    )

    assert result.status == "ok"
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["label"] == "chart"
    assert captured["log_path"] == codex_plan.DEFAULT_JUDGMENT_CALL_LOG_PATH


def test_buy_call_rejects_an_unexpected_model_without_fallback():
    with pytest.raises(codex_plan.CodexPlanUnavailable, match="Unsupported"):
        asyncio.run(
            codex_plan.call_structured(
                system="",
                user="",
                response_model=codex_plan.HealthResponse,
                json_schema={},
                model="claude-sonnet-5",
            )
        )


def test_cancelled_codex_call_is_logged_as_failure(monkeypatch, tmp_path):
    def slow_sync(*args, **kwargs):
        time.sleep(0.05)
        return codex_plan.HealthResponse(status="ok"), {}

    monkeypatch.setattr(codex_plan, "_call_sync", slow_sync)
    log_path = tmp_path / "calls.jsonl"

    async def cancel_in_flight():
        task = asyncio.create_task(
            codex_plan._call_structured(
                system="",
                user="",
                response_model=codex_plan.HealthResponse,
                json_schema={},
                label="chart",
                model=codex_plan.DEFAULT_MODEL,
                log_path=log_path,
            )
        )
        await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_in_flight())

    entry = json.loads(log_path.read_text())
    assert entry["success"] is False
    assert entry["error"] == "cancelled"
    assert entry["input_tokens"] == 0


def test_reads_weekly_capacity_without_an_llm_turn(monkeypatch):
    monkeypatch.setattr(codex_plan, "_ensure_filesystem_isolation", lambda env: None)
    monkeypatch.setattr(codex_plan, "_ensure_chatgpt_login", lambda env: None)
    responses = [
        {"id": 1, "result": {"userAgent": "test"}},
        {
            "id": 2,
            "result": {
                "ordinaryUsageAllowed": True,
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 83,
                            "windowDurationMins": 10080,
                            "resetsAt": 1789975211,
                        }
                    }
                },
            },
        },
    ]

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stdin = StringIO()
            self.stdout = StringIO("".join(json.dumps(r) + "\n" for r in responses))
            self.stderr = StringIO()

        def terminate(self): pass
        def wait(self, timeout=None): return 0
        def kill(self): pass

    monkeypatch.setattr(codex_plan.subprocess, "Popen", FakeProcess)

    status = codex_plan._read_capacity_sync()

    assert status.used_percent == 83
    assert status.remaining_percent == 17
    assert status.window_duration_mins == 10080
    assert status.buy_judgment_allowed is True
    assert status.estimated_daily_runs_remaining == 8.5
    assert status.estimated_token_equivalent_remaining == round(8.5 * 3_683_668)


def test_capacity_two_percent_is_not_allowed(monkeypatch):
    monkeypatch.setattr(codex_plan, "_ensure_filesystem_isolation", lambda env: None)
    monkeypatch.setattr(codex_plan, "_ensure_chatgpt_login", lambda env: None)
    responses = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {
            "usedPercent": 98, "windowDurationMins": 10080, "resetsAt": 1789975211
        }}}},
    ]

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stdin, self.stderr = StringIO(), StringIO()
            self.stdout = StringIO("".join(json.dumps(r) + "\n" for r in responses))
        def terminate(self): pass
        def wait(self, timeout=None): return 0
        def kill(self): pass

    monkeypatch.setattr(codex_plan.subprocess, "Popen", FakeProcess)
    assert codex_plan._read_capacity_sync().buy_judgment_allowed is False


def test_capacity_state_must_be_for_today(tmp_path):
    path = tmp_path / "capacity.json"
    status = codex_plan.CapacityStatus(
        checked_at="2026-09-15T08:05:00+09:00",
        day="2026-09-15",
        used_percent=83,
        remaining_percent=17,
        window_duration_mins=10080,
        resets_at="2026-09-21T00:00:00+00:00",
        ordinary_usage_allowed=True,
        buy_judgment_allowed=True,
        estimated_daily_runs_remaining=8.5,
        estimated_token_equivalent_remaining=31_311_178,
    )
    codex_plan.save_capacity_status(status, path)

    assert codex_plan.load_capacity_status("2026-09-15", path) == status
    with pytest.raises(codex_plan.CodexPlanUnavailable, match="stale"):
        codex_plan.load_capacity_status("2026-09-16", path)


@pytest.mark.parametrize("invalid_used", [-1, 101, "NaN"])
def test_invalid_backend_capacity_fails_closed(monkeypatch, invalid_used):
    monkeypatch.setattr(codex_plan, "_ensure_filesystem_isolation", lambda env: None)
    monkeypatch.setattr(codex_plan, "_ensure_chatgpt_login", lambda env: None)
    responses = [
        {"id": 1, "result": {}},
        {"id": 2, "result": {"ordinaryUsageAllowed": True, "rateLimits": {"primary": {
            "usedPercent": invalid_used, "windowDurationMins": 10080, "resetsAt": 1789975211
        }}}},
    ]

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            self.stdin, self.stderr = StringIO(), StringIO()
            self.stdout = StringIO("".join(json.dumps(r) + "\n" for r in responses))
        def terminate(self): pass
        def wait(self, timeout=None): return 0
        def kill(self): pass

    monkeypatch.setattr(codex_plan.subprocess, "Popen", FakeProcess)
    with pytest.raises(codex_plan.CodexPlanUnavailable, match="payload is invalid"):
        codex_plan._read_capacity_sync()


def test_tampered_capacity_state_cannot_authorize(tmp_path):
    path = tmp_path / "capacity.json"
    path.write_text(json.dumps({
        "checked_at": "2026-09-15T08:05:00+09:00",
        "day": "2026-09-15",
        "used_percent": 98,
        "remaining_percent": 2,
        "window_duration_mins": 10080,
        "resets_at": "2026-09-21T00:00:00+00:00",
        "ordinary_usage_allowed": True,
        "buy_judgment_allowed": True,
        "threshold_percent": 0,
        "estimated_daily_runs_remaining": 1,
        "estimated_token_equivalent_remaining": 3_683_668,
    }))

    with pytest.raises(codex_plan.CodexPlanUnavailable, match="state unavailable"):
        codex_plan.load_capacity_status("2026-09-15", path)
