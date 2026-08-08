import json
import logging
import time
from typing import Any, TypeVar

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

_client = AsyncAnthropic(max_retries=3, timeout=30.0)

T = TypeVar("T", bound=BaseModel)


async def call_structured(
    system: str,
    user: str,
    response_model: type[T],
    json_schema: dict[str, Any],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1024,
    effort: str = DEFAULT_EFFORT,
) -> T:
    """구조화 출력을 받아 response_model로 검증한다.

    output_config.format으로 JSON 형태 자체는 API가 보장하지만, score/confidence의
    수치 범위 같은 의미적 제약은 pydantic이 검증한다. 그 검증에 실패했을 때만(=파싱
    실패와 동급) 재시도한다 — "점수가 마음에 안 들어서" 재시도하는 경로는 없다.
    """
    last_error: Exception | None = None

    for attempt in range(1, MAX_STRUCTURED_RETRIES + 1):
        start = time.monotonic()
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

        elapsed = time.monotonic() - start
        logger.info(
            "llm_call model=%s input_tokens=%d output_tokens=%d elapsed_s=%.2f stop_reason=%s",
            model,
            response.usage.input_tokens,
            response.usage.output_tokens,
            elapsed,
            response.stop_reason,
        )

        if response.stop_reason == "refusal":
            raise RuntimeError(f"LLM refused the request: {response.stop_reason}")

        try:
            text = next(b.text for b in response.content if b.type == "text")
            data = json.loads(text)
            return response_model.model_validate(data)
        except (StopIteration, json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            logger.warning("llm_call_validation_failed attempt=%d error=%s", attempt, exc)
            continue

    raise RuntimeError(
        f"LLM structured call failed after {MAX_STRUCTURED_RETRIES} attempts"
    ) from last_error
