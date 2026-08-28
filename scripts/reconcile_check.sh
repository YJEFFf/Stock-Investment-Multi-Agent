#!/bin/bash
cd $HOME/sima
export PATH="$HOME/.local/bin:$PATH"
uv run python scripts/reconcile_portfolio.py --alert >> $HOME/sima/logs/cron.log 2>&1
