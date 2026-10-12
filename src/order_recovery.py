"""접수된 신규 매수의 지연 체결을 재대조한다. 주문·분석·출구 재설정 없음.

브로커 조회는 포트폴리오 락 밖에서 하고, 짧은 커밋 락 안에서 원본을 다시 비교한다.
매도/추가매수/응답 유실/지난 날짜/애매한 원장은 자동 해제하지 않는다.
"""

import hashlib
import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src import kis, order_guard, portfolio_store

logger = logging.getLogger(__name__)
JOURNAL_PATH = Path("logs/trade_journal.jsonl")
AUDIT_DIR = Path("logs/order_recoveries")
# 다음 분에 다시 조회한다. 회차 안에서 재시도하거나 체결을 기다리지 않는다.
QUERY_POLICY = kis.RetryPolicy(timeout_seconds=2.0, max_attempts=1, backoff_seconds=(0,))
KST = ZoneInfo("Asia/Seoul")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _position_values(position):
    return position.model_dump(mode="json", exclude={"peak_price", "peak_reset_day"})


def recover_pending_buy(now: datetime) -> bool:
    """한 회차 한 종목을 순환 대조한다. 호출자는 포트폴리오 락을 잡지 않아야 한다.

    여러 미확인 건이 있어도 한 종목의 지연이 다른 종목의 조회를 굶기지 않게 분 단위로
    순환한다. 매수 후보를 제한하는 정책과는 무관한 조회 부하 제한이다.
    False는 확인 못 함/변화 없음이다. 파일 쓰기 실패는 전파하며 마커를 유지한다.
    """
    now = now.astimezone(KST)
    day = now.date().isoformat()
    try:
        with portfolio_store.portfolio_lock(blocking=False):
            orders = order_guard.load_orders()
            candidates = sorted(t for t, o in orders.items() if o.side == "buy"
                                and not o.ready_to_commit and o.created_at.astimezone(KST).date() == now.date())
            if not candidates or not JOURNAL_PATH.exists():
                return False
            ticker = candidates[int(now.timestamp() // 60) % len(candidates)]
            order = orders[ticker]
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
            if (row["event"] != "buy" or not row.get("order_no") or row.get("quantity") != order.quantity
                    or row.get("exit_plan_source") == "existing_position"):
                return False
            # 재시작 시 이미 교정된 일지는 그대로 검증한다. 원본 정보는 교정 행에 보존한다.
            recovery = row.get("fill_recovery")
            if recovery is None:
                if row.get("entry_price_source") != "fill_partial":
                    return False
                old_price = row["entry_price"]
            else:
                if recovery.get("order_created_at") != order.created_at.isoformat():
                    return False
                old_price = recovery["entry_price_before"]
            # '신규' 플래그만 믿지 않고 과거 수량 체인도 확인한다.
            previous_qty = sum((r.get("quantity", 0) if r.get("event") == "buy" else -r.get("shares_sold", 0))
                               for r in rows[:index] if r.get("ticker") == ticker and r.get("event") in ("buy", "sell"))
            if previous_qty != 0:
                return False
            portfolio = portfolio_store.load_portfolio()
            position = next((p for p in portfolio.positions if p.ticker == ticker), None)
            if (position is None or position.quantity != order.quantity or position.entry_day != now.date()
                    or position.take_profit_stage != 0 or position.entry_price not in (old_price, row["entry_price"])
                    or (position.exit_plan.model_dump(mode="json") if position.exit_plan else None) != row.get("exit_plan")):
                return False
            before_position = _position_values(position)
    except portfolio_store.PortfolioLockBusy:
        return False

    # 순차 GET 세 건, 각 한 번만. 조회 실패는 다음 크론 회차까지 차단 유지.
    buy = kis.fetch_daily_fill_totals(ticker, now.date(), "buy", policy=QUERY_POLICY, require_fee=True)
    if buy is None or buy[0] != order.quantity or not all(math.isfinite(x) for x in buy) or buy[1] <= 0 or buy[2] < 0:
        return False
    sold = kis.fetch_daily_fill_totals(ticker, now.date(), "sell", policy=QUERY_POLICY, require_fee=True)
    if sold != (0, 0.0, 0.0):
        return False
    holdings = kis.fetch_holdings(policy=QUERY_POLICY)
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
            _atomic_write(audit / "committed.json", json.dumps({"at": now.isoformat(), "quantity": quantity,
                          "price": price, "fee": buy[2]}, indent=2).encode())
            order_guard.clear(ticker)
            logger.info("buy_fill_recovered ticker=%s quantity=%d price=%.8f fee=%.2f", ticker, quantity, price, buy[2])
            return True
    except portfolio_store.PortfolioLockBusy:
        return False
