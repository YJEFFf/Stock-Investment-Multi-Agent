import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src import collectors, kis, llm, notify, sell, translate
from src.analysts import chart_analyst, disclosure_analyst, dummy_analyst, news_analyst
from src.schemas import (
    AnalystOpinion,
    Decision,
    DisclosureContext,
    FillRecord,
    GateResult,
    NewsContext,
    OHLCVBar,
    PortfolioState,
    Position,
    RiskGateConfig,
    SellAction,
)

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")


def _kst_today() -> date:
    """KRX 주문일자(한국 날짜). 장중(09:00-15:30 KST = 00:00-06:30 UTC)에는 UTC
    날짜와 같지만, 주문일자는 원래 KST 개념이라 명시적으로 변환한다 — 이 프로젝트는
    이미 UTC 날짜를 그대로 쓰다가 일일 리포트가 항상 비어 보이는 버그를 한 번 겪었다
    (커밋 832fb8b)."""
    return datetime.now(KST).date()


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

# CLAUDE.md "감시 지표" 창. 신호율은 영업일 기준, LLM 호출 집계는 달력일 기준이다.
MONITORING_WINDOW_TRADING_DAYS = 20
MONITORING_WINDOW_CALENDAR_DAYS = 30

# 운영 트래픽이 아닌 일회성 진단·실험 호출의 label 접두사. 감시 지표에서 제외한다 —
# 2026-08-24 reasoning A/B 90건이 분석가 호출수·토큰 집계에 섞였다. 앞으로 실험용
# 호출은 이 접두사를 쓴다. 그래야 원본 로그에는 남기면서 지표는 오염시키지 않는다.
NON_PRODUCTION_LABEL_PREFIX = "ab_"
DEFAULT_SELL_LOG_PATH = Path("logs/sell.jsonl")
# 매수/매도 "판단"(위 두 파일)과는 별개로, 실제 체결 시점의 원본 사실 + 그 결정을 만든
# 근거 전체(분석가 의견·토론·매니저 사유)를 사람이 읽을 수 있는 형태로 남긴다. 나중에
# 노션 매매일지 동기화가 이 파일 하나만 읽으면 되도록 하는 게 목적(2026-08-09).
DEFAULT_TRADE_JOURNAL_LOG_PATH = Path("logs/trade_journal.jsonl")

# 시세 공백(보유 종목 전멸) 구간을 세는 키. notify의 하루 1회 알림 마커와 같은 키를
# 써서 "같은 사건"임을 한눈에 보이게 한다 — 마커는 .txt, 공백 상태는 .blackout.json.
BLACKOUT_CONTEXT = "holdings_all_prices_unavailable"

