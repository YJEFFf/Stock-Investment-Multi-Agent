"""ChatGPT 구독에 포함된 Codex를 비대화형 구조화 출력으로 호출한다.

OpenAI API 키를 쓰지 않는다는 사실이 이 모듈의 핵심 계약이다. 실행 환경에서
OPENAI_API_KEY/CODEX_API_KEY를 제거하고, 저장된 Codex 로그인이 ChatGPT 계정인지
매 호출 전에 확인한다. 연결 점검과 매수 판단이 이 모듈을 공유하지만 Codex 프로세스는
운영 저장소를 읽지 못하며, 매수 판단 호출은 기존 LLM 감시 로그에 함께 남긴다.
"""

import asyncio
import json
import logging
import math
import os
import queue
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_CALL_LOG_PATH = Path("logs/codex_plan_calls.jsonl")
DEFAULT_JUDGMENT_CALL_LOG_PATH = Path("logs/llm_calls.jsonl")
DEFAULT_CAPACITY_STATE_PATH = Path("logs/codex_plan_capacity.json")
MAX_CONCURRENT_CALLS = 8
CODEX_OS_USER = "sima-codex"
CODEX_EXECUTABLE = "/home/sima-codex/.local/bin/codex"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
KST = ZoneInfo("Asia/Seoul")
T = TypeVar("T", bound=BaseModel)

# 2026-09-15 전체 일일 섀도 실측(input 3,645,441 + output 38,227)이 Codex의
# 7일 한도 표시를 약 2%p 사용했다. 절대 토큰 한도는 제품이 공개하지 않으므로,
# 아래 값은 현재와 같은 모델·프롬프트 부하를 몇 번 더 돌릴 수 있는지 환산할 때만 쓴다.
FULL_DAILY_REFERENCE_TOKENS = 3_683_668
FULL_DAILY_REFERENCE_PERCENT_POINTS = 2.0
BUY_CAPACITY_FLOOR_PERCENT = 2.0

_CALL_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_CALLS)


class CodexPlanUnavailable(RuntimeError):
    """ChatGPT 로그인·CLI·플랜 호출 중 하나를 사용할 수 없을 때."""


class HealthResponse(BaseModel):
    status: Literal["ok"]


class CapacityStatus(BaseModel):
    checked_at: datetime
    day: str
    used_percent: float = Field(ge=0.0, le=100.0, allow_inf_nan=False)
    remaining_percent: float = Field(ge=0.0, le=100.0, allow_inf_nan=False)
    window_duration_mins: Literal[10_080]
    resets_at: datetime
    ordinary_usage_allowed: bool
    buy_judgment_allowed: bool
    threshold_percent: Literal[2.0] = BUY_CAPACITY_FLOOR_PERCENT
    estimated_daily_runs_remaining: float = Field(ge=0.0, allow_inf_nan=False)
    estimated_token_equivalent_remaining: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_derived_fields(self):
        expected_remaining = round(100.0 - self.used_percent, 2)
        if self.remaining_percent != expected_remaining:
            raise ValueError("remaining_percent does not match used_percent")
        expected_allowed = self.ordinary_usage_allowed and self.remaining_percent > self.threshold_percent
        if self.buy_judgment_allowed != expected_allowed:
            raise ValueError("buy_judgment_allowed does not match the capacity floor")
        expected_runs = round(self.remaining_percent / FULL_DAILY_REFERENCE_PERCENT_POINTS, 1)
        if self.estimated_daily_runs_remaining != expected_runs:
            raise ValueError("estimated_daily_runs_remaining does not match remaining_percent")
        expected_tokens = round(
            self.remaining_percent
            / FULL_DAILY_REFERENCE_PERCENT_POINTS
            * FULL_DAILY_REFERENCE_TOKENS
        )
        if self.estimated_token_equivalent_remaining != expected_tokens:
            raise ValueError("estimated_token_equivalent_remaining does not match remaining_percent")
        return self


_HEALTH_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}},
    "required": ["status"],
    "additionalProperties": False,
}


def _subscription_environment() -> dict[str, str]:
    """Codex 실행에 필요한 경로만 넘기고 운영 자격증명은 전부 격리한다."""
    env = {"PATH": os.environ.get("PATH", os.defpath)}
    for key in ("LANG", "LC_ALL"):
        if value := os.environ.get(key):
            env[key] = value
    return env


def _codex_command(*args: str) -> list[str]:
    """운영 비밀값을 읽을 수 없는 전용 OS 계정에서만 Codex를 실행한다."""
    return ["/usr/bin/sudo", "-n", "-H", "-u", CODEX_OS_USER, CODEX_EXECUTABLE, *args]


def _run(command: list[str], *, env: dict[str, str], input_text: str | None = None, cwd: str | None = None):
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=DEFAULT_TIMEOUT_S,
        check=False,
        env=env,
        cwd=cwd,
    )


