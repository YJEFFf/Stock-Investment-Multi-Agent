"""Deterministic validation at external-data and persisted-state boundaries."""
import math
from datetime import datetime, timedelta

from src.market_calendar import market_session, previous_session_day, KST


def validate_bars(bars, *, as_of: datetime | None = None):
    if not bars or len({b.date for b in bars}) != len(bars):
        raise ValueError("missing or duplicate bars")
    if [b.date for b in bars] != sorted(b.date for b in bars):
        raise ValueError("bars out of order")
    for b in bars:
        if (not all(math.isfinite(v) for v in (b.open, b.high, b.low, b.close))
                or b.volume < 0 or b.close <= 0):
            raise ValueError("invalid OHLCV values")
        # A suspended session may carry its close with zero O/H/L/volume.
        if b.volume == 0 and b.open == b.high == b.low == 0:
            continue
        if not 0 < b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high:
            raise ValueError("inconsistent OHLC range")
    if as_of is not None:
        at = as_of.astimezone(KST)
        session = market_session(at.date())
        expected = at.date() if session and at >= session.close else previous_session_day(at.date())
        if bars[-1].date != expected:
            raise ValueError("stale or future bars")
    return bars


def validate_portfolio(portfolio):
    weights = [p.weight for p in portfolio.positions]
    if (not all(math.isfinite(w) and 0 <= w <= 1 for w in [portfolio.cash_weight, *weights])
            or not math.isclose(portfolio.cash_weight + sum(weights), 1, abs_tol=1e-8)
            or len({p.ticker for p in portfolio.positions}) != len(portfolio.positions)):
        raise ValueError("invalid portfolio weights or duplicate positions")
    for p in portfolio.positions:
        if p.quantity is not None and (type(p.quantity) is not int or p.quantity <= 0):
            raise ValueError("invalid position quantity")
        for value in (p.entry_price, p.peak_price):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("invalid position price")
    return portfolio
