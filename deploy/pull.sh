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

git pull --ff-only
uv run pytest -q 2>&1 | tail -2
git log --oneline -1
