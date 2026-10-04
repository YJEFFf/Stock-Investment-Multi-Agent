"""응답 유실 주문의 불확실성을 프로세스 사이에 보존한다.

주문 전 표시하고, 명시적 거부 또는 완전체결의 장부 저장 후에만 해제한다. 응답과 체결을
모두 확인하지 못한 주문은 날짜가 바뀌어도 자동으로 재전송하지 않는다. 운영 호출자는
portfolio_lock 안에서 이 함수를 사용한다. 사람이 원장·장부를 대조한 뒤 해제할 수 있다.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from src.schemas import PortfolioState, UnconfirmedOrder

STATE_PATH = Path("logs/unconfirmed_orders.json")


def load_orders() -> dict[str, UnconfirmedOrder]:
    if not STATE_PATH.exists():
        return {}
    payload = json.loads(STATE_PATH.read_text())
    if not isinstance(payload, dict):
        raise ValueError("unconfirmed order state is not an object")
    orders = {ticker: UnconfirmedOrder.model_validate(row) for ticker, row in payload.items()}
    if any(ticker != row.ticker for ticker, row in orders.items()):
        raise ValueError("unconfirmed order key mismatch")
    return orders


def _save(orders: dict[str, UnconfirmedOrder]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump({t: row.model_dump(mode="json") for t, row in orders.items()}, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)


def is_blocked(ticker: str) -> bool:
    return ticker in load_orders()


def begin(ticker: str, side: str, quantity: int, fills_before: tuple | None) -> None:
    orders = load_orders()
    if ticker in orders:
        raise ValueError(f"unconfirmed order already exists: {ticker}")
    orders[ticker] = UnconfirmedOrder(
        ticker=ticker, side=side, quantity=quantity,
        created_at=datetime.now(timezone.utc), fills_before=fills_before,
    )
    _save(orders)


def clear(ticker: str) -> None:
    orders = load_orders()
    orders.pop(ticker, None)
    _save(orders)


def mark_settled(ticker: str, portfolio: PortfolioState) -> None:
    """완전체결의 예상 장부만 기록한다. 실제 저장 전에는 여전히 주문을 막는다."""
    orders = load_orders()
    if ticker not in orders:
        return
    position = next((p for p in portfolio.positions if p.ticker == ticker), None)
    orders[ticker].ready_to_commit = True
    orders[ticker].expected_position = position.model_dump(mode="json", exclude={"peak_price", "peak_reset_day"}) if position else None
    _save(orders)


def release_committed(portfolio: PortfolioState) -> None:
    """portfolio_store가 파일을 영속화한 뒤 호출한다. 일치하는 완전체결만 해제한다."""
    orders = load_orders()
    # execute_open은 매수 후 같은 회차에 고점을 관측한다. 시세 관측값이 바뀌어도
    # 수량·원가·비중·출구 계획·익절 단계의 영속화가 일치하면 그 주문은 기록됐다.
    positions = {p.ticker: p.model_dump(mode="json", exclude={"peak_price", "peak_reset_day"}) for p in portfolio.positions}
    releasable = [t for t, o in orders.items()
                  if o.ready_to_commit and o.expected_position == positions.get(t)]
    if releasable:
        _save({t: o for t, o in orders.items() if t not in releasable})
