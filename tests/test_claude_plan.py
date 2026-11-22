"""Claude 구독 호출 경로 — 실제 CLI는 띄우지 않고 subprocess 경계에서 가짜 응답을 준다."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import claude_plan, operating_mode

LIMITS = {"status": "allowed", "resetsAt": 1795377600, "rateLimitType": "five_hour",
          "unifiedWindows": {"five_hour": {"utilization": 0.18, "resetsAt": 1795377600, "status": "allowed"},
                             "seven_day": {"utilization": 0.02, "resetsAt": 1795417200, "status": "allowed"}}}


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _stream(structured=None, *, api_key_source="none", limits=LIMITS, subtype="success", is_error=False,
            api_error_status=None):
    events = [{"type": "system", "subtype": "init", "apiKeySource": api_key_source, "tools": ["StructuredOutput"]}]
    if limits is not None:
        events.append({"type": "rate_limit_event", "rate_limit_info": limits})
    events.append({"type": "result", "subtype": subtype, "is_error": is_error, "api_error_status": api_error_status,
                   "structured_output": structured, "total_cost_usd": 0.0006,
                   "usage": {"input_tokens": 12, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 7,
                             "output_tokens": 3, "output_tokens_details": {"thinking_tokens": 1}}})
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _fake(monkeypatch, *, auth=None, isolated=True, reply=None, seen=None):
    auth = auth or {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}

    def run(command, **kwargs):
        assert "ANTHROPIC_API_KEY" not in kwargs["env"] and "KIS_APP_SECRET" not in kwargs["env"]
        if "--system-prompt-file" in command:
            # 임시 디렉터리는 호출이 끝나면 사라지므로 실행 시점에 읽어 둔다.
            kwargs["system_text"] = Path(command[command.index("--system-prompt-file") + 1]).read_text()
        if seen is not None:
            seen.append((command, kwargs))
        if "/usr/bin/test" in command:
            return _Completed(0 if isolated else 1)
        if command[-3:] == ["auth", "status", "--json"]:
            return _Completed(stdout=json.dumps(auth))
        return reply(command, kwargs) if reply else _Completed(stdout=_stream({"status": "ok"}))

    monkeypatch.setattr(claude_plan.subprocess, "run", run)


def test_structured_call_runs_isolated_without_tools_and_logs_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("KIS_APP_SECRET", "must-not-leak")
    seen = []
    _fake(monkeypatch, seen=seen)
    log = tmp_path / "calls.jsonl"
    result = asyncio.run(claude_plan.call_structured(
        system="Judge one ticker", user="ticker data", response_model=claude_plan.HealthResponse,
        json_schema=claude_plan._HEALTH_SCHEMA, label="chart", log_path=log, timeout_s=30.0))
    assert result == claude_plan.HealthResponse(status="ok")
    command, kwargs = seen[-1]
    assert command[:8] == ["/usr/bin/sudo", "-n", "-H", "-u", "sima-claude", "/usr/bin/env",
                           "DISABLE_AUTOUPDATER=1", "/home/sima-claude/.local/bin/claude"]
    assert command[command.index("--tools") + 1] == "" and "--allowedTools" not in command
    assert command[command.index("--setting-sources") + 1] == ""
    assert {"-p", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(command)
    assert command[command.index("--model") + 1] == claude_plan.DEFAULT_MODEL
    assert json.loads(command[command.index("--json-schema") + 1]) == claude_plan._HEALTH_SCHEMA
    assert kwargs["input"] == "ticker data" and kwargs["system_text"] == "Judge one ticker"
    assert 0 < kwargs["timeout"] <= 30.0
    entry = json.loads(log.read_text())
    assert entry["success"] and entry["label"] == "chart" and entry["provider"] == "claude_plan"
    assert (entry["input_tokens"], entry["cached_input_tokens"], entry["cache_write_input_tokens"],
            entry["output_tokens"], entry["reasoning_output_tokens"]) == (12, 4, 7, 3, 1)
    assert entry["list_cost_usd"] == pytest.approx(0.0006)


def test_empty_system_prompt_never_falls_back_to_the_default_agent_prompt(monkeypatch):
    seen = []
    _fake(monkeypatch, seen=seen)
    claude_plan.run_structured_sync("", "data", claude_plan._HEALTH_SCHEMA)
    assert seen[-1][1]["system_text"].strip()


@pytest.mark.parametrize("auth", [
    {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"},
    {"loggedIn": False, "authMethod": "claude.ai", "apiProvider": "firstParty"},
    {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"},
])
def test_refuses_anything_but_a_claude_ai_subscription_before_inference(monkeypatch, auth):
    seen = []
    _fake(monkeypatch, auth=auth, seen=seen,
          reply=lambda c, k: pytest.fail("inference started without subscription login"))
    with pytest.raises(claude_plan.PlanUnavailable, match="subscription"):
        claude_plan.run_structured_sync("s", "u", claude_plan._HEALTH_SCHEMA)


def test_refuses_when_the_isolated_user_can_traverse_the_repo(monkeypatch):
    _fake(monkeypatch, isolated=False, reply=lambda c, k: pytest.fail("inference started"))
    with pytest.raises(claude_plan.PlanUnavailable, match="production repository"):
        claude_plan.run_structured_sync("s", "u", claude_plan._HEALTH_SCHEMA)


def test_discards_a_result_produced_with_an_api_key(monkeypatch):
    _fake(monkeypatch, reply=lambda c, k: _Completed(stdout=_stream({"status": "ok"}, api_key_source="ANTHROPIC_API_KEY")))
    with pytest.raises(claude_plan.PlanUnavailable, match="API key"):
        claude_plan.run_structured_sync("s", "u", claude_plan._HEALTH_SCHEMA)


@pytest.mark.parametrize("returncode,kwargs,expected,code", [
    (1, dict(subtype="error_during_execution", is_error=True, api_error_status=429), claude_plan.PlanProviderRejected, "rate_limited"),
    (1, dict(subtype="error_during_execution", is_error=True, limits={**LIMITS, "status": "rejected"}), claude_plan.PlanProviderRejected, "rate_limited"),
    (1, dict(subtype="error_during_execution", is_error=True, api_error_status=401), claude_plan.PlanProviderRejected, "authentication_failed"),
    (1, dict(subtype="error_during_execution", is_error=True, api_error_status=403), claude_plan.PlanProviderRejected, "access_denied"),
    (1, dict(subtype="error_during_execution", is_error=True, api_error_status=529), claude_plan.PlanTransientFailure, None),
    (0, dict(subtype="error_max_turns", is_error=True), claude_plan.PlanUnavailable, None),
])
def test_execution_errors_are_classified(monkeypatch, returncode, kwargs, expected, code):
    _fake(monkeypatch, reply=lambda c, k: _Completed(returncode, stdout=_stream(None, **kwargs)))
    with pytest.raises(expected) as caught:
        claude_plan.run_structured_sync("s", "u", claude_plan._HEALTH_SCHEMA)
    assert type(caught.value) is expected
    if code:
        assert caught.value.code == code


def test_completed_turn_with_nonzero_exit_is_uncertain_and_never_retried(monkeypatch, tmp_path):
    calls = []
    _fake(monkeypatch, reply=lambda c, k: calls.append(1) or _Completed(1, stdout=_stream({"status": "ok"})))
    with pytest.raises(claude_plan.PlanUnavailable, match="uncertain"):
        asyncio.run(claude_plan.check_health(log_path=tmp_path / "calls.jsonl"))
    assert len(calls) == 1


def test_rate_limited_call_retries_only_the_failed_transport(monkeypatch, tmp_path):
    replies = iter([_Completed(1, stdout=_stream(None, subtype="error_during_execution", is_error=True, api_error_status=429)),
                    _Completed(stdout=_stream({"status": "ok"}))])
    _fake(monkeypatch, reply=lambda c, k: next(replies))
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(claude_plan.asyncio, "sleep", no_sleep)
    result = asyncio.run(claude_plan.call_structured(
        system="s", user="u", response_model=claude_plan.HealthResponse, json_schema=claude_plan._HEALTH_SCHEMA,
        log_path=tmp_path / "calls.jsonl", timeout_s=110.0))
    assert result.status == "ok" and slept == [15.0]
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [r["success"] for r in rows] == [False, True] and rows[0]["recovery_retry"]


def test_invalid_structured_output_fails_without_retry(monkeypatch, tmp_path):
    calls = []
    _fake(monkeypatch, reply=lambda c, k: calls.append(1) or _Completed(stdout=_stream({"status": "maybe"})))
    with pytest.raises(claude_plan.PlanUnavailable, match="invalid"):
        asyncio.run(claude_plan.check_health(log_path=tmp_path / "calls.jsonl"))
    assert len(calls) == 1


def test_unexpected_model_is_rejected_without_fallback(monkeypatch):
    _fake(monkeypatch, reply=lambda c, k: pytest.fail("inference started"))
    with pytest.raises(claude_plan.PlanUnavailable, match="Unsupported"):
        asyncio.run(claude_plan.call_structured(system="s", user="u", response_model=claude_plan.HealthResponse,
                                                json_schema={}, model="claude-opus-5-5"))


def test_provider_rejection_stops_queued_calls_for_the_session(monkeypatch, tmp_path):
    _fake(monkeypatch, reply=lambda c, k: _Completed(1, stdout=_stream(None, subtype="error_during_execution",
                                                                     is_error=True, api_error_status=401)))
    session = claude_plan.CallSession()

    async def run():
        token = claude_plan.CALL_SESSION.set(session)
        try:
            for _ in range(2):
                with pytest.raises(claude_plan.PlanProviderRejected):
                    await claude_plan.call_structured(system="s", user="u", response_model=claude_plan.HealthResponse,
                                                      json_schema={}, log_path=tmp_path / "calls.jsonl")
        finally:
            claude_plan.CALL_SESSION.reset(token)
    asyncio.run(run())
    assert session.rejected.code == "authentication_failed" and session.skipped == 1


def test_capacity_reads_seven_day_window_and_keeps_two_percent_floor():
    at = datetime(2026, 11, 23, 6, 55, tzinfo=claude_plan.KST)
    status = claude_plan.capacity_from_limits(LIMITS, checked_at=at)
    assert (status.used_percent, status.remaining_percent, status.five_hour_used_percent) == (2.0, 98.0, 18.0)
    assert status.buy_judgment_allowed and status.day == "2026-11-23"
    floor = {**LIMITS, "unifiedWindows": {**LIMITS["unifiedWindows"], "seven_day": {
        "utilization": 0.98, "resetsAt": 1795417200, "status": "allowed"}}}
    low = claude_plan.capacity_from_limits(floor, checked_at=at)
    assert low.remaining_percent == 2.0 and not low.buy_judgment_allowed and low.ordinary_usage_allowed
    blocked = {**LIMITS, "unifiedWindows": {**LIMITS["unifiedWindows"], "five_hour": {
        "utilization": 1.0, "resetsAt": 1795377600, "status": "rejected"}}}
    assert not claude_plan.capacity_from_limits(blocked, checked_at=at).ordinary_usage_allowed


@pytest.mark.parametrize("limits", [None, {}, {"unifiedWindows": {"seven_day": {"utilization": "x"}}},
                                    {**LIMITS, "unifiedWindows": {**LIMITS["unifiedWindows"], "seven_day": {
                                        "utilization": 1.5, "resetsAt": 1795417200, "status": "allowed"}}}])
def test_invalid_capacity_payload_fails_closed(limits):
    with pytest.raises(claude_plan.PlanUnavailable):
        claude_plan.capacity_from_limits(limits)


def test_capacity_state_must_be_for_today(tmp_path):
    path = tmp_path / "capacity.json"
    claude_plan.save_capacity_status(claude_plan.capacity_from_limits(
        LIMITS, checked_at=datetime(2026, 11, 20, 6, 55, tzinfo=claude_plan.KST)), path)
    assert claude_plan.load_capacity_status("2026-11-20", path).buy_judgment_allowed
    with pytest.raises(claude_plan.PlanUnavailable, match="stale"):
        claude_plan.load_capacity_status("2026-11-23", path)
    path.write_text(path.read_text().replace('"buy_judgment_allowed": true', '"buy_judgment_allowed": false'))
    with pytest.raises(claude_plan.PlanUnavailable):
        claude_plan.load_capacity_status("2026-11-20", path)


def test_readiness_failure_removes_old_permission_and_success_saves_limits(monkeypatch, tmp_path):
    from scripts import check_claude_plan as script
    monkeypatch.setattr(script, "is_krx_trading_day", lambda day: True)
    state = tmp_path / "capacity.json"
    state.write_text("previous permission")
    monkeypatch.setattr(claude_plan, "DEFAULT_CAPACITY_STATE_PATH", state)
    calls = []

    async def failing(limits_sink):
        calls.append(1)
        if len(calls) == 1:
            raise claude_plan.PlanProviderRejected("429", "rate_limited")
        limits_sink.append(LIMITS)
        return claude_plan.HealthResponse(status="ok")
    monkeypatch.setattr(claude_plan, "check_health", failing)
    with pytest.raises(claude_plan.PlanUnavailable, match="Concurrent"):
        asyncio.run(script.main())
    assert len(calls) == 2 and not state.exists()

    async def healthy(limits_sink):
        limits_sink.append(LIMITS)
        return claude_plan.HealthResponse(status="ok")
    monkeypatch.setattr(claude_plan, "check_health", healthy)
    asyncio.run(script.main())
    saved = claude_plan.CapacityStatus.model_validate_json(state.read_text())
    assert saved.remaining_percent == 98.0 and saved.buy_judgment_allowed


def test_exit_only_blocks_every_cli_start(monkeypatch):
    monkeypatch.setattr(operating_mode, "EXIT_ONLY", True)
    monkeypatch.setattr(claude_plan.subprocess, "run", lambda *a, **k: pytest.fail("CLI started in EXIT_ONLY"))
    with pytest.raises(RuntimeError, match="exit_only"):
        claude_plan.run_structured_sync("s", "u", claude_plan._HEALTH_SCHEMA)
    with pytest.raises(RuntimeError, match="exit_only"):
        asyncio.run(claude_plan.call_structured(system="s", user="u", response_model=claude_plan.HealthResponse,
                                                json_schema={}))


def test_collector_repair_search_allows_only_web_tools(monkeypatch, tmp_path):
    from src import collector_repair
    seen = []
    _fake(monkeypatch, seen=seen, reply=lambda c, k: _Completed(stdout=_stream({"status": "ok"})))
    monkeypatch.setattr(collector_repair, "budget", lambda seconds: seconds)
    monkeypatch.setattr(collector_repair, "audit", lambda *a, **k: None)
    collector_repair.model_call("collector_repair.md", {"x": 1}, claude_plan.HealthResponse, search=True)
    command, kwargs = seen[-1]
    assert command[command.index("--tools") + 1] == "WebSearch,WebFetch"
    assert command[command.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert kwargs["input"].startswith("UNTRUSTED INPUT DATA:")
