#!/usr/bin/python3
"""Root-owned, fixed allowlist only. Never reads repository code or credentials."""
import json
import math
import logging
import subprocess
import time
from pathlib import Path

SERVICES = ('cron', 'sima-balance', 'sima-pwa', 'sima-push', 'caddy')
STATE = Path('/var/lib/sima-recovery/services.json')


def tick(now, state, run, health_host=None):
    for service in SERVICES:
        row = state.setdefault(service, {'attempts': []})
        prior_failure = (row.get('status'), row.get('result'), row.get('error'))
        try:
            status = run(['systemctl', 'is-active', service], capture_output=True, text=True).stdout.strip()
            if service == 'sima-pwa' and status == 'active' and health_host:
                probe = run(['curl', '--fail', '--silent', '--max-time', '3', '--header', 'Host: ' + health_host, 'http://127.0.0.1:8765/healthz'], capture_output=True, text=True)
                row['health_failures'] = row.get('health_failures', 0) + 1 if probe.returncode else 0
                if row['health_failures'] >= 3:
                    status = 'unresponsive'
            row['status'] = status
            row['attempts'] = [t for t in row['attempts'] if now - t < 3600]
            if status not in ('failed', 'inactive', 'unresponsive') or len(row['attempts']) >= 3:
                continue
            if row['attempts'] and now - row['attempts'][-1] < 300:
                continue
            # Persist attempt before mutation, so a supervisor crash cannot erase the budget.
            row['attempts'].append(now)
            row['last_attempt'] = now
            row.pop('result', None)
            row.pop('error', None)
            yield state
            run(['systemctl', 'reset-failed', service], capture_output=True, text=True)
            result = run(['systemctl', 'restart', service], capture_output=True, text=True)
            row['result'] = result.returncode
            row['status'] = run(['systemctl', 'is-active', service], capture_output=True, text=True).stdout.strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            code = 124 if isinstance(exc, subprocess.TimeoutExpired) else 127
            if prior_failure != ('unknown', code, type(exc).__name__):
                row['last_attempt'] = now
            row['status'] = 'unknown'
            row['result'] = code
            row['error'] = type(exc).__name__
            logging.exception('service recovery command failed: %s', service)
        yield state
    yield state


def load_state(now):
    if not STATE.exists():
        return {}
    raw = STATE.read_bytes()
    try:
        state = json.loads(raw)
        if not isinstance(state, dict):
            raise ValueError('state is not an object')
        for name, row in state.items():
            if not isinstance(row, dict) or not isinstance(row.get('attempts'), list):
                raise ValueError('invalid service row')
            if any(type(t) not in (int, float) or not math.isfinite(t) for t in row['attempts']):
                raise ValueError('invalid attempt timestamp')
            failures = row.get('health_failures', 0)
            if type(failures) is not int or failures < 0:
                raise ValueError('invalid health counter')
        return state
    except (ValueError, UnicodeError):
        # Preserve evidence before replacing it. Unknown budgets must never become zero.
        backup = STATE.with_name(STATE.name + '.corrupt-' + str(time.time_ns()))
        with backup.open('xb') as handle:
            handle.write(raw)
        state = {name: {'attempts': [now] * 3} for name in SERVICES}
        state['_state_error'] = {
            'attempts': [], 'last_attempt': now, 'result': 1,
            'status': 'state_corrupt; restart budget blocked for one hour',
        }
        persist(state)
        logging.error('Corrupt recovery state preserved at %s', backup)
        return state


def persist(state):
    tmp = STATE.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.chmod(0o644)
    tmp.replace(STATE)


def main():
    if Path('/etc/sima-recovery.disabled').exists():
        return
    def run(args, **kwargs):
        return subprocess.run(args, timeout=20, check=False, **kwargs)
    try:
        STATE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        state = load_state(time.time())
        host_file = Path('/etc/sima-recovery-host')
        host = host_file.read_text().strip() if host_file.exists() else None
        for snapshot in tick(time.time(), state, run, host):
            persist(snapshot)
        run(['logrotate', '/etc/logrotate.d/sima'])
    finally:
        # Service/log rotation failures must not suppress missed-start recovery or alerts.
        run(['systemctl', 'start', '--no-block', 'sima-operations.service'])


if __name__ == '__main__':
    main()
