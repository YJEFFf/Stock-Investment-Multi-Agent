"""Recover missed starts, never replay a started analysis or an order."""
import asyncio
import json
import logging
import time as clock
import subprocess
import sys
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import codex_plan, notify, pipeline, run_history
from src.market_calendar import is_krx_trading_day
from src.portfolio_store import portfolio_lock, PortfolioLockBusy
from scripts import decide_buys

STATE = Path('logs/operations_recovery.json')
SERVICES = Path('/var/lib/sima-recovery/services.json')
logging.basicConfig(level=logging.INFO)


def save(state):
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def eligible(at, rows, already):
    if not is_krx_trading_day(at.date()) or not time(7, 5) <= at.time() < time(8):
        return False
    if already or any(r.analysis_started for r in rows):
        return False
    # A readiness failure has not made an investment judgment. Other failures are
    # deliberately left to their existing recovery paths.
    return not rows or rows[-1].reason == 'codex_capacity_unavailable'


def main():
    if Path('/etc/sima-recovery.disabled').exists():
        return
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with portfolio_lock(Path('logs/operations_recovery.lock'), blocking=False):
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        if SERVICES.exists():
            for name, row in json.loads(SERVICES.read_text()).items():
                stamp = [row.get('last_attempt'), row.get('result'), row.get('status')]
                if stamp[0] and 'result' in row and state.get(name) != stamp:
                    notify.send_telegram_alert(notify.format_error_alert(
                        '서비스 자동 복구 결과', f'{name}: {row.get("status")}; exit={row.get("result", "pending")}'))
                    state[name] = stamp
                    save(state)
        at = datetime.now(codex_plan.KST)
        # Use exactly the analysis lock. A live run is never finalized or replaced.
        with portfolio_lock(run_history.RUN_LOCK_PATH, blocking=False):
            latest = {r.run_id: r for r in run_history.load_runs()}
            for row in latest.values():
                if row.ended_at is None:
                    row.status = 'failed'
                    row.reason = 'process_interrupted'
                    row.ended_at = at
                    row.elapsed_seconds = (at - row.started_at).total_seconds()
                    run_history.write(row)
                    if row.day == at.date().isoformat():
                        decide_buys._write_pending_state(row.day, [], skipped='decision_process_failed')
                    notify.send_telegram_alert(notify.format_error_alert(
                        '중단된 분석 자동 정리 — 재분석 없음', f'{row.day}: {row.run_id}'))
            rows = [r for r in run_history.load_runs() if r.day == at.date().isoformat()]
            if not eligible(at, rows, run_history.already_analyzed(at.date().isoformat(), pipeline.DEFAULT_LOG_PATH)):
                return
            day = at.date().isoformat()
            attempts = state.setdefault('premarket', {})
            if attempts.get(day, 0) >= 2:
                return
            attempts[day] = attempts.get(day, 0) + 1
            save(state)
        # Readiness has its own cross-process lock. Analysis rechecks its own lock
        # and durable analysis_started marker after we release it above.
        try:
            result = subprocess.run(['bash', 'scripts/check_codex_plan.sh'], timeout=500)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124
        if code:
            if attempts[day] >= 2:
                notify.send_telegram_alert(notify.format_error_alert(
                    '장전 준비 자동 복구 실패 — 확인 필요',
                    f'{day}: 2회 복구 소진, exit={code}; 신규 매수 판단 차단 유지'))
            return
        capacity = codex_plan.load_capacity_status(day)
        if not capacity.buy_judgment_allowed or datetime.now(codex_plan.KST).time() >= time(8):
            return
        asyncio.run(decide_buys.main(recovery=True))


if __name__ == '__main__':
    try:
        main()
    except PortfolioLockBusy:
        pass
    except Exception as exc:
        logging.exception('operations_recovery_failed')
        # Separate receipt survives a damaged workflow file and prevents minute-by-minute alerts.
        receipt = Path('logs/operations_recovery_error.json')
        try:
            prior = json.loads(receipt.read_text()) if receipt.exists() else {}
            if clock.time() - prior.get('at', 0) >= 21600:
                notify.send_telegram_alert(notify.format_error_alert('자동 운영 복구 실패 — 확인 필요', type(exc).__name__))
                receipt.write_text(json.dumps({'at': clock.time(), 'error': type(exc).__name__}))
        except Exception:
            logging.exception('recovery_alert_receipt_failed')
        raise
