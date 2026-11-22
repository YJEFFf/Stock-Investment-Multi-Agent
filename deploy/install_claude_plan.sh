#!/bin/bash
# Claude 구독 CLI를 운영 저장소·API 키와 격리된 전용 계정에 설치한다(2026-11-23 Codex 대체).
# 매매 크론·주문·장부·모델 호출은 실행하지 않는다. 로그인은 사용자가 직접 한다.
# Usage: sudo bash deploy/install_claude_plan.sh
set -euo pipefail
USER_NAME=sima-claude
CLI="/home/$USER_NAME/.local/bin/claude"

if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --create-home --shell /usr/sbin/nologin "$USER_NAME"
fi
chmod 750 "/home/$USER_NAME"
if [ ! -x "$CLI" ]; then
  # 공식 네이티브 설치 스크립트. 전용 계정 홈 아래에만 설치된다.
  sudo -u "$USER_NAME" -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
fi
# claude_plan._ensure_filesystem_isolation과 같은 검사 — 실패하면 호출 경로가 전부 거부된다.
if ! sudo -n -u "$USER_NAME" /usr/bin/test ! -x /home/ubuntu/sima; then
  echo "거부: $USER_NAME 계정이 운영 저장소를 탐색할 수 있다." >&2
  exit 1
fi
sudo -n -H -u "$USER_NAME" /usr/bin/env DISABLE_AUTOUPDATER=1 "$CLI" --version
if sudo -n -H -u "$USER_NAME" /usr/bin/env DISABLE_AUTOUPDATER=1 "$CLI" auth status --json \
    | grep -q '"authMethod": "claude.ai"'; then
  echo "구독 로그인 확인됨"
else
  echo "로그인 필요: ssh -t sima-ec2 'sudo -u $USER_NAME -H $CLI auth login'"
fi
