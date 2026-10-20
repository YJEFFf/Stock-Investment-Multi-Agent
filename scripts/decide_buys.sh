#!/bin/bash
cd "$HOME/sima" || exit 1
export PATH="$HOME/.local/bin:$PATH"
uv run python scripts/decide_buys.py >> $HOME/sima/logs/cron.log 2>&1
status=$?
if [ "$status" -ne 0 ]; then
  uv run python scripts/decide_buys.py --finalize-aborted "$status" >> "$HOME/sima/logs/cron.log" 2>&1
fi
exit "$status"
