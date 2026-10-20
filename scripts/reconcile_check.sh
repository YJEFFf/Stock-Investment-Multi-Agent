#!/bin/bash
cd $HOME/sima
export PATH="$HOME/.local/bin:$PATH"
export SIMA_EXTENDED_ORDER_RECOVERY=1
uv run python scripts/reconcile_portfolio.py --alert --recover >> $HOME/sima/logs/cron.log 2>&1
