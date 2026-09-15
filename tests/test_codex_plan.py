import asyncio
import json
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
