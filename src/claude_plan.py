"""Claude 구독에 포함된 Claude Code CLI를 비대화형 구조화 출력으로 호출한다.

2026-11-23 Codex 구독 종료 후 사용자 결정으로 `codex_plan`의 역할을 넘겨받았다.
Anthropic API 키를 쓰지 않는다는 사실이 이 모듈의 핵심 계약이다. 실행 환경에서 운영
자격증명을 전부 제거하고, 저장된 로그인이 claude.ai 구독 계정인지 매 호출 전에 확인하며,
CLI가 보고한 `apiKeySource`가 "none"이 아니면 결과를 버린다. Claude 프로세스는 전용 OS
계정에서 돌아 운영 저장소를 읽지 못하고, 기본 도구·설정·MCP·슬래시 명령 없이 주입한
입력만으로 스키마 응답을 낸다. 매수 판단 호출은 기존 LLM 감시 로그에 함께 남긴다.
"""

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
import json
import logging
import math
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, StrictBool, ValidationError, model_validator

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5-5"  # 종목당 독립 호출로 하루 수백 회 — 구독 한도 대비 Sonnet
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_CALL_LOG_PATH = Path("logs/claude_plan_calls.jsonl")
DEFAULT_JUDGMENT_CALL_LOG_PATH = Path("logs/llm_calls.jsonl")
DEFAULT_CAPACITY_STATE_PATH = Path("logs/claude_plan_capacity.json")
# Codex 실측(전체 일일 부하 ≈ 주간 2%p)에서 옮긴 자리표시값이다. Claude 구독의 일일 부하는
# 아직 측정하지 않았다 — 매수 재개 전 섀도 실행으로 다시 정한다(docs/PLAN.md).
BUY_CAPACITY_FLOOR_PERCENT = 2.0
MAX_CONCURRENT_CALLS = 2
CLAUDE_OS_USER = "sima-claude"
CLAUDE_EXECUTABLE = "/home/sima-claude/.local/bin/claude"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
KST = ZoneInfo("Asia/Seoul")
T = TypeVar("T", bound=BaseModel)
_ALLOWED_LIMIT_STATUSES = frozenset({"allowed", "allowed_warning"})

_CALL_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
# 기본 executor를 다른 작업과 공유하면 시간 제한 계산이 큐 대기에 묻힌다.
_CALL_EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CALLS, thread_name_prefix="sima-claude")


class PlanUnavailable(RuntimeError):
    """구독 로그인·CLI·플랜 호출 중 하나를 사용할 수 없을 때."""


class PlanTransientFailure(PlanUnavailable):
    """A failed turn with a confirmed temporary provider error, never a completed turn."""


class PlanProviderRejected(PlanUnavailable):
    def __init__(self, message: str, code: str, *, attempted: bool = True):
        super().__init__(message)
        self.code = code
        self.attempted = attempted


@dataclass
class CallSession:
    """한 분석 회차의 공급자 거부를 공유한다. 성공한 판단은 재시도하지 않는다."""
    rejected: PlanProviderRejected | None = None
    skipped: int = 0


CALL_SESSION: contextvars.ContextVar[CallSession | None] = contextvars.ContextVar("claude_call_session", default=None)


class HealthResponse(BaseModel):
    status: Literal["ok"]


class CapacityStatus(BaseModel):
    """CLI의 rate_limit_event에서 읽은 구독 한도. 7일 창을 주간 한도로 쓴다."""
    checked_at: datetime
    day: str
    used_percent: float = Field(ge=0.0, le=100.0, allow_inf_nan=False)
    remaining_percent: float = Field(ge=0.0, le=100.0, allow_inf_nan=False)
    five_hour_used_percent: float = Field(ge=0.0, le=100.0, allow_inf_nan=False)
    resets_at: datetime
    five_hour_resets_at: datetime
    ordinary_usage_allowed: StrictBool
    buy_judgment_allowed: StrictBool
    threshold_percent: Literal[2.0] = BUY_CAPACITY_FLOOR_PERCENT

    @model_validator(mode="after")
    def validate_derived_fields(self):
        if self.checked_at.astimezone(KST).date().isoformat() != self.day:
            raise ValueError("day does not match checked_at in KST")
        if self.remaining_percent != round(100.0 - self.used_percent, 2):
            raise ValueError("remaining_percent does not match used_percent")
        expected_allowed = self.ordinary_usage_allowed and self.remaining_percent > self.threshold_percent
        if self.buy_judgment_allowed != expected_allowed:
            raise ValueError("buy_judgment_allowed does not match the capacity floor")
        return self


