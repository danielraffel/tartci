#!/usr/bin/env bash
# The Python supervisor owns independent process groups and forwards launchd
# stop/restart signals to both groups before it exits.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$HERE/shipyard_queue_service_tick.py"