# 그날 안전장치가 **실제로 본** 가격의 최저/최고. 매분 한 번 찍는 구조라, 두 샘플
# 사이를 스쳐간 가격은 존재 자체가 기록되지 않는다 — 그래서 "문턱을 안 넘어서 조용한"
# 것과 "넘었는데 그 순간을 안 본" 것이 로그에서 똑같이 조용하다. 이 파일이 그 둘을
# 가른다: 장 마감 후 여기 담긴 관측 범위를 그날 일봉과 대조하면(audit_observation_gap)
# 시장이 지난 문턱을 우리가 못 본 날이 매일 자동으로 드러난다.
#
# 2026-08-27에 실제로 그런 날이 있었다(192820, 09:01 저가 271,000 vs 트레일 275,745).
# 그날은 사람이 분봉을 따로 받아서야 알아냈다 — 그 조사를 코드가 대신하게 한 것이다.
DEFAULT_OBSERVED_RANGE_PATH = Path("logs/observed_range.json")

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
# 실제 체결 내역(FillRecord)을 함께 돌려준다 — 매매일지에 적히는 건 판단 시점
# 호가가 아니라 체결가여야 하고, 그걸 정확히 잴 수 있는 건 주문을 낸 함수뿐이다.
# 시뮬레이션 경로는 체결 자체가 없으므로 항상 None이다.
SellExecuteFn = Callable[
    [PortfolioState, SellAction, float], Awaitable[tuple[PortfolioState, FillRecord | None]]
]

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
    """룰을 순서대로 확정 판정. 첫 위반에서 즉시 거부.

    일일 손실 한도(`daily_loss_limit`) 룰은 2026-08-20에 폐기했다(사용자 확정).
    두 가지가 겹쳤다: `daily_pnl_pct`를 production 경로에서 아무도 계산하지 않아
    항상 0.0이었고, 애초에 이 게이트가 도는 시점이 08:30(개장 전)이라 "오늘 손익"은
    정의상 0이다 — 하루 한 번 개장 직후에만 매수하는 지금 구조에서는 장중
    서킷브레이커라는 개념이 성립하지 않는다. 계산만 채워 넣으면 살아나는 룰이
    아니라 구조가 안 맞는 룰이라, 살아있는 척하게 두는 대신 걷어냈다.
    """
    if decision.action != "BUY":
        return GateResult(approved=False, rejected_by=None)

    existing = next((p for p in portfolio.positions if p.ticker == decision.ticker), None)
    existing_weight = existing.weight if existing else 0.0
    if existing_weight + trade_weight > config.position_limit:
        return GateResult(approved=False, rejected_by="position_limit")

    sector_weight = sum(p.weight for p in portfolio.positions if p.sector == sector)
    if sector_weight + trade_weight > config.sector_concentration_limit:
        return GateResult(approved=False, rejected_by="sector_concentration")

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
    log_path: Path = DEFAULT_TRADE_JOURNAL_LOG_PATH,
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
    # UTC 날짜가 아니라 KRX 주문일자다. 이 값은 매매일지의 day로도 들어가고 일일
    # 리포트가 그 day로 필터링하므로, 09:01(=00:01 UTC)보다 조금이라도 이른 시각에
    # 돌면 그날 기록이 통째로 하루 전으로 밀린다 — _kst_today docstring의 832fb8b와
    # 같은 버그다. 같은 함수가 원장 조회에는 이미 _kst_today()를 쓰고 있었다.
    today = _kst_today()

    prev_bars, current_price = await asyncio.gather(
        asyncio.to_thread(kis.fetch_daily_ohlcv, ticker, 2),
        asyncio.to_thread(kis.fetch_current_price, ticker),
    )
    if not prev_bars or current_price is None:
        logger.warning("execute_buy_order_skipped ticker=%s reason=price_data_unavailable", ticker)
        _log_buy_skip(log_path, today, ticker, "price_data_unavailable")
        return portfolio

    prev_close = prev_bars[-1].close
    gap_pct = abs(current_price - prev_close) / prev_close
    if gap_pct > GAP_SKIP_THRESHOLD_PCT:
        logger.warning("execute_buy_order_skipped ticker=%s reason=gap_too_large gap_pct=%.4f", ticker, gap_pct)
        _log_buy_skip(log_path, today, ticker, "gap_too_large", gap_pct=round(gap_pct, 4))
        return portfolio

    total_value = await asyncio.to_thread(kis.fetch_account_balance)
    if total_value is None:
        logger.warning("execute_buy_order_skipped ticker=%s reason=balance_unavailable", ticker)
        _log_buy_skip(log_path, today, ticker, "balance_unavailable")
        return portfolio

    quantity = int((total_value * trade_weight) // current_price)
    if quantity <= 0:
        logger.warning("execute_buy_order_skipped ticker=%s reason=quantity_zero", ticker)
        _log_buy_skip(log_path, today, ticker, "quantity_zero")
        return portfolio

    # 주문 직전/직후의 누적 체결 집계를 사이에 두고 재면 이 주문 하나의 체결
    # 수량·금액이 정확히 나온다 — 같은 종목을 같은 날 두 번 매수해도(추가매수)
    # 섞이지 않는다. 집계 하나만 사후 조회하면 두 건이 합산돼버린다.
    fills_before = await asyncio.to_thread(kis.fetch_daily_fill_totals, ticker, _kst_today(), "buy")

    try:
        order_no = await asyncio.to_thread(kis.place_market_buy_order, ticker, quantity)
    except kis.OrderResponseLost:
        # 주문은 나갔는데 응답을 못 받았다. 재전송하면 두 번 사므로 하지 않는다 —
        # 실제 접수 여부는 아래에서 원장(체결 조회)으로 가른다. 여기서 그냥
        # 포기해버리면 브로커엔 포지션이 있는데 우리 상태엔 없는, 손절·익절
        # 대상에서 통째로 빠진 보유가 생긴다.
        logger.error("execute_buy_order_response_lost ticker=%s quantity=%d", ticker, quantity)
        order_no = None
    else:
        if order_no is None:
            # 브로커가 응답으로 거부했다 — 주문이 안 나간 게 확실하다.
            logger.error("execute_buy_order_failed ticker=%s reason=order_rejected", ticker)
            _log_buy_skip(log_path, today, ticker, "order_rejected")
            return portfolio

    # 진입가는 손절·익절 판정의 유일한 기준점이라 반드시 **실제 체결가**여야 한다.
    # 주문 직전 호가로 폴백하면 그 오차가 포지션 수명 내내 남는다 — 192820이
    # 호가 210,000에 주문돼 232,000에 체결됐는데 진입가는 210,000으로 남았고,
    # 그 결과 실제 +6.6% 지점에서 익절이 +20%로 오판돼 발동했다(2026-08-15 실측).
    # 그래서 브로커 쪽 출처를 두 개 순서대로 쓰고, 호가는 마지막 수단으로 민다.
    # 주문 수량이 다 잡힐 때까지 기다렸다가 잰다(kis.fill_after_order, 2026-08-18).
    # 매수는 체결가만 쓰므로 부분 체결이어도 평균가는 크게 안 틀리지만, 여러 단가로
    # 나뉘어 체결된 주문의 앞부분만 보면 그 오차가 포지션 수명 내내 진입가에 남는다.
    fill = await asyncio.to_thread(
        kis.fill_after_order, ticker, _kst_today(), "buy", fills_before, quantity
    )
    if order_no is None and fill is None:
        # 응답 유실 + 원장에 체결 흔적 없음 = 주문이 접수되지 않았다고 본다.
        # 체결이 잡혔다면(fill is not None) 주문은 살아 있는 것이므로 아래 정상
        # 경로로 그대로 내려가 포지션으로 기록된다.
        logger.error("execute_buy_order_failed ticker=%s reason=order_response_lost", ticker)
        _log_buy_skip(log_path, today, ticker, "order_response_lost")
        return portfolio

    fill_price = fill.price if fill is not None else None
    entry_price_source = "fill"
    if fill_price is None:
        # 전후 집계 차를 못 구한 경우(조회 실패 등) — 그날 매수 집계 평균으로 물러선다.
        # 같은 날 추가매수가 있으면 섞이지만, 호가보다는 훨씬 낫다.
        fill_price = await asyncio.to_thread(kis.fetch_fill_price, ticker, _kst_today())
        entry_price_source = "daily_avg"
    if fill_price is None:
        # 체결 직후라 일별체결 집계에 아직 안 잡혔을 수 있다 — 잔고의 매입평균가가
        # 2차 출처다. 방금 산 종목이라 보유분 평균가 = 이 주문의 체결가다.
        fill_price = await asyncio.to_thread(kis.fetch_position_avg_price, ticker)
        entry_price_source = "position_avg"
    if fill_price is None:
        # 체결가를 끝내 못 구했다. 그래도 주문은 이미 나갔으므로 포지션을 리스크
        # 관리 대상에서 빼지는 않는다(entry_price=None이면 매도 평가 자체가 스킵된다).
        # 대신 호가 근사치임을 로그와 매매일지에 남겨 나중에 구분할 수 있게 한다.
        fill_price = current_price
        entry_price_source = "quote_fallback"
        logger.error(
            "execute_buy_order_entry_price_unverified ticker=%s reason=fill_and_balance_both_unavailable "
            "using_quote=%.2f",
            ticker,
            current_price,
        )
    entry_price = fill_price

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
        # entry_day는 최초 진입일 그대로 둔다 — 추가매수로 갱신하면 보유기간이 매번 리셋된다.
    else:
        positions.append(
            Position(
                ticker=ticker,
                sector=sector,
                weight=trade_weight,
                entry_day=today,
                entry_price=entry_price,
                peak_price=entry_price,
                quantity=quantity,
                # 진입 시점에 확정된 출구 규칙을 여기서 한 번 박고 끝이다. 추가매수
                # 경로(existing is not None)에서는 일부러 건드리지 않는다 — 나중에
                # 다시 정할 수 있게 두면 "물타기하면서 손절선도 같이 넓히는" 경로가
                # 열린다. 기존 포지션의 규칙이 그대로 이긴다.
                exit_plan=decision.exit_plan,
            )
        )

    _append_log(
        log_path,
        {
            "event": "buy",
            "day": today.isoformat(),
            "ticker": ticker,
            "sector": sector,
            "quantity": quantity,
            "entry_price": entry_price,
            # 진입가를 어디서 얻었는지 — "fill"(일별체결 집계) / "position_avg"(잔고
            # 매입평균가) / "quote_fallback"(둘 다 실패해 호가로 근사). 마지막 것은
            # 손절·익절 기준이 실제 원가와 다를 수 있다는 뜻이라 반드시 구분해서
            # 남긴다(2026-08-15, 192820 오익절 건 이후).
            "entry_price_source": entry_price_source,
            "order_no": order_no,
            "gap_pct": round(gap_pct, 4),
            # 이 포지션에 박힌 출구 규칙. None이면 고정 기본값(degraded 판단이거나
            # 추가매수라 기존 규칙 유지). 나중에 "LLM이 정한 출구가 고정값보다
            # 나았나"를 가르는 기준이 이 필드다.
            "exit_plan": decision.exit_plan.model_dump(mode="json") if decision.exit_plan else None,
            "decision": decision.model_dump(mode="json"),
        },
    )
    # decision.reason은 영어 그대로 로그에 남고(위 _append_log), 텔레그램에 보일
    # 때만 번역한다(src/translate.py, 사용자 요청 2026-08-13).
    reason_ko = await translate.to_korean(decision.reason, label="translate_buy_reason")
    notify.send_telegram_alert(notify.format_buy_alert(display_name(ticker), entry_price, quantity, reason_ko))

    return PortfolioState(
        positions=positions,
        cash_weight=portfolio.cash_weight - trade_weight,
    )


def _append_log(log_path: Path, entry: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def display_name(ticker: str) -> str:
    """텔레그램 알림에 종목코드 대신 종목명을 보여주려고 쓴다(사용자 요청,
    2026-08-09). collectors.fetch_kospi200_ticker_names()가 30일 캐시라 대부분
    파일 읽기 한 번으로 끝난다. 실패해도 코드로 폴백할 뿐 알림 자체를 막지 않는다."""
    names = collectors.fetch_kospi200_ticker_names()
    return (names or {}).get(ticker, ticker)


def _log_buy_skip(log_path: Path, day, ticker: str, reason: str, **extra) -> None:
    """게이트는 승인했는데 execute_buy_order 단계(가격 갭·잔고·주문거부 등)에서
    실제 체결까지는 못 간 경우를 남긴다. 이걸 안 남기면 pipeline.jsonl엔
    approved=true로만 남아 실제로는 안 산 걸 산 것처럼 보인다 — trade_journal.jsonl
    쪽에서 "매수"가 아니라 "buy_skipped"로 구분해서, 매매일지·일일 리포트에
    "특별한 일"로 드러나게 한다."""
    _append_log(log_path, {"event": "buy_skipped", "day": day.isoformat(), "ticker": ticker, "reason": reason, **extra})
    reason_label = notify.REASON_LABELS.get(reason, reason)
    detail = f"{extra['gap_pct']:.1%}" if "gap_pct" in extra else ""
    notify.send_telegram_alert(notify.format_buy_skipped_alert(display_name(ticker), reason_label, detail))


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
                "reason": decision.reason,
                # 분석가가 빠진 채 나온 판단인가(스키마 계약의 Decision.degraded), 그리고
                # 실제로 어떤 분석가가 기여했는가. 이 두 필드가 없어서 2026-08-24 점검 때
                # "매수 판단이 정상인가"에 답하려고 분석가 커버리지를 llm_calls.jsonl
                # 타임스탬프로 역산해야 했다 — 결정 로그만으로는 알 수 없었다.
                "degraded": decision.degraded,
                "analysts": sorted(o.agent for o in decision.inputs),
                # HOLD에도 남긴다. 매니저는 매수 여부와 무관하게 출구 계획을 내는데
                # (prompts/portfolio_manager.md), 지금까지 그 값은 매수가 성사된
                # 종목에서만 기록됐다. 그래서 "LLM이 종목마다 손절폭을 실제로 다르게
                # 잡는가, 아니면 늘 같은 숫자를 뱉는가"를 확인할 방법이 없었다 —
                # 매수 없는 날이 정상인 시스템에서는 표본이 거의 안 쌓인다.
                "exit_plan": decision.exit_plan.model_dump(mode="json") if decision.exit_plan else None,
            },
        )

    return portfolio, results


