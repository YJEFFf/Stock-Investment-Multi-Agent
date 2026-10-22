#!/bin/bash
# Run after deploy/pull.sh on the independently reviewed exact commit.
set -euo pipefail
cd "$HOME/sima"
[[ "${SIMA_REVIEWED:-}" = "$(git rev-parse HEAD)" ]] || { echo 'Exact reviewed commit required'; exit 1; }
.venv/bin/python - <<'PY'
from datetime import datetime,time
from zoneinfo import ZoneInfo
from src.market_calendar import is_krx_trading_day
now=datetime.now(ZoneInfo('Asia/Seoul'))
assert not (is_krx_trading_day(now.date()) and time(6,50)<=now.time()<time(16,10)), 'Deployment window closed'
PY
# Copy only the public host, never app credentials, into the supervisor config.
sudo python3 - <<'PY_HOST'
from pathlib import Path
from urllib.parse import urlsplit
values = dict(line.split('=',1) for line in Path('/etc/sima-pwa.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
host = urlsplit(values['SIMA_WEB_ORIGIN'].strip().strip("\"'")).netloc
assert host and not any(c.isspace() for c in host)
p = Path('/etc/sima-recovery-host')
p.write_text(host + '\n')
p.chmod(0o644)
PY_HOST
sudo install -d -m 755 /var/lib/sima-recovery
sudo install -m 644 deploy/recover_services.py /usr/local/lib/sima-recover-services.py
for name in sima-recovery.service sima-recovery.timer sima-operations.service; do
    sudo install -m 644 "deploy/$name" "/etc/systemd/system/$name"
done
sudo install -m 644 deploy/sima-logrotate /etc/logrotate.d/sima
sudo install -d -m 755 /etc/systemd/journald.conf.d
sudo install -m 644 deploy/sima-journald.conf /etc/systemd/journald.conf.d/sima.conf
# Small persistent buffer for transient memory spikes, only when disk permits.
sudo python3 - <<'PY_SWAP'
import pathlib, shutil, subprocess
swap=pathlib.Path('/sima-recovery.swap')
active=pathlib.Path('/proc/swaps').read_text().splitlines()
if len(active)==1 and not swap.exists() and shutil.disk_usage('/').free >= 1536*1024*1024:
    subprocess.run(['fallocate','-l','512M',str(swap)],check=True)
    swap.chmod(0o600)
    subprocess.run(['mkswap',str(swap)],check=True)
    subprocess.run(['swapon',str(swap)],check=True)
    with open('/etc/fstab','a') as f:
        f.write('\n/sima-recovery.swap none swap sw 0 0\n')
PY_SWAP
sudo systemctl daemon-reload
sudo systemctl restart systemd-journald
sudo systemctl enable --now sima-recovery.timer
sudo systemctl start sima-recovery.service
