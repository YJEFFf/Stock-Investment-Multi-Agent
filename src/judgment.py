import asyncio
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src import llm
from src.schemas import AnalystOpinion, DebateArgument, Decision, SellAction

BULL_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "debate_bull.md"
BEAR_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "debate_bear.md"
MANAGER_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "portfolio_manager.md"

STAY_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "debate_stay.md"
EXIT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "debate_exit.md"
SELL_MANAGER_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "portfolio_manager_sell.md"


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
    prompt_path: Path,
    stance: Literal["bull", "bear"],
    ticker: str,
    opinions: list[AnalystOpinion],
    **extra_format_fields: str,
) -> DebateArgument:
    template = prompt_path.read_text()
    version = _prompt_version(template)
    prompt = template.format(ticker=ticker, opinions=_format_opinions(opinions), **extra_format_fields)

    result = await llm.call_structured(
        system="",
        user=prompt,
        response_model=_DebateResponse,
        json_schema=_DEBATE_RESPONSE_SCHEMA,
        label=prompt_path.stem,
    )

    # evidence는 stance가 아니라 실제 쓰인 프롬프트 파일명 기준 — 매수용
    # bull/bear와 보유 재평가용 stay/exit가 같은 stance("bull"/"bear")를
    # 공유하므로, stance만으로는 어떤 템플릿이 실제로 쓰였는지 구분이 안 된다.
    evidence_tag = prompt_path.stem.removeprefix("debate_")
    return DebateArgument(
        stance=stance,
        ticker=ticker,
        argument=result.argument,
        strength=result.strength,
        evidence=[f"prompt:debate_{evidence_tag}@{version}"],
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
        label="portfolio_manager_buy",
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


async def debate_holding(
    ticker: str, opinions: list[AnalystOpinion], unrealized_pct: float
) -> tuple[DebateArgument, DebateArgument]:
    """보유 종목 재평가용 존버/이탈 논거. debate()와 같은 원리(독립 생성으로 동조
    편향 방지, docs/PLAN.md §2)지만 "매수할까"가 아니라 "계속 들고 있을까"를
    놓고 논쟁한다 — 이미 보유 중인 포지션 재평가라 매수용 프롬프트를 그대로
    쓰면 LLM이 엉뚱한 질문(신규 매수 여부)에 답하게 된다.

    `DebateArgument.stance`는 그대로 bull=존버/bear=이탈로 재사용한다 — 새
    stance 값을 스키마에 추가할 필요가 없다(이미 자유 설계 가능한 신규 타입).
    """
    stay, exit_case = await asyncio.gather(
        _run_debate_side(STAY_PROMPT_PATH, "bull", ticker, opinions, unrealized_pct=f"{unrealized_pct:+.1%}"),
        _run_debate_side(EXIT_PROMPT_PATH, "bear", ticker, opinions, unrealized_pct=f"{unrealized_pct:+.1%}"),
    )
    return stay, exit_case


class _SellManagerResponse(BaseModel):
    action: Literal["SELL", "HOLD"]
    reasoning: str


_SELL_MANAGER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["SELL", "HOLD"]},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "reasoning"],
    "additionalProperties": False,
}


async def portfolio_manager_sell(
    ticker: str,
    opinions: list[AnalystOpinion],
    stay: DebateArgument,
    exit_case: DebateArgument,
    unrealized_pct: float,
) -> SellAction | None:
    """보유 종목 재평가 — portfolio_manager(매수용)와 대칭이지만 Decision이 아니라
    SellAction을 반환한다("매도는 별도 경로", Decision.action 주석). 이 판단은
    게이트를 거치지 않는다 — 포지션을 줄이는 행위 자체가 위험을 낮추는 방향이라
    한도 초과 개념이 성립하지 않는다(src/sell.py의 결정론적 매도와 같은 근거).
    그래서 매수용 portfolio_manager와 달리 여기선 LLM의 SELL이 그대로 실행된다
    (프롬프트에도 이 비대칭을 명시해뒀다). HOLD면 "매도 안 함"을 None으로 표현한다.
    """
    template = SELL_MANAGER_PROMPT_PATH.read_text()
    version = _prompt_version(template)
    prompt = template.format(
        ticker=ticker,
        unrealized_pct=f"{unrealized_pct:+.1%}",
        opinions=_format_opinions(opinions),
        stay_strength=stay.strength,
        stay_argument=stay.argument,
        exit_strength=exit_case.strength,
        exit_argument=exit_case.argument,
    )

    result = await llm.call_structured(
        system="",
        user=prompt,
        response_model=_SellManagerResponse,
        json_schema=_SELL_MANAGER_RESPONSE_SCHEMA,
        label="portfolio_manager_sell",
    )

    if result.action == "HOLD":
        return None
    return SellAction(ticker=ticker, reason="llm_discretionary", sell_fraction=1.0, reasoning=result.reasoning)


async def judge_sell(ticker: str, opinions: list[AnalystOpinion], unrealized_pct: float) -> SellAction | None:
    """evaluate_holdings가 기대하는 형태. 의견이 하나도 없으면(분석 실패) 판단
    자체를 안 한다 — judge()와 같은 패턴("판단 불가" != "판단했으나 기각")."""
    if not opinions:
        return None

    stay, exit_case = await debate_holding(ticker, opinions, unrealized_pct)
    return await portfolio_manager_sell(ticker, opinions, stay, exit_case, unrealized_pct)