def record_observed_prices(
    price_by_ticker: dict[str, float], *, path: Path | None = None
) -> None:
    """이번 회차에 실제로 관측한 가격으로 종목별 당일 최저/최고를 갱신한다.

    날짜가 바뀌면 통째로 새로 시작한다 — 어제 관측 범위를 오늘 일봉과 대조하면
    아무 의미가 없다(notify._read_blackout이 날짜 경계에서 이어붙이지 않는 것과
    같은 이유). 기록 실패가 손절 판정을 막지 않는다.
    """
    path = path or DEFAULT_OBSERVED_RANGE_PATH
    if not price_by_ticker:
        return

    today = _kst_today().isoformat()
    try:
        state = json.loads(path.read_text())
        if state.get("day") != today:
            state = {"day": today, "observed": {}}
    except (OSError, ValueError):
        state = {"day": today, "observed": {}}

    observed = state.setdefault("observed", {})
    for ticker, price in price_by_ticker.items():
        seen = observed.get(ticker)
        if seen is None:
            observed[ticker] = {"min": price, "max": price, "rounds": 1}
        else:
            seen["min"] = min(seen["min"], price)
            seen["max"] = max(seen["max"], price)
            seen["rounds"] = seen.get("rounds", 0) + 1

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError as exc:
        logger.warning("observed_range_write_failed path=%s error=%s", path, exc)


