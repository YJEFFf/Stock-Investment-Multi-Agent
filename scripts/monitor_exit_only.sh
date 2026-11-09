#!/bin/bash
set -euo pipefail
cd "$HOME/sima"
export PATH="$HOME/.local/bin:$PATH"
uv run python scripts/monitor_exit_only.py >> "$HOME/sima/logs/cron.log" 2>&1
