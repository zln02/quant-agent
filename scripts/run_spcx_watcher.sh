#!/usr/bin/env bash
# PR #29: SPCX 상장 감지 cron 진입점.
# crontab: 0 23 * * * /home/wlsdud5035/quant-agent/scripts/run_spcx_watcher.sh
set -euo pipefail

source "$(dirname "$0")/load_env.sh"
load_openclaw_env
export PYTHONPATH="${WORKSPACE}:${PYTHONPATH:-}"

PYTHON_BIN="${WORKSPACE}/.venv/bin/python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] SPCX listing watcher start"
"$PYTHON_BIN" -m scripts.spcx_listing_watcher
echo "[$(date '+%Y-%m-%d %H:%M:%S')] SPCX listing watcher end"
