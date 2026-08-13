"""판단 로그(LLM이 영어로 만든 사유 문장)를 사람에게 보여줄 때만 한국어로
옮긴다. 분석가·토론·매니저 프롬프트는 그대로 영어로 둔다(사용자 확정,
2026-08-13) — 번역은 텔레그램 알림·노션 일지 같은 "사람이 읽는 표면"에서만
일어나고, logs/*.jsonl에 남는 원문이나 Decision/AnalystOpinion 등 계약
스키마에는 전혀 개입하지 않는다. 원문을 그대로 남겨야 나중에 프롬프트
버전별로 원인을 추적할 수 있다(evidence의 prompt 버전 태그와 같은 이유).

번역 자체는 투자 판단이 아니므로 실패해도 원문을 그대로 보여주고 넘어간다
(리스크 게이트·매수/매도 로직에 영향 없음 — 노션 동기화 실패를 삼키는
기존 패턴과 같다)."""

import logging

from pydantic import BaseModel

from src import llm

logger = logging.getLogger(__name__)

TRANSLATE_SYSTEM = (
    "You translate short English investment-analysis text into natural, concise Korean for a "
    "retail investor reading a trade journal. Preserve the meaning and tone exactly — do not "
    "add, remove, hedge, or embellish any claim. Keep ticker names, numbers, and financial terms "
    "precise. Return only the Korean translation, nothing else."
)


class _Translation(BaseModel):
    translated: str


_TRANSLATION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"translated": {"type": "string"}},
    "required": ["translated"],
    "additionalProperties": False,
}


async def to_korean(text: str | None, label: str = "translate") -> str | None:
    """text가 비어있으면 그대로 반환한다. 번역 호출이 실패하면(재시도 소진 등)
    원문을 그대로 반환한다 — 화면에 영어가 한 번 보이는 것이 알림/일지 작성
    자체를 막는 것보다 낫다."""
    if not text:
        return text

    try:
        result = await llm.call_structured(
            system=TRANSLATE_SYSTEM,
            user=text,
            response_model=_Translation,
            json_schema=_TRANSLATION_RESPONSE_SCHEMA,
            max_tokens=2048,
            label=label,
        )
        return result.translated
    except Exception:
        logger.exception("translate_to_korean_failed label=%s", label)
        return text
