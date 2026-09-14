#!/bin/bash
# EC2 배포는 이 스크립트로만 한다. `git pull`을 직접 치지 말 것.
#
# 거래일 08:25~16:10 KST에는 거부한다. 08:30 decide_buys부터 16:00 IC 측정까지
# 크론이 돌고, 그 사이에 코드를 바꾸면 한 회차 안에서 옛 코드와 새 코드가 섞여 돈다.
# 2026-09-08에 장중 배포로 매수·매도 경로가 다른 버전으로 반나절 돌았고, 첫 1개월
# 평가는 "같은 날 발견-수정-배포 6일, 장중 배포 1회"를 운영 신뢰도 감점 사유로 꼽았다.
#
# 풀 뒤에 테스트를 돌린다 — 테스트가 깨진 코드를 크론이 다음 회차에 바로 집는 것을
# 막는다(conftest가 장중 EC2 테스트도 거부하므로 이 순서가 안전하다).
set -euo pipefail
cd "$HOME/sima"
export PATH="$HOME/.local/bin:$PATH"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "거부: 운영 체크아웃에 추적 파일 변경이 있다. 먼저 원인을 확인할 것." >&2
  exit 5
fi

uv run python - <<'PY'
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo
from src.market_calendar import is_krx_trading_day

now = datetime.now(ZoneInfo("Asia/Seoul"))
if is_krx_trading_day(now.date()) and time(8, 25) <= now.time() < time(16, 10):
    print(f"거부: 거래일 장중({now:%H:%M})에는 배포하지 않는다. 16:10 이후에 다시 실행할 것.", file=sys.stderr)
    sys.exit(2)
PY

# **매매 경로 파일이 바뀌면 독립 리뷰를 거쳤다는 확인 없이는 배포하지 않는다**(2026-09-14,
# 첫 1개월 평가 P1-10). 발견-수정-자기검증이 같은 날 한 사람+AI로 반복되며 회귀를 낳았다
# (9/8 격리 "수정"이 실제로는 안 먹은 건). 리뷰한 커밋 해시를 SIMA_REVIEWED로 넘겨야 한다 —
# "리뷰했다"가 아니라 "이 커밋을 리뷰했다"를 적게 하려는 것이다. 절차는 docs/PLAN.md
# "매매 경로 변경 절차".
#   SIMA_REVIEWED=$(git rev-parse --short origin/main) ~/sima/deploy/pull.sh
# 한계: 이 검사는 리뷰를 **강제**하지 못한다(누구나 해시를 넣을 수 있다). 하는 일은 "무엇을
# 리뷰했는가"를 명시하게 하고, 검사한 그 커밋만 배포되게 하는 것이다.
TRADING_PATHS=(
  src/sell.py src/pipeline.py src/kis.py src/collectors.py src/schemas.py src/portfolio_store.py
  src/notify.py src/judgment.py src/translate.py src/llm.py
  src/codex_plan.py
  scripts/execute_open.py scripts/check_stop_loss.py scripts/decide_buys.py scripts/decide_llm_sell.py
  scripts/check_codex_plan.py
  scripts/check_stop_loss.sh scripts/execute_open.sh scripts/decide_buys.sh scripts/decide_llm_sell.sh
  scripts/check_codex_plan.sh
  deploy/crontab
)
git fetch --quiet origin
TARGET=$(git rev-parse origin/main)
CHANGED=$(git diff --name-only HEAD "$TARGET" -- "${TRADING_PATHS[@]}")
if [ -n "$CHANGED" ]; then
  REVIEWED_FULL=$(git rev-parse --verify --quiet "${SIMA_REVIEWED:-none}^{commit}" || true)
  if [ "$REVIEWED_FULL" != "$TARGET" ]; then
    echo "거부: 매매 경로 파일이 바뀌었다 — 독립 리뷰 후 SIMA_REVIEWED=${TARGET:0:7} 로 다시 실행할 것." >&2
    echo "$CHANGED" | sed 's/^/  /' >&2
    exit 4
  fi
fi

# 검사한 바로 그 커밋으로만 옮긴다. `git pull`은 한 번 더 fetch해서, 검사 뒤에 push된 커밋이
# 리뷰 없이 따라 들어올 수 있다(독립 리뷰 지적).
OLD_HEAD=$(git rev-parse HEAD)
CRONTAB_BACKUP=$(mktemp)
CRONTAB_ERROR=$(mktemp)
HAD_CRONTAB=0
if crontab -l >"$CRONTAB_BACKUP" 2>"$CRONTAB_ERROR"; then
  HAD_CRONTAB=1
elif grep -q '^no crontab for ' "$CRONTAB_ERROR"; then
  HAD_CRONTAB=0
else
  cat "$CRONTAB_ERROR" >&2
  rm -f "$CRONTAB_BACKUP" "$CRONTAB_ERROR"
  echo "거부: 기존 크론을 읽지 못해 안전한 롤백 상태를 만들 수 없다." >&2
  exit 6
fi
rollback_on_failure() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ]; then
    git reset --hard --quiet "$OLD_HEAD"
    if [ "$HAD_CRONTAB" -eq 1 ]; then
      crontab "$CRONTAB_BACKUP"
    else
      crontab -r 2>/dev/null || true
    fi
    echo "배포 실패: 코드와 크론을 $OLD_HEAD 상태로 복구했다." >&2
  fi
  rm -f "$CRONTAB_BACKUP" "$CRONTAB_ERROR"
  exit "$status"
}
trap rollback_on_failure EXIT

git merge --ff-only --quiet "$TARGET"
uv run pytest -q 2>&1 | tail -2
if ! cmp -s <(crontab -l 2>/dev/null || true) deploy/crontab; then
  crontab deploy/crontab
  echo "crontab updated from deploy/crontab"
fi
git log --oneline -1