def _ensure_chatgpt_login(env: dict[str, str]) -> None:
    try:
        result = _run(_codex_command("login", "status"), env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise CodexPlanUnavailable(f"Codex CLI unavailable: {exc}") from exc
    status = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or "Logged in using ChatGPT" not in status:
        raise CodexPlanUnavailable("Codex CLI is not logged in using ChatGPT")


def _ensure_filesystem_isolation(env: dict[str, str]) -> None:
    """전용 계정이 운영 저장소를 탐색할 수 있으면 호출을 거부한다."""
    command = [
        "/usr/bin/sudo", "-n", "-u", CODEX_OS_USER,
        "/usr/bin/test", "!", "-x", str(PROJECT_ROOT),
    ]
    try:
        result = _run(command, env=env)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise CodexPlanUnavailable(f"Codex isolation check unavailable: {exc}") from exc
    if result.returncode != 0:
        raise CodexPlanUnavailable("Codex OS user can access the production repository")


def _app_server_response(
    process: subprocess.Popen, responses: queue.Queue, request: dict, request_id: int
) -> dict:
    """app-server JSONL 응답 하나를 읽는다. 계정 ID·인증 토큰은 저장하지 않는다."""
    assert process.stdin is not None
    process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()
    deadline = time.monotonic() + DEFAULT_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            line = responses.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty as exc:
            raise CodexPlanUnavailable(f"Codex app-server response timed out for request {request_id}") from exc
        if line is None:
            break
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if message.get("id") != request_id:
            continue
        if message.get("error"):
            raise CodexPlanUnavailable(f"Codex app-server request failed: {message['error']}")
        if isinstance(message.get("result"), dict):
            return message["result"]
        break
    raise CodexPlanUnavailable(f"Codex app-server response missing for request {request_id}")


def _read_capacity_sync() -> CapacityStatus:
    """ChatGPT 계정의 일반 Codex 7일 한도를 조회한다(LLM 추론 호출 없음)."""
    env = _subscription_environment()
    _ensure_filesystem_isolation(env)
    _ensure_chatgpt_login(env)
    try:
        process = subprocess.Popen(
            _codex_command("app-server", "--stdio"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except OSError as exc:
        raise CodexPlanUnavailable(f"Codex app-server unavailable: {exc}") from exc
    responses: queue.Queue[str | None] = queue.Queue()

    def _read_stdout() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            responses.put(line)
        responses.put(None)

    threading.Thread(target=_read_stdout, daemon=True, name="codex-capacity-reader").start()
    try:
        _app_server_response(
            process,
            responses,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "sima-capacity-check", "version": "1.0"},
                    "capabilities": {},
                },
            },
            1,
        )
        result = _app_server_response(
            process,
            responses,
            {
                "id": 2,
                "method": "account/rateLimits/read",
                "params": {"excludeResetCreditDetails": True},
            },
            2,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    rate_limits = (result.get("rateLimitsByLimitId") or {}).get("codex") or result.get("rateLimits")
    primary = rate_limits.get("primary") if isinstance(rate_limits, dict) else None
    if not isinstance(primary, dict) or primary.get("windowDurationMins") != 10_080:
        raise CodexPlanUnavailable("Codex weekly rate-limit window is unavailable")
    try:
        used = float(primary["usedPercent"])
        if not math.isfinite(used) or not 0.0 <= used <= 100.0:
            raise ValueError(f"usedPercent is outside 0..100: {used!r}")
        reset_at = datetime.fromtimestamp(int(primary["resetsAt"]), timezone.utc)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise CodexPlanUnavailable(f"Codex weekly rate-limit payload is invalid: {exc}") from exc
    remaining = round(100.0 - used, 2)
    ordinary_allowed = result.get("ordinaryUsageAllowed") is True
    runs = remaining / FULL_DAILY_REFERENCE_PERCENT_POINTS
    return CapacityStatus(
        checked_at=datetime.now(timezone.utc),
        day=datetime.now(KST).date().isoformat(),
        used_percent=used,
        remaining_percent=remaining,
        window_duration_mins=10_080,
        resets_at=reset_at,
        ordinary_usage_allowed=ordinary_allowed,
        buy_judgment_allowed=ordinary_allowed and remaining > BUY_CAPACITY_FLOOR_PERCENT,
        estimated_daily_runs_remaining=round(runs, 1),
        estimated_token_equivalent_remaining=round(runs * FULL_DAILY_REFERENCE_TOKENS),
    )


def save_capacity_status(status: CapacityStatus, path: Path | None = None) -> None:
    """08:30 판단이 부분 파일을 읽지 않도록 원자적으로 교체한다."""
    path = path or DEFAULT_CAPACITY_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(status.model_dump_json(indent=2))
    temp_path.replace(path)


def load_capacity_status(day: str, path: Path | None = None) -> CapacityStatus:
    """당일 08:05 상태만 허용한다. 없거나 낡거나 손상되면 매수 판단을 실패 폐쇄한다."""
    path = path or DEFAULT_CAPACITY_STATE_PATH
    try:
        status = CapacityStatus.model_validate_json(path.read_text())
    except (OSError, ValidationError, ValueError) as exc:
        raise CodexPlanUnavailable(f"Codex capacity state unavailable: {exc}") from exc
    if status.day != day:
        raise CodexPlanUnavailable(f"Codex capacity state is stale: checked_day={status.day} today={day}")
    return status


async def read_capacity() -> CapacityStatus:
    return await asyncio.to_thread(_read_capacity_sync)


def _usage_from_events(stdout: str) -> dict[str, int]:
    usage: dict[str, int] = {}
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = {
                key: int(event["usage"].get(key, 0))
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                )
            }
    return usage


