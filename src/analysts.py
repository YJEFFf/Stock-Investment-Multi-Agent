import hashlib
import random
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from src import llm
from src.schemas import (
    AnalystOpinion,
    DisclosureContext,
    DisclosureItem,
    MarketContext,
    NewsContext,
    NewsItem,
)

MISSING_DATA_PROBABILITY = 0.15
CHART_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "chart_analyst.md"
NEWS_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "news_analyst.md"
DISCLOSURE_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "disclosure_analyst.md"


def _combined_seed(*parts: str) -> int:
    """PYTHONHASHSEED에 의존하지 않는 결정론적 시드 조합."""
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return int(digest[:16], 16)


def dummy_analyst(
    ticker: str, as_of: datetime, base_seed: int = 0, agent: str = "dummy"
) -> AnalystOpinion | None:
    """마일스톤 1용 더미 분석가. 실제 데이터 없이 시드로만 점수를 생성한다.

    같은 (ticker, as_of, base_seed) 조합은 항상 같은 결과를 낸다 — 재현성 조건.
    다른 종목의 정보를 참조하지 않는다 — 종목당 독립 호출 원칙(규칙 5)을 처음부터 지킴.
    """
    rng = random.Random(_combined_seed(ticker, as_of.isoformat(), str(base_seed)))

    if rng.random() < MISSING_DATA_PROBABILITY:
        return None

    return AnalystOpinion(
        agent=agent,
        ticker=ticker,
        score=rng.uniform(-1.0, 1.0),
        confidence=rng.uniform(0.0, 1.0),
        evidence=["prompt:dummy@m1"],
        as_of=as_of,
    )


class _ChartAnalysisResponse(BaseModel):
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


_CHART_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["score", "confidence", "reasoning"],
    "additionalProperties": False,
}


def _prompt_version(template: str) -> str:
    return hashlib.sha256(template.encode()).hexdigest()[:6]


def _format_chart_prompt(template: str, context: MarketContext) -> str:
    ohlcv_table = "\n".join(
        f"{bar.date} O:{bar.open} H:{bar.high} L:{bar.low} C:{bar.close} V:{bar.volume}"
        for bar in context.bars
    )
    indicators_table = "\n".join(
        f"{name}: {value:.4f}" for name, value in sorted(context.indicators.items())
    )
    return template.format(
        ticker=context.ticker,
        as_of=context.as_of.isoformat(),
        ohlcv_table=ohlcv_table,
        indicators_table=indicators_table,
    )


async def chart_analyst(context: MarketContext) -> AnalystOpinion | None:
    """실제 차트 분석가. MarketContext를 텍스트로 변환해 LLM에게 판단을 받는다
    (docs/PLAN.md §5 — 이미지+vision이 아니라 텍스트 지표로 먼저 시도).

    LLM 호출이 재시도를 소진하고도 실패하면 예외를 그대로 전파한다. 오케스트레이션
    레이어(pipeline.py)가 asyncio.gather(..., return_exceptions=True)로 잡아 해당
    종목만 스킵하게 한다 — 다른 종목·다른 분석가로는 전파되지 않는다.
    """
    template = CHART_PROMPT_PATH.read_text()
    version = _prompt_version(template)
    prompt = _format_chart_prompt(template, context)

    result = await llm.call_structured(
        system="",
        user=prompt,
        response_model=_ChartAnalysisResponse,
        json_schema=_CHART_RESPONSE_SCHEMA,
        label="chart",
    )

    return AnalystOpinion(
        agent="chart",
        ticker=context.ticker,
        score=result.score,
        confidence=result.confidence,
        evidence=[f"prompt:chart@{version}"],
        as_of=context.as_of,
    )


class _NewsAnalysisResponse(BaseModel):
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


_NEWS_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["score", "confidence", "reasoning"],
    "additionalProperties": False,
}


def _format_news_items(items: list[NewsItem]) -> str:
    if not items:
        return "(없음)"
    lines = []
    for item in items:
        press = item.press or "출처 미상"
        when = item.published_at.isoformat() if item.published_at else "시간 미상"
        lines.append(f"- [{press}, {when}] {item.title}")
    return "\n".join(lines)


def _format_news_prompt(template: str, context: NewsContext) -> str:
    return template.format(
        ticker=context.ticker,
        sector=context.sector,
        as_of=context.as_of.isoformat(),
        company_news=_format_news_items(context.company_news),
        sector_news=_format_news_items(context.sector_news),
    )


async def news_analyst(context: NewsContext) -> AnalystOpinion | None:
    """실제 뉴스 분석가. 종목 뉴스가 메인, 업종 뉴스는 배경 참고용이다 (docs/PLAN.md §5
    — 종목 간 비교가 아니라 이 종목이 속한 시장 맥락을 주는 것이라 규칙 5 위반이 아니다).

    종목·업종 뉴스가 둘 다 없으면 LLM을 호출하지 않고 곧바로 None을 반환한다 — 데이터
    없이 판단하지 않는다 (스키마 계약: "판단 불가"와 "판단했으나 기각"은 다른 상태다).
    """
    if not context.company_news and not context.sector_news:
        return None

    template = NEWS_PROMPT_PATH.read_text()
    version = _prompt_version(template)
    prompt = _format_news_prompt(template, context)

    result = await llm.call_structured(
        system="",
        user=prompt,
        response_model=_NewsAnalysisResponse,
        json_schema=_NEWS_RESPONSE_SCHEMA,
        label="news",
    )

    return AnalystOpinion(
        agent="news",
        ticker=context.ticker,
        score=result.score,
        confidence=result.confidence,
        evidence=[f"prompt:news@{version}"],
        as_of=context.as_of,
    )


class _DisclosureAnalysisResponse(BaseModel):
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str


_DISCLOSURE_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
    },
    "required": ["score", "confidence", "reasoning"],
    "additionalProperties": False,
}


def _format_disclosure_items(items: list[DisclosureItem]) -> str:
    if not items:
        return "(없음)"
    lines = []
    for item in items:
        remark = f", 비고: {item.remark}" if item.remark else ""
        lines.append(f"- [{item.received_at.isoformat()}] {item.report_name} (제출인: {item.submitter}{remark})")
    return "\n".join(lines)


def _format_disclosure_prompt(template: str, context: DisclosureContext) -> str:
    return template.format(
        ticker=context.ticker,
        as_of=context.as_of.isoformat(),
        disclosures=_format_disclosure_items(context.disclosures),
    )


async def disclosure_analyst(context: DisclosureContext) -> AnalystOpinion | None:
    """실제 공시 분석가. DART 공시 목록(제목만, 원문은 안 가져옴)을 보고 판단한다 —
    한국 공시 보고서명은 그 자체로 정보 밀도가 높아 뉴스 분석가와 같은 스코프로
    맞췄다 (docs/PLAN.md §5).

    공시가 하나도 없으면 LLM을 호출하지 않고 곧바로 None을 반환한다 — 데이터 없이
    판단하지 않는다.
    """
    if not context.disclosures:
        return None

    template = DISCLOSURE_PROMPT_PATH.read_text()
    version = _prompt_version(template)
    prompt = _format_disclosure_prompt(template, context)

    result = await llm.call_structured(
        system="",
        user=prompt,
        response_model=_DisclosureAnalysisResponse,
        json_schema=_DISCLOSURE_RESPONSE_SCHEMA,
        label="disclosure",
    )

    return AnalystOpinion(
        agent="disclosure",
        ticker=context.ticker,
        score=result.score,
        confidence=result.confidence,
        evidence=[f"prompt:disclosure@{version}"],
        as_of=context.as_of,
    )