def load_observed_range(*, path: Path | None = None) -> dict[str, dict]:
    """오늘 것만 돌려준다. 날짜가 다르면 빈 dict — 낡은 관측으로 대조하면 안 된다."""
    path = path or DEFAULT_OBSERVED_RANGE_PATH
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if state.get("day") != _kst_today().isoformat():
        return {}
    return state.get("observed") or {}


async def audit_observation_gap(
    portfolio: PortfolioState, day: datetime, *, path: Path | None = None
) -> list[str]:
    """장 마감 후 한 번. **시장은 문턱을 지났는데 우리는 그 순간을 못 본** 종목을 찾는다.

    일봉이 문턱 통과라고 말하는데 그날 관측 범위로는 통과가 아니면, 그 문턱은
    두 샘플 사이로 지나간 것이다. 이게 안전장치가 "조용했다"와 "못 봤다"를
    가르는 마지막 조각이다 — 시세 공백(blackout)은 전멸한 회차를 잡지만, 이건
    **회차가 정상으로 다 돈 날에도** 생기는 구멍이다.

    관측 기록이 없는 종목은 판정하지 않는다. 오늘 산 종목이나 상태 파일이 날아간
    경우인데, 둘 다 "안 넘었다"가 아니라 "모른다"다.
    """
    observed = load_observed_range(path=path)
    if not observed:
        return []

    targets = [p for p in portfolio.positions if p.entry_price is not None and p.ticker in observed]
    if not targets:
        return []

    bars_list = await asyncio.gather(
        *(asyncio.to_thread(kis.fetch_daily_ohlcv, p.ticker, 1) for p in targets),
        return_exceptions=True,
    )

    missed: list[str] = []
    for position, bars in zip(targets, bars_list):
        if isinstance(bars, BaseException) or not bars:
            continue
        bar = bars[-1]
        if bar.date != day.date():
            continue

        by_market = sell.threshold_crossed_in_bar(position, bar)
        if by_market is None:
            continue

        # 우리가 본 범위만으로 같은 판정을 다시 한다. 관측 범위가 이미 문턱을
        # 넘었다면 안전장치는 볼 기회가 있었던 것이고(그래서 팔았거나, 그 회차에
        # 판 뒤 stage가 올라갔거나다) 여기서 보고할 일이 아니다.
        seen = observed[position.ticker]
        seen_bar = OHLCVBar(
            date=bar.date, open=seen["max"], high=seen["max"], low=seen["min"], close=seen["min"], volume=0
        )
        if sell.threshold_crossed_in_bar(position, seen_bar) is not None:
            continue

        label = notify.REASON_LABELS.get(by_market, by_market)
        missed.append(f"{display_name(position.ticker)} {label}")
        logger.error(
            "observation_gap_missed_threshold ticker=%s reason=%s bar_low=%.0f bar_high=%.0f "
            "seen_low=%.0f seen_high=%.0f rounds=%d",
            position.ticker,
            by_market,
            bar.low,
            bar.high,
            seen["min"],
            seen["max"],
            seen.get("rounds", 0),
        )

    logger.info("observation_gap_audit_done checked=%d missed=%d", len(targets), len(missed))
    return missed


