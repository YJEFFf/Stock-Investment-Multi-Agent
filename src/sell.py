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

from src.schemas import PortfolioState, Position, SellAction

# 사용자 확정치 (2026-08-09):
STOP_LOSS_PCT = -0.10  # 진입가 대비 평가손익 -10% 도달 시 전량 매도
TAKE_PROFIT_TRIGGER_PCT = 0.20  # 진입가 대비 +20%에서 첫 부분 익절 트리거
TAKE_PROFIT_SELL_FRACTION = 1.0 / 3.0  # 트리거마다 "현재" 보유량의 이 비율만큼 매도

# 첫 트리거 이후엔 고점 대비 트레일링 스톱으로 전환 — 정확한 조정폭은 사용자가
# 아직 숫자를 확정하지 않아 제안값으로 시작한다. 관행값과 같은 성격: 나중에
# 실제 포지션 로그가 쌓이면 조정하되, 과거 수익률에 맞춰 역산하지 않는다.
TAKE_PROFIT_TRAIL_PCT = -0.07  # 고점 대비 이 비율만큼 조정 시 다음 단계 매도 (제안값)


def evaluate_deterministic_sell(position: Position, current_price: float) -> SellAction | None:
    """무조건 실행되는 안전장치 — LLM 관여 없이 코드가 100% 결정한다.

    진입가가 아직 없는 포지션(Position.entry_price is None)은 판단 자체를 하지
    않는다 — "판단 불가"와 "판단했으나 매도 안 함"은 다른 상태다(AnalystOpinion과
    같은 패턴). 손절이 익절보다 우선한다.
    """
    if position.entry_price is None:
        return None

    unrealized_pct = (current_price - position.entry_price) / position.entry_price

    if unrealized_pct <= STOP_LOSS_PCT:
        return SellAction(ticker=position.ticker, reason="stop_loss", sell_fraction=1.0)

    if position.take_profit_stage == 0:
        if unrealized_pct >= TAKE_PROFIT_TRIGGER_PCT:
            return SellAction(
                ticker=position.ticker, reason="take_profit_trail", sell_fraction=TAKE_PROFIT_SELL_FRACTION
            )
        return None

    # 이미 1회 이상 부분 익절함 -> 그 이후엔 고점 대비 트레일링 스톱으로 감시한다.
    if position.peak_price is None:
        return None
    drawdown_from_peak = (current_price - position.peak_price) / position.peak_price
    if drawdown_from_peak <= TAKE_PROFIT_TRAIL_PCT:
        return SellAction(
            ticker=position.ticker, reason="take_profit_trail", sell_fraction=TAKE_PROFIT_SELL_FRACTION
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
    """SellAction을 포트폴리오에 반영하는 순수 함수. 실거래 API 호출 없음(규칙 7).

    트레일링 익절이 실행되면 다음 구간을 새 고점부터 추적하도록 peak_price를
    현재가로 리셋한다 — 안 그러면 같은 하락 하나로 여러 단계가 연달아 발동해버린다.
    잔여 비중이 사실상 0이면(부동소수 오차 감안) 포지션 자체를 제거한다.
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
    if action.reason == "take_profit_trail":
        update["take_profit_stage"] = position.take_profit_stage + 1
        update["peak_price"] = current_price

    return PortfolioState(
        positions=[*other_positions, position.model_copy(update=update)],
        cash_weight=portfolio.cash_weight + sold_weight,
        daily_pnl_pct=portfolio.daily_pnl_pct,
    )
