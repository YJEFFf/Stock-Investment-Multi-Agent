import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from src import collectors, kis, sell
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
    SellAction,
)

logger = logging.getLogger(__name__)

# 마일스톤 1 더미 판단 문턱값. 실제 강세/약세 토론 + 포트폴리오 매니저가 들어오면
# propose_decision()을 통째로 교체한다 (마일스톤 2 이후).
BUY_SCORE_THRESHOLD = 0.85
MIN_CONFIDENCE = 0.7

# 포지션 사이징 로직이 아직 없어서 쓰는 고정값 — 실제 사이징 알고리즘이 들어오면 교체.
TRADE_WEIGHT = 0.08

# 판단 시점(전일 데이터 기준)과 실제 집행 시점(장 시작) 사이에 가격이 이 이상
# 벌어지면 매수를 스킵한다 — 사용자 확정치(2026-08-09). 판단 근거가 낡았다고
# 보고 억지로 체결시키지 않는다(규칙 2·3과 같은 정신: 조건이 안 맞으면 안 산다).
GAP_SKIP_THRESHOLD_PCT = 0.03

DEFAULT_LOG_PATH = Path("logs/pipeline.jsonl")
DEFAULT_SELL_LOG_PATH = Path("logs/sell.jsonl")

# 정량 사전 필터 절대 문턱값 (docs/PLAN.md §5, 2026-08-08 확정). 관행값으로 시작하고
# 나중에 필터 통과율 로그를 보고 조정한다 — 과거 수익률에 맞춰 역산하지 않는다.
# top-N이 아니라 절대 문턱이라 통과 개수는 매일 다르고 0일 수도 있다 (규칙 2·3).
QUANT_VOLUME_SURGE_RATIO = 2.0  # 거래량이 20일 평균 대비 이 배수 이상
QUANT_RSI_OVERBOUGHT = 70.0
QUANT_RSI_OVERSOLD = 30.0

# 초과수익률(지수 대비) 문턱은 고정 %p가 아니라 종목 자신의 변동성 단위로 잰다.
# 2026-08-08 실측: 코스피200 지수가 5일간 -6.9% 빠진 주에 고정 5%p 문턱을 썼더니
# 199종목 중 188종목(94.5%)이 통과했다 — 지수가 크게 움직인 주엔 종목 간 분산
# 자체가 커져서 고정폭 문턱이 무의미해진다는 게 드러났다. daily_return_stdev_20d
# (그 종목 자신의 최근 20일 일별 수익률 표준편차)를 5일치로 스케일(√5)해 "이번
# 5일 초과수익률이 이 종목 자신의 정상적인 변동폭 대비 몇 배인가"로 정규화한다 —
# 시장이 흔들리는 주엔 종목 자신의 변동성도 같이 커지므로 문턱이 자동으로 따라
# 올라간다. 횡단면(그날 200종목 분포) z-score와 다르다 — 그건 매일 일정 비율이
# 통과하도록 구조적으로 보장되어 규칙 2·3을 우회하는 통로가 될 위험이 있어 반려한
# 방식이고, 이건 종목 자신의 시계열 대비라 그런 문제가 없다.
QUANT_EXCESS_RETURN_Z_THRESHOLD = 2.0

# 종목 하나에 대해 (현재 구성된 모든) 분석가를 호출하고 얻은 의견 목록을 반환하는 함수.
# 더미 경로(마일스톤 1)와 실데이터 경로(마일스톤 2)가 같은 run_day를 공유하도록 주입한다.
# sector는 뉴스 분석가처럼 종목이 속한 업종 정보가 필요한 분석가를 위한 것 — 필요 없는
# 분석가(차트 등)는 그냥 무시하면 된다.
AnalystFn = Callable[[str, str, datetime], Awaitable[list[AnalystOpinion]]]

# 종목 하나의 의견 목록을 받아 최종 Decision을 내리는 함수. 비용 없는 propose_decision()
# (더미/테스트 경로)와 실제 LLM 토론+매니저인 judgment.judge()가 같은 형태를 공유한다.
JudgeFn = Callable[[list[AnalystOpinion], int], Awaitable[Decision | None]]

# 승인된(또는 거부된) Decision을 포트폴리오에 반영하는 함수. 가격 개념 없는
# 순수 시뮬레이션(execute_simulated, 무비용)과 실제 KIS 주문까지 내는
# execute_buy_order가 같은 형태를 공유한다.
ExecuteFn = Callable[[Decision, GateResult, PortfolioState, str, float], Awaitable[PortfolioState]]

