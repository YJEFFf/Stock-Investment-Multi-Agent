import asyncio
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src import llm
from src.schemas import AnalystOpinion, DebateArgument, Decision, ExitPlan, SellAction

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


# 출구 규칙 바운드 (사용자 확정, 2026-08-15). LLM은 고정값(-10%/1-3/-7%)보다 조이는
# 쪽으로도 느슨한 쪽으로도 갈 수 있다 — 대신 진입 시점에 한 번만 정하고 보유 중에는
# 재결정하지 않는다(ExitPlan docstring). 이 바운드는 "적정 수준" 판단이 아니라
# 파싱 사고 방지선이다: 손절 3% 미만은 KOSPI 대형주 일간 노이즈에 그냥 털리고,
# 15% 초과는 종목당 비중 한도 15%와 곱해도 일일 손실 한도(-5%) 안에 들어야 한다.
_MIN_STOP_LOSS_PCT = 3.0
_MAX_STOP_LOSS_PCT = 15.0
_MIN_TAKE_PROFIT_FRACTION = 0.15
_MAX_TAKE_PROFIT_FRACTION = 0.60  # 1.0 미만이어야 (1-f)^n으로 항상 잔량이 남는다
_MIN_TRAIL_PCT = 3.0
_MAX_TRAIL_PCT = 12.0

# 익절선은 LLM이 내지 않는다 — 손절폭 한 숫자만 받아서 코드가 2배로 계산한다.
# LLM이 자유롭게 정하는 숫자 개수를 최소화하기 위해서고, 2:1 손익비를 코드가
# 강제하기 위해서다(사용자 확정).
_REWARD_RISK_RATIO = 2.0


class _ManagerResponse(BaseModel):
    action: Literal["BUY", "HOLD"]
    reasoning: str
    # 아래 셋은 퍼센트 "크기"(양수)로 받는다 — 부호는 코드가 붙인다. 모델이 음수
    # 부호를 흘리는 실수를 아예 못 하게 하려는 것.
    stop_loss_pct: float
    take_profit_fraction: float
    trail_pct: float


_MANAGER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "HOLD"]},
        "reasoning": {"type": "string"},
        # number에 minimum/maximum을 넣으면 API가 400을 낸다("For 'number' type,
        # properties maximum, minimum are not supported", 2026-08-15 실호출로 확인).
        # 범위는 프롬프트 문구로 알려주고, 실제 강제는 _exit_plan_from의 클램핑이
        # 한다 — 어차피 구조화 출력이 범위를 지킨다고 믿으면 안 되는 자리였다.
        "stop_loss_pct": {"type": "number"},
        "take_profit_fraction": {"type": "number"},
        "trail_pct": {"type": "number"},
    },
    "required": ["action", "reasoning", "stop_loss_pct", "take_profit_fraction", "trail_pct"],
    "additionalProperties": False,
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _exit_plan_from(result: _ManagerResponse) -> ExitPlan:
    """LLM이 낸 숫자를 ExitPlan으로 만든다. 스키마에 min/max를 이미 넣었지만 여기서
    한 번 더 자른다 — 구조화 출력이 범위를 안 지키는 경우가 실제로 있고, 범위를
    벗어난 값이 조용히 통과하면 손절선이 사라지는 쪽으로 틀리기 때문이다."""
    stop_loss = _clamp(abs(result.stop_loss_pct), _MIN_STOP_LOSS_PCT, _MAX_STOP_LOSS_PCT) / 100.0
    return ExitPlan(
        stop_loss_pct=-stop_loss,
        take_profit_pct=stop_loss * _REWARD_RISK_RATIO,
        take_profit_fraction=_clamp(
            result.take_profit_fraction, _MIN_TAKE_PROFIT_FRACTION, _MAX_TAKE_PROFIT_FRACTION
        ),
        trail_pct=-_clamp(abs(result.trail_pct), _MIN_TRAIL_PCT, _MAX_TRAIL_PCT) / 100.0,
    )


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

    매수 판단과 함께 이 종목의 출구 규칙(ExitPlan)도 같이 받는다 — 별도 호출이
    아니라 이 응답에 필드를 얹는 방식이라 LLM 호출 수는 그대로다. 진입 시점 한
    번만 정하고 보유 중에는 다시 묻지 않는다(ExitPlan docstring).
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

    # degraded면 LLM이 정한 출구 규칙을 버리고 고정 기본값으로 떨어뜨린다. 분석가가
    # 일부 빠진 상태에서 나온 판단은 게이트에서 기준을 높인다는 기존 원칙(CLAUDE.md
    # 기술 스택)과 같은 방향이다 — 절반만 보고 있는 모델에게 손절선을 넓힐 재량까지
    # 주지는 않는다.
    exit_plan = None if degraded else _exit_plan_from(result)

    return Decision(
        ticker=ticker,
        action=result.action,
        reason=result.reasoning,
        inputs=opinions,
        degraded=degraded,
        debate=[bull, bear],
        evidence=[f"prompt:manager@{version}"],
        exit_plan=exit_plan,
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
