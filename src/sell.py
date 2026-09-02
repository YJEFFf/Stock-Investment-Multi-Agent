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
from datetime import date, datetime
from zoneinfo import ZoneInfo

from src import kis
from src.schemas import ExitPlan, FillRecord, OHLCVBar, PortfolioState, Position, SellAction

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


def _threshold_crossed(position: Position, plan: ExitPlan, low: float, high: float) -> str | None:
    """저가~고가 구간이 문턱을 지났는지. 지났으면 사유, 아니면 None.

    **점 판정(현재가)과 구간 판정(당일 저가/고가)이 같은 함수를 쓴다** — low=high=현재가로
    부르면 예전 동작 그대로다. 둘을 따로 구현하면 사후 판정이 실제 안전장치와 어긋나고,
    그 어긋남은 "안 팔았어야 했는데 팔았다"로 나타나서 되돌릴 수가 없다.
    손절이 익절보다 우선한다.

    **트레일링은 구간으로 판정하지 않는다**(2026-09-02, 사용자 확정). 손절과 첫 익절은
    기준이 *진입가*라 하루 중 언제 찍힌 저가/고가든 상관없지만, 트레일링의 기준은
    *고점*이고 그 고점은 하루 사이에도 올라간다. 저가와 고점의 시간 순서를 모르는 채
    둘을 비교하면 **고점보다 먼저 찍힌 저가가 나중에 올라간 고점 대비 -N%가 되어**,
    가격이 오른 순간에 매도가 발동한다.

    2026-09-02 192820이 정확히 그랬다: 당일 저가 271,000은 09:00, 고가 292,000은
    13:28이었는데 13:29에 "고점 대비 -7.19%"로 팔렸다. 그 저가는 이미 09:01 매도로
    소진된 값이라 같은 저가로 두 번 판 셈이다. 오전 저가가 이후 고가보다 트레일 폭만큼
    아래이기만 하면 성립하므로 거의 매일 재발할 수 있었다.

    분당 샘플이 놓친 트레일링 하락은 이제 못 잡는다. 받아들이는 이유: 트레일링은 이미
    부분 익절을 한 번 이상 한 포지션에만 걸리므로 놓쳐도 손실이 아니라 이익 축소이고,
    점 판정은 매분 그대로 돈다. 손절은 반대로 놓치면 손실이라 구간 판정에 그대로 둔다.
    """
    entry = position.entry_price
    if entry is None:
        return None

    if (low - entry) / entry <= plan.stop_loss_pct:
        return "stop_loss"

    if position.take_profit_stage == 0 and (high - entry) / entry >= plan.take_profit_pct:
        return "take_profit"
    return None


def _action_for(position: Position, plan: ExitPlan, reason: str) -> SellAction:
    if reason == "stop_loss":
        return SellAction(ticker=position.ticker, reason="stop_loss", sell_fraction=1.0)
    return SellAction(
        ticker=position.ticker, reason="take_profit_trail", sell_fraction=plan.take_profit_fraction
    )


def evaluate_with_day_range(
    position: Position, quote: kis.Quote, *, today: date | None = None
) -> tuple[SellAction | None, bool]:
    """현재가로 먼저 판정하고, 못 잡았으면 **당일 저가/고가로 되짚는다.**
    돌려주는 두 번째 값은 "당일 범위로만 잡힌 것인가"다.

    왜 필요한가 (2026-08-27): 안전장치는 매분 현재가를 한 번 찍는데 시장은 연속이라,
    두 샘플 사이를 스쳐간 가격은 존재 자체를 모른다. 그날 192820의 저가 271,000은
    09:01 분봉 안에서만 찍혔고(그 분은 280,000에 열려 278,000에 닫혔다) 트레일 라인
    275,745를 지났는데도 매도가 나가지 않았다. 당일 저가/고가는 `inquire-price`
    응답에 원래 같이 오던 값이라(kis.Quote) **추가 조회 없이** 그 구간을 복원한다.

    **집행은 발견 시점의 현재가로 한다.** 꼬리 바닥(271,000)에 파는 게 아니라
    "문턱에 닿았으니 지금 정리한다"이다 — 사용자 확정(2026-08-27). 되돌아온 가격에
    파는 게 이상해 보일 수 있지만, 반대로 하면 이미 지나간 순간의 가격에 체결됐다고
    가정하게 되고 그건 사실이 아니다.

    **트레일링은 이 경로로 판정하지 않는다**(2026-09-02, 사용자 확정). 이유는
    _threshold_crossed docstring에 있다 — 저가와 고점의 시간 순서를 모르면 가격이
    오른 순간에 매도가 발동한다. 여기 남는 건 손절과 첫 익절뿐이고, 그 둘은 기준이
    진입가라 순서와 무관하다. 반복 발동 위험도 없다: 손절은 전량 매도라 포지션이
    사라지고, 첫 익절은 stage가 올라가며 분기가 바뀐다. 그래서 하루 1회 제한
    (옛 `Position.range_trigger_day`)도 같이 걷어냈다 — 걸릴 일이 없는 가드를
    남겨두면 이 경로가 실제보다 촘촘해 보인다.
    """
    action = evaluate_deterministic_sell(position, quote.price)
    if action is not None:
        return action, False

    if position.entry_price is None or (quote.day_low is None and quote.day_high is None):
        return None, False

    today = today or datetime.now(KST).date()

    # **진입 당일에는 구간 판정을 하지 않는다.** 당일 저가/고가는 하루 전체의 값이라
    # 우리가 사기 *전에* 찍힌 가격을 포함한다. 오늘 고가 근처에서 산 종목은 오늘
    # 아침의 저가만으로 손절 문턱을 넘은 것처럼 보이고, 그러면 보유한 적도 없는
    # 구간을 근거로 진입 당일에 전량 청산된다. 점 판정은 그대로 도므로, 진짜로
    # 진입 후에 문턱을 지나면 그쪽이 잡는다.
    if position.entry_day == today:
        return None, False

    plan = plan_for(position)
    reason = _threshold_crossed(
        position, plan, quote.day_low or quote.price, quote.day_high or quote.price
    )
    if reason is None:
        return None, False

    return _action_for(position, plan, reason), True


