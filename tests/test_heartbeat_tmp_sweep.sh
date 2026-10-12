#!/usr/bin/env bash
# Real runner.sh heartbeat() entry-point regression test.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/tartci-heartbeat-test.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
state="$tmp/state"
mkdir -p "$state"
RUNNER_NAME="fixture-runner"
STATE_DIR="$state"
EVENT_LOG="$tmp/events"
HOST_NAME=fixture CURRENT_VM=vm CURRENT_LABELS=labels REPO=repo
CURRENT_RUN_ID=run CURRENT_JOB_ID=job CURRENT_JOB_CAPTURE_STATUS=observed
CURRENT_ASSIGNMENT_QUARANTINE=none SERVING_BLOCKED_SINCE='' SERVING_BLOCKED_STREAK=0
SERVING_BLOCKED_LAST_PHASE='' SUPERVISOR_PID='' SUPERVISOR_PID_STARTED_AT=''
LAST_HEARTBEAT_PHASE=
json_sanitize() { printf '%s' "$1"; }
tartci_heartbeat_keepalive_stop() { :; }
export RUNNER_NAME STATE_DIR EVENT_LOG HOST_NAME CURRENT_VM CURRENT_LABELS REPO
export CURRENT_RUN_ID CURRENT_JOB_ID CURRENT_JOB_CAPTURE_STATUS CURRENT_ASSIGNMENT_QUARANTINE
export SERVING_BLOCKED_SINCE SERVING_BLOCKED_STREAK SERVING_BLOCKED_LAST_PHASE
export SUPERVISOR_PID SUPERVISOR_PID_STARTED_AT LAST_HEARTBEAT_PHASE
function_file="$tmp/heartbeat-function.sh"
sed -n '/^heartbeat(){/,/^}/p' "$ROOT/providers/tart-macos/runner.sh" >"$function_file"
# shellcheck source=/dev/null
source "$function_file"
printf 'live\n' >"$state/$RUNNER_NAME.state.json"
printf 'old\n' >"$state/$RUNNER_NAME.state.json.tmp.old"
printf 'fresh\n' >"$state/$RUNNER_NAME.state.json.tmp.fresh"
touch -t 202001010000 "$state/$RUNNER_NAME.state.json.tmp.old"
heartbeat checking
test ! -e "$state/$RUNNER_NAME.state.json.tmp.old"
test -e "$state/$RUNNER_NAME.state.json.tmp.fresh"
test "$(grep -c 'checking' "$state/$RUNNER_NAME.state.json")" -eq 1
echo 'heartbeat tmp sweep: ok'
