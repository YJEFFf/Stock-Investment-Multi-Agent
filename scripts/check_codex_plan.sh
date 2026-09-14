#!/bin/bash
cd $HOME/sima
export PATH="$HOME/.local/bin:$PATH"
uv run python scripts/check_codex_plan.py >> $HOME/sima/logs/cron.log 2>&1
