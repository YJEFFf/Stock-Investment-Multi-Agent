import asyncio
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src import llm
from src.schemas import AnalystOpinion, DebateArgument, Decision

BULL_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "debate_bull.md"
BEAR_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "debate_bear.md"
MANAGER_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "portfolio_manager.md"


def _prompt_version(template: str) -> str:
    return hashlib.sha256(template.encode()).hexdigest()[:6]


def _format_opinions(opinions: list[AnalystOpinion]) -> str:
    return "\n".join(f"- {o.agent}: score={o.score:.3f}, confidence={o.confidence:.3f}" for o in opinions)


class _DebateResponse(BaseModel):
    argument: str
    strength: float = Field(ge=0.0, le=1.0)


_DEBATE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "argument": {"type": "string"},
        "strength": {"type": "number"},
    },
    "required": ["argument", "strength"],
    "additionalProperties": False,
}


async def _run_debate_side(
    prompt_path: Path, stance: Literal["bull", "bear"], ticker: str, opinions: list[AnalystOpinion]
) -> DebateArgument:
    template = prompt_path.read_text()
    version = _prompt_version(template)
    prompt = template.format(ticker=ticker, opinions=_format_opinions(opinions))

    result = await llm.call_structured(
        system="",
        user=prompt,
        response_model=_DebateResponse,
        json_schema=_DEBATE_RESPONSE_SCHEMA,
    )

    return DebateArgument(
        stance=stance,
        ticker=ticker,
        argument=result.argument,
        strength=result.strength,
        evidence=[f"prompt:debate_{stance}@{version}"],
    )


async def debate(ticker: str, opinions: list[AnalystOpinion]) -> tuple[DebateArgument, DebateArgument]:
    """강세/약세 논거를 독립적으로 생성한다 — 서로의 출력을 보지 않고 같은 의견
    집합에서 각자 최선의 논거를 만든다 (docs/PLAN.md §2, LLM 동조 편향 방지)."""
    bull, bear = await asyncio.gather(
        _run_debate_side(BULL_PROMPT_PATH, "bull", ticker, opinions),
        _run_debate_side(BEAR_PROMPT_PATH, "bear", ticker, opinions),
    )
    return bull, bear


class _ManagerResponse(BaseModel):
    action: Literal["BUY", "HOLD"]
    reasoning: str


_MANAGER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "HOLD"]},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "reasoning"],
    "additionalProperties": False,
}


async def portfolio_manager(
    ticker: str,
    opinions: list[AnalystOpinion],
    bull: DebateArgument,
    bear: DebateArgument,
    degraded: bool,
) -> Decision:
    """분석가 의견과 양쪽 논거를 종합해 최종 판단을 내린다.

    이 판단은 추천일 뿐이다 — 최종 승인권은 리스크 게이트(코드)에 있다 (규칙 6).
    매니저의 BUY는 게이트가 거부할 수 있고, HOLD는 그대로 확정(매수 없음)된다.
    """
    template = MANAGER_PROMPT_PATH.read_text()
    version = _prompt_version(template)
    prompt = template.format(
        ticker=ticker,
        degraded="예" if degraded else "아니오",
        opinions=_format_opinions(opinions),
        bull_strength=bull.strength,
        bull_argument=bull.argument,
        bear_strength=bear.strength,
        bear_argument=bear.argument,
    )

    result = await llm.call_structured(
        system="",
        user=prompt,
        response_model=_ManagerResponse,
        json_schema=_MANAGER_RESPONSE_SCHEMA,
    )

    return Decision(
        ticker=ticker,
        action=result.action,
        reason=result.reasoning,
        inputs=opinions,
        degraded=degraded,
        debate=[bull, bear],
        evidence=[f"prompt:manager@{version}"],
    )


async def judge(opinions: list[AnalystOpinion], total_expected_analysts: int) -> Decision | None:
    """propose_decision()의 임시 문턱 로직을 대체하는 실제 판단 계층.

    강세/약세 토론 후 포트폴리오 매니저가 종합한다. 의견이 하나도 없으면 토론 자체를
    시작하지 않고 곧바로 None을 반환한다 — "판단 불가"와 "판단했으나 기각"은 다른
    상태다 (스키마 계약).
    """
    if not opinions:
        return None

    ticker = opinions[0].ticker
    degraded = len(opinions) < total_expected_analysts

    bull, bear = await debate(ticker, opinions)
    return await portfolio_manager(ticker, opinions, bull, bear, degraded)
