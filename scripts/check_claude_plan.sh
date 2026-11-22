#!/bin/bash
set -u
cd "$HOME/sima" || exit 1
export PATH="$HOME/.local/bin:$PATH"
exec 8>logs/plan-readiness.lock
flock -w 240 8 || exit 1
# Single readiness publication; failed transport calls have a bounded retry budget.
timeout --kill-after=10s 240s .venv/bin/python scripts/check_claude_plan.py >> logs/cron.log 2>&1
