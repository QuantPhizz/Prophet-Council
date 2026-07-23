#!/usr/bin/env bash
# Dynamic-cadence wrapper for the watchdog pass.
#
# Cron fires this every 2 minutes unconditionally (see README/crontab). This
# script itself decides whether to actually run orchestrator.py --watchdog-only:
#   - If any position is open: run every time (i.e. every 2 min).
#   - If idle (no positions): only run on the :00/:20/:40 tick, preserving the
#     original ~20 min idle cadence instead of hitting the API every 2 min for
#     nothing to watch.
set -euo pipefail
cd "$(dirname "$0")"
set -a
. ./.env
set +a

positions=$(./.venv/bin/python -c "
import json
try:
    with open('orchestrator_state.json') as f:
        print(len(json.load(f).get('positions', {})))
except FileNotFoundError:
    print(0)
" 2>/dev/null || echo 0)

minute=$(date -u +%M)
if [ "$positions" -gt 0 ] || [ $((10#$minute % 20)) -eq 0 ]; then
    exec ./.venv/bin/python orchestrator.py --watchdog-only >> orchestrator_watchdog.log 2>&1
fi
