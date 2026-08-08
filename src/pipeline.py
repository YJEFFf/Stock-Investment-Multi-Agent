import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from src import collectors
from src.analysts import chart_analyst, disclosure_analyst, dummy_analyst, news_analyst
from src.schemas import (
    AnalystOpinion,
    Decision,
    DisclosureContext,
    GateResult,
    NewsContext,
    PortfolioState,
    Position,
    RiskGateConfig,
)

logger = logging.getLogger(__name__)

# 마일스톤 1 더미 판단 문턱값. 실제 강세/약세 토론 + 포트폴리오 매니저가 들어오면
# propose_decision()을 통째로 교체한다 (마일스톤 2 이후).
BUY_SCORE_THRESHOLD = 0.85
MIN_CONFIDENCE = 0.7

# 포지션 사이징 로직이 아직 없어서 쓰는 고정값 — 실제 사이징 알고리즘이 들어오면 교체.
TRADE_WEIGHT = 0.08

DEFAULT_LOG_PATH = Path("logs/pipeline.jsonl")

# 종목 하나에 대해 (현재 구성된 모든) 분석가를 호출하고 얻은 의견 목록을 반환하는 함수.
# 더미 경로(마일스톤 1)와 실데이터 경로(마일스톤 2)가 같은 run_day를 공유하도록 주입한다.
# sector는 뉴스 분석가처럼 종목이 속한 업종 정보가 필요한 분석가를 위한 것 — 필요 없는
# 분석가(차트 등)는 그냥 무시하면 된다.
AnalystFn = Callable[[str, str, datetime], Awaitable[list[AnalystOpinion]]]

# 종목 하나의 의견 목록을 받아 최종 Decision을 내리는 함수. 비용 없는 propose_decision()
# (더미/테스트 경로)와 실제 LLM 토론+매니저인 judgment.judge()가 같은 형태를 공유한다.
JudgeFn = Callable[[list[AnalystOpinion], int], Awaitable[Decision | None]]


async def propose_decision(opinions: list[AnalystOpinion], total_expected_analysts: int) -> Decision | None:
    """비용 없는 절대 문턱 판단 — 더미 분석가 경로와 대규모 신호율 측정용으로 남겨둔다.

    실제 파이프라인은 이 함수 대신 judgment.judge()(강세/약세 토론 + 포트폴리오
    매니저)를 쓴다. 의견이 하나도 없으면 판단 자체를 하지 않는다 — "판단 불가"와
    "판단했으나 기각"은 다른 상태다 (스키마 계약).
    """
    if not opinions:
        return None

    ticker = opinions[0].ticker
    avg_score = sum(o.score for o in opinions) / len(opinions)
    avg_confidence = sum(o.confidence for o in opinions) / len(opinions)
    is_buy = avg_score >= BUY_SCORE_THRESHOLD and avg_confidence >= MIN_CONFIDENCE

    return Decision(
        ticker=ticker,
        action="BUY" if is_buy else "HOLD",
        reason=(
            f"avg_score={avg_score:.3f} (threshold={BUY_SCORE_THRESHOLD}), "
            f"avg_confidence={avg_confidence:.3f} (min={MIN_CONFIDENCE}), "
            f"opinions={len(opinions)}/{total_expected_analysts}"
        ),
        inputs=opinions,
        degraded=len(opinions) < total_expected_analysts,
    )


def check_gate(
    decision: Decision,
    portfolio: PortfolioState,
    config: RiskGateConfig,
    sector: str,
    trade_weight: float,
) -> GateResult:
    """4개 룰을 순서대로 확정 판정. 첫 위반에서 즉시 거부."""
    if decision.action != "BUY":
        return GateResult(approved=False, rejected_by=None)

    existing = next((p for p in portfolio.positions if p.ticker == decision.ticker), None)
    existing_weight = existing.weight if existing else 0.0
    if existing_weight + trade_weight > config.position_limit:
        return GateResult(approved=False, rejected_by="position_limit")

    sector_weight = sum(p.weight for p in portfolio.positions if p.sector == sector)
    if sector_weight + trade_weight > config.sector_concentration_limit:
        return GateResult(approved=False, rejected_by="sector_concentration")

    if portfolio.daily_pnl_pct <= config.daily_loss_limit:
        return GateResult(approved=False, rejected_by="daily_loss_limit")

    invested_weight = 1.0 - portfolio.cash_weight
    if invested_weight + trade_weight > config.total_exposure_limit:
        return GateResult(approved=False, rejected_by="total_exposure")

    return GateResult(approved=True, rejected_by=None)