def threshold_crossed_in_bar(position: Position, bar: OHLCVBar) -> str | None:
    """이 포지션이 그날 **일봉 안에서** 문턱을 지났는지 사후 판정한다. 지났으면
    사유("stop_loss" | "take_profit" | "take_profit_trail"), 아니면 None.

    시세 공백 구간을 사람이 사후 확인할 때 쓰던 방법을 코드로 옮긴 것이다
    (2026-08-26 장애를 8/27에 이렇게 확인했다). 매분 시세를 못 받은 구간은
    "문턱을 안 넘어서 조용했는지" "눈을 감아서 조용했는지"가 구분되지 않는데,
    일봉의 저가/고가는 그 구간을 포함한 하루 전체의 상한·하한이므로 **최소한
    "넘지 않았다"는 확실히 증명된다.** 넘었다면 "하루 중 어딘가에서 닿았다"까지만
    말할 수 있다 — 장중 시각은 일봉으로 복원되지 않는다.

    evaluate_deterministic_sell과 같은 순서·같은 문턱을 쓴다(손절 우선). 다르게
    두면 사후 판정이 실제 안전장치와 어긋나서 오히려 사람을 헷갈리게 한다.

    한계 하나: **트레일링 익절은 이 판정에 안 잡힌다**(2026-09-02부터). 일봉은 저가와
    고가의 시간 순서를 주지 않는데 트레일링은 그 순서에 의존하기 때문이다
    (_threshold_crossed docstring). 실제 안전장치와 같은 함수를 쓴다는 규약은 그대로다
    — 안전장치도 구간으로는 트레일링을 안 본다. 공백 구간에서 트레일링 하락을 놓쳤는지는
    이 도구로 답할 수 없고, 답할 수 있는 척하면 안 된다.
    """
    if position.entry_price is None:
        return None
    return _threshold_crossed(position, plan_for(position), bar.low, bar.high)


def update_peak_price(
    position: Position,
    current_price: float,
    *,
    day_high: float | None = None,
    today: date | None = None,
) -> Position:
    """그날 관측한 가격으로 고점을 갱신한다. evaluate_deterministic_sell보다 먼저
    호출해 그날의 고점이 이미 반영된 상태에서 매도를 평가한다.

    day_high를 같이 받는 이유는 evaluate_with_day_range와 같다 — 분당 1회 샘플링은
    고점도 놓친다. 2026-08-26에 192820의 실제 고가는 297,000이었는데 기록된 고점은
    296,500이었다(시세 공백 구간에 찍혔다). 고점이 낮게 남으면 트레일 라인도 낮아져
    매도가 늦어진다. 이것도 이미 받아오던 값이라 추가 조회가 없다."""
    if position.entry_price is None:
        return position
    # 진입 당일의 당일 고가는 우리가 사기 전 구간을 포함한다 — 그걸 고점으로 잡으면
    # 트레일 라인이 보유한 적 없는 가격 위에 서고, 첫날부터 트레일링이 걸린다
    # (evaluate_with_day_range의 진입 당일 제외와 같은 이유).
    #
    # **트레일링 익절로 고점을 리셋한 날도 같다**(2026-09-02 추가). 그날의 고가는
    # 리셋 이전 구간을 포함하므로 리셋을 그대로 되돌린다. day_high >= 현재가는 항상
    # 참이라 이 제외가 없으면 리셋은 단 한 회차도 살아남지 못했고, 트리거가 된 고점이
    # *그날의* 고가일 때(장중 반락) 매분 재발동해 -7% 하락 한 번에 포지션이 통째로
    # 나갔다 — 설계 의도는 1/3이다(execute_sell docstring).
    today = today or datetime.now(KST).date()
    if day_high is not None and (position.entry_day == today or position.peak_reset_day == today):
        day_high = None
    observed = max(current_price, day_high) if day_high is not None else current_price
    new_peak = max(position.peak_price or position.entry_price, observed)
    if new_peak == position.peak_price:
        return position
    return position.model_copy(update={"peak_price": new_peak})


