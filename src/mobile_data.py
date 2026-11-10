"""개인용 PWA 조회 모델. 운영 파일의 허용한 필드만 읽고 외부 API는 호출하지 않는다."""

import json
import math
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.market_calendar import is_krx_trading_day, session_clock
from src.schemas import MobileSnapshot

KST = ZoneInfo("Asia/Seoul")
COHORT = "kis_master_naver_json_20261005"
TRADE_FIELDS = (
    "event", "day", "ticker", "reason", "reasoning", "shares", "shares_bought", "quantity",
    "shares_sold", "shares_before", "shares_after", "entry_price", "exit_price", "fill_price",
    "buy_amount", "sell_amount", "sell_amount_source", "exit_price_source", "fee_amount",
    "net_pnl_amount", "net_pnl_pct", "realized_pnl_pct", "holding_days", "position_fraction_sold",
    "decision_price", "correction_note", "entry_price_source",
)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


class Records:
    def __init__(self, root: Path):
        self.root = root
        self.warnings: list[str] = []

    def json(self, name: str):
        try:
            p = self.root / name
            if not p.exists():
                return None
            if p.stat().st_size > 8_000_000:
                raise ValueError("oversize")
            return json.loads(p.read_text())
        except (OSError, ValueError):
            self.warnings.append(f"{name} 기록을 읽지 못했습니다.")
            return None

    def rows(self, name: str) -> list[dict]:
        p = self.root / name
        if not p.exists():
            return []
        rows = []
        try:
            if p.stat().st_size > 128_000_000:
                raise ValueError("oversize")
            with p.open() as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError("invalid record")
                        rows.append(row)
                    except ValueError:
                        self.warnings.append(f"{name}에 읽을 수 없는 기록이 있습니다.")
        except (OSError, ValueError):
            self.warnings.append(f"{name} 기록을 읽지 못했습니다.")
        return rows


def trading_days(through: date, count: int) -> list[str]:
    days = []
    while len(days) < count:
        if is_krx_trading_day(through):
            days.append(through.isoformat())
        through -= timedelta(days=1)
    return list(reversed(days))


