"""실행 관측과 일일 분석 중복 방지. 수집 실패와 정상 관망을 별도로 보존한다."""

import json
import os
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from src.market_calendar import is_krx_trading_day
from src.schemas import DailyRunRecord, DataSourceObservation

RUN_LOG_PATH = Path("logs/daily_runs.jsonl")
RUN_LOCK_PATH = Path("logs/decide_buys.lock")
NAV_LOG_PATH = Path("logs/account_nav.jsonl")
IC_HISTORY_PATH = Path("logs/ic_history.jsonl")
CURRENT: ContextVar[DailyRunRecord | None] = ContextVar("daily_run", default=None)
KST = ZoneInfo("Asia/Seoul")
COHORT = "kis_master_naver_json_20261005"


def append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        f.flush()
        os.fsync(f.fileno())


def git_commit() -> str:
    # subprocess를 쓰지 않는다. 테스트의 Codex 프로세스 차단과 무관한 메타데이터다.
    git = Path(__file__).resolve().parent.parent / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head
        ref = head[5:]
        if (git / ref).exists():
            return (git / ref).read_text().strip()
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line.split()[0]
    except OSError:
        pass
    return "unknown"


def new_run(now: datetime, provider: str, model: str) -> DailyRunRecord:
    return DailyRunRecord(run_id=uuid4().hex, day=now.date().isoformat(), started_at=now,
                          git_commit=git_commit(), provider=provider, model=model)


def write(record: DailyRunRecord) -> None:
    append(RUN_LOG_PATH, record.model_dump(mode="json"))


def load_runs() -> list[DailyRunRecord]:
    if not RUN_LOG_PATH.exists():
        return []
    # 손상된 중복 방지 이력은 무시하고 재분석하지 않는다.
    return [DailyRunRecord.model_validate_json(line)
            for line in RUN_LOG_PATH.read_text().splitlines() if line.strip()]


def already_analyzed(day: str, decision_log: Path) -> bool:
    if any(r.day == day and r.analysis_started for r in load_runs()):
        return True
    # 복구 배포 전 구형 로그도 재분석 방지 근거다.
    if decision_log.exists():
        return any(json.loads(line).get("day") == day
                   for line in decision_log.read_text().splitlines() if line.strip())
    return False


def claim_analysis() -> None:
    record = CURRENT.get()
    if record is not None:
        record.analysis_started = True
        record.status = "analysis_started"
        write(record)  # LLM 호출 전 영속화. 프로세스가 죽어도 당일 판단은 재실행하지 않는다.


def metadata() -> dict:
    record = CURRENT.get()
    if record is None:
        return {}
    return {"run_id": record.run_id, "cohort": record.cohort,
            "provider": record.provider, "model": record.model, "git_commit": record.git_commit}


def observe(ticker: str, source: str, items, *, latest=None, required=True) -> None:
    record = CURRENT.get()
    if record is not None:
        record.sources.append(DataSourceObservation(
            ticker=ticker, source=source, status="failed" if items is None else ("ok" if items else "empty"),
            count=len(items) if items is not None else None, latest=str(latest) if latest is not None else None,
            required=required))


def input_failed(record: DailyRunRecord) -> bool:
    return any((record.collection_failed, record.index_unavailable, record.provider_failed,
                record.analyst_failed, any(s.status == "failed" and s.required for s in record.sources)))


def trading_days(as_of: date, n_days: int) -> list[str]:
    days: list[str] = []
    cursor = as_of
    while len(days) < n_days:
        if is_krx_trading_day(cursor):
            days.append(cursor.isoformat())
        cursor -= timedelta(days=1)
    return sorted(days)


def coverage(as_of: date, n_days: int = 20) -> dict:
    days = trading_days(as_of, n_days)
    latest = {r.day: r for r in load_runs() if r.day in days}
    counts = {s: sum(r.status == s for r in latest.values())
              for s in ("completed", "degraded", "failed", "skipped", "started", "collecting", "analysis_started")}
    return {"from": days[0], "through": days[-1], "total_days": n_days,
            "normal_days": counts.pop("completed"), "missing_days": n_days - len(latest), **counts}


def record_nav(day: str, account) -> None:
    rows = [json.loads(line) for line in NAV_LOG_PATH.read_text().splitlines() if line.strip()] if NAV_LOG_PATH.exists() else []
    # 같은 날짜 재수집은 원본을 보존하되 계산에는 마지막 관측만 쓴다.
    by_day = {r["day"]: r for r in rows if r.get("total") is not None and r["day"] < day}
    prior = [by_day[d] for d in sorted(by_day)]
    if account is None:
        append(NAV_LOG_PATH, {"day": day, "status": "unavailable", "observed_at": datetime.now(KST)})
        return
    total = account.total
    peak = max([total] + [r["total"] for r in prior])
    drawdown = total / peak - 1 if peak else None
    previous_day = trading_days(date.fromisoformat(day) - timedelta(days=1), 1)[0]
    append(NAV_LOG_PATH, {"day": day, "status": "ok", "observed_at": datetime.now(KST),
                          "total": total, "cash": account.cash, "securities": account.securities,
                          "holdings": getattr(account, "holdings", []),
                          "daily_return": total / prior[-1]["total"] - 1 if prior and prior[-1]["day"] == previous_day else None,
                          "previous_observation_day": prior[-1]["day"] if prior else None,
                          "return_since_first_observation": total / prior[0]["total"] - 1 if prior else 0,
                          "drawdown_since_first_observation": drawdown,
                          "max_drawdown_since_first_observation": min([drawdown] + [r.get("drawdown_since_first_observation", 0) for r in prior])})
