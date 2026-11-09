"""Forward-only liquidation simulation. Never writes the broker portfolio or trade journal."""
import asyncio
import hashlib
import logging
import math
import os
import uuid
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from src import app_notifications, collectors, kis, notify, sell
from src.market_calendar import is_krx_trading_day
from src.portfolio_store import portfolio_lock, PortfolioLockBusy
from src.schemas import AppAlert, ExitOnlyQuote, ExitOnlySale, ExitOnlyState, PortfolioState

KST = ZoneInfo("Asia/Seoul")
STATE_PATH = Path("logs/exit_only_state.json")
logger = logging.getLogger(__name__)


def save(state: ExitOnlyState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        stream.write(state.model_dump_json(indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o644)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load(path: Path | None = None) -> ExitOnlyState:
    state = ExitOnlyState.model_validate_json((path or STATE_PATH).read_text())
    original = {p.ticker: p for p in state.source_portfolio.positions}
    current = state.portfolio.positions
    if len(original) != len(state.source_portfolio.positions) or len({p.ticker for p in current}) != len(current):
        raise ValueError("duplicate simulation holdings")
    quantities = {t: p.quantity for t, p in original.items()}
    cash = state.initial_cash
    ids = set()
    for sale in state.sales:
        if (sale.id in ids or sale.ticker not in original or quantities[sale.ticker] != sale.shares_before
                or sale.shares_before - sale.shares_sold != sale.shares_after
                or sale.entry_price != original[sale.ticker].entry_price):
            raise ValueError("inconsistent simulated sale history")
        ids.add(sale.id)
        quantities[sale.ticker] = sale.shares_after
        cash += sale.sell_amount - sale.fee_amount - sale.tax_amount
    if ({p.ticker: p.quantity for p in current} != {t: q for t, q in quantities.items() if q}
            or not math.isclose(cash, state.cash, abs_tol=.001, rel_tol=0)):
        raise ValueError("simulation cash or share conservation failed")
    if (state.started_at.tzinfo is None or state.updated_at.tzinfo is None
            or state.updated_at < state.started_at):
        raise ValueError("invalid simulation timestamps")
    for p in current:
        old = original.get(p.ticker)
        if (old is None or not p.quantity or p.quantity < 0 or p.quantity > old.quantity
                or p.entry_price != old.entry_price or p.entry_day != old.entry_day
                or p.exit_plan != old.exit_plan):
            raise ValueError("simulation holding differs from preserved entry")
        if (p.ticker not in state.names or p.ticker not in state.quotes
                or state.quotes[p.ticker].observed_at.tzinfo is None):
            raise ValueError("missing simulation quote or name")
    if bool(state.completed_at) != (not current):
        raise ValueError("inconsistent simulation completion")
    return state


def notice(state: ExitOnlyState, at: datetime, title: str, body: str, kind: str = "info") -> None:
    key = f"{state.session_id}:{at.isoformat()}:{title}:{body}"
    state.outbox.append(AppAlert(id=uuid.uuid5(uuid.NAMESPACE_URL, key).hex,
                                created_at=at, title=title, body=body, kind=kind))


def initialize(portfolio_bytes: bytes, account: dict, *, at: datetime, path: Path | None = None) -> ExitOnlyState:
    """Explicit one-time import from a separately preserved broker observation."""
    path = path or STATE_PATH
    with portfolio_lock(path.with_suffix(".lock"), blocking=False):
        if path.exists():
            raise FileExistsError("simulation already initialized; refusing to reset it")
        portfolio = PortfolioState.model_validate_json(portfolio_bytes)
        holdings = account["holdings"]
        rows = {str(h["pdno"]): h for h in holdings if int(h["hldg_qty"]) > 0}
        if len(rows) != len(holdings) or not portfolio.positions:
            raise ValueError("invalid transition holdings")
        if {p.ticker: p.quantity for p in portfolio.positions} != {t: int(h["hldg_qty"]) for t, h in rows.items()}:
            raise ValueError("broker and preserved portfolio quantities differ")
        if any(not p.entry_price or p.entry_price <= 0 for p in portfolio.positions):
            raise ValueError("missing preserved entry price")
        total, securities = float(account["total"]), float(account["securities"])
        computed = sum(int(h["hldg_qty"]) * float(h["prpr"]) for h in rows.values())
        if not math.isclose(computed, securities, abs_tol=1, rel_tol=0):
            raise ValueError("transition securities do not match quantities and prices")
        observed = datetime.fromisoformat(account["observed_at"])
        if observed.tzinfo is None or not 0 <= (at - observed).total_seconds() <= 3600:
            raise ValueError("transition observation must be timezone-aware and less than an hour old")
        state = ExitOnlyState(
            session_id=uuid.uuid4().hex, started_at=at, updated_at=at,
            source_portfolio_sha256=hashlib.sha256(portfolio_bytes).hexdigest(),
            source_portfolio=portfolio.model_copy(deep=True), portfolio=portfolio,
            names={t: str(h.get("prdt_name") or t) for t, h in rows.items()},
            cash=total - securities, initial_cash=total - securities, initial_total=total,
            fee_rate=kis.BROKERAGE_FEE_RATE, tax_rate=kis.SELL_TAX_RATE,
            quotes={t: ExitOnlyQuote(price=float(h["prpr"]), observed_at=observed,
                                     source="transition_snapshot") for t, h in rows.items()},
            nav_history=[{"day": at.date().isoformat(), "total": total, "daily_return": None}],
        )
        notice(state, at, "기존 보유분 가상 매도 추적 시작",
               f"{len(rows)}종목의 기존 매도 기준을 유지합니다. 신규 매수 판단과 증권사 주문은 중단했습니다. "
               "이후 매도는 기준 확인 시점의 조회 가격으로 기록하며 수수료·세금은 추정합니다.")
        save(state, path)
        return state


def apply_quotes(state: ExitOnlyState, quotes: dict, *, at: datetime) -> None:
    """All mutations stay in memory until positions, cash and sales can be saved together."""
    at = at.astimezone(KST)
    if at < state.updated_at:
        raise ValueError("simulation clock moved backwards")
    trading = is_krx_trading_day(at.date()) and time(9) <= at.time() <= time(15, 30, 59)
    state.last_checked_at = at
    state.missing_quotes = []
    positions = []
    for position in state.portfolio.positions:
        entry = quotes.get(position.ticker)
        if entry is None:
            state.missing_quotes.append(position.ticker)
            positions.append(position)
            continue
        quote, source = entry
        if not valid_quote(quote) or (trading and not quote.traded_today):
            state.missing_quotes.append(position.ticker)
            positions.append(position)
            continue
        state.quotes[position.ticker] = ExitOnlyQuote(price=quote.price, observed_at=at, source=source)
        if not trading:
            positions.append(position)
            continue
        # The transition day's daily range includes prices before this simulation existed.
        # Keep the imported peak, but only use new observed prices on that one day.
        if at.date() == state.started_at.astimezone(KST).date():
            quote = kis.Quote(price=quote.price, day_high=quote.price, day_low=quote.price,
                              open_price=quote.open_price, prev_close=quote.prev_close)
        position = sell.update_peak_price(position, quote.price, day_high=quote.day_high, today=at.date())
        action, from_range = sell.evaluate_with_day_range(position, quote, today=at.date())
        if action is None:
            positions.append(position)
            continue
        before = position.quantity
        quantity = int(before * action.sell_fraction)
        if quantity == 0:
            quantity = before  # Existing broker path liquidates the remaining dust shares.
        remaining = before - quantity
        amount = quote.price * quantity
        fee, tax = amount * state.fee_rate, amount * state.tax_rate
        entry_cost = position.entry_price * quantity
        net = amount - fee - tax - entry_cost * (1 + state.fee_rate)
        event = ExitOnlySale(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"{state.session_id}:{len(state.sales)}").hex,
            at=at, ticker=position.ticker, reason=sell.exit_trigger(action, position),
            shares_before=before, shares_sold=quantity, shares_after=remaining,
            entry_price=position.entry_price, exit_price=quote.price, sell_amount=amount,
            fee_amount=fee, tax_amount=tax, net_pnl_amount=net,
            from_day_range=from_range, quote_source=source,
        )
        state.sales.append(event)
        state.cash += amount - fee - tax
        if remaining:
            update = {"quantity": remaining, "weight": position.weight * remaining / before}
            if action.reason == "take_profit_trail":
                update.update(take_profit_stage=position.take_profit_stage + 1,
                              peak_price=quote.price, peak_reset_day=at.date())
            positions.append(position.model_copy(update=update))
        label = {"stop_loss": "손절", "take_profit_first": "첫 부분 익절", "take_profit_trail": "트레일링 익절"}[event.reason]
        notice(state, at, f"가상 매도 · {state.names[position.ticker]}",
               f"{label} 기준 충족\n{quantity}주 × {quote.price:,.0f}원 (조회 가격 가정)\n"
               f"잔여 {remaining}주 · 추정 비용 반영 손익 {net:+,.0f}원\n증권사 주문·체결은 없습니다.", "sell")
    state.portfolio = PortfolioState(positions=positions, cash_weight=1 - sum(p.weight for p in positions))
    state.updated_at = at
    if not positions and state.completed_at is None:
        state.completed_at = at
        notice(state, at, "기존 보유분 가상 매도 완료", "모든 보유 수량이 0주가 되었습니다. 추가 매수와 시세 조회를 멈춥니다.")
    total = state.cash + sum(p.quantity * state.quotes[p.ticker].price for p in positions)
    if not state.missing_quotes:
        today = at.date().isoformat()
        previous = next((r["total"] for r in reversed(state.nav_history) if r["day"] < today), None)
        state.nav_history = [r for r in state.nav_history if r["day"] != today]
        state.nav_history.append({"day": today, "total": total, "daily_return": total / previous - 1 if previous else None})


def flush_outbox(state: ExitOnlyState, path: Path) -> None:
    while state.outbox:
        if not app_notifications.enqueue_alert(state.outbox[0]):
            return
        state.outbox.pop(0)
        save(state, path)


def valid_quote(quote: kis.Quote) -> bool:
    try:
        return (all(math.isfinite(v) for v in (quote.price, quote.day_high, quote.day_low))
                and quote.price > 0 and quote.day_high >= quote.price >= quote.day_low > 0)
    except (TypeError, ValueError):
        return False  # None high/low is normal before that stock's first opening trade.


async def monitor(*, at: datetime | None = None, path: Path | None = None) -> None:
    path = path or STATE_PATH
    supplied_time = at is not None
    at = (at or datetime.now(KST)).astimezone(KST)
    try:
        with portfolio_lock(path.with_suffix(".lock"), blocking=False):
            state = load(path)
            flush_outbox(state, path)
            if not state.portfolio.positions or not is_krx_trading_day(at.date()):
                return
            # Include a closing valuation, but never simulate an after-hours sale.
            if not time(9) <= at.time() < time(15, 36):
                return
            results = await asyncio.gather(*(
                asyncio.to_thread(kis.fetch_quote, p.ticker, policy=kis.FAST_FAIL_POLICY)
                for p in state.portfolio.positions), return_exceptions=True)
            quotes = {p.ticker: (q, "kis") for p, q in zip(state.portfolio.positions, results)
                      if isinstance(q, kis.Quote) and valid_quote(q)}
            missing = [p.ticker for p in state.portfolio.positions if p.ticker not in quotes]
            if missing:
                try:
                    fallback = await asyncio.wait_for(asyncio.to_thread(collectors.fetch_naver_quotes, missing), 5)
                    quotes.update({t: (q, "naver") for t, q in fallback.items()
                                   if t in missing and isinstance(q, kis.Quote) and valid_quote(q)})
                except Exception:
                    logger.exception("exit_only_secondary_quote_failed")
            at = at if supplied_time else datetime.now(KST)
            apply_quotes(state, quotes, at=at)
            save(state, path)
            flush_outbox(state, path)
            if state.missing_quotes:
                notify.alert_once_per_day("exit_only_missing_quotes", notify.format_error_alert(
                    "가상 매도 시세 조회 실패", ", ".join(state.names[t] for t in state.missing_quotes)), today=at.date())
            logger.info("exit_only_checked positions=%d quotes=%d sales=%d missing=%d",
                        len(state.portfolio.positions), len(quotes), len(state.sales), len(state.missing_quotes))
    except PortfolioLockBusy:
        logger.info("exit_only_skipped lock_busy")
