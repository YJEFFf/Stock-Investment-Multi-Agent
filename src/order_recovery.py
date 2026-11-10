"""신규 매수와 매도의 지연 체결을 재대조한다. 주문·분석·출구 재설정 없음.

브로커 조회는 포트폴리오 락 밖에서 하고, 짧은 커밋 락 안에서 원본을 다시 비교한다.
부분 익절은 확정된 선행 체결과 잔여 포지션까지 일치해야 해제한다.
추가매수/귀속 불명확한 응답 유실/애매한 원장은 자동 해제하지 않는다.
주문 날짜가 지나도 이후 거래가 없고 원장·장부·잔고가 모두 일치하면 복구한다.
"""

import hashlib
import json
import logging
import math
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src import kis, notify, order_guard, portfolio_store

logger = logging.getLogger(__name__)
JOURNAL_PATH = Path("logs/trade_journal.jsonl")
AUDIT_DIR = Path("logs/order_recoveries")
# 다음 분에 다시 조회한다. 회차 안에서 재시도하거나 체결을 기다리지 않는다.
QUERY_POLICY = kis.RetryPolicy(timeout_seconds=2.0, max_attempts=1, backoff_seconds=(0,))
CLOSE_QUERY_POLICY = kis.RetryPolicy(timeout_seconds=10.0, max_attempts=1, backoff_seconds=(0,))
KST = ZoneInfo("Asia/Seoul")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _flush_recovery_alerts() -> None:
    """확정·차단 해제 후에만 알린다. 발송함 실패는 다음 회차에 다시 저장한다."""
    try:
        with portfolio_store.portfolio_lock(blocking=False):
            orders = order_guard.load_orders()
            for path in sorted(AUDIT_DIR.glob("*/committed.json")):
                attempted = False
                try:
                    receipt = json.loads(path.read_text())
                    message = receipt.get("notification")
                    if not message or receipt.get("notification_queued"):
                        continue  # 이전 버전의 과거 복구를 소급 발송하지 않는다.
                    if receipt["ticker"] in orders:
                        continue
                    attempted = True
                    if notify.send_telegram_alert(message):
                        receipt["notification_queued"] = True
                        _atomic_write(path, json.dumps(receipt, ensure_ascii=False).encode())
                except Exception:
                    logger.exception("order_recovery_notification_failed path=%s", path)
                if attempted:
                    break  # 영수증 저장 실패 때도 발송함 대기는 한 건만 허용한다.
    except portfolio_store.PortfolioLockBusy:
        pass
    except Exception:
        logger.exception("order_recovery_notifications_failed")


def _recovery_receipt(now, order, quantity, price, fee):
    return {"at": now.isoformat(), "quantity": quantity, "price": price, "fee": fee,
            "ticker": order.ticker,
            "notification": notify.format_order_recovery_alert(
                order.ticker, order.side, quantity, price, now),
            "notification_queued": False}


def _position_values(position):
    return position.model_dump(mode="json", exclude={"peak_price", "peak_reset_day"})


def _historical_quantity(rows: list[dict], ticker: str) -> int | None:
    quantity = 0
    for row in rows:
        if row.get("ticker") != ticker or row.get("event") not in ("buy", "sell"):
            continue
        amount = row.get("quantity" if row["event"] == "buy" else "shares_sold")
        if type(amount) is not int or amount <= 0:
            return None
        quantity += amount if row["event"] == "buy" else -amount
        if quantity < 0:
            return None
    return quantity