async def _audit_blackout_window(
    portfolio: PortfolioState, day: datetime
) -> tuple[int, int, list[str]]:
    """시세 공백이 끝난 직후, 그 구간에 문턱을 넘은 종목이 있었는지 일봉으로 판정한다.

    돌려주는 것은 (판정 대상 수, 실제로 확인한 수, 닿은 종목 설명)이다. 확인한
    수를 따로 세는 이유는 일봉 조회도 같은 KIS를 쓰기 때문이다 — 복구 직후라 또
    실패할 수 있고, 그때 "닿은 종목 없음"이라고 알리면 확인하지도 않은 것을
    안전하다고 말하는 게 된다. 공백 알림이 존재하는 이유가 바로 그 혼동을 없애는
    것이라(evaluate_holdings 주석), 여기서 같은 실수를 하면 안 된다.

    조회는 FAST_FAIL_POLICY로 짧게 끝낸다. 이 함수는 매분 도는 회차 안에서 돌고,
    회차가 길어지면 다음 분이 락에 막혀 공백이 오히려 늘어난다.
    """
    targets = [p for p in portfolio.positions if p.entry_price is not None]
    if not targets:
        return 0, 0, []

    results = await asyncio.gather(
        *(
            asyncio.to_thread(kis.fetch_daily_ohlcv, p.ticker, 1, policy=kis.FAST_FAIL_POLICY)
            for p in targets
        ),
        return_exceptions=True,
    )

    checked = 0
    crossed: list[str] = []
    for position, bars in zip(targets, results):
        if isinstance(bars, BaseException) or not bars:
            logger.warning("blackout_audit_bar_unavailable ticker=%s", position.ticker)
            continue
        bar = bars[-1]
        # 오늘 일봉이 아직 안 잡혔으면 그 종목은 판정하지 않는다 — 전날 봉으로
        # 재면 오늘 공백과 무관한 답이 나온다.
        if bar.date != day.date():
            logger.warning(
                "blackout_audit_stale_bar ticker=%s bar_date=%s day=%s",
                position.ticker,
                bar.date,
                day.date(),
            )
            continue
        checked += 1
        reason = sell.threshold_crossed_in_bar(position, bar)
        if reason is not None:
            label = notify.REASON_LABELS.get(reason, reason)
            crossed.append(f"{display_name(position.ticker)} {label}")
            logger.error(
                "blackout_audit_threshold_crossed ticker=%s reason=%s low=%.0f high=%.0f",
                position.ticker,
                reason,
                bar.low,
                bar.high,
            )

    logger.info(
        "blackout_audit_done total=%d checked=%d crossed=%d", len(targets), checked, len(crossed)
    )
    return len(targets), checked, crossed