def _append_log(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _call_sync(
    system: str,
    user: str,
    response_model: type[T],
    json_schema: dict[str, Any],
    model: str,
) -> tuple[T, dict[str, int]]:
    env = _subscription_environment()
    _ensure_filesystem_isolation(env)
    _ensure_chatgpt_login(env)
    prompt = (
        "Do not inspect files, run commands, or use tools. The input below is complete. "
        "Return only the JSON object required by the supplied output schema.\n\n"
        f"<system>\n{system}\n</system>\n\n<user>\n{user}\n</user>"
    )
    with tempfile.TemporaryDirectory(prefix="sima-codex-plan-") as tmp:
        tmp_path = Path(tmp)
        # 전용 계정은 이 임시 디렉터리 안에서만 스키마를 읽고 결과를 쓴다.
        # 임의 경로를 나열할 수 없게 other에는 write+execute만 준다.
        tmp_path.chmod(0o733)
        schema_path = tmp_path / "schema.json"
        output_path = tmp_path / "result.json"
        schema_path.write_text(json.dumps(json_schema, ensure_ascii=False))
        schema_path.chmod(0o644)
        command = _codex_command(
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            model,
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-C",
            tmp,
            "-",
        )
        try:
            result = _run(command, env=env, input_text=prompt, cwd=tmp)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise CodexPlanUnavailable(f"Codex execution unavailable: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout)[-1000:].strip()
            raise CodexPlanUnavailable(f"Codex execution failed ({result.returncode}): {detail}")
        try:
            parsed = json.loads(output_path.read_text())
            validated = response_model.model_validate(parsed)
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            raise CodexPlanUnavailable(f"Codex structured output invalid: {exc}") from exc
        return validated, _usage_from_events(result.stdout)


async def _call_structured(
    *, system: str, user: str, response_model: type[T], json_schema: dict[str, Any],
    label: str, model: str, log_path: Path | None,
) -> T:
    path = log_path or DEFAULT_CALL_LOG_PATH
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    base = {
        "timestamp": now.isoformat(),
        "day": now.astimezone(KST).date().isoformat(),
        "label": label,
        "model": model,
    }
    try:
        result, usage = await asyncio.to_thread(_call_sync, system, user, response_model, json_schema, model)
    except asyncio.CancelledError:
        # asyncio.to_thread의 바깥 대기는 취소돼도 이미 시작한 subprocess 스레드는 끝까지
        # 돌 수 있다. 실제 usage는 회수할 수 없지만 호출/실패 자체를 감시에서 잃지 않는다.
        _append_log(
            path,
            {
                **base,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "elapsed_s": round(time.monotonic() - started, 2),
                "success": False,
                "error": "cancelled",
            },
        )
        raise
    except Exception as exc:
        _append_log(
            path,
            {**base, "elapsed_s": round(time.monotonic() - started, 2), "success": False, "error": repr(exc)},
        )
        raise
    _append_log(
        path,
        {**base, **usage, "elapsed_s": round(time.monotonic() - started, 2), "success": True},
    )
    logger.info("codex_plan_call label=%s model=%s usage=%s", label, model, usage)
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
) -> T:
    """기존 ``llm.call_structured``와 같은 형태로 매수 판단에서 쓰는 진입점.

    ``max_tokens``와 ``effort``는 호환 목적으로 받는다. 모델명이 다르면 조용히 다른
    모델로 바꾸지 않고 실패시킨다. 파이프라인이 종목을 병렬 처리할 때 CLI 프로세스가
    한꺼번에 수백 개 뜨지 않도록 동시 실행도 제한한다.
    """
    del max_tokens, effort
    if model != DEFAULT_MODEL:
        raise CodexPlanUnavailable(f"Unsupported Codex plan model: {model}")
    async with _CALL_SEMAPHORE:
        return await _call_structured(
            system=system,
            user=user,
            response_model=response_model,
            json_schema=json_schema,
            label=label,
            model=model,
            log_path=log_path or DEFAULT_JUDGMENT_CALL_LOG_PATH,
        )


async def check_health(*, log_path: Path | None = None) -> HealthResponse:
    """고정 입력으로 ChatGPT 플랜 연결만 확인한다. 외부 데이터·주문 입력은 받지 않는다."""
    return await _call_structured(
        system="You are a connectivity probe.",
        user='Return {"status":"ok"}.',
        response_model=HealthResponse,
        json_schema=_HEALTH_SCHEMA,
        label="health_check",
        model=DEFAULT_MODEL,
        log_path=log_path,
    )
