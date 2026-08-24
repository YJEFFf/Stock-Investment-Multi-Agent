import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import anthropic
from anthropic import AsyncAnthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5"  # 종목당 독립 호출로 하루 수백 회 불리는 구조라 비용 민감 (docs/PLAN.md §2)
DEFAULT_EFFORT = "low"  # 채점형 판단이라 깊은 추론까지는 불필요 — 필요시 상향 튜닝

# 스키마 검증/파싱 실패에만 재시도 (규칙 4). SDK 자체가 네트워크·429·5xx는 이미
# max_retries만큼 재시도하므로, 여기서는 "응답은 왔는데 우리 스키마와 안 맞는" 경우만 다룬다.
MAX_STRUCTURED_RETRIES = 2

# 분석가 응답의 정상 길이는 120~190토큰인데, `reasoning`이 유일하게 길이 제한 없는
# 필드라 이따금 1024를 꽉 채우고 잘렸다(1638콜 중 22콜, 2026-08-24 CHANGELOG).
# 여유를 준다. max_tokens는 모델에게 보이지 않으므로 이 값을 올려도 채점은 안 바뀐다
# — 프롬프트를 건드리는 쪽은 점수를 움직인다는 걸 A/B로 확인했다(같은 항목).
_DEFAULT_MAX_TOKENS = 2048

# CLAUDE.md "감시 지표" — 분석가별(label) 호출수·실패율·토큰사용량 집계용 원본 로그.
# pipeline.py의 DEFAULT_LOG_PATH/DEFAULT_SELL_LOG_PATH와 같은 패턴(호출부에서 override 가능).
DEFAULT_LLM_CALL_LOG_PATH = Path("logs/llm_calls.jsonl")

KST = ZoneInfo("Asia/Seoul")

_client = AsyncAnthropic(max_retries=3, timeout=30.0)

T = TypeVar("T", bound=BaseModel)


def _append_call_log(log_path: Path, entry: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _call_log_entry(label: str, model: str, **fields) -> dict:
    """호출 로그 한 줄. timestamp(UTC)와 day(KST)를 **둘 다** 남긴다.

    timestamp만으로는 일별 집계가 하루씩 밀린다: 매수 판단 cron은 08:30 KST인데
    그건 UTC로 전날 23:30이라, 하루 호출의 대부분(분석가·토론·매니저 수백 건)이
    전날 몫으로 잡히고 그날엔 15:35 KST 매도 판단분만 남는다. 2026-08-20이
    실제로 그랬다 — 파일상 35건, 실제로는 그 10배 이상. CLAUDE.md 감시 지표의
    "분석가별 호출 수·토큰 사용량"이 그대로 틀린 숫자가 된다.
    pipeline.jsonl이 같은 이유로 이미 KST를 쓴다(2026-08-13, 커밋 832fb8b) —
    "하루"의 경계는 장이 도는 시간대 기준으로 통일한다. timestamp를 KST로
    바꾸지 않고 day를 더하는 이유는 기존 줄과의 비교 가능성을 깨지 않기 위해서다.
    """
    now = datetime.now(timezone.utc)
    return {
        "timestamp": now.isoformat(),
        "day": now.astimezone(KST).date().isoformat(),
        "label": label,
        "model": model,
        **fields,
    }


async def call_structured(
    system: str,
    user: str,
    response_model: type[T],
    json_schema: dict[str, Any],
    model: str = DEFAULT_MODEL,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,
    label: str = "unknown",
    log_path: Path = DEFAULT_LLM_CALL_LOG_PATH,
) -> T:
    """구조화 출력을 받아 response_model로 검증한다.

    output_config.format으로 JSON 형태 자체는 API가 보장하지만, score/confidence의
    수치 범위 같은 의미적 제약은 pydantic이 검증한다. 그 검증에 실패했을 때만(=파싱
    실패와 동급) 재시도한다 — "점수가 마음에 안 들어서" 재시도하는 경로는 없다.
    `stop_reason=max_tokens`(응답 절단)도 같은 등급으로 재시도하되, 파싱 실패와는
    다른 로그(`llm_call_truncated`)를 남긴다.

    `label`은 이 호출이 어떤 분석가/판단 단계에서 왔는지("chart", "news",
    "debate_bull", "portfolio_manager_buy" 등) 식별하는 태그다 — 이 함수는 모든
    분석가·토론·매니저가 공유하는 통로라 여기서 남기지 않으면 나중에 호출별
    분석가 귀속이 불가능해진다.
    """
    last_error: Exception | None = None
    total_input_tokens = 0
    total_output_tokens = 0
    start = time.monotonic()

    for attempt in range(1, MAX_STRUCTURED_RETRIES + 1):
        try:
            response = await _client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "format": {"type": "json_schema", "schema": json_schema},
                    "effort": effort,
                },
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            last_error = exc
            logger.warning("llm_call_transport_failed attempt=%d error=%s", attempt, exc)
            continue

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens
        logger.info(
            "llm_call label=%s model=%s input_tokens=%d output_tokens=%d stop_reason=%s",
            label,
            model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            response.stop_reason,
        )

        if response.stop_reason == "refusal":
            _append_call_log(
                log_path,
                _call_log_entry(
                    label,
                    model,
                    attempts=attempt,
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                    elapsed_s=round(time.monotonic() - start, 2),
                    success=False,
                    error="refusal",
                ),
            )
            raise RuntimeError(f"LLM refused the request: {response.stop_reason}")

        # 잘린 응답은 스키마 위반이 아니라 절단이다. 여기서 안 걸러내면 아래
        # json.loads가 "Unterminated string"을 내고, 진짜 스키마 위반과 로그에서
        # 구분이 안 된다 — 8/24까지 매일 나오던 그 로그다.
        if response.stop_reason == "max_tokens":
            last_error = RuntimeError(f"response truncated at max_tokens={max_tokens}")
            logger.warning(
                "llm_call_truncated label=%s attempt=%d max_tokens=%d output_tokens=%d",
                label,
                attempt,
                max_tokens,
                response.usage.output_tokens,
            )
            continue

        try:
            text = next(b.text for b in response.content if b.type == "text")
            data = json.loads(text)
            result = response_model.model_validate(data)
        except (StopIteration, json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            logger.warning("llm_call_validation_failed attempt=%d error=%s", attempt, exc)
            continue

        _append_call_log(
            log_path,
            _call_log_entry(
                label,
                model,
                attempts=attempt,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                elapsed_s=round(time.monotonic() - start, 2),
                success=True,
            ),
        )
        return result

    _append_call_log(
        log_path,
        _call_log_entry(
            label,
            model,
            attempts=MAX_STRUCTURED_RETRIES,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            elapsed_s=round(time.monotonic() - start, 2),
            success=False,
            error=repr(last_error),
        ),
    )
    raise RuntimeError(
        f"LLM structured call failed after {MAX_STRUCTURED_RETRIES} attempts"
    ) from last_error
