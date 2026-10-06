"""영어 판단 원문은 보존하고 앱 알림·노션에 표시할 문장만 한국어로 번역한다."""

import hashlib
import logging
import re
from pathlib import Path

from pydantic import BaseModel, field_validator

from src import codex_plan

logger = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "translate.md"
DEFAULT_CACHE_DIR = Path("logs/translations")
TRANSLATION_TIMEOUT_S = 30.0


class _Translation(BaseModel):
    translated: str

    @field_validator("translated")
    @classmethod
    def require_korean(cls, value: str) -> str:
        if not re.search(r"[가-힣]", value):
            raise ValueError("translation must contain Korean text")
        return value.strip()


_TRANSLATION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"translated": {"type": "string"}},
    "required": ["translated"],
    "additionalProperties": False,
}


async def to_korean(text: str | None, label: str = "translate") -> str | None:
    """번역 실패는 원문으로 폴백한다. 성공한 번역만 프롬프트+원문별로 재사용한다.

    번역을 위해 분석·판단을 다시 실행하지 않는다. Claude API는 계속 꺼 둔다.
    """
    if not text or not re.search(r"[A-Za-z]", text):
        return text
    try:
        system = PROMPT_PATH.read_text()
        key = hashlib.sha256((system + "\0" + text).encode()).hexdigest()
        cache_path = DEFAULT_CACHE_DIR / f"{key}.json"
        try:
            return _Translation.model_validate_json(cache_path.read_text()).translated
        except (OSError, ValueError):
            pass
        result = await codex_plan.call_structured(
            system=system,
            user=text,
            response_model=_Translation,
            json_schema=_TRANSLATION_RESPONSE_SCHEMA,
            label=label,
            timeout_s=TRANSLATION_TIMEOUT_S,
        )
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            # 쓰기 중 읽힌 불완전 JSON은 위에서 cache miss로 처리한다.
            cache_path.write_text(result.model_dump_json())
        except OSError:
            logger.warning("translation_cache_write_failed label=%s", label, exc_info=True)
        return result.translated
    except Exception:
        logger.exception("translate_to_korean_failed label=%s", label)
        return text