def snapshot(root: Path, *, now: datetime | None = None, initial_capital: float = 100_000_000, account_path: Path | None = None) -> MobileSnapshot:
    now = (now or datetime.now(KST)).astimezone(KST)
    from src import exit_only, operating_mode
    monitoring_day = now.date()
    if operating_mode.EXIT_ONLY:
        try:
            monitoring_day = min(monitoring_day, exit_only.load(root / "exit_only_state.json").started_at.date())
        except (OSError, ValueError, TypeError):
            pass  # The projection below fails closed; no broker fallback.
    today = now.date().isoformat()
    data = Records(root)
    nav_rows = data.rows("account_nav.jsonl")
    nav_by_day = {r["day"]: r for r in nav_rows if isinstance(r.get("day"), str) and r["day"] <= today}
    valid_nav = [r for _, r in sorted(nav_by_day.items()) if r.get("status") == "ok" and number(r.get("total")) is not None]
    latest = valid_nav[-1] if valid_nav else None
    daily_latest = latest
    if account_path and account_path.exists():
        from src.schemas import MobileAccountObservation
        try:
            observation = MobileAccountObservation.model_validate_json(account_path.read_text())
            observed = observation.observed_at
            previous = datetime.fromisoformat(latest["observed_at"]) if latest else None
            if observed.tzinfo and observed <= now and (previous is None or observed > previous):
                latest = observation.model_dump(mode="json")
        except (ValueError, TypeError, KeyError, OSError):
            data.warnings.append("수동 잔고 조회 기록을 읽지 못했습니다.")
    account = None
    holdings = []
    names: dict[str, str] = {}
    names_file = Path(__file__).resolve().parent.parent / "web" / "assets" / "ticker-names.json"
    if names_file.exists():
        try:
            names = json.loads(names_file.read_text()).get("ticker_names", {})
        except (OSError, ValueError, AttributeError):
            pass
    # 매도 후 최신 잔고에서 사라져도 과거 브로커 관측의 종목명을 유지한다.
    for observation in valid_nav:
        for holding in observation.get("holdings", []):
            if isinstance(holding, dict) and holding.get("pdno") and holding.get("prdt_name"):
                names[str(holding["pdno"])] = str(holding["prdt_name"])
    if latest:
        account = {k: latest.get(k) for k in ("day", "observed_at", "total", "cash", "securities", "daily_return",
                                             "return_since_first_observation", "max_drawdown_since_first_observation")}
        total = number(latest["total"])
        cash = number(latest.get("cash"))
        account.update(initial_capital=initial_capital, pnl=total - initial_capital,
                       return_since_start=total / initial_capital - 1 if initial_capital > 0 else None,
                       cash_ratio=cash / total if cash is not None and total > 0 else None)
        for h in latest.get("holdings", []):
            if not isinstance(h, dict):
                continue
            ticker = str(h.get("pdno", ""))
            name = str(h.get("prdt_name") or names.get(ticker) or ticker)
            names[ticker] = name
            holdings.append({"ticker": ticker, "name": name,
                             "quantity": number(h.get("hldg_qty")), "entry_price": number(h.get("pchs_avg_pric")),
                             "price": number(h.get("prpr")), "value": number(h.get("evlu_amt")),
                             "pnl": number(h.get("evlu_pfls_amt")), "pnl_pct": number(h.get("evlu_pfls_rt"))})
        # 같은 잔고 관측의 평가손익을 합산한다. 종목 수익률을 단순 평균하지 않는다.
        account.update(securities_pnl=None, securities_return_pct=None)
        securities = number(latest.get("securities"))
        if securities == 0 and not latest.get("holdings"):
            account["securities_pnl"] = 0.0
        elif (holdings and len(holdings) == len(latest.get("holdings", []))
              and all(h["value"] is not None and h["pnl"] is not None for h in holdings)
              and securities is not None
              and math.isclose(sum(h["value"] for h in holdings), securities, rel_tol=0, abs_tol=1)):
            stock_pnl = sum(h["pnl"] for h in holdings)
            stock_cost = securities - stock_pnl
            account["securities_pnl"] = stock_pnl
            account["securities_return_pct"] = stock_pnl / stock_cost * 100 if stock_cost > 0 else None
    if not operating_mode.EXIT_ONLY and nav_by_day and list(sorted(nav_by_day.items()))[-1][1].get("status") != "ok":
        data.warnings.append("최근 계좌 조회가 실패했습니다. 마지막으로 확인한 금액을 표시합니다.")

    runs = {r["day"]: r for r in data.rows("daily_runs.jsonl") if isinstance(r.get("day"), str) and r["day"] <= today}
    run = runs.get(today)
    try:
        market_open_day = is_krx_trading_day(now.date())
        days = trading_days(monitoring_day, 20)
        expected_nav_day = trading_days(now.date() if now.time() >= session_clock(now.date(), 15, 40) else now.date() - timedelta(days=1), 1)[0]
        if not operating_mode.EXIT_ONLY and daily_latest and daily_latest["day"] < expected_nav_day:
            data.warnings.append(f"일별 계좌 기록이 {daily_latest['day']} 이후 갱신되지 않았습니다.")
        next_day = now.date() + timedelta(days=1)
        while not is_krx_trading_day(next_day):
            next_day += timedelta(days=1)
        next_trading_day = next_day.isoformat()
    except ValueError:
        market_open_day, days, next_trading_day = None, [], None
        data.warnings.append("거래일 달력을 갱신해야 합니다.")

    state, label = "waiting", "분석 시작 대기"
    if market_open_day is False:
        state, label = "holiday", "오늘은 휴장일"
    elif market_open_day is None:
        state, label = "failed", "거래 달력 확인 필요"
    elif run:
        state = run.get("status", "unknown")
        labels = {"completed": "정상 관망" if not run.get("pending") else "매수 판단 완료",
                  "degraded": "일부 분석 실패", "failed": "분석 실패", "skipped": "분석 미실행",
                  "started": "분석 준비 중", "collecting": "자료 수집 중", "analysis_started": "분석 중"}
        label = labels.get(state, "실행 상태 확인 필요")
        if state in {"started", "collecting", "analysis_started"} and now.time() >= session_clock(now.date(), 8, 55):
            state, label = "unconfirmed", "분석 종료 기록 없음"
    elif now.time() >= session_clock(now.date(), 8, 55):
        state, label = "missing", "오늘 분석 기록 없음"
    capacity = data.json("codex_plan_capacity.json") or {}
    if not isinstance(capacity, dict):
        capacity = {}
    unconfirmed = data.json("unconfirmed_orders.json")
    unconfirmed_count = len(unconfirmed) if isinstance(unconfirmed, (dict, list)) else None
    operation = {"day": today, "status": state, "label": label, "next_trading_day": next_trading_day,
                 "reason": (run or {}).get("reason"), "run": {k: (run or {}).get(k) for k in
                 ("started_at", "ended_at", "universe", "candidates", "decisions", "hold", "buy", "pending",
                  "collection_failed", "provider_failed", "analyst_failed")}, "unconfirmed_orders": unconfirmed_count,
                 "capacity": {k: capacity.get(k) for k in ("checked_at", "day", "remaining_percent", "buy_judgment_allowed")}}

    entries = {(r.get("day"), r.get("ticker")): r for r in data.rows("pipeline.jsonl") if r.get("day") in days}
    signal_days = {r["day"] for r in entries.values() if r.get("action") == "BUY" and r.get("approved")
                   and (r["day"] not in runs or runs[r["day"]].get("status") in {"completed", "degraded"})}
    gate = Counter(r["rejected_by"] for r in entries.values() if r.get("rejected_by") and not r.get("approved"))
    day_status = [{"day": d, "status": runs.get(d, {}).get("status", "unknown"), "signal": d in signal_days,
                   "has_decisions": any(k[0] == d for k in entries)} for d in days]
    normal_days = sum(r.get("status") == "completed" and r.get("cohort") == COHORT for r in runs.values())
    usage = {}
    for r in data.rows("llm_calls.jsonl"):
        stamp = r.get("day")
        if not stamp:
            try:
                moment = datetime.fromisoformat(str(r.get("timestamp", "")).replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    raise ValueError("timezone missing")
                stamp = moment.astimezone(KST).date().isoformat()
            except ValueError:
                data.warnings.append("시각을 확인할 수 없는 분석가 호출 기록이 있습니다.")
                continue
        label_name = str(r.get("label", "unknown"))
        if stamp not in days or label_name.startswith(("shadow", "probe", "audit")):
            continue
        u = usage.setdefault(label_name, {"calls": 0, "failures": 0, "input_tokens": 0, "output_tokens": 0})
        u["calls"] += 1
        u["failures"] += not bool(r.get("success"))
        u["input_tokens"] += number(r.get("input_tokens")) or 0
        u["output_tokens"] += number(r.get("output_tokens")) or 0
    monitoring = {"days": day_status, "signal_days": len(signal_days), "total_days": len(days),
                  "signal_ratio": len(signal_days) / len(days) if days else None,
                  "gate_rejections": dict(gate), "analysts": usage}

    trades = []
    for index, row in enumerate(data.rows("trade_journal.jsonl")):
        if str(row.get("day", "")) > today:
            continue
        trade = {k: row.get(k) for k in TRADE_FIELDS if k in row}
        if row.get("event") == "buy" and isinstance(row.get("decision"), dict):
            trade["reasoning"] = row["decision"].get("reason")
        trade.update(id=str(index), name=names.get(str(row.get("ticker")), str(row.get("ticker", ""))))
        trades.append(trade)
    trades.sort(key=lambda r: (str(r.get("day", "")), int(r["id"])), reverse=True)
    ic = data.json("ic_summary.json") or {}
    history = data.rows("ic_history.jsonl")
    if history:
        ic = history[-1]
    if not isinstance(ic, dict):
        ic = {}
    measurements = {"normal_days": normal_days, "target_days": 60, "as_of": ic.get("as_of"),
                    "segments": ic.get("segments", {}), "first_nav_day": valid_nav[0]["day"] if valid_nav else None}
    result = MobileSnapshot(generated_at=now, account=account, holdings=holdings, trades=trades[:250],
                          nav_history=[{k: r.get(k) for k in ("day", "total", "daily_return")} for r in valid_nav],
                          operation=operation, monitoring=monitoring, measurements=measurements,
                          warnings=list(dict.fromkeys(data.warnings)))
    if operating_mode.EXIT_ONLY:
        return exit_only_snapshot(result, root, now, initial_capital)
    return result


def exit_only_snapshot(result: MobileSnapshot, root: Path, now: datetime, initial_capital: float) -> MobileSnapshot:
    from src import exit_only
    result.account, result.holdings = None, []
    result.operation.update(mode="exit_only", status="exit_only", label="기존 보유분 가상 매도 추적",
                            reason="신규 매수 판단 중단", run={}, capacity={}, unconfirmed_orders=0)
    result.measurements["paused"] = True
    try:
        state = exit_only.load(root / "exit_only_state.json")
    except (OSError, ValueError, TypeError):
        result.operation.update(status="failed", label="가상 매도 장부 확인 필요")
        result.warnings.append("가상 매도 장부를 읽지 못했습니다. 이전 증권사 잔고를 현재 보유분으로 표시하지 않습니다.")
        return result
    result.operation.update(started_at=state.started_at.isoformat(),
                            last_checked_at=state.last_checked_at.isoformat() if state.last_checked_at else None,
                            completed_at=state.completed_at.isoformat() if state.completed_at else None)
    for day in result.monitoring.get("days", []):
        if day["day"] > state.started_at.date().isoformat():
            day.update(status="paused", signal=False)
    if state.completed_at:
        result.operation.update(status="completed", label="기존 보유분 가상 매도 완료")
    for p in state.portfolio.positions:
        q = state.quotes[p.ticker]
        value, cost = p.quantity * q.price, p.quantity * p.entry_price
        result.holdings.append({"ticker": p.ticker, "name": state.names[p.ticker], "quantity": p.quantity,
                               "entry_price": p.entry_price, "price": q.price, "value": value,
                               "pnl": value - cost, "pnl_pct": (value / cost - 1) * 100,
                               "observed_at": q.observed_at.isoformat(), "simulated": True})
    stocks = sum(h["value"] for h in result.holdings)
    costs = sum(h["quantity"] * h["entry_price"] for h in result.holdings)
    total = state.cash + stocks
    observed = min((state.quotes[p.ticker].observed_at for p in state.portfolio.positions), default=state.updated_at)
    result.account = {"day": observed.astimezone(KST).date().isoformat(), "observed_at": observed.isoformat(),
                      "total": total, "cash": state.cash, "securities": stocks, "initial_capital": initial_capital,
                      "pnl": total - initial_capital, "return_since_start": total / initial_capital - 1 if initial_capital else None,
                      "cash_ratio": state.cash / total if total else None, "securities_pnl": stocks - costs,
                      "securities_return_pct": (stocks / costs - 1) * 100 if costs else None,
                      "simulated": True, "transition_total": state.initial_total,
                      "return_since_transition": total / state.initial_total - 1}
    result.nav_history = [r for r in result.nav_history if r["day"] < state.started_at.date().isoformat()] + state.nav_history
    for sale in state.sales:
        result.trades.append({"id": sale.id, "event": "sell", "day": sale.at.date().isoformat(),
                              "at": sale.at.isoformat(), "ticker": sale.ticker, "name": state.names[sale.ticker],
                              "reason": sale.reason, "shares_sold": sale.shares_sold, "shares_after": sale.shares_after,
                              "position_fraction_sold": sale.shares_sold / sale.shares_before,
                              "entry_price": sale.entry_price, "exit_price": sale.exit_price,
                              "sell_amount": sale.sell_amount, "fee_amount": sale.fee_amount,
                              "tax_amount": sale.tax_amount, "net_pnl_amount": sale.net_pnl_amount,
                              "simulated": True, "sell_amount_source": "simulated_quote"})
    result.trades.sort(key=lambda r: (r["day"], r.get("at", "")), reverse=True)
    result.trades = result.trades[:250]
    if state.missing_quotes:
        result.warnings.append("시세 미확인: " + ", ".join(state.names[t] for t in state.missing_quotes) + ". 해당 종목은 마지막 가격을 표시합니다.")
    if state.basis_warnings:
        result.warnings.append("주식 수·가격 기준 변경 확인 필요: " + ", ".join(state.names[t] for t in state.basis_warnings)
                               + ". 해당 종목의 가상 매도를 중단하고 기존 수량·매도 기준을 보존했습니다.")
    if state.portfolio.positions:
        from src.market_calendar import monitor_due
        try:
            due = monitor_due(now)
            if (due >= state.started_at and (state.last_checked_at is None
                    or (due - state.last_checked_at).total_seconds() > 120)):
                result.warnings.append("가상 매도 감시의 예정 확인 시각을 2분 넘게 놓쳤습니다. 마지막 가격 확인 시각을 확인하세요.")
        except ValueError:
            result.warnings.append("거래 달력이 등록되지 않아 시세 감시를 중단했습니다. 달력 갱신이 필요합니다.")
        from src.market_calendar import SUPPORTED_YEARS
        if now.month == 12 and now.year + 1 not in SUPPORTED_YEARS:
            result.warnings.append("다음 연도 거래 달력 확인이 필요합니다. 미등록 연도에는 감시가 중단됩니다.")
    return result