_HEALTH_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}},
    "required": ["status"],
    "additionalProperties": False,
}


def _subscription_environment() -> dict[str, str]:
    """CLI 실행에 필요한 경로만 넘기고 운영 자격증명은 전부 격리한다."""
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    for key in ("LANG", "LC_ALL"):
        if value := os.environ.get(key):
            env[key] = value
    return env


def _claude_command(*args: str) -> list[str]:
    """운영 비밀값을 읽을 수 없는 전용 OS 계정에서만 CLI를 실행한다. 자동 업데이트는 끈다."""
    return ["/usr/bin/sudo", "-n", "-H", "-u", CLAUDE_OS_USER,
            "/usr/bin/env", "DISABLE_AUTOUPDATER=1", CLAUDE_EXECUTABLE, *args]


def _run(command: list[str], *, env: dict[str, str], input_text: str | None = None, cwd: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S):
    from src import operating_mode
    operating_mode.require_broker_mode()
    return subprocess.run(command, input=input_text, text=True, capture_output=True,
                          timeout=timeout_s, check=False, env=env, cwd=cwd)


def _ensure_subscription_login(env: dict[str, str], timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    try:
        result = _run(_claude_command("auth", "status", "--json"), env=env, timeout_s=timeout_s)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise PlanUnavailable(f"Claude CLI unavailable: {exc}") from exc
    try:
        status = json.loads(result.stdout)
    except ValueError:
        status = None
    if (result.returncode != 0 or not isinstance(status, dict) or status.get("loggedIn") is not True
            or status.get("authMethod") != "claude.ai" or status.get("apiProvider") != "firstParty"):
        raise PlanUnavailable("Claude CLI is not logged in with a claude.ai subscription")


def _ensure_filesystem_isolation(env: dict[str, str], timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    """전용 계정이 운영 저장소를 탐색할 수 있으면 호출을 거부한다."""
    command = ["/usr/bin/sudo", "-n", "-u", CLAUDE_OS_USER, "/usr/bin/test", "!", "-x", str(PROJECT_ROOT)]
    try:
        result = _run(command, env=env, timeout_s=timeout_s)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise PlanUnavailable(f"Claude isolation check unavailable: {exc}") from exc
    if result.returncode != 0:
        raise PlanUnavailable("Claude OS user can access the production repository")


def _events(stdout: str) -> tuple[dict | None, dict | None, dict | None]:
    """stream-json에서 init·최종 result·마지막 rate_limit_info를 고른다."""
    init = final = limits = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init = event
        elif event.get("type") == "result":
            final = event
        elif event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
            limits = event["rate_limit_info"]
    return init, final, limits


def _usage(final: dict | None) -> dict[str, Any]:
    raw = (final or {}).get("usage")
    if not isinstance(raw, dict):
        return {}
    details = raw.get("output_tokens_details") if isinstance(raw.get("output_tokens_details"), dict) else {}
    usage: dict[str, Any] = {
        "input_tokens": int(raw.get("input_tokens", 0)),
        "cached_input_tokens": int(raw.get("cache_read_input_tokens", 0)),
        "cache_write_input_tokens": int(raw.get("cache_creation_input_tokens", 0)),
        "output_tokens": int(raw.get("output_tokens", 0)),
        "reasoning_output_tokens": int(details.get("thinking_tokens", 0)),
    }
    cost = final.get("total_cost_usd")
    if isinstance(cost, (int, float)) and math.isfinite(cost):
        usage["list_cost_usd"] = float(cost)  # 구독 청구가 아니라 표준 단가 환산치
    return usage


def _execution_error(returncode: int, final: dict | None, limits: dict | None, stderr: str) -> PlanUnavailable:
    detail = ""
    if final is not None:
        detail = f"subtype={final.get('subtype')} api_error_status={final.get('api_error_status')} result={str(final.get('result'))[:500]}"
    message = f"Claude execution failed ({returncode}): {detail} {stderr[-1000:].strip()}".strip()
    status = (final or {}).get("api_error_status")
    if (limits or {}).get("status") == "rejected" or status == 429:
        return PlanProviderRejected(message, "rate_limited")
    if status == 401:
        return PlanProviderRejected(message, "authentication_failed")
    if status == 403:
        return PlanProviderRejected(message, "access_denied")
    if status in (500, 502, 503, 504, 529):
        return PlanTransientFailure(message)
    return PlanUnavailable(message)


def run_structured_sync(
    system: str, user: str, json_schema: dict[str, Any], *, model: str = DEFAULT_MODEL,
    timeout_s: float | None = None, tools: tuple[str, ...] = (), max_turns: int = 2,
) -> tuple[dict, dict[str, Any], dict | None]:
    """한 번의 CLI 실행. (structured_output, usage, rate_limit_info)를 돌려준다.

    tools가 비어 있으면 내장 도구가 하나도 없다(구조화 출력 전용 도구만 CLI가 붙인다).
    수집 자동 수리처럼 검색이 필요한 호출만 WebSearch·WebFetch를 명시적으로 허용한다.
    """
    env = _subscription_environment()
    deadline = None if timeout_s is None else time.monotonic() + timeout_s

    def remaining() -> float:
        if deadline is None:
            return DEFAULT_TIMEOUT_S
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise PlanUnavailable("Claude call deadline exceeded")
        return seconds

    _ensure_filesystem_isolation(env, timeout_s=remaining())
    _ensure_subscription_login(env, timeout_s=remaining())
    with tempfile.TemporaryDirectory(prefix="sima-claude-plan-") as tmp:
        tmp_path = Path(tmp)
        # 전용 계정은 이 임시 디렉터리 안에서 시스템 프롬프트만 읽는다.
        # 임의 경로를 나열할 수 없게 other에는 write+execute만 준다.
        tmp_path.chmod(0o733)
        system_path = tmp_path / "system.txt"
        # 빈 시스템 프롬프트를 넘기면 CLI가 기본 코딩 에이전트 프롬프트로 돌아갈 수 있다.
        system_path.write_text(system or "Answer using only the information in the user message.")
        system_path.chmod(0o644)
        args = ["-p", "--output-format", "stream-json", "--verbose", "--model", model,
                "--json-schema", json.dumps(json_schema, ensure_ascii=False),
                "--system-prompt-file", str(system_path), "--tools", ",".join(tools),
                "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
                "--no-session-persistence", "--max-turns", str(max_turns)]
        if tools:
            args += ["--allowedTools", ",".join(tools)]
        try:
            result = _run(_claude_command(*args), env=env, input_text=user, cwd=tmp, timeout_s=remaining())
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise PlanUnavailable(f"Claude execution unavailable: {exc}") from exc
    init, final, limits = _events(result.stdout)
    if init is not None and init.get("apiKeySource") != "none":
        raise PlanUnavailable(f"Claude CLI used an API key source: {init.get('apiKeySource')}")
    ok = final is not None and final.get("subtype") == "success" and final.get("is_error") is False
    if result.returncode != 0:
        if ok:
            raise PlanUnavailable("Claude exited with output but uncertain completion; no retry")
        raise _execution_error(result.returncode, final, limits, result.stderr)
    if init is None:
        raise PlanUnavailable("Claude init event missing; API key source unverified")
    if not ok:
        raise _execution_error(result.returncode, final, limits, result.stderr)
    structured = final.get("structured_output")
    if not isinstance(structured, dict):
        raise PlanUnavailable("Claude structured output missing")
    return structured, _usage(final), limits


def save_capacity_status(status: CapacityStatus, path: Path | None = None) -> None:
    """07:00 판단이 부분 파일을 읽지 않도록 원자적으로 교체한다."""
    path = path or DEFAULT_CAPACITY_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(status.model_dump_json(indent=2))
    temp_path.replace(path)


def load_capacity_status(day: str, path: Path | None = None) -> CapacityStatus:
    """당일 06:55 상태만 허용한다. 없거나 낡거나 손상되면 매수 판단을 실패 폐쇄한다."""
    path = path or DEFAULT_CAPACITY_STATE_PATH
    try:
        status = CapacityStatus.model_validate_json(path.read_text())
    except (OSError, ValidationError, ValueError) as exc:
        raise PlanUnavailable(f"Claude capacity state unavailable: {exc}") from exc
    if status.day != day:
        raise PlanUnavailable(f"Claude capacity state is stale: checked_day={status.day} today={day}")
    return status


def capacity_from_limits(limits: dict | None, *, checked_at: datetime | None = None) -> CapacityStatus:
    windows = (limits or {}).get("unifiedWindows")
    try:
        week, short = windows["seven_day"], windows["five_hour"]
        used = round(float(week["utilization"]) * 100, 2)
        short_used = round(float(short["utilization"]) * 100, 2)
        if not (math.isfinite(used) and math.isfinite(short_used)):
            raise ValueError("non-finite utilization")
        resets = datetime.fromtimestamp(int(week["resetsAt"]), timezone.utc)
        short_resets = datetime.fromtimestamp(int(short["resetsAt"]), timezone.utc)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise PlanUnavailable(f"Claude rate-limit payload is invalid: {exc}") from exc
    allowed = all(w.get("status") in _ALLOWED_LIMIT_STATUSES for w in (limits, week, short))
    checked_at = checked_at or datetime.now(timezone.utc)
    remaining = round(100.0 - used, 2)
    try:
        return CapacityStatus(
            checked_at=checked_at, day=checked_at.astimezone(KST).date().isoformat(),
            used_percent=used, remaining_percent=remaining, five_hour_used_percent=short_used,
            resets_at=resets, five_hour_resets_at=short_resets, ordinary_usage_allowed=allowed,
            buy_judgment_allowed=allowed and remaining > BUY_CAPACITY_FLOOR_PERCENT)
    except ValidationError as exc:
        raise PlanUnavailable(f"Claude rate-limit payload is invalid: {exc}") from exc


def _append_log(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _call_sync(system, user, response_model, json_schema, model, timeout_s=None):
    structured, usage, limits = run_structured_sync(system, user, json_schema, model=model, timeout_s=timeout_s)
    try:
        return response_model.model_validate(structured), usage, limits
    except ValidationError as exc:
        raise PlanUnavailable(f"Claude structured output invalid: {exc}") from exc


async def _call_structured(
    *, system: str, user: str, response_model: type[T], json_schema: dict[str, Any],
    label: str, model: str, log_path: Path | None,
    timeout_s: float | None = None, limits_sink: list | None = None,
) -> T:
    path = log_path or DEFAULT_CALL_LOG_PATH
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    base = {"timestamp": now.isoformat(), "day": now.astimezone(KST).date().isoformat(),
            "label": label, "model": model, "provider": "claude_plan"}
    try:
        deadline = started + (DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s)
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PlanUnavailable("Claude recovery deadline exceeded")
            context = contextvars.copy_context()
            call = partial(_call_sync, system, user, response_model, json_schema, model, remaining)
            try:
                result, usage, limits = await asyncio.get_running_loop().run_in_executor(_CALL_EXECUTOR, context.run, call)
                break
            except (PlanProviderRejected, PlanTransientFailure) as exc:
                # 실패한 호출만 재시도한다. 완료된 판단은 결과와 무관하게 다시 묻지 않는다.
                retryable = isinstance(exc, PlanTransientFailure) or exc.code == "rate_limited"
                delay = 15.0 * (2 ** attempt)
                if not retryable or attempt == 2 or deadline - time.monotonic() <= delay + 5:
                    raise
                _append_log(path, {**base, "success": False, "attempt": attempt + 1,
                                   "recovery_retry": True, "retry_delay_s": delay, "error": repr(exc)})
                await asyncio.sleep(delay)
    except asyncio.CancelledError:
        # 바깥 대기가 취소돼도 이미 시작한 subprocess 스레드는 끝까지 돌 수 있다.
        _append_log(path, {**base, "input_tokens": 0, "output_tokens": 0,
                           "elapsed_s": round(time.monotonic() - started, 2), "success": False, "error": "cancelled"})
        raise
    except Exception as exc:
        _append_log(path, {**base, "elapsed_s": round(time.monotonic() - started, 2), "success": False, "error": repr(exc)})
        raise
    if limits_sink is not None:
        limits_sink.append(limits)
    _append_log(path, {**base, **usage, "elapsed_s": round(time.monotonic() - started, 2), "success": True})
    logger.info("claude_plan_call label=%s model=%s usage=%s", label, model, usage)
    return result


async def call_structured(
    system: str,
    user: str,
    response_model: type[T],
    json_schema: dict[str, Any],
    model: str = DEFAULT_MODEL,
    max_tokens: int | None = None,
    effort: str | None = None,
    label: str = "unknown",
    log_path: Path | None = None,
    timeout_s: float | None = None,
) -> T:
    """기존 ``llm.call_structured``와 같은 형태로 매수 판단에서 쓰는 진입점.

    ``max_tokens``와 ``effort``는 호환 목적으로 받는다. 모델명이 다르면 조용히 다른
    모델로 바꾸지 않고 실패시킨다. 파이프라인이 종목을 병렬 처리할 때 CLI 프로세스가
    한꺼번에 수백 개 뜨지 않도록 동시 실행도 제한한다.
    """
    from src import operating_mode
    operating_mode.require_broker_mode()
    del max_tokens, effort
    if model != DEFAULT_MODEL:
        raise PlanUnavailable(f"Unsupported Claude plan model: {model}")
    started = time.monotonic()
    if timeout_s is None:
        await _CALL_SEMAPHORE.acquire()
    else:
        await asyncio.wait_for(_CALL_SEMAPHORE.acquire(), timeout=timeout_s)
    try:
        session = CALL_SESSION.get()
        if session is not None and session.rejected is not None:
            session.skipped += 1
            raise PlanProviderRejected("Claude calls stopped for this run: " + session.rejected.code,
                                       session.rejected.code, attempted=False)
        remaining = None if timeout_s is None else timeout_s - (time.monotonic() - started)
        if remaining is not None and remaining <= 0:
            raise PlanUnavailable("Claude call deadline exceeded while queued")
        try:
            return await _call_structured(
                system=system, user=user, response_model=response_model, json_schema=json_schema,
                label=label, model=model,
                log_path=log_path or DEFAULT_JUDGMENT_CALL_LOG_PATH, timeout_s=remaining,
            )
        except PlanProviderRejected as exc:
            if session is not None and session.rejected is None:
                session.rejected = exc
            raise
    finally:
        _CALL_SEMAPHORE.release()


async def check_health(*, log_path: Path | None = None, limits_sink: list | None = None) -> HealthResponse:
    """고정 입력으로 구독 연결만 확인한다. 외부 데이터·주문 입력은 받지 않는다."""
    return await _call_structured(
        system="You are a connectivity probe.", user='Return {"status":"ok"}.',
        response_model=HealthResponse, json_schema=_HEALTH_SCHEMA, label="health_check",
        model=DEFAULT_MODEL, log_path=log_path, limits_sink=limits_sink,
    )


async def read_capacity() -> CapacityStatus:
    """구독 한도는 응답에 딸려 오므로 고정 연결 확인 한 번으로 읽는다."""
    sink: list = []
    await check_health(limits_sink=sink)
    return capacity_from_limits(sink[-1] if sink else None)