async def evaluate_holdings(
    portfolio: PortfolioState,
    day: datetime,
    sell_execute_fn: SellExecuteFn,
    analyst_fn: AnalystFn | None = None,
    judge_sell_fn: JudgeSellFn | None = None,
    log_path: Path = DEFAULT_SELL_LOG_PATH,
    trade_journal_log_path: Path = DEFAULT_TRADE_JOURNAL_LOG_PATH,
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

    # 이 경로만 FAST_FAIL_POLICY다 — 매분 도는 크론이 곧 재시도 루프라 회차 안에서
    # 오래 버틸수록 다음 분이 락에 막혀 공백만 길어진다(kis.FAST_FAIL_POLICY 주석).
    price_results = await asyncio.gather(
        *(
            asyncio.to_thread(kis.fetch_current_price, p.ticker, policy=kis.FAST_FAIL_POLICY)
            for p in portfolio.positions
        ),
        return_exceptions=True,
    )

    price_by_ticker: dict[str, float] = {}
    for position, price in zip(portfolio.positions, price_results):
        if isinstance(price, BaseException) or price is None:
            logger.warning("evaluate_holdings_price_unavailable ticker=%s", position.ticker)
            continue
        price_by_ticker[position.ticker] = price

    record_observed_prices(price_by_ticker)

    # 한 종목도 못 받았으면 이 회차는 손절·익절을 **아무것도 판정하지 않았다** —
    # 문턱을 넘지 않아서 조용한 것과 눈을 감아서 조용한 것은 전혀 다른데, 지금까지
    # 둘 다 똑같이 조용했다. 2026-08-20 15:17~15:27에 KIS 시세가 통째로 죽어
    # 장 마감 직전 11분간 안전장치가 한 번도 판정을 못 했고, 그 사실이 로그
    # 안쪽에만 남아 아무도 몰랐다. 종목별 WARNING은 정상 장애에서도 흔해서
    # 알림 기준으로 못 쓴다 — "전부 실패"만 알린다.
    if not price_by_ticker:
        logger.error(
            "evaluate_holdings_all_prices_unavailable positions=%d — 이번 회차 손절/익절 판정 없음",
            len(portfolio.positions),
        )
        notify.alert_once_per_day(
            "holdings_all_prices_unavailable",
            notify.format_error_alert(
                "보유 종목 시세를 하나도 못 받아 손절/익절 판정을 못 했습니다 (이 회차)",
                f"보유 {len(portfolio.positions)}종목 전부 시세 조회 실패 — KIS 장애 의심. "
                "복구될 때까지 매분 체크가 계속 헛돕니다(공백이 길어지면 다시 알립니다).",
            ),
        )

    # 하루 1회 알림은 "시작됐다"까지만 알린다. 얼마나 길어지고 있는지는 따로 센다 —
    # 2026-08-21에 13:02 알림 하나로 하루치를 써버려 15:07~15:29의 23분 공백이
    # 무알림으로 지나갔다(notify.track_blackout 주석).
    blackout = notify.track_blackout(BLACKOUT_CONTEXT, not price_by_ticker)
    if blackout is not None:
        if blackout.kind == "escalated":
            logger.error("evaluate_holdings_blackout_escalated minutes=%.1f", blackout.minutes)
            notify.send_telegram_alert(
                notify.format_blackout_escalation_alert(blackout.minutes, len(portfolio.positions))
            )
        else:
            logger.info("evaluate_holdings_blackout_recovered minutes=%.1f", blackout.minutes)
            # 복구 직후에 바로 일봉을 받아 사후 판정한다(사용자 확정, 2026-08-27).
            # 감사가 통째로 실패해도 복구 알림 자체는 반드시 나가야 한다 — 알림을
            # 잃는 것이 알림 내용이 부실한 것보다 나쁘다.
            try:
                audit_total, audit_checked, audit_crossed = await _audit_blackout_window(portfolio, day)
            except Exception as exc:  # noqa: BLE001 - 감사 실패로 복구 알림을 막지 않는다
                logger.warning("blackout_audit_failed error=%s", exc)
                audit_total, audit_checked, audit_crossed = len(portfolio.positions), 0, []
            notify.send_telegram_alert(
                notify.format_blackout_recovery_alert(
                    blackout.minutes, audit_total, audit_checked, audit_crossed
                )
            )

    sells = 0
    for ticker, current_price in price_by_ticker.items():
        position = next(p for p in portfolio.positions if p.ticker == ticker)
        position = sell.update_peak_price(position, current_price)
        portfolio = PortfolioState(
            positions=[position if p.ticker == ticker else p for p in portfolio.positions],
            cash_weight=portfolio.cash_weight,
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

        portfolio = await finalize_sell(
            portfolio, action, position, current_price, day, sell_execute_fn, log_path, trade_journal_log_path
        )
        sells += 1

    # 성공한 회차도 한 줄 남긴다. 이게 없으면 "문턱을 안 넘어서 조용한 회차"와
    # "아예 안 돈 회차"가 로그에서 완전히 같은 모양이다 — 2026-08-21 장애를
    # 사후 분석할 때 실패 로그의 *부재*로 성공을 역추정해야 했다. 매분 한 줄씩
    # 늘지만(하루 약 390줄) 안전장치가 실제로 돌았다는 유일한 증거다.
    logger.info(
        "evaluate_holdings_done positions=%d priced=%d sells=%d",
        len(portfolio.positions),
        len(price_by_ticker),
        sells,
    )
    return portfolio


def _sell_amount(fill: FillRecord | None, shares_sold: int | None, current_price: float) -> float | None:
    """매매일지에 적을 매도 총액. 출처 우선순위는 체결 원본 > 체결 단가 x 수량 > 호가.

    가운데 단계가 필요한 이유: 체결 조회가 주문 수량을 다 못 따라잡으면 브로커
    총액도 그만큼 작다. 2026-08-18 036570은 31주가 다 나갔는데 19주분
    4,364,500원만 잡혀 2,756,500원이 일지에서 빠졌다. 관측된 평균 단가
    (229,710원)는 실제 평균(229,710원)과 거의 같았으므로, 단가에 실제 수량을
    곱하는 쪽이 훨씬 정확하다.
    """
    if fill is not None and fill.complete:
        return fill.amount
    if shares_sold:
        unit_price = fill.price if fill is not None else current_price
        return shares_sold * unit_price
    return None


async def finalize_sell(
    portfolio: PortfolioState,
    action: SellAction,
    position: Position,
    current_price: float,
    day: datetime,
    sell_execute_fn: SellExecuteFn,
    log_path: Path,
    trade_journal_log_path: Path,
) -> PortfolioState:
    """이미 정해진 SellAction 하나를 집행 + 로그 + 알림까지 마무리한다.

    evaluate_holdings(판단과 동시에 집행, C의 1분 체크가 씀)와 execute_open.py
    (전날 장 마감 후 정해둔 LLM 재량 매도를 다음날 장 시작 때 집행, D/B 분리)가
    이 함수를 공유한다 — "액션이 이미 정해졌을 때 어떻게 집행하고 남기는가"는
    판단이 언제 내려졌는지와 무관하게 항상 같아야 한다.
    """
    portfolio, fill = await sell_execute_fn(portfolio, action, current_price)

    # 매매일지에 적히는 값은 판단 시점 호가가 아니라 **실제 체결 내역**이다
    # (사용자 확정 2026-08-15). 시장가 주문이라 둘은 항상 다를 수 있고, 호가를
    # 적으면 실현손익률·매도금액이 전부 실제와 어긋난다. 체결 조회가 실패했거나
    # 시뮬레이션 경로라 체결 자체가 없으면 호가로 밀되, 그 사실을 출처로 남긴다.
    exit_price = fill.price if fill is not None else current_price
    if fill is None:
        exit_price_source = "quote_fallback"
    elif fill.complete:
        exit_price_source = "fill"
    else:
        # 덜 잡힌 체결이라도 가중평균 단가 자체는 실제 체결의 일부라 호가보다 훨씬
        # 낫다. 다만 총액은 못 쓴다 — 아래 sell_amount에서 단가 x 수량으로 되세운다.
        exit_price_source = "fill_partial"

    _append_log(
        log_path,
        {
            "day": day.date().isoformat(),
            "ticker": position.ticker,
            "reason": action.reason,
            "sell_fraction": action.sell_fraction,
            "price": exit_price,
        },
    )

    # 요청 비율(action.sell_fraction)이 아니라 집행 전후 상태 차이에서 실제로 빠진
    # 양을 뽑는다 — 주식수 내림 때문에 요청과 실제가 다를 수 있고, 매매일지엔 실제로
    # 일어난 일이 적혀야 한다. 포지션이 통째로 사라졌으면 전량 매도된 것이다.
    after = next((p for p in portfolio.positions if p.ticker == action.ticker), None)
    weight_sold = position.weight - (after.weight if after else 0.0)
    # 체결 조회가 주문 수량을 다 못 따라잡았으면(complete=False) fill.quantity는 실제보다
    # 작다 — 그 값을 쓰면 일지가 "31주 중 19주 매도, 잔여 0주" 같은 모순이 된다
    # (2026-08-18 036570 실측). 그럴 땐 집행 전후 상태 차이가 더 믿을 만한 출처다.
    fill_covers_order = fill is not None and fill.complete
    shares_sold = fill.quantity if fill_covers_order else None
    if shares_sold is None and position.quantity is not None:
        shares_sold = position.quantity - (after.quantity or 0 if after else 0)

    # **이 종목 보유분을 100%로 놓고** 이번에 몇 %를 팔았고 몇 %가 남았는지
    # (사용자 요청 2026-08-15). 포트폴리오 전체 대비 비중으로 적으면 "1.19% 축소"
    # 같은 값이 되는데, 그건 읽는 사람에게 아무 의미가 없다 — 일지는 사람이 읽으라고
    # 쓰는 것이다. 두 값의 합은 항상 1.0이라 한 행만 보고도 해석된다.
    # 실제 주식수 기준이 가장 명확하지만, 주식수를 추적 안 하는 시뮬레이션 경로에서는
    # 비중 비율로 떨어진다.
    shares_before = position.quantity
    shares_after = (after.quantity if after else 0) or 0
    if shares_before:
        position_fraction_sold = (shares_sold or 0) / shares_before
        # 잔여는 1 - 매도로 되계산하지 않고 **잔여 주식수에서 직접** 뽑는다. 되계산하면
        # 매도 수량이 어떤 이유로든 틀렸을 때 잔여까지 같이 틀리면서 합만 1.0으로
        # 맞아떨어져, 한 행만 봐서는 틀린 걸 알 수 없다 — 실제로 "38.7% 잔여"와
        # "잔여 0주"가 같은 행에 실렸다(2026-08-18 036570).
        position_fraction_remaining = shares_after / shares_before
    elif position.weight:
        position_fraction_sold = weight_sold / position.weight
        position_fraction_remaining = 1.0 - position_fraction_sold
    else:
        position_fraction_sold = None
        position_fraction_remaining = None

    realized_pnl_pct = (
        (exit_price - position.entry_price) / position.entry_price if position.entry_price else None
    )
    holding_days = (day.date() - position.entry_day).days if position.entry_day else None
    _append_log(
        trade_journal_log_path,
        {
            "event": "sell",
            "day": day.date().isoformat(),
            "ticker": position.ticker,
            "reason": action.reason,
            "reasoning": action.reasoning,
            "sell_fraction": action.sell_fraction,
            "exit_price": exit_price,
            # "fill"이면 브로커 체결 원본, "quote_fallback"이면 호가 근사치라
            # 실현손익률·매도금액이 실제와 다를 수 있다는 뜻이다.
            "exit_price_source": exit_price_source,
            "decision_price": current_price,  # 판단 시점 호가 — 체결가와의 슬리피지 추적용
            "entry_price": position.entry_price,
            "realized_pnl_pct": realized_pnl_pct,
            "holding_days": holding_days,
            # 매도를 발동시킨 값 자체. 이게 없으면 "왜 지금 팔렸나"를 사후에 되짚을 수
            # 없다 — 트레일링은 진입가가 아니라 고점 대비로 발동하는데, 고점은 부분
            # 익절마다 리셋돼서 나중에는 어디에도 안 남는다. 매도 시점의 값을 그대로 박는다.
            "peak_price": position.peak_price,
            "take_profit_stage": position.take_profit_stage,
            # 이 종목 보유분을 100%로 놓은 비율 — 매매일지에 보이는 건 이 값이다.
            "position_fraction_sold": position_fraction_sold,
            "position_fraction_remaining": position_fraction_remaining,
            "shares_before": shares_before,
            "shares_after": shares_after,
            # 포트폴리오 전체 대비 비중. 사람이 읽는 값은 위쪽이고 이건 나중에
            # 포트폴리오 단위로 분석할 때 쓰려고 남겨둔다.
            "portfolio_weight_sold": weight_sold,
            "portfolio_weight_before": position.weight,
            "portfolio_weight_after": after.weight if after else 0.0,
            "shares_sold": shares_sold,
            # 체결 총액은 브로커 원본을 그대로 쓴다 — 수량 x 단가로 되계산하면
            # 부분 체결이 여러 단가로 나뉘었을 때 어긋난다. 단, 조회가 주문 수량을
            # 다 못 따라잡았으면 그 총액은 실제보다 작으므로 쓰지 않고, 관측된
            # 평균 단가에 실제 매도 수량을 곱해 되세운다.
            "sell_amount": _sell_amount(fill, shares_sold, current_price),
            "sell_amount_source": (
                "fill" if fill_covers_order else ("estimated" if fill is not None else "quote_fallback")
            ),
            # 이 포지션에 실제로 적용된 출구 규칙 — LLM이 정한 값인지 고정 기본값인지
            # 나중에 성과를 갈라볼 때 필요하다.
            "exit_plan": position.exit_plan.model_dump(mode="json") if position.exit_plan else None,
        },
    )
    # action.reasoning(LLM 재량매도일 때만 있음)도 로그엔 영어 원문 그대로 남고
    # (위 _append_log), 텔레그램에 보일 때만 번역한다.
    reasoning_ko = await translate.to_korean(action.reasoning, label="translate_sell_reasoning")
    notify.send_telegram_alert(
        notify.format_sell_alert(
            display_name(position.ticker),
            notify.REASON_LABELS.get(action.reason, action.reason),
            exit_price,  # 알림에도 호가가 아니라 체결가를 보여준다
            realized_pnl_pct,
            reasoning=reasoning_ko,
        )
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


def load_decision_entries(log_path: Path) -> list[dict]:
    """pipeline.jsonl을 읽고 (day, ticker) 중복을 제거한다 — 나중 기록을 남긴다.

    2026-08-12에 같은 37종목 유니버스가 두 번 완주해 74개 레코드가 남았다(그날
    decide_buys가 UTC 날짜로 시작한 버그와 같은 시기다. 8/14부터 정상). 두 실행은
    승인 종목까지 달랐고(298050 vs 282330), `decide_buys_done`과 매매일지는 **뒤쪽**
    실행과 일치한다 — 실제로 집행된 쪽이다. 그래서 나중 기록을 남긴다.

    원본 로그는 덮어쓰지 않는다. 그날 무슨 일이 있었는지는 이 파일이 유일한 기록이라
    (CHANGELOG 2026-08-24) 지우지 않고 읽는 쪽에서 걸러낸다.
    """
    if not log_path.exists():
        return []
    entries = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    deduped: dict[tuple[str, str], dict] = {}
    for e in entries:
        deduped[(e["day"], e["ticker"])] = e
    return list(deduped.values())


def summarize_recent_trading_days(log_path: Path, n_days: int) -> dict:
    """cron이 매일 같은 파일에 계속 이어 쓰는 실환경용 래퍼.

    summarize_log(total_days=N)는 파일 안의 모든 기록을 N으로 나누는데, 이는
    "정확히 N일치만 담긴 로그"를 전제한다(마일스톤1 시뮬레이션·실측 테스트가 그렇게
    썼다). 실제 cron 로그는 배포 이후 계속 누적되므로 그 가정이 깨진다 — 최근
    n_days개의 서로 다른 날짜만 추려서 그 위에서 같은 집계를 돌린다. 로그에 아직
    n_days보다 적은 날짜만 쌓여있으면 있는 만큼만으로 계산한다(허수로 채우지 않음).
    """
    if not log_path.exists():
        return {"total_days": 0, "signal_days": 0, "signal_day_ratio": 0.0, "rejected_by_counts": {}}

    entries = load_decision_entries(log_path)

    distinct_days = sorted({e["day"] for e in entries})
    recent_days = set(distinct_days[-n_days:])
    recent_entries = [e for e in entries if e["day"] in recent_days]

    signal_days: set[str] = set()
    rejected_by_counts: dict[str, int] = {}
    for e in recent_entries:
        if e["action"] == "BUY" and e["approved"]:
            signal_days.add(e["day"])
        if not e["approved"] and e["rejected_by"]:
            rejected_by_counts[e["rejected_by"]] = rejected_by_counts.get(e["rejected_by"], 0) + 1

    total_days = len(recent_days)
    return {
        "total_days": total_days,
        "signal_days": len(signal_days),
        "signal_day_ratio": (len(signal_days) / total_days) if total_days else 0.0,
        "rejected_by_counts": rejected_by_counts,
    }


def summarize_llm_calls(log_path: Path, since: datetime | None = None) -> dict:
    """분석가/토론/매니저(label)별 호출수·실패율·토큰사용량 집계 (CLAUDE.md "감시 지표").

    llm.call_structured가 매 호출마다 logs/llm_calls.jsonl에 남기는 원본을 읽는다.
    `since`를 주면 그 시각 이후 기록만 본다 — run_daily.py는 "최근 20영업일"과
    같은 창을 쓰기 위해 넘긴다.
    """
    if not log_path.exists():
        return {}

    lines = [line for line in log_path.read_text().splitlines() if line.strip()]
    entries = [json.loads(line) for line in lines]
    if since is not None:
        entries = [e for e in entries if datetime.fromisoformat(e["timestamp"]) >= since]
    entries = [e for e in entries if not e["label"].startswith(NON_PRODUCTION_LABEL_PREFIX)]

    by_label: dict[str, dict] = {}
    for e in entries:
        stats = by_label.setdefault(
            e["label"], {"calls": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0}
        )
        stats["calls"] += 1
        if not e["success"]:
            stats["failures"] += 1
        stats["input_tokens"] += e["input_tokens"]
        stats["output_tokens"] += e["output_tokens"]

    for stats in by_label.values():
        stats["failure_rate"] = stats["failures"] / stats["calls"] if stats["calls"] else 0.0

    return by_label


def log_monitoring_summary(
    decision_log_path: Path | None = None,
    llm_log_path: Path | None = None,
) -> None:
    """CLAUDE.md "감시 지표"를 하루 한 번 cron 로그에 남긴다.

    읽기 전용 리포트다 — 판단 로직 어디에도 이 결과를 되먹이지 않는다. 되먹이는
    순간 "신호가 적으니 기준을 낮추자"는 경로가 생기고, 그게 이전 버전을 무너뜨린
    실패다(CLAUDE.md 서두).

    이 집계는 원래 run_daily.py(마일스톤1 모놀리스) 안에만 있었다. 스크립트를
    decide_buys/execute_open/check_stop_loss/decide_llm_sell로 쪼갤 때 딸려가지
    않아, 크론 어디에서도 호출되지 않은 채 2026-08-24까지 **한 번도 실행된 적이
    없었다**(cron.log에 monitoring_signal_rate 0회). 그래서 여기로 옮겨 장 마감
    작업(decide_llm_sell)에서 부른다.
    """
    # 기본값을 인자 기본값으로 묶지 않는다 — 정의 시점에 고정돼서 테스트가
    # 모듈 상수를 갈아끼워도 안 먹는다.
    if decision_log_path is None:
        decision_log_path = DEFAULT_LOG_PATH
    if llm_log_path is None:
        llm_log_path = llm.DEFAULT_LLM_CALL_LOG_PATH

    signal = summarize_recent_trading_days(decision_log_path, MONITORING_WINDOW_TRADING_DAYS)
    logger.info(
        "monitoring_signal_rate days=%d signal_days=%d signal_day_ratio=%.3f rejected_by=%s",
        signal["total_days"],
        signal["signal_days"],
        signal["signal_day_ratio"],
        signal["rejected_by_counts"],
    )

    since = datetime.now(timezone.utc) - timedelta(days=MONITORING_WINDOW_CALENDAR_DAYS)
    for label, stats in sorted(summarize_llm_calls(llm_log_path, since=since).items()):
        logger.info(
            "monitoring_llm_calls label=%s calls=%d failure_rate=%.3f input_tokens=%d output_tokens=%d",
            label,
            stats["calls"],
            stats["failure_rate"],
            stats["input_tokens"],
            stats["output_tokens"],
        )