# SellAction을 포트폴리오에 반영하는 함수. 무비용 시뮬레이션(sell.execute_sell_simulated)
# 과 실제 KIS 매도 주문(sell.execute_sell_order)이 같은 형태를 공유한다.
SellExecuteFn = Callable[[PortfolioState, SellAction, float], Awaitable[PortfolioState]]

# 보유 종목 재평가(LLM 재량 매도)용 판단 함수. judgment.judge_sell이 이 형태를 따른다.
# evaluate_holdings에서 기본값 None이면 이 계층 자체를 건너뛴다 — 결정론적
# 안전장치와 달리 LLM 재량은 진짜로 선택적인 추가 비용이라, 명시적으로 넣지
# 않으면 안전하게 꺼져 있는 쪽을 기본으로 한다.
JudgeSellFn = Callable[[str, list[AnalystOpinion], float], Awaitable[SellAction | None]]


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
    """승인된 BUY만 포트폴리오에 반영하는 순수 함수. 실거래 API 호출 없음(규칙 7) —
    가격 개념이 아예 없는 시뮬레이션 전용 경로다(entry_price를 채우지 않는다).
    무비용 테스트·측정용으로 계속 남겨둔다. 실제 KIS 모의투자 주문까지 내는
    경로는 execute_buy_order를 쓴다.
    """
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


async def execute_simulated(
    decision: Decision,
    gate_result: GateResult,
    portfolio: PortfolioState,
    sector: str,
    trade_weight: float,
) -> PortfolioState:
    """execute()를 ExecuteFn 인터페이스에 맞춘 비동기 래퍼 — 무비용 시뮬레이션 경로.
    run_day가 실제 실행(execute_buy_order)과 동일한 형태로 주입받을 수 있게 한다."""
    return execute(decision, gate_result, portfolio, sector, trade_weight)