def recover_pending_buy(now: datetime, *, ticker: str | None = None, extended: bool = False,
                        policy: kis.RetryPolicy | None = None) -> bool:
    """한 회차 한 종목을 순환 대조한다. 호출자는 포트폴리오 락을 잡지 않아야 한다.

    여러 미확인 건이 있어도 한 종목의 지연이 다른 종목의 조회를 굶기지 않게 분 단위로
    순환한다. 매수 후보를 제한하는 정책과는 무관한 조회 부하 제한이다.
    False는 확인 못 함/변화 없음이다. 파일 쓰기 실패는 전파하며 마커를 유지한다.
    """
    _flush_recovery_alerts()
    now = now.astimezone(KST)
    policy = policy or QUERY_POLICY
    day = now.date().isoformat()
    try:
        with portfolio_store.portfolio_lock(blocking=False):
            orders = order_guard.load_orders()
            candidates = sorted(t for t, o in orders.items() if o.side == "buy"
                                and not o.ready_to_commit and (o.created_at.astimezone(KST).date() <= now.date() if extended
                                                               else o.created_at.astimezone(KST).date() == now.date()))
            if not candidates or not JOURNAL_PATH.exists():
                return False
            ticker = ticker or candidates[int(now.timestamp() // 60) % len(candidates)]
            if ticker not in candidates:
                return False
            order = orders[ticker]
            order_day = order.created_at.astimezone(KST).date()
            day = order_day.isoformat()
            if not extended and order_day != now.date():
                return False
            if order.fills_before != (0, 0.0, 0.0):
                return False
            original = JOURNAL_PATH.read_bytes()
            lines = original.decode().splitlines(keepends=True)
            rows = [json.loads(line) for line in lines]
            matches = [(i, r) for i, r in enumerate(rows)
                       if r.get("ticker") == ticker and r.get("day") == day and r.get("event") in ("buy", "sell")]
            if len(matches) != 1:
                return False
            index, row = matches[0]
            if any(r.get("ticker") == ticker and r.get("event") in ("buy", "sell") for r in rows[index + 1:]):
                return False
            if (row["event"] != "buy" or not row.get("order_no") or row.get("quantity") != order.quantity
                    or row.get("exit_plan_source") == "existing_position"):
                return False
            # 재시작 시 이미 교정된 일지는 그대로 검증한다. 원본 정보는 교정 행에 보존한다.
            recovery = row.get("fill_recovery")
            if recovery is None:
                allowed_sources = {"fill_partial", "quote_fallback", "daily_avg", "position_avg"} if extended else {"fill_partial"}
                if row.get("entry_price_source") not in allowed_sources:
                    return False
                old_price = row["entry_price"]
            else:
                if recovery.get("order_created_at") != order.created_at.isoformat():
                    return False
                old_price = recovery["entry_price_before"]
            # '신규' 플래그만 믿지 않고 과거 수량 체인도 확인한다.
            previous_qty = _historical_quantity(rows[:index], ticker)
            if previous_qty != 0:
                return False
            portfolio = portfolio_store.load_portfolio()
            position = next((p for p in portfolio.positions if p.ticker == ticker), None)
            if (position is None or position.quantity != order.quantity or position.entry_day != order_day
                    or position.take_profit_stage != 0 or position.entry_price not in (old_price, row["entry_price"])
                    or (position.exit_plan.model_dump(mode="json") if position.exit_plan else None) != row.get("exit_plan")):
                return False
            before_position = _position_values(position)
    except portfolio_store.PortfolioLockBusy:
        return False

    # 순차 GET 세 건, 각 한 번만. 조회 실패는 다음 크론 회차까지 차단 유지.
    buy = kis.fetch_daily_fill_totals(ticker, order_day, "buy", policy=policy, require_fee=True)
    if buy is None or buy[0] != order.quantity or not all(math.isfinite(x) for x in buy) or buy[1] <= 0 or buy[2] < 0:
        return False
    sold = kis.fetch_daily_fill_totals(ticker, order_day, "sell", policy=policy, require_fee=True)
    if sold != (0, 0.0, 0.0):
        return False
    holdings = kis.fetch_holdings(policy=policy, require_complete=True)
    price = buy[1] / buy[0]
    if holdings is None or ticker not in holdings:
        return False
    quantity, average = holdings[ticker]
    if quantity != order.quantity or not math.isfinite(average) or abs(average - price) > 0.001:
        return False
    if recovery is not None and (row["entry_price"] != price or row.get("fee_amount") != buy[2]):
        return False

    try:
        with portfolio_store.portfolio_lock(blocking=False):
            if order_guard.load_orders().get(ticker) != order or JOURNAL_PATH.read_bytes() != original:
                return False
            portfolio = portfolio_store.load_portfolio()
            position = next((p for p in portfolio.positions if p.ticker == ticker), None)
            if position is None or _position_values(position) != before_position:
                return False
            # 커밋 직전까지 다른 종목·고점 관측값을 보존한다.
            key = hashlib.sha256(f"{ticker}:{order.created_at.isoformat()}".encode()).hexdigest()[:20]
            audit = AUDIT_DIR / key
            if not (audit / "before.json").exists():
                _atomic_write(audit / "before.json", json.dumps({
                    "observed_at": now.isoformat(), "order": order.model_dump(mode="json"),
                    "portfolio": portfolio.model_dump(mode="json"), "journal_row": row,
                    "journal_sha256": hashlib.sha256(original).hexdigest(),
                    "buy": buy, "sell": sold, "holding": holdings[ticker],
                }, ensure_ascii=False, indent=2).encode())
            row.update(entry_price=price, fee_amount=buy[2], entry_price_source="fill",
                       fill_recovery=recovery or {"order_created_at": order.created_at.isoformat(),
                                                 "entry_price_before": old_price, "fee_before": row.get("fee_amount"),
                                                 "recovered_at": now.isoformat()})
            lines[index] = json.dumps(row, ensure_ascii=False) + "\n"
            corrected = "".join(lines).encode()
            _atomic_write(JOURNAL_PATH, corrected)
            position.entry_price = price
            position.peak_price = max(position.peak_price or price, price)
            # ready_to_commit=False 유지: 일지와 상태를 모두 검증한 뒤에만 해제한다.
            portfolio_store.save_portfolio(portfolio)
            if (portfolio_store.load_portfolio() != portfolio or JOURNAL_PATH.read_bytes() != corrected):
                raise RuntimeError("recovery persistence verification failed")
            _atomic_write(audit / "committed.json", json.dumps(_recovery_receipt(now, order, quantity, price, buy[2]), ensure_ascii=False).encode())
            order_guard.clear(ticker)
            logger.info("buy_fill_recovered ticker=%s quantity=%d price=%.8f fee=%.2f", ticker, quantity, price, buy[2])
    except portfolio_store.PortfolioLockBusy:
        return False
    _flush_recovery_alerts()
    return True


def recover_pending_order(now: datetime) -> bool:
    """미확인 주문을 순환 대조한다. 한 회차 한 종목, 주문 재전송 없음."""
    try:
        with portfolio_store.portfolio_lock(blocking=False):
            order_guard.recover_commits()
    except portfolio_store.PortfolioLockBusy:
        return False
    candidates = sorted(t for t, o in order_guard.load_orders().items() if o.settlement_context is not None)
    if candidates:
        return recover_observed_fill(now, candidates[int(now.timestamp() // 60) % len(candidates)])
    _flush_recovery_alerts()
    if os.getenv("SIMA_EXTENDED_ORDER_RECOVERY", "0") != "1":
        return recover_pending_buy(now)
    now = now.astimezone(KST)
    orders = order_guard.load_orders()
    candidates = sorted(t for t, o in orders.items()
                        if not o.ready_to_commit and o.created_at.astimezone(KST).date() <= now.date())
    if not candidates:
        return False
    ticker = candidates[int(now.timestamp() // 60) % len(candidates)]
    if orders[ticker].side == "buy":
        return recover_pending_buy(now, ticker=ticker, extended=True)
    return _recover_sell(now, ticker)


def recover_observed_fill(now, ticker, *, policy=None):
    """New orders: complete only when original totals AND full holdings reconcile.

    Partial/cancelled/ambiguous orders stay blocked. This never submits an order or
    recalculates its frozen exit plan. The remaining confirmed leg is journalled.
    """
    from src.schemas import PortfolioState, Position, FillRecord, SellAction
    from src import pipeline, sell
    import asyncio
    policy = policy or QUERY_POLICY
    order = order_guard.load_orders().get(ticker)
    if (order is None or not order.order_no or order.initial_portfolio is None
            or order.settlement_context is None or order.fills_before is None):
        return False
    context = order.settlement_context
    day = order.created_at.astimezone(KST).date()
    if day > now.astimezone(KST).date():
        return False
    totals = kis.fetch_daily_fill_totals(ticker, day, order.side, policy=policy)
    cumulative = kis.fill_between(order.fills_before, totals)
    if cumulative is None or cumulative.quantity != order.quantity:
        return False
    quantity = cumulative.quantity - order.accounted_quantity
    amount = cumulative.amount - order.accounted_amount
    if quantity <= 0 or amount <= 0:
        return False
    fee = cumulative.fee - order.accounted_fee if cumulative.fee is not None and order.accounted_fee is not None else None
    if fee is not None and fee < 0:
        return False
    opposite = kis.fetch_daily_fill_totals(ticker, day, "sell" if order.side == "buy" else "buy",
                                          policy=policy)
    if opposite is None or opposite[:2] != (0, 0.0):
        return False
    holdings = kis.fetch_holdings(policy=policy, require_complete=True)
    if holdings is None:
        return False
    baseline = PortfolioState.model_validate(order.initial_portfolio)
    initial = next((p for p in baseline.positions if p.ticker == ticker), None)
    original_quantity = initial.quantity if initial else 0
    if original_quantity is None:
        return False
    expected_quantity = original_quantity + order.quantity * (1 if order.side == "buy" else -1)
    held = holdings.get(ticker)
    if (held[0] if held else 0) != expected_quantity:
        return False
    if order.side == "buy":
        expected_price = ((initial.entry_price * original_quantity if initial else 0) + cumulative.amount) / expected_quantity
        if not held or abs(held[1] - expected_price) > .001:
            return False
    elif held and (not initial or abs(held[1] - initial.entry_price) > .001):
        return False
    try:
        with portfolio_store.portfolio_lock(blocking=False):
            if order_guard.load_orders().get(ticker) != order:
                return False
            current = portfolio_store.load_portfolio()
            position = next((p for p in current.positions if p.ticker == ticker), None)
            observed = position.model_dump(mode="json", exclude={"peak_price", "peak_reset_day"}) if position else None
            if observed != order.expected_position:
                return False
            # Previously committed legs must still exist unchanged.
            for entry in context.get("last_entries", []):
                rows = [json.loads(line) for line in Path(entry["path"]).read_text().splitlines() if line.strip()]
                if [r for r in rows if r.get("event_id") == entry["row"]["event_id"]] != [entry["row"]]:
                    return False
            fill = FillRecord(quantity=quantity, amount=amount, fee=fee, order_total_fee=cumulative.fee)
            if order.side == "buy":
                weight = context["trade_weight"] * quantity / order.quantity
                if position:
                    updated_position = position.model_copy(update={
                        "quantity": position.quantity + quantity,
                        "entry_price": (position.entry_price * position.quantity + amount) / (position.quantity + quantity),
                        "weight": position.weight + weight,
                        "peak_price": max(position.peak_price or fill.price, fill.price),
                    })
                else:
                    updated_position = Position(ticker=ticker, sector=context["sector"], weight=weight,
                        quantity=quantity, entry_day=context["day"], entry_price=fill.price, peak_price=fill.price,
                        exit_plan=context["exit_plan"])
                updated = PortfolioState(positions=[p for p in current.positions if p.ticker != ticker] + [updated_position],
                                         cash_weight=current.cash_weight - weight)
                row = {"event": "buy", "day": context["day"], "ticker": ticker,
                       "sector": context["sector"], "quantity": quantity, "entry_price": fill.price,
                       "entry_price_source": "fill", "fee_amount": fee, "order_no": order.order_no,
                       "order_total_fee": cumulative.fee,
                       "decision": context["decision"], "recovery": True, "recovered_at": now.isoformat(),
                       "exit_plan": updated_position.exit_plan.model_dump(mode="json") if updated_position.exit_plan else None,
                       "exit_plan_source": context["exit_plan_source"]}
                order_guard.commit_effect(ticker, current, updated, [(context["log_path"], row)],
                                          complete=True, quantity=quantity, amount=amount, fee=fee)
            else:
                if position is None or quantity > position.quantity:
                    return False
                action = SellAction.model_validate(context["action"])
                action = action.model_copy(update={"sell_fraction": quantity / position.quantity, "reasoning": None})
                updated = sell.execute_sell(current, action, fill.price, today=day)
                if order.accounted_quantity and action.reason == "take_profit_trail":
                    for p in updated.positions:
                        if p.ticker == ticker:
                            p.take_profit_stage = position.take_profit_stage
                            p.peak_price, p.peak_reset_day = position.peak_price, position.peak_reset_day
                for p in updated.positions:
                    if p.ticker == ticker:
                        p.quantity = position.quantity - quantity
                async def verified(*args):
                    return updated, fill
                # All legs belong to the original trigger, not a newly evaluated
                # trailing decision after the first partial fill advanced stage.
                event_position = position.model_copy(update={"take_profit_stage": initial.take_profit_stage})
                journal = Path(context["last_entries"][0]["path"]) if context.get("last_entries") else JOURNAL_PATH
                asyncio.run(pipeline.finalize_sell(current, action, event_position, fill.price,
                    order.created_at.astimezone(KST), verified,
                    pipeline.DEFAULT_SELL_LOG_PATH, journal, recovered_at=now.astimezone(KST)))
            return True
    except portfolio_store.PortfolioLockBusy:
        return False


def recover_after_close(now: datetime) -> list[str]:
    """장후 대조 전 한 번씩 조회한다. 장중 2초 예산과 주문 경로는 그대로다."""
    now = now.astimezone(KST)
    from src.market_calendar import market_session
    session = market_session(now.date())
    if session and now < session.close + timedelta(minutes=10):
        raise RuntimeError("extended reconciliation is only allowed after 15:40 KST")
    with portfolio_store.portfolio_lock():
        order_guard.recover_commits()
    _flush_recovery_alerts()
    if os.getenv("SIMA_EXTENDED_ORDER_RECOVERY", "0") != "1":
        return []
    recovered = []
    for ticker, order in list(order_guard.load_orders().items()):
        if order.settlement_context is not None:
            if recover_observed_fill(now, ticker, policy=CLOSE_QUERY_POLICY):
                recovered.append(ticker)
            continue
        if order.ready_to_commit or order.created_at.astimezone(KST).date() > now.date():
            continue
        success = (recover_pending_buy(now, ticker=ticker, extended=True, policy=CLOSE_QUERY_POLICY)
                   if order.side == "buy" else _recover_sell(now, ticker, policy=CLOSE_QUERY_POLICY))
        if success:
            recovered.append(ticker)
    return recovered


def _sell_baseline(rows: list[dict]) -> tuple[int, float, float] | None:
    """같은 날 선행 매도는 확정 원장값만 누적 집계에서 빼낼 수 있다."""
    quantity, amount, fee = 0, 0.0, 0.0
    for row in rows:
        if (row.get("event") != "sell" or row.get("exit_price_source") != "fill"
                or row.get("sell_amount_source") != "fill" or row.get("fee_source") != "fill"):
            return None
        q, a, f = row.get("shares_sold"), row.get("sell_amount"), row.get("fee_amount")
        if (type(q) is not int or q <= 0
                or not isinstance(a, (int, float)) or not math.isfinite(a) or a <= 0
                or not isinstance(f, (int, float)) or not math.isfinite(f) or f < 0):
            return None
        quantity, amount, fee = quantity + q, amount + a, fee + f
    return quantity, amount, fee


def _recover_sell(now: datetime, ticker: str, *, policy: kis.RetryPolicy | None = None) -> bool:
    """마지막 매도의 지연 원장을 확정한다. 잔여 수량·출구 규칙·고점은 보존한다."""
    from src.pipeline import _trade_costs
    from src.schemas import FillRecord
    policy = policy or QUERY_POLICY

    try:
        with portfolio_store.portfolio_lock(blocking=False):
            order = order_guard.load_orders().get(ticker)
            if (order is None or order.side != "sell" or order.ready_to_commit
                    or order.fills_before is None or len(order.fills_before) != 3
                    or any(not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
                           for value in order.fills_before)):
                return False
            day = order.created_at.astimezone(KST).date()
            if day > now.date() or not JOURNAL_PATH.exists():
                return False
            original = JOURNAL_PATH.read_bytes()
            lines = original.decode().splitlines(keepends=True)
            rows = [json.loads(line) for line in lines]
            matches = [(i, r) for i, r in enumerate(rows) if r.get("ticker") == ticker
                       and r.get("day") == day.isoformat() and r.get("event") in ("buy", "sell")]
            if not matches:
                return False
            index, row = matches[-1]
            baseline = _sell_baseline([r for _, r in matches[:-1]])
            if (baseline is None or baseline[0] != order.fills_before[0]
                    or any(abs(a - b) > 0.005 for a, b in zip(baseline[1:], order.fills_before[1:]))):
                return False
            shares_before, shares_after = row.get("shares_before"), row.get("shares_after")
            if (row.get("event") != "sell" or row.get("shares_sold") != order.quantity
                    or type(shares_before) is not int or type(shares_after) is not int
                    or shares_after < 0 or shares_before - order.quantity != shares_after
                    or (shares_after == 0 and row.get("portfolio_weight_after") != 0)):
                return False
            if any(r.get("ticker") == ticker and r.get("event") in ("buy", "sell") for r in rows[index + 1:]):
                return False
            recovery = row.get("fill_recovery")
            if recovery:
                if recovery.get("order_created_at") != order.created_at.isoformat():
                    return False
            elif row.get("exit_price_source") != "fill_partial" and not (
                    row.get("exit_price_source") == "quote_fallback" and order.order_no):
                return False
            # 과거 수량 체인·잔여 장부와 브로커 잔고가 같은 거래를 가리켜야 한다.
            previous_qty = _historical_quantity(rows[:index], ticker)
            if previous_qty != shares_before:
                return False
            entry_price = row.get("entry_price")
            if not isinstance(entry_price, (int, float)) or not math.isfinite(entry_price) or entry_price <= 0:
                return False
            position = next((p for p in portfolio_store.load_portfolio().positions if p.ticker == ticker), None)
            before_position = _position_values(position) if position else None
            if shares_after:
                stage = row.get("take_profit_stage")
                if (not order.order_no or row.get("reason") != "take_profit_trail"
                        or position is None or position.quantity != shares_after
                        or position.entry_price != entry_price
                        or position.weight != row.get("portfolio_weight_after")
                        or position.exit_plan is None or position.exit_plan.model_dump(mode="json") != row.get("exit_plan")
                        or type(stage) is not int or position.take_profit_stage != stage + 1):
                    return False
            elif position is not None:
                return False
    except portfolio_store.PortfolioLockBusy:
        return False

    sold = kis.fetch_daily_fill_totals(ticker, day, "sell", policy=policy, require_fee=True)
    if (sold is None or sold[0] != baseline[0] + order.quantity or not all(math.isfinite(x) for x in sold)
            or sold[1] <= 0 or sold[2] < 0):
        return False
    bought = kis.fetch_daily_fill_totals(ticker, day, "buy", policy=policy, require_fee=True)
    if bought != (0, 0.0, 0.0):
        return False
    holdings = kis.fetch_holdings(policy=policy, require_complete=True)
    if holdings is None:
        return False
    holding = holdings.get(ticker)
    if shares_after:
        if (holding is None or holding[0] != shares_after or not math.isfinite(holding[1])
                or abs(holding[1] - entry_price) > 0.001):
            return False
    elif holding is not None:
        return False
    amount, fee = sold[1] - baseline[1], sold[2] - baseline[2]
    if amount <= 0 or fee < 0:
        return False
    fill = FillRecord(quantity=order.quantity, amount=amount, fee=fee)
    if recovery and (row.get("sell_amount") != fill.amount or row.get("fee_amount") != fill.fee):
        return False
    try:
        with portfolio_store.portfolio_lock(blocking=False):
            if order_guard.load_orders().get(ticker) != order or JOURNAL_PATH.read_bytes() != original:
                return False
            current = next((p for p in portfolio_store.load_portfolio().positions if p.ticker == ticker), None)
            if (_position_values(current) if current else None) != before_position:
                return False
            key = hashlib.sha256(f"{ticker}:{order.created_at.isoformat()}".encode()).hexdigest()[:20]
            audit = AUDIT_DIR / key
            if not (audit / "before.json").exists():
                _atomic_write(audit / "before.json", json.dumps({
                    "observed_at": now.isoformat(), "order": order.model_dump(mode="json"),
                    "journal_row": row, "journal_sha256": hashlib.sha256(original).hexdigest(),
                    "buy": bought, "sell": sold, "holding": holding,
                    "position": before_position, "fills_before": baseline,
                }, ensure_ascii=False, indent=2).encode())
            evidence = recovery or {"order_created_at": order.created_at.isoformat(),
                                   "exit_price_before": row.get("exit_price"),
                                   "amount_before": row.get("sell_amount"), "fee_before": row.get("fee_amount"),
                                   "recovered_at": now.isoformat()}
            row.update(exit_price=fill.price, exit_price_source="fill", sell_amount=fill.amount,
                       sell_amount_source="fill", realized_pnl_pct=fill.price / entry_price - 1,
                       fill_recovery=evidence, **_trade_costs(fill, fill.quantity, fill.amount, entry_price))
            lines[index] = json.dumps(row, ensure_ascii=False) + "\n"
            corrected = "".join(lines).encode()
            _atomic_write(JOURNAL_PATH, corrected)
            if JOURNAL_PATH.read_bytes() != corrected:
                raise RuntimeError("sell recovery persistence verification failed")
            _atomic_write(audit / "committed.json", json.dumps(_recovery_receipt(now, order, fill.quantity, fill.price, fill.fee), ensure_ascii=False).encode())
            order_guard.clear(ticker)
            logger.info("sell_fill_recovered ticker=%s quantity=%d price=%.8f fee=%.2f", ticker, fill.quantity, fill.price, fill.fee)
    except portfolio_store.PortfolioLockBusy:
        return False
    _flush_recovery_alerts()
    return True