def execute_sell(
    portfolio: PortfolioState, action: SellAction, current_price: float, *, today: date | None = None
) -> PortfolioState:
    """SellAction을 포트폴리오 상태에 반영하는 순수 함수. 실거래 API 호출 없음
    (규칙 7) — 무비용 시뮬레이션 경로다. 실제 KIS 주문까지 내려면
    execute_sell_order를 쓴다.

    트레일링 익절이 실행되면 다음 구간을 새 고점부터 추적하도록 peak_price를
    현재가로 리셋한다 — 안 그러면 같은 하락 하나로 여러 단계가 연달아 발동해버린다.
    리셋한 날짜(peak_reset_day)도 같이 남긴다. 이게 없으면 update_peak_price가 다음
    회차에 그날의 고가로 리셋을 되돌려 리셋 자체가 무의미해진다(2026-09-02).
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
        )

    update = {"weight": remaining_weight}
    if position.quantity:
        update["quantity"] = position.quantity - int(position.quantity * action.sell_fraction)
    if action.reason == "take_profit_trail":
        update["take_profit_stage"] = position.take_profit_stage + 1
        update["peak_price"] = current_price
        update["peak_reset_day"] = today or datetime.now(KST).date()

    return PortfolioState(
        positions=[*other_positions, position.model_copy(update=update)],
        cash_weight=portfolio.cash_weight + sold_weight,
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
        # 부분 익절 비율이 내림 때문에 0주가 됐다. 여기서 그냥 돌아가면 이 포지션은
        # **영원히 청산되지 않는다** — 트리거는 계속 걸리는데 매번 0주라 아무 일도
        # 안 일어나고, 비중도 1/3씩만 줄어드니 execute_sell의 제거 조건(잔여 ≈ 0)에
        # 닿지 않는다. 실제로 192820이 4단계까지 내려와 8주가 됐고, 그대로 두면
        # 8→6→4→3→2주에서 int(2 * 1/3)=0으로 굳는다(2026-08-20 발견).
        # 그래서 남은 걸 전량 판다. 진입 시 확정된 문턱(ExitPlan)을 건드리는 게
        # 아니라 이미 내려진 매도 판정을 집행하는 방법의 문제라, 보유 중 문턱을
        # 재결정하지 않는다는 원칙과 충돌하지 않는다(사용자 확정 2026-08-20).
        logger.info(
            "execute_sell_order_liquidating_dust ticker=%s quantity=%d reason=fraction_rounds_to_zero",
            action.ticker,
            position.quantity,
        )
        shares_to_sell = position.quantity

    today = datetime.now(KST).date()
    before = await asyncio.to_thread(kis.fetch_daily_fill_totals, action.ticker, today, "sell")

    try:
        order_no = await asyncio.to_thread(kis.place_market_sell_order, action.ticker, shares_to_sell)
    except kis.OrderResponseLost:
        # 매도 주문을 보냈는데 응답을 못 받았다. 재전송하면 두 번 팔리므로 하지
        # 않는다 — 아래 체결 조회로 실제 접수 여부를 가른다. 매수(pipeline)와
        # 대칭이되 방향이 더 위험하다: 팔렸는데 안 팔린 걸로 두면 없는 주식을
        # 계속 리스크 관리 대상으로 들고 있게 된다.
        logger.error(
            "execute_sell_order_response_lost ticker=%s shares=%d", action.ticker, shares_to_sell
        )
        order_no = None
    else:
        if order_no is None:
            logger.error("execute_sell_order_failed ticker=%s reason=order_rejected", action.ticker)
            return portfolio, None

    # 주문 수량이 다 잡힐 때까지 기다린다 — 시장가는 여러 번에 나뉘어 체결되고,
    # 곧바로 조회하면 그 중 일부만 잡힌다(kis.fill_after_order docstring, 2026-08-18).
    fill = await asyncio.to_thread(
        kis.fill_after_order, action.ticker, today, "sell", before, shares_to_sell
    )
    if order_no is None and fill is None:
        # 응답 유실 + 원장에 체결 흔적 없음 = 주문이 접수되지 않았다고 본다.
        # 포지션을 그대로 두는 쪽이 안전하다 — 안 팔렸는데 판 걸로 기록하면
        # 실제로는 남아 있는 주식이 리스크 관리에서 사라진다.
        logger.error("execute_sell_order_failed ticker=%s reason=order_response_lost", action.ticker)
        return portfolio, None

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
        portfolio,
        action.model_copy(update={"sell_fraction": effective_fraction}),
        effective_price,
        today=today,
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
        ),
        fill,
    )
