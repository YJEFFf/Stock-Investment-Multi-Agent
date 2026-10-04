#!/bin/bash
# deploy/pull.sh 배포 완료 후 실행. 매매 크론·주문·장부는 실행하지 않는다.
# Usage: sudo env SIMA_PWA_HOST=sima.3-35-112-100.sslip.io bash deploy/install_pwa.sh
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'root로 실행해야 한다.' >&2
  exit 1
fi
PWA_REPO=/home/ubuntu/sima
PWA_HOST=${SIMA_PWA_HOST:?SIMA_PWA_HOST must be set}
if ! [[ "$PWA_HOST" =~ ^[a-z0-9][a-z0-9.-]+[a-z0-9]$ ]]; then
  echo 'Invalid PWA hostname' >&2
  exit 1
fi
cd "$PWA_REPO"
PWA_COMMIT=$(sudo -u ubuntu git rev-parse HEAD)
PWA_RELEASE="/opt/sima-pwa/releases/$PWA_COMMIT"
PWA_PREVIOUS=$(readlink -f /opt/sima-pwa/current 2>/dev/null || true)
PWA_UV=/home/ubuntu/.local/bin/uv
test -x "$PWA_UV"

if ! id sima-web >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/sima-pwa --shell /usr/sbin/nologin sima-web
fi
install -d -m 755 /opt/sima-pwa/releases
install -d -m 700 -o sima-web -g sima-web /var/lib/sima-pwa
install -d -m 755 -o sima-web -g sima-web /var/lib/sima-pwa/data
if [ ! -d "$PWA_RELEASE" ]; then
  install -d -m 755 "$PWA_RELEASE"
  sudo -u ubuntu git archive "$PWA_COMMIT" src web scripts/mobile.py pyproject.toml uv.lock | tar -x -C "$PWA_RELEASE"
fi
"$PWA_UV" sync --project "$PWA_RELEASE" --python /usr/bin/python3 --frozen --no-dev --cache-dir /var/cache/sima-pwa-uv
chmod -R a+rX "$PWA_RELEASE"

cat > /etc/sima-pwa.env <<EOF
SIMA_WEB_ORIGIN=https://$PWA_HOST
SIMA_WEB_DATA_DIR=/var/lib/sima-pwa/data
SIMA_WEB_STATE_DIR=/var/lib/sima-pwa
SIMA_INITIAL_CAPITAL=100000000
EOF
chmod 644 /etc/sima-pwa.env
sudo -u sima-web "$PWA_RELEASE/.venv/bin/python" "$PWA_RELEASE/scripts/mobile.py" init

# 웹 서비스와 분리된 잔고 조회 프로세스에 KIS 필수 키만 제공한다.
"$PWA_RELEASE/.venv/bin/python" - <<'PYENV'
import os
from pathlib import Path
from dotenv import dotenv_values
values = dotenv_values("/home/ubuntu/sima/.env")
path = Path("/etc/sima-balance.env")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as output:
    for key in ("KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"):
        value = values.get(key)
        if not value or any(c in value for c in '\n\r"\\'):
            raise ValueError("Invalid broker configuration")
        output.write(f'{key}="{value}"\n')
path.chmod(0o600)
PYENV
touch "$PWA_REPO/.kis_token_cache.json"
chown ubuntu:ubuntu "$PWA_REPO/.kis_token_cache.json"
chmod 600 "$PWA_REPO/.kis_token_cache.json"

rollback() {
  if [ -n "$PWA_PREVIOUS" ] && [ -d "$PWA_PREVIOUS" ]; then
    ln -sfn "$PWA_PREVIOUS" /opt/sima-pwa/current
    systemctl restart sima-balance sima-pwa sima-push || true
  fi
}
trap 'rollback' ERR
install -m 644 deploy/sima-balance.service /etc/systemd/system/sima-balance.service
install -m 644 deploy/sima-pwa.service /etc/systemd/system/sima-pwa.service
install -m 644 deploy/sima-push.service /etc/systemd/system/sima-push.service
ln -sfn "$PWA_RELEASE" /opt/sima-pwa/current
systemctl daemon-reload
systemctl enable --now sima-balance sima-pwa sima-push
systemctl restart sima-balance sima-pwa sima-push

if ! command -v caddy >/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy
fi
if [ -f /etc/caddy/Caddyfile ] && [ ! -f /etc/caddy/Caddyfile.before-sima ]; then
  cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-sima
fi
cat > /etc/caddy/Caddyfile.sima <<EOF
$PWA_HOST {
    encode zstd gzip
    header X-Robots-Tag "noindex, nofollow, noarchive"
    reverse_proxy 127.0.0.1:8765
}
EOF
caddy validate --config /etc/caddy/Caddyfile.sima --adapter caddyfile
install -m 644 /etc/caddy/Caddyfile.sima /etc/caddy/Caddyfile
systemctl enable --now caddy
systemctl reload caddy
trap - ERR
echo "PWA deployed at https://$PWA_HOST ($PWA_COMMIT)"
