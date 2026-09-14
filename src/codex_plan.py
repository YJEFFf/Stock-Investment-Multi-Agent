"""ChatGPT 구독에 포함된 Codex를 비대화형 구조화 출력으로 호출한다.

OpenAI API 키를 쓰지 않는다는 사실이 이 모듈의 핵심 계약이다. 실행 환경에서
OPENAI_API_KEY/CODEX_API_KEY를 제거하고, 저장된 Codex 로그인이 ChatGPT 계정인지
매 호출 전에 확인한다. 현재는 주문과 분리된 연결 상태 점검에만 사용한다.
"""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_CALL_LOG_PATH = Path("logs/codex_plan_calls.jsonl")
CODEX_OS_USER = "sima-codex"
CODEX_EXECUTABLE = "/home/sima-codex/.local/bin/codex"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
KST = ZoneInfo("Asia/Seoul")
T = TypeVar("T", bound=BaseModel)


class CodexPlanUnavailable(RuntimeError):
    """ChatGPT 로그인·CLI·플랜 호출 중 하나를 사용할 수 없을 때."""


class HealthResponse(BaseModel):
    status: Literal["ok"]


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
