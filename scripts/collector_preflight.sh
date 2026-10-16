#!/bin/bash
set -euo pipefail
cd /home/ubuntu/sima
export PATH="/home/ubuntu/.local/bin:$PATH"
mkdir -p logs
timeout --kill-after=10s 1800s uv run python scripts/collector_preflight.py "$@" >> logs/collector_preflight.log 2>&1
