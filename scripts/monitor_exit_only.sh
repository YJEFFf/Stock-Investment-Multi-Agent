#!/bin/bash
set -euo pipefail
cd "$HOME/sima"
export PATH="$HOME/.local/bin:$PATH"
timeout --signal=TERM --kill-after=3s 50s uv run python scripts/monitor_exit_only.py >> "$HOME/sima/logs/cron.log" 2>&1
