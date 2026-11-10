"""응답 유실 주문의 불확실성을 프로세스 사이에 보존한다.

주문 전 표시하고, 명시적 거부 또는 완전체결의 장부 저장 후에만 해제한다. 응답과 체결을
모두 확인하지 못한 주문은 날짜가 바뀌어도 자동으로 재전송하지 않는다. 운영 호출자는
portfolio_lock 안에서 이 함수를 사용한다. 사람이 원장·장부를 대조한 뒤 해제할 수 있다. 신규 매수와 전량 매도의
지연 체결과 잔여 상태가 일치하는 부분 익절은 order_recovery가 주문일 원장·잔고·일지를
검증하고 저장한 뒤 해제할 수 있다.
"""

import json
import hashlib
import math
import os
from datetime import datetime, timezone
from pathlib import Path

from src.schemas import PortfolioState, UnconfirmedOrder

STATE_PATH = Path("logs/unconfirmed_orders.json")


def buy_decision_id(decision, day):
    payload = {"day": day.isoformat(), "decision": decision.model_dump(mode="json")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def decision_consumed(key):
    return (STATE_PATH.parent / "consumed_buy_decisions" / f"{key}.json").exists()


def load_orders() -> dict[str, UnconfirmedOrder]:
    if not STATE_PATH.exists():
        return {}
    payload = json.loads(STATE_PATH.read_text())
    if not isinstance(payload, dict):
        raise ValueError("unconfirmed order state is not an object")
    orders = {ticker: UnconfirmedOrder.model_validate(row) for ticker, row in payload.items()}
    if any(ticker != row.ticker for ticker, row in orders.items()):
        raise ValueError("unconfirmed order key mismatch")
    for order in orders.values():
        if order.accounted_quantity > order.quantity or bool(order.accounted_quantity) != bool(order.accounted_amount):
            raise ValueError("invalid accounted fill totals")
        if order.settlement_context is None:
            continue  # Historical guard format; its legacy recovery verifies journals.
        from src.data_quality import validate_portfolio
        baseline = validate_portfolio(PortfolioState.model_validate(order.initial_portfolio))
        initial = next((p for p in baseline.positions if p.ticker == order.ticker), None)
        quantity = initial.quantity if initial else 0
        if quantity is None:
            raise ValueError("unknown original order quantity")
        expected_quantity = quantity + order.accounted_quantity * (1 if order.side == "buy" else -1)
        expected = order.expected_position
        if expected_quantity < 0 or (expected.get("quantity") if expected else 0) != expected_quantity:
            raise ValueError("accounted quantity does not match expected holding")
        if expected_quantity:
            initial_weight = initial.weight if initial else 0
            if order.side == "buy":
                weight = initial_weight + order.settlement_context["trade_weight"] * order.accounted_quantity / order.quantity
                price = ((initial.entry_price * quantity if initial else 0) + order.accounted_amount) / expected_quantity
            else:
                weight = initial_weight * expected_quantity / quantity
                price = initial.entry_price
            if not math.isclose(expected["weight"], weight, abs_tol=1e-8) or not math.isclose(expected["entry_price"], price, abs_tol=1e-6):
                raise ValueError("accounted cost or weight does not match expected holding")
    return orders


def _save(orders: dict[str, UnconfirmedOrder]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump({t: row.model_dump(mode="json") for t, row in orders.items()}, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_PATH)
    _sync_directory(STATE_PATH.parent)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".commit-tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _sync_directory(path.parent)


def commit_effect(ticker, before, after, entries, *, complete, quantity, amount, fee=None):
    """Write-ahead intent precedes both portfolio and journal mutations.

    The caller holds portfolio_lock. Replay requires the same before/after state;
    conflicting state is never overwritten. Event IDs make journal replay idempotent.
    """
    from src import portfolio_store
    from src.data_quality import validate_portfolio
    validate_portfolio(before)
    validate_portfolio(after)
    orders = load_orders()
    order = orders[ticker]
    if (type(quantity) is not int or quantity <= 0 or order.accounted_quantity + quantity > order.quantity
            or not math.isfinite(amount) or amount <= 0
            or (fee is not None and (not math.isfinite(fee) or fee < 0))
            or bool(complete) != (order.accounted_quantity + quantity == order.quantity)):
        raise ValueError("invalid confirmed fill commit")
    event_id = f"{ticker}:{order.side}:{order.created_at.isoformat()}:{order.accounted_quantity + quantity}"
    order.pending_commit = {
        "before": before.model_dump(mode="json"), "after": after.model_dump(mode="json"),
        "portfolio_path": str(portfolio_store.PORTFOLIO_STATE_PATH.resolve()),
        "entries": [{"path": str(Path(path).resolve()), "row": {**row, "event_id": event_id}}
                    for path, row in entries], "complete": complete, "quantity": quantity,
        "amount": amount, "fee": fee,
    }
    _save(orders)
    recover_commits()


def recover_commits():
    """Finish interrupted local commits without any broker/model call."""
    from src.data_quality import validate_portfolio
    orders = load_orders()
    for ticker, order in list(orders.items()):
        intent = order.pending_commit
        if intent is None:
            continue
        path = Path(intent["portfolio_path"])
        before = validate_portfolio(PortfolioState.model_validate(intent["before"]))
        after = validate_portfolio(PortfolioState.model_validate(intent["after"]))
        current = validate_portfolio(PortfolioState.model_validate_json(path.read_text()))
        def accounting(p):
            return p.model_dump(mode="json", exclude={"positions": {"__all__": {"peak_price", "peak_reset_day"}}})
        if accounting(current) not in (accounting(before), accounting(after)):
            raise ValueError(f"order commit conflicts with portfolio: {ticker}")
        for entry in intent["entries"]:
            journal = Path(entry["path"])
            raw = journal.read_bytes() if journal.exists() else b""
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            matching = [r for r in rows if r.get("event_id") == entry["row"]["event_id"]]
            if matching and matching != [entry["row"]]:
                raise ValueError(f"order commit conflicts with journal: {ticker}")
            if not matching:
                _atomic_bytes(journal, raw + (b"\n" if raw and not raw.endswith(b"\n") else b"")
                              + json.dumps(entry["row"], ensure_ascii=False).encode() + b"\n")
        _atomic_bytes(path, after.model_dump_json(indent=2).encode())
        order.accounted_quantity += intent["quantity"]
        order.accounted_amount += intent["amount"]
        order.accounted_fee = (order.accounted_fee + intent["fee"]
                              if order.accounted_fee is not None and intent["fee"] is not None else None)
        position = next((p for p in after.positions if p.ticker == ticker), None)
        order.expected_position = position.model_dump(mode="json", exclude={"peak_price", "peak_reset_day"}) if position else None
        if order.settlement_context is not None:
            order.settlement_context["last_entries"] = intent["entries"]
        order.pending_commit = None
        if intent["complete"]:
            del orders[ticker]
        else:
            order.ready_to_commit = False
        _save(orders)


def is_blocked(ticker: str) -> bool:
    return ticker in load_orders()


def begin(ticker: str, side: str, quantity: int, fills_before: tuple | None, *, portfolio=None, context=None) -> None:
    orders = load_orders()
    if ticker in orders:
        raise ValueError(f"unconfirmed order already exists: {ticker}")
    if portfolio is not None:
        from src.portfolio_store import PORTFOLIO_STATE_PATH
        from src.data_quality import validate_portfolio
        validate_portfolio(portfolio)
        saved = validate_portfolio(PortfolioState.model_validate_json(PORTFOLIO_STATE_PATH.read_text()))
        exclude = {"positions": {"__all__": {"peak_price", "peak_reset_day"}}}
        if saved.model_dump(exclude=exclude) != portfolio.model_dump(exclude=exclude):
            raise ValueError("order's portfolio differs from persisted accounting")
    orders[ticker] = UnconfirmedOrder(
        ticker=ticker, side=side, quantity=quantity,
        created_at=datetime.now(timezone.utc), fills_before=fills_before,
        initial_portfolio=portfolio.model_dump(mode="json") if portfolio is not None else None,
        settlement_context=context,
        expected_position=next((p.model_dump(mode="json", exclude={"peak_price", "peak_reset_day"})
                                for p in portfolio.positions if p.ticker == ticker), None) if portfolio else None,
    )
    _save(orders)
    if context and context.get("decision_id"):
        receipt = STATE_PATH.parent / "consumed_buy_decisions" / f"{context['decision_id']}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.open("x") as handle:
            json.dump({"ticker": ticker, "day": context["day"], "decision_id": context["decision_id"]}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(receipt.parent)


def mark_accepted(ticker: str, order_no: str) -> None:
    """접수 번호를 영속화해 호가 폴백 매도의 후속 대조에도 접수 증거를 남긴다."""
    orders = load_orders()
    orders[ticker].order_no = str(order_no)
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