def execute(
    decision: Decision,
    gate_result: GateResult,
    portfolio: PortfolioState,
    sector: str,
    trade_weight: float,
) -> PortfolioState:
    """승인된 BUY만 포트폴리오에 반영하는 순수 함수. 실거래 API 호출 없음 (규칙 7)."""
    if not (decision.action == "BUY" and gate_result.approved):
        return portfolio

    positions = [p.model_copy() for p in portfolio.positions]
    existing = next((p for p in positions if p.ticker == decision.ticker), None)
    if existing is not None:
        existing.weight += trade_weight
    else:
        positions.append(Position(ticker=decision.ticker, sector=sector, weight=trade_weight))

    return PortfolioState(
        positions=positions,
        cash_weight=portfolio.cash_weight - trade_weight,
        daily_pnl_pct=portfolio.daily_pnl_pct,
    )


def _append_log(log_path: Path, entry: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def make_dummy_analyst_fn(base_seed: int) -> AnalystFn:
    """마일스톤 1 더미 경로. 실제 분석가와 동일한 AnalystFn 인터페이스를 따른다."""

    async def _fn(ticker: str, sector: str, day: datetime) -> list[AnalystOpinion]:
        opinion = dummy_analyst(ticker, day, base_seed)
        return [opinion] if opinion is not None else []

    return _fn


def make_chart_analyst_fn(lookback_days: int = 60) -> AnalystFn:
    """실데이터 경로: 차트 분석가만 단독으로 호출. sector는 필요 없어 무시한다."""

    async def _fn(ticker: str, sector: str, day: datetime) -> list[AnalystOpinion]:
        context = await asyncio.to_thread(collectors.fetch_market_context, ticker, lookback_days)
        if context is None:
            return []
        opinion = await chart_analyst(context)
        return [opinion] if opinion is not None else []

    return _fn


def make_news_analyst_fn(news_limit: int = 10) -> AnalystFn:
    """실데이터 경로: 뉴스 분석가만 단독으로 호출. 종목 뉴스 + 업종 배경 뉴스를 모아
    NewsContext를 만든다 (docs/PLAN.md §5 — 업종 뉴스는 비교가 아니라 배경 참고용)."""

    async def _fn(ticker: str, sector: str, day: datetime) -> list[AnalystOpinion]:
        company_news, sector_news = await asyncio.gather(
            asyncio.to_thread(collectors.fetch_company_news, ticker, news_limit),
            asyncio.to_thread(collectors.fetch_sector_news, sector, news_limit),
        )
        if company_news is None and sector_news is None:
            return []

        context = NewsContext(
            ticker=ticker,
            sector=sector,
            as_of=day,
            company_news=company_news or [],
            sector_news=sector_news or [],
        )
        opinion = await news_analyst(context)
        return [opinion] if opinion is not None else []

    return _fn


def make_disclosure_analyst_fn(lookback_days: int = 30, limit: int = 10) -> AnalystFn:
    """실데이터 경로: 공시 분석가만 단독으로 호출. sector는 필요 없어 무시한다."""

    async def _fn(ticker: str, sector: str, day: datetime) -> list[AnalystOpinion]:
        disclosures = await asyncio.to_thread(collectors.fetch_disclosures, ticker, lookback_days, limit)
        if disclosures is None:
            return []

        context = DisclosureContext(ticker=ticker, as_of=day, disclosures=disclosures)
        opinion = await disclosure_analyst(context)
        return [opinion] if opinion is not None else []

    return _fn


def make_combined_analyst_fn(component_fns: list[AnalystFn]) -> AnalystFn:
    """종목 하나에 대해 여러 분석가를 동시에 호출하고 성공한 의견만 모은다.

    asyncio.gather(..., return_exceptions=True) — 분석가 하나가 실패해도 다른
    분석가는 살아남는다 (CLAUDE.md 아키텍처 원칙: "DART가 죽어도 차트 분석가는
    돌아야 한다").
    """

    async def _fn(ticker: str, sector: str, day: datetime) -> list[AnalystOpinion]:
        raw_results = await asyncio.gather(
            *(fn(ticker, sector, day) for fn in component_fns), return_exceptions=True
        )
        opinions: list[AnalystOpinion] = []
        for fn, raw in zip(component_fns, raw_results):
            if isinstance(raw, BaseException):
                logger.warning("component_analyst_fn_failed ticker=%s fn=%s error=%s", ticker, fn, raw)
                continue
            opinions.extend(raw)
        return opinions

    return _fn


async def run_day(
    universe: list[tuple[str, str]],
    day: datetime,
    portfolio: PortfolioState,
    config: RiskGateConfig,
    analyst_fn: AnalystFn,
    judge_fn: JudgeFn,
    total_expected_analysts: int = 1,
    log_path: Path = DEFAULT_LOG_PATH,
) -> tuple[PortfolioState, list[tuple[Decision, GateResult]]]:
    """유니버스(종목, 섹터)를 순회하며 분석→판단→게이트→집행을 실행하고 로그를 남긴다.

    분석가 호출과 판단(judge_fn) 호출 모두 asyncio.gather(..., return_exceptions=True)로
    병렬 실행한다 — 한 종목의 호출이 실패해도 다른 종목은 영향받지 않는다. judge_fn은
    기본값이 없다 — 비용 없는 propose_decision인지 실제 LLM 토론+매니저(judgment.judge)
    인지 호출부가 항상 명시적으로 골라야 한다.
    """
    raw_results = await asyncio.gather(
        *(analyst_fn(ticker, sector, day) for ticker, sector in universe), return_exceptions=True
    )

    opinions_per_ticker: list[list[AnalystOpinion]] = []
    for (ticker, _), raw in zip(universe, raw_results):
        if isinstance(raw, BaseException):
            logger.warning("analyst_fn_failed ticker=%s error=%s", ticker, raw)
            opinions_per_ticker.append([])
        else:
            opinions_per_ticker.append(raw)

    judge_raw_results = await asyncio.gather(
        *(judge_fn(opinions, total_expected_analysts) for opinions in opinions_per_ticker),
        return_exceptions=True,
    )

    decisions: list[Decision | None] = []
    for (ticker, _), raw in zip(universe, judge_raw_results):
        if isinstance(raw, BaseException):
            logger.warning("judge_fn_failed ticker=%s error=%s", ticker, raw)
            decisions.append(None)
        else:
            decisions.append(raw)

    results: list[tuple[Decision, GateResult]] = []

    for (ticker, sector), decision in zip(universe, decisions):
        if decision is None:
            continue  # 의견이 없음 — 관망. score=0 등으로 대체하지 않는다.

        if decision.action == "BUY":
            gate_result = check_gate(decision, portfolio, config, sector, TRADE_WEIGHT)
        else:
            gate_result = GateResult(approved=False, rejected_by=None)

        portfolio = execute(decision, gate_result, portfolio, sector, TRADE_WEIGHT)
        results.append((decision, gate_result))

        # avg_score/avg_confidence는 마일스톤 3 IC 계산의 원재료 — decision.inputs에서
        # 다시 뽑아낸다 (propose_decision의 반환 계약은 안 건드림).
        avg_score = sum(o.score for o in decision.inputs) / len(decision.inputs)
        avg_confidence = sum(o.confidence for o in decision.inputs) / len(decision.inputs)

        _append_log(
            log_path,
            {
                "day": day.date().isoformat(),
                "ticker": ticker,
                "action": decision.action,
                "approved": gate_result.approved,
                "rejected_by": gate_result.rejected_by,
                "avg_score": avg_score,
                "avg_confidence": avg_confidence,
            },
        )

    return portfolio, results


def summarize_log(log_path: Path, total_days: int) -> dict:
    """최근 실행 로그에서 신호 발생률과 게이트 거부 사유별 집계를 계산한다."""
    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    entries = [json.loads(line) for line in lines]

    signal_days: set[str] = set()
    rejected_by_counts: dict[str, int] = {}

    for e in entries:
        if e["action"] == "BUY" and e["approved"]:
            signal_days.add(e["day"])
        if not e["approved"] and e["rejected_by"]:
            rejected_by_counts[e["rejected_by"]] = rejected_by_counts.get(e["rejected_by"], 0) + 1

    return {
        "total_days": total_days,
        "signal_days": len(signal_days),
        "signal_day_ratio": (len(signal_days) / total_days) if total_days else 0.0,
        "rejected_by_counts": rejected_by_counts,
    }
