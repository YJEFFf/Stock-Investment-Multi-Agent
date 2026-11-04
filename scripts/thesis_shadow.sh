#!/bin/bash
cd "$HOME/sima" || exit 1
export PATH="$HOME/.local/bin:$PATH"
# Deployment takes the exclusive counterpart; don't mix source versions mid-run.
exec 6>.deploy.lock
flock -s -n 6 || exit 0
# Isolate read-only experiment failures from the production trading jobs.
exec 7>logs/thesis-shadow.lock
flock -n 7 || exit 0
uv run python scripts/thesis_shadow.py "$@" >> logs/cron.log 2>&1
