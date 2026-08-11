#!/bin/bash
cd $HOME/sima
export PATH="$HOME/.local/bin:$PATH"
uv run python scripts/execute_open.py >> $HOME/sima/logs/cron.log 2>&1
