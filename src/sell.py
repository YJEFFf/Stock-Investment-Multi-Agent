"""보유 포지션에 대한 매도 로직 — 결정론적 안전장치(손절/트레일링 익절).

왜 매수와 다르게 설계하는가 (사용자 확정, 2026-08-09): 매수는 망설이면 현금
(안전)이지만, 매도는 망설이면 손실 종목을 계속 들고 있는 것이라 "판단 안 함 =
안전"이 성립하지 않는다. 그래서 순수 LLM 재량이 아니라, 코드가 무조건 실행하는
결정론적 안전장치를 먼저 둔다. LLM 재량 매도(분석가 재평가 기반 임의 매도)는
이 안전장치가 자리잡은 다음 단계로 남겨뒀다 — 아직 구현하지 않았다.

규칙 6과의 관계: 리스크 게이트는 "새 매수를 막는" 장치라 SellAction엔 적용되지
않는다 — 보유분을 줄이는 행위 자체가 이미 위험을 낮추는 방향이라 "한도 초과"라는
개념이 성립하지 않는다.

pipeline.py(신규 매수 오케스트레이션)와 분리한 이유: 보유 포지션 리스크 관리는
손절선·트레일링 로직을 손보는 이유로 고치게 되고, 매수 쪽은 분석가·정량 필터
조정이 이유가 된다 — 고치는 이유가 다르다.
"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from src import kis
from src.schemas import ExitPlan, FillRecord, PortfolioState, Position, SellAction

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")  # 체결 조회 주문일자는 KST 기준 (pipeline._kst_today와 같은 이유)

# 사용자 확정치 (2026-08-09). 2026-08-15부터 이 값들은 "고정 규칙"이 아니라
# **폴백 기본값**이다 — 진입 시 LLM이 종목별 출구 규칙(ExitPlan)을 정하고, 그게
# 없는 포지션만 이 값으로 떨어진다. LLM 자유도를 준 뒤에도 이 상수를 남겨두는
# 이유는 두 가지다: (1) degraded 판단이나 응답 파싱 실패 시 돌아올 자리가 필요하고,
# (2) 매도할 때마다 "고정 규칙이었다면 어땠을지"를 같이 기록해 LLM이 정한 출구가
# 상수보다 나은지 나중에 숫자로 검증하기 위해서다(pipeline.finalize_sell).
STOP_LOSS_PCT = -0.10  # 진입가 대비 평가손익 -10% 도달 시 전량 매도
TAKE_PROFIT_TRIGGER_PCT = 0.20  # 진입가 대비 +20%에서 첫 부분 익절 트리거
TAKE_PROFIT_SELL_FRACTION = 1.0 / 3.0  # 트리거마다 "현재" 보유량의 이 비율만큼 매도
TAKE_PROFIT_TRAIL_PCT = -0.07  # 고점 대비 이 비율만큼 조정 시 다음 단계 매도

DEFAULT_EXIT_PLAN = ExitPlan(
    stop_loss_pct=STOP_LOSS_PCT,
    take_profit_pct=TAKE_PROFIT_TRIGGER_PCT,
    take_profit_fraction=TAKE_PROFIT_SELL_FRACTION,
    trail_pct=TAKE_PROFIT_TRAIL_PCT,
)


def plan_for(position: Position) -> ExitPlan:
    """이 포지션에 적용할 출구 규칙. 진입 시 확정된 게 있으면 그걸 쓰고, 없으면
    고정 기본값으로 떨어진다 — 이 기능 이전에 열린 포지션도 그대로 돌아가야 한다."""
    return position.exit_plan or DEFAULT_EXIT_PLAN


def evaluate_deterministic_sell(position: Position, current_price: float) -> SellAction | None:
    """무조건 실행되는 안전장치 — 집행 시점엔 LLM 관여 없이 코드가 100% 결정한다.

    문턱 자체는 진입 시점에 LLM이 정했을 수 있지만(ExitPlan), 그건 이미 포지션에
    얼어붙은 숫자다. 여기서는 다시 묻지 않는다 — 손실 종목 앞에서 손절선을 재협상하는
    경로를 만들지 않는 게 이 설계의 핵심이다.

    진입가가 아직 없는 포지션(Position.entry_price is None)은 판단 자체를 하지
    않는다 — "판단 불가"와 "판단했으나 매도 안 함"은 다른 상태다(AnalystOpinion과
    같은 패턴). 손절이 익절보다 우선한다.
    """
    if position.entry_price is None:
        return None

    plan = plan_for(position)
    unrealized_pct = (current_price - position.entry_price) / position.entry_price

    if unrealized_pct <= plan.stop_loss_pct:
        return SellAction(ticker=position.ticker, reason="stop_loss", sell_fraction=1.0)

    if position.take_profit_stage == 0:
        if unrealized_pct >= plan.take_profit_pct:
            return SellAction(
                ticker=position.ticker, reason="take_profit_trail", sell_fraction=plan.take_profit_fraction
            )
        return None

    # 이미 1회 이상 부분 익절함 -> 그 이후엔 고점 대비 트레일링 스톱으로 감시한다.
    if position.peak_price is None:
        return None
    drawdown_from_peak = (current_price - position.peak_price) / position.peak_price
    if drawdown_from_peak <= plan.trail_pct:
        return SellAction(
            ticker=position.ticker, reason="take_profit_trail", sell_fraction=plan.take_profit_fraction
        )

    return None


def update_peak_price(position: Position, current_price: float) -> Position:
    """그날 관측한 가격으로 고점을 갱신한다. evaluate_deterministic_sell보다 먼저
    호출해 그날의 고점이 이미 반영된 상태에서 매도를 평가한다."""
    if position.entry_price is None:
        return position
    new_peak = max(position.peak_price or position.entry_price, current_price)
    if new_peak == position.peak_price:
        return position
    return position.model_copy(update={"peak_price": new_peak})


def execute_sell(portfolio: PortfolioState, action: SellAction, current_price: float) -> PortfolioState:
    """SellAction을 포트폴리오 상태에 반영하는 순수 함수. 실거래 API 호출 없음
    (규칙 7) — 무비용 시뮬레이션 경로다. 실제 KIS 주문까지 내려면
    execute_sell_order를 쓴다.

    트레일링 익절이 실행되면 다음 구간을 새 고점부터 추적하도록 peak_price를
    현재가로 리셋한다 — 안 그러면 같은 하락 하나로 여러 단계가 연달아 발동해버린다.
    잔여 비중이 사실상 0이면(부동소수 오차 감안) 포지션 자체를 제거한다.
    quantity가 채워져 있으면(실제 주문으로 연 포지션) 비중과 같은 비율로 줄인다 —
    실제 매도 없이 상태만 시뮬레이션하는 경로이므로 근사치다.
    """
    position = next((p for p in portfolio.positions if p.ticker == action.ticker), None)
    if position is None:
        return portfolio

    sold_weight = position.weight * action.sell_fraction
    remaining_weight = position.weight - sold_weight
    other_positions = [p for p in portfolio.positions if p.ticker != action.ticker]

    if remaining_weight <= 1e-9:
        return PortfolioState(
            positions=other_positions,
            cash_weight=portfolio.cash_weight + sold_weight,
            daily_pnl_pct=portfolio.daily_pnl_pct,
        )

    update = {"weight": remaining_weight}
    if position.quantity:
        update["quantity"] = position.quantity - int(position.quantity * action.sell_fraction)
    if action.reason == "take_profit_trail":
        update["take_profit_stage"] = position.take_profit_stage + 1
        update["peak_price"] = current_price

    return PortfolioState(
        positions=[*other_positions, position.model_copy(update=update)],
        cash_weight=portfolio.cash_weight + sold_weight,
        daily_pnl_pct=portfolio.daily_pnl_pct,
    )


async def execute_sell_simulated(
    portfolio: PortfolioState, action: SellAction, current_price: float
) -> tuple[PortfolioState, FillRecord | None]:
    """execute_sell()을 evaluate_holdings의 SellExecuteFn 인터페이스에 맞춘
    비동기 래퍼 — 무비용 시뮬레이션 경로.

    체결 기록은 항상 None이다. 실주문을 낸 적이 없으니 체결이라는 사실 자체가
    없고, 호가를 체결가인 척 지어내면 매매일지가 거짓이 된다."""
    return execute_sell(portfolio, action, current_price), None


async def execute_sell_order(
    portfolio: PortfolioState, action: SellAction, current_price: float
) -> tuple[PortfolioState, FillRecord | None]:
    """SellAction을 실제 KIS 모의투자 시장가 매도 주문으로 집행하고, **실제 체결
    내역**을 함께 돌려준다.

    체결 기록을 여기서 만들어 반환하는 이유: 주문을 낸 함수만이 "주문 직전/직후"의
    누적 체결 집계를 정확히 사이에 두고 잴 수 있다. 호출부가 나중에 조회하면 같은
    종목을 같은 날 두 번 매도했을 때 두 건이 섞여버린다(kis.fetch_daily_fill_totals
    docstring). 시장가 주문이라 판단 시점 호가와 체결가는 항상 다를 수 있고,
    매매일지에 적혀야 하는 건 체결가다(사용자 확정 2026-08-15).

    execute_sell()과 달리 실제 KIS API를 호출한다(모의투자만, 규칙 7).
    Position.quantity가 없으면(execute()의 순수 시뮬레이션 경로로 열린 포지션 —
    실제로 브로커에 주문이 나간 적이 없다) 팔 실주식이 없으므로 상태만
    execute_sell()로 갱신하고 실제 주문은 생략한다.
    """
    position = next((p for p in portfolio.positions if p.ticker == action.ticker), None)
    if position is None:
        return portfolio, None

    if not position.quantity:
        logger.warning(
            "execute_sell_order_simulated_only ticker=%s reason=no_real_quantity_tracked", action.ticker
        )
        return execute_sell(portfolio, action, current_price), None

    shares_to_sell = (
        position.quantity if action.sell_fraction >= 1.0 else int(position.quantity * action.sell_fraction)
    )
    if shares_to_sell <= 0:
        logger.warning("execute_sell_order_skipped ticker=%s reason=rounds_to_zero_shares", action.ticker)
        return portfolio, None

    today = datetime.now(KST).date()
    before = await asyncio.to_thread(kis.fetch_daily_fill_totals, action.ticker, today, "sell")

    order_no = await asyncio.to_thread(kis.place_market_sell_order, action.ticker, shares_to_sell)
    if order_no is None:
        logger.error("execute_sell_order_failed ticker=%s reason=order_rejected", action.ticker)
        return portfolio, None

    after = await asyncio.to_thread(kis.fetch_daily_fill_totals, action.ticker, today, "sell")
    fill = kis.fill_between(before, after)
    if fill is None:
        logger.warning(
            "execute_sell_order_fill_unverified ticker=%s order_no=%s reason=fill_totals_unavailable_or_unchanged",
            action.ticker,
            order_no,
        )

    # 비중·고점 리셋도 호가가 아니라 실제 체결가 기준으로 한다.
    effective_price = fill.price if fill is not None else current_price

    # 요청 비율이 아니라 **실제로 판 주식수 비율**로 비중을 줄인다. 주식수는 내림이라
    # 4주 보유 중 1/3 익절이면 1주(25%)만 나가는데, 여기서 요청값 33.3%로 비중을
    # 깎으면 장부 비중이 실제 보유보다 작아지고 매매일지의 "줄인 비중"도 틀린 값이
    # 된다. 전량 매도(shares_to_sell == quantity)면 정확히 1.0이라 포지션이 제거된다.
    effective_fraction = shares_to_sell / position.quantity
    updated_portfolio = execute_sell(
        portfolio, action.model_copy(update={"sell_fraction": effective_fraction}), effective_price
    )
    # 주식수는 execute_sell의 비율 재계산(int(quantity * fraction))에 맡기지 않고 정확한
    # 값으로 덮어쓴다 — 3주 중 1주면 int(3 * (1/3))이 부동소수 오차로 0이 되어버린다.
    updated_positions = [
        p.model_copy(update={"quantity": position.quantity - shares_to_sell}) if p.ticker == action.ticker else p
        for p in updated_portfolio.positions
    ]
    return (
        PortfolioState(
            positions=updated_positions,
            cash_weight=updated_portfolio.cash_weight,
            daily_pnl_pct=updated_portfolio.daily_pnl_pct,
        ),
        fill,
    )
