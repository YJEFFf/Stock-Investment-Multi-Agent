#!/usr/bin/python3
"""Root-owned, fixed allowlist only. Never reads repository code or credentials."""
import json
import subprocess
import time
from pathlib import Path

SERVICES = ('cron', 'sima-balance', 'sima-pwa', 'sima-push', 'caddy')
STATE = Path('/var/lib/sima-recovery/services.json')


def tick(now, state, run):
    for service in SERVICES:
        status = run(['systemctl', 'is-active', service], capture_output=True, text=True).stdout.strip()
        row = state.setdefault(service, {'attempts': []})
        if service == 'sima-pwa' and status == 'active':
            probe = run(['curl', '--fail', '--silent', '--max-time', '3', 'http://127.0.0.1:8765/healthz'], capture_output=True, text=True)
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
        yield state
        run(['systemctl', 'reset-failed', service], capture_output=True, text=True)
        result = run(['systemctl', 'restart', service], capture_output=True, text=True)
        row['result'] = result.returncode
        row['status'] = run(['systemctl', 'is-active', service], capture_output=True, text=True).stdout.strip()
    yield state


def main():
    if Path('/etc/sima-recovery.disabled').exists():
        return
    STATE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    def run(args, **kwargs):
        return subprocess.run(args, timeout=20, check=False, **kwargs)
    for snapshot in tick(time.time(), state, run):
        tmp = STATE.with_suffix('.tmp')
        tmp.write_text(json.dumps(snapshot))
        tmp.chmod(0o644)
        tmp.replace(STATE)
    run(['logrotate', '/etc/logrotate.d/sima'])
    run(['systemctl', 'start', '--no-block', 'sima-operations.service'])


if __name__ == '__main__':
    main()