async def execute_buy_order(
    decision: Decision,
    gate_result: GateResult,
    portfolio: PortfolioState,
    sector: str,
    trade_weight: float,
) -> PortfolioState:
    """게이트를 통과한 BUY를 실제 KIS 모의투자 시장가 주문으로 집행한다.

    execute()와 달리 실제 KIS API를 호출한다(모의투자만, 규칙 7). 판단 시점(전일
    데이터 기준)과 집행 시점(장 시작) 사이 가격이 GAP_SKIP_THRESHOLD_PCT 이상
    벌어지면 판단 근거가 낡았다고 보고 스킵한다. 가격·잔고 조회 중 하나라도
    실패하거나 주문이 거부되면 매수 없이 그대로 반환한다 — 매수를 강행할 이유가
    없다(규칙 1, 기본 상태는 현금).

    포지션이 이미 있으면 진입가를 비중 가중평균으로 갱신한다(근사치 — weight는
    "그 시점 총자산 대비 비중"이라 매수 시점마다 총자산이 달라지면 완전히
    정확하진 않다. 포지션을 추가매수하는 경우는 드물어 이 근사로 충분하다고
    본다).
    """
    if not (decision.action == "BUY" and gate_result.approved):
        return portfolio

    ticker = decision.ticker

    prev_bars, current_price = await asyncio.gather(
        asyncio.to_thread(kis.fetch_daily_ohlcv, ticker, 2),
        asyncio.to_thread(kis.fetch_current_price, ticker),
    )
    if not prev_bars or current_price is None:
        logger.warning("execute_buy_order_skipped ticker=%s reason=price_data_unavailable", ticker)
        return portfolio

    prev_close = prev_bars[-1].close
    gap_pct = abs(current_price - prev_close) / prev_close
    if gap_pct > GAP_SKIP_THRESHOLD_PCT:
        logger.warning("execute_buy_order_skipped ticker=%s reason=gap_too_large gap_pct=%.4f", ticker, gap_pct)
        return portfolio

    total_value = await asyncio.to_thread(kis.fetch_account_balance)
    if total_value is None:
        logger.warning("execute_buy_order_skipped ticker=%s reason=balance_unavailable", ticker)
        return portfolio

    quantity = int((total_value * trade_weight) // current_price)
    if quantity <= 0:
        logger.warning("execute_buy_order_skipped ticker=%s reason=quantity_zero", ticker)
        return portfolio

    order_no = await asyncio.to_thread(kis.place_market_buy_order, ticker, quantity)
    if order_no is None:
        logger.error("execute_buy_order_failed ticker=%s reason=order_rejected", ticker)
        return portfolio

    fill_price = await asyncio.to_thread(kis.fetch_fill_price, ticker, datetime.now(timezone.utc).date())
    # 체결가 조회가 실패해도 주문 자체는 이미 나갔다 — None보다 현재가 근사치가
    # 낫다(포지션이 리스크 관리 대상에서 아예 빠지는 걸 막는다).
    entry_price = fill_price if fill_price is not None else current_price

    positions = [p.model_copy() for p in portfolio.positions]
    existing = next((p for p in positions if p.ticker == ticker), None)
    if existing is not None:
        old_basis = (existing.entry_price or entry_price) * existing.weight
        new_basis = entry_price * trade_weight
        new_weight = existing.weight + trade_weight
        existing.entry_price = (old_basis + new_basis) / new_weight
        existing.weight = new_weight
        existing.peak_price = max(existing.peak_price or entry_price, entry_price)
        existing.quantity = (existing.quantity or 0) + quantity
    else:
        positions.append(
            Position(
                ticker=ticker,
                sector=sector,
                weight=trade_weight,
                entry_price=entry_price,
                peak_price=entry_price,
                quantity=quantity,
            )
        )

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


def passes_quant_filter(indicators: dict[str, float], index_return_5d_pct: float | None) -> bool:
    """비용 게이트일 뿐 품질 판단이 아니다 — "이 종목이 좋다"가 아니라 "오늘 이
    종목에 평소보다 뭔가 있다"만 본다. 방향 판단은 전적으로 LLM 분석가 몫이다.

    세 조건을 OR로 묶는다: 거래량 급증 / 자기 변동성 대비 정규화한 지수 초과 모멘텀
    / RSI 극단. 절대 문턱이라 통과 여부에 종목 개수 목표가 없다 — 규칙 2·3.
    """
    volume_ratio = indicators.get("volume_vs_20d_avg_ratio")
    if volume_ratio is not None and volume_ratio >= QUANT_VOLUME_SURGE_RATIO:
        return True

    rsi = indicators.get("rsi14")
    if rsi is not None and (rsi >= QUANT_RSI_OVERBOUGHT or rsi <= QUANT_RSI_OVERSOLD):
        return True

    stock_return = indicators.get("return_5d_pct")
    daily_vol = indicators.get("daily_return_stdev_20d")
    if stock_return is not None and index_return_5d_pct is not None and daily_vol:
        excess_return = stock_return - index_return_5d_pct
        expected_5d_vol = daily_vol * (5**0.5)
        if expected_5d_vol > 0 and abs(excess_return) / expected_5d_vol >= QUANT_EXCESS_RETURN_Z_THRESHOLD:
            return True

    return False


async def quant_prefilter(
    universe: list[tuple[str, str]], lookback_days: int = 60
) -> list[tuple[str, str]]:
    """코스피200 전체를 개별 종목 시세 조회 없이 LLM 분석가에 넘기면 하루 800회
    호출이 된다 (docs/PLAN.md §2). 이 함수는 그 앞단에서 KIS 시세 데이터만으로
    "오늘 볼 가치가 있는가"를 절대 문턱으로 걸러 비용을 줄인다.

    시세 조회 자체가 실패한 종목(KIS 데이터 수집 실패)은 그냥 이번 라운드에서
    빠진다 — 이미 collectors 단에서 재시도를 다 소진한 뒤의 결과라 여기서 다시
    재시도하지 않는다(규칙 4는 데이터 수집 계층에서 지킨다).
    """
    index_bars = await asyncio.to_thread(collectors.fetch_kospi200_index_bars, lookback_days)
    index_indicators = collectors.compute_indicators(index_bars) if index_bars else {}
    index_return_5d_pct = index_indicators.get("return_5d_pct")

    async def _check(ticker: str, sector: str) -> tuple[str, str] | None:
        context = await asyncio.to_thread(collectors.fetch_market_context, ticker, lookback_days)
        if context is None:
            return None
        if passes_quant_filter(context.indicators, index_return_5d_pct):
            return (ticker, sector)
        return None

    raw_results = await asyncio.gather(*(_check(t, s) for t, s in universe), return_exceptions=True)

    passed: list[tuple[str, str]] = []
    for (ticker, _), result in zip(universe, raw_results):
        if isinstance(result, BaseException):
            logger.warning("quant_prefilter_failed ticker=%s error=%s", ticker, result)
            continue
        if result is not None:
            passed.append(result)

    logger.info("quant_prefilter_done universe=%d passed=%d", len(universe), len(passed))
    return passed


async def build_universe_with_sectors() -> list[tuple[str, str]] | None:
    """코스피200 유니버스(네이버)에 업종(네이버, 캐시됨)을 붙여 run_day가 기대하는
    (종목코드, 업종) 형태로 만든다.

    유니버스 조회 자체가 실패하면 None — 유니버스가 틀리면 그날 전체 판단이
    왜곡되므로 조용히 진행하지 않는다. 업종 맵은 이보다 관대하게 다룬다: 캐시조차
    없어 전부 실패해도(사실상 없음) 빈 문자열로 대체하고 계속 진행한다 — 업종을
    쓰는 건 뉴스 분석가의 업종 배경 뉴스뿐이라, 이거 하나 때문에 하루 전체를 막을
    이유가 없다(차트·공시 분석가는 sector를 아예 안 씀).
    """
    universe, sector_map = await asyncio.gather(
        asyncio.to_thread(collectors.fetch_kospi200_universe),
        asyncio.to_thread(collectors.fetch_kospi200_sector_map),
    )
    if universe is None:
        logger.error("build_universe_with_sectors_failed reason=universe_fetch_failed")
        return None

    if sector_map is None:
        logger.warning("build_universe_with_sectors_degraded reason=sector_map_unavailable")
        sector_map = {}

    return [(code, sector_map.get(code, "")) for code, _name in universe]


async def run_daily(
    day: datetime,
    portfolio: PortfolioState,
    config: RiskGateConfig,
    analyst_fn: AnalystFn,
    judge_fn: JudgeFn,
    execute_fn: ExecuteFn,
    total_expected_analysts: int = 1,
    log_path: Path = DEFAULT_LOG_PATH,
) -> tuple[PortfolioState, list[tuple[Decision, GateResult]]]:
    """하루치 전체 파이프라인 진입점: 코스피200 유니버스 구성 -> 정량 필터 ->
    run_day(analyst_fn, judge_fn, execute_fn). 유니버스 조회 자체가 실패하면(네이버
    접근 불가 등) 그날은 빈 결과로 관망한다 — 매수 없음이 기본 상태다(규칙 1).

    analyst_fn/judge_fn/execute_fn은 run_day와 마찬가지로 기본값이 없다 — 실제 LLM
    경로(judgment.judge, 비용 발생)·실제 주문 집행(execute_buy_order, 실거래 발생)을
    쓸지 무비용 경로(propose_decision/execute_simulated)를 쓸지 호출부가 항상
    명시해야 실수로 비용이나 실주문이 나가지 않는다.
    """
    universe = await build_universe_with_sectors()
    if universe is None:
        logger.error("run_daily_aborted day=%s reason=universe_fetch_failed", day.date().isoformat())
        return portfolio, []

    filtered = await quant_prefilter(universe)
    logger.info(
        "run_daily_filtered day=%s universe=%d filtered=%d",
        day.date().isoformat(),
        len(universe),
        len(filtered),
    )

    return await run_day(
        filtered, day, portfolio, config, analyst_fn, judge_fn, execute_fn, total_expected_analysts, log_path
    )


async def run_day(
    universe: list[tuple[str, str]],
    day: datetime,
    portfolio: PortfolioState,
    config: RiskGateConfig,
    analyst_fn: AnalystFn,
    judge_fn: JudgeFn,
    execute_fn: ExecuteFn,
    total_expected_analysts: int = 1,
    log_path: Path = DEFAULT_LOG_PATH,
) -> tuple[PortfolioState, list[tuple[Decision, GateResult]]]:
    """유니버스(종목, 섹터)를 순회하며 분석→판단→게이트→집행을 실행하고 로그를 남긴다.

    분석가 호출과 판단(judge_fn) 호출 모두 asyncio.gather(..., return_exceptions=True)로
    병렬 실행한다 — 한 종목의 호출이 실패해도 다른 종목은 영향받지 않는다. judge_fn과
    execute_fn 둘 다 기본값이 없다 — 비용 없는 propose_decision/execute_simulated인지
    실제 LLM 토론+매니저(judgment.judge)·실제 KIS 주문(execute_buy_order)인지 호출부가
    항상 명시적으로 골라야 한다.
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

        portfolio = await execute_fn(decision, gate_result, portfolio, sector, TRADE_WEIGHT)
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


async def evaluate_holdings(
    portfolio: PortfolioState,
    day: datetime,
    sell_execute_fn: SellExecuteFn,
    analyst_fn: AnalystFn | None = None,
    judge_sell_fn: JudgeSellFn | None = None,
    log_path: Path = DEFAULT_SELL_LOG_PATH,
) -> PortfolioState:
    """보유 포지션 전체를 매일 재평가한다 — 결정론적 안전장치(손절/트레일링
    익절, src/sell.py)는 항상 돌고, LLM 재량 매도(judgment.judge_sell)는
    analyst_fn/judge_sell_fn을 넣었을 때만 추가로 돈다.

    매수 쪽엔 정량 필터(quant_prefilter)가 있지만 이쪽엔 없다 — 필요가 없다.
    리스크 게이트가 종목당 최대 비중을 15%로 막아둬서 보유 종목 수 자체가
    원천적으로 적다(최대 ~7개). 그래서 매일 보유 종목 전부를 그냥 평가한다.

    sell_execute_fn은 기본값이 없다 — 무비용 시뮬레이션(execute_sell_simulated)인지
    실제 KIS 매도 주문(execute_sell_order)인지 호출부가 항상 명시해야 한다.
    analyst_fn/judge_sell_fn은 반대로 기본값이 None이다 — 결정론적 안전장치와
    달리 LLM 재량은 진짜 선택적인 추가 비용이라 명시적으로 켜지 않으면 꺼져
    있는 쪽이 안전한 기본값이다.

    한 종목에 대해 결정론적 매도가 이미 트리거되면 그날은 그걸로 끝이다 —
    같은 포지션에 대해 코드가 이미 팔기로 결정했는데 LLM에게 다시 물어볼
    이유가 없다. 가격 조회가 실패한 종목은 오늘 평가를 건너뛴다(collectors/kis
    단에서 이미 재시도를 소진한 뒤라 여기서 다시 재시도하지 않는다, 규칙 4).
    """
    if not portfolio.positions:
        return portfolio

    price_results = await asyncio.gather(
        *(asyncio.to_thread(kis.fetch_current_price, p.ticker) for p in portfolio.positions),
        return_exceptions=True,
    )

    price_by_ticker: dict[str, float] = {}
    for position, price in zip(portfolio.positions, price_results):
        if isinstance(price, BaseException) or price is None:
            logger.warning("evaluate_holdings_price_unavailable ticker=%s", position.ticker)
            continue
        price_by_ticker[position.ticker] = price

    for ticker, current_price in price_by_ticker.items():
        position = next(p for p in portfolio.positions if p.ticker == ticker)
        position = sell.update_peak_price(position, current_price)
        portfolio = PortfolioState(
            positions=[position if p.ticker == ticker else p for p in portfolio.positions],
            cash_weight=portfolio.cash_weight,
            daily_pnl_pct=portfolio.daily_pnl_pct,
        )

        action = sell.evaluate_deterministic_sell(position, current_price)

        if action is None and analyst_fn is not None and judge_sell_fn is not None:
            try:
                opinions = await analyst_fn(ticker, position.sector, day)
            except Exception as exc:  # noqa: BLE001 - 재평가 실패는 그냥 오늘 평가 스킵
                logger.warning("evaluate_holdings_reanalysis_failed ticker=%s error=%s", ticker, exc)
                opinions = []

            unrealized_pct = (
                (current_price - position.entry_price) / position.entry_price
                if position.entry_price
                else 0.0
            )
            action = await judge_sell_fn(ticker, opinions, unrealized_pct)

        if action is None:
            continue

        portfolio = await sell_execute_fn(portfolio, action, current_price)
        _append_log(
            log_path,
            {
                "day": day.date().isoformat(),
                "ticker": ticker,
                "reason": action.reason,
                "sell_fraction": action.sell_fraction,
                "price": current_price,
            },
        )

    return portfolio


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
