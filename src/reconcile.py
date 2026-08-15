"""자체 상태 파일(logs/portfolio_state.json)을 브로커 실제 잔고와 대조·교정한다.

왜 필요한가 (2026-08-15): 시장가로 주문하므로 판단 시점 호가와 실제 체결가는
항상 다를 수 있는데, 진입가를 호가로 기록하던 버그 때문에 192820의 진입가가
210,000원으로 남았다(실제 체결 232,000원, +10.5% 차이). 진입가는 손절·익절 판정의
유일한 기준점이라, 그 결과 실제 +6.6% 지점에서 익절이 "+20% 도달"로 오판돼
발동했다. 버그 자체는 pipeline.execute_buy_order에서 고쳤지만 그건 앞으로의
매수에만 적용되고, 이미 어긋난 채로 들고 있는 포지션은 계속 틀린 기준으로
판정된다 — 그걸 잡는 게 이 모듈이다.

원칙: **브로커가 사실의 출처다.** 자체 상태와 다르면 항상 브로커가 맞다.

교정하지 않는 것 — `weight`: 브로커에는 "포트폴리오 비중"이라는 개념 자체가 없다.
이 필드는 매수 시점의 원가 기준 비중(TRADE_WEIGHT)이고 시세로 재평가되지 않으므로
시장가치 비중과는 원래 다르다. 그건 이 버그와 무관한 설계 사안이라 여기서 조용히
바꾸지 않고 드리프트만 보고한다.
"""

from pydantic import BaseModel

from src.schemas import PortfolioState

# 부동소수 표현 오차만 걸러내는 수준. 실제 어긋남은 이보다 훨씬 크다
# (192820이 10.5%, 051900이 1.4%).
RELATIVE_TOLERANCE = 1e-6


class Drift(BaseModel):
    """자체 상태와 브로커 잔고가 어긋난 지점 하나."""

    ticker: str
    field: str  # "entry_price" | "quantity" | "missing_at_broker" | "missing_locally"
    local: float | None
    broker: float | None
    corrected: bool  # 이번 실행에서 실제로 고쳤는가

    @property
    def relative_diff(self) -> float | None:
        if self.local in (None, 0) or self.broker is None:
            return None
        return self.broker / self.local - 1


def _differs(local: float | None, broker: float) -> bool:
    if local is None or local == 0:
        return True
    return abs(broker / local - 1) > RELATIVE_TOLERANCE


def reconcile(
    portfolio: PortfolioState, holdings: dict[str, tuple[int, float]]
) -> tuple[PortfolioState, list[Drift]]:
    """브로커 잔고를 기준으로 포지션의 진입가·수량을 맞춘 새 상태와 드리프트 목록.

    순수 함수다 — 네트워크도 파일도 건드리지 않아 실제 계좌 없이 전부 테스트된다.

    브로커에 없는 종목은 **지우지 않는다**. 조회가 부분적으로 실패했을 수도 있고,
    포지션을 자동으로 없애는 건 되돌릴 수 없는 데다 손절 대상이 조용히 사라지는
    결과가 될 수 있다. 보고만 하고 사람이 판단하게 둔다.
    """
    drifts: list[Drift] = []
    corrected_positions = []

    for position in portfolio.positions:
        held = holdings.get(position.ticker)
        if held is None:
            drifts.append(
                Drift(
                    ticker=position.ticker,
                    field="missing_at_broker",
                    local=float(position.quantity) if position.quantity else None,
                    broker=None,
                    corrected=False,
                )
            )
            corrected_positions.append(position)
            continue

        broker_quantity, broker_price = held
        update: dict = {}

        if _differs(position.entry_price, broker_price):
            drifts.append(
                Drift(
                    ticker=position.ticker,
                    field="entry_price",
                    local=position.entry_price,
                    broker=broker_price,
                    corrected=True,
                )
            )
            update["entry_price"] = broker_price

        if position.quantity != broker_quantity:
            drifts.append(
                Drift(
                    ticker=position.ticker,
                    field="quantity",
                    local=float(position.quantity) if position.quantity is not None else None,
                    broker=float(broker_quantity),
                    corrected=True,
                )
            )
            update["quantity"] = broker_quantity

        # 진입가가 올라가면서 고점을 추월할 수 있다. 고점은 브로커가 모르는 값이라
        # 교정 대상이 아니지만, 진입가보다 낮은 고점은 성립하지 않는다
        # (update_peak_price가 max(peak or entry, current)로 잡는 것과 같은 규약).
        new_entry = update.get("entry_price", position.entry_price)
        if new_entry is not None and (position.peak_price is None or position.peak_price < new_entry):
            update["peak_price"] = new_entry

        corrected_positions.append(position.model_copy(update=update) if update else position)

    for ticker in holdings:
        if not any(p.ticker == ticker for p in portfolio.positions):
            quantity, price = holdings[ticker]
            drifts.append(
                Drift(
                    ticker=ticker,
                    field="missing_locally",
                    local=None,
                    broker=float(quantity),
                    corrected=False,
                )
            )

    return (
        PortfolioState(
            positions=corrected_positions,
            cash_weight=portfolio.cash_weight,
            daily_pnl_pct=portfolio.daily_pnl_pct,
        ),
        drifts,
    )
