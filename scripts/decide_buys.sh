#!/bin/bash
cd "$HOME/sima" || exit 1
export PATH="$HOME/.local/bin:$PATH"
# Wait for an already-running readiness check before reading its permission.
exec 8>logs/codex-readiness.lock
flock -w 240 8 || exit 1
flock -u 8
uv run python scripts/decide_buys.py >> $HOME/sima/logs/cron.log 2>&1
status=$?
if [ "$status" -ne 0 ]; then
  uv run python scripts/decide_buys.py --finalize-aborted "$status" >> "$HOME/sima/logs/cron.log" 2>&1
fi
exit "$status"
