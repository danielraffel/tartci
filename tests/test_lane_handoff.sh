#!/usr/bin/env bash
# One supervisor per lane, with real processes and the functions as runner.sh
# ships them. A replacement never takes a lane from a live incumbent, not even
# after its wait times out; it claims once the incumbent has exited. A stale or
# reused claim is not an incumbent. An incumbent is superseded only when the
# claim names another live supervisor.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNNER="$ROOT/providers/tart-macos/runner.sh"
tmp="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/tartci-handoff-test.XXXXXX")" && pwd)"
pids=""
cleanup() { for p in $pids; do kill "$p" 2>/dev/null || true; done; rm -rf "$tmp"; }
trap cleanup EXIT
fns="$tmp/fns.sh"
for name in tartci_proc_start tartci_lane_claim_path tartci_lane_handoff_path \
            tartci_lane_other_supervisor tartci_lane_claim tartci_lane_superseded; do
  awk -v n="$name" '
    $0 ~ "^" n "\\(\\)\\{" { on = 1; print; if ($0 ~ /\}$/) exit; next }
    on { print; if ($0 ~ /^\}/) exit }' "$RUNNER" >>"$fns"
  grep -q "^$name()" "$fns" || { echo "missing $name in runner.sh" >&2; exit 1; }
done
cat >>"$fns" <<EOF
STATE_DIR="$tmp/state"; RUNNER_NAME=lane-1; POLL=1
TARTCI_LANE_HANDOFF_POLL=1 TARTCI_LANE_HANDOFF_MAX_WAIT=2
event() { printf '%s %s\n' "\$1" "\$2" >>"$tmp/events"; }
note() { :; }
heartbeat() { echo "heartbeat \$*" >>"$tmp/heartbeats"; }
EOF
claim="$tmp/state/lane-1.supervisor"
handoff="$tmp/state/lane-1.handoff"
# Under set -e a failing `! cmd` does not exit, so negative checks use refute.
refute() { if "$@"; then echo "unexpected: $*" >&2; exit 1; fi; }
wait_for() { for _ in $(seq 1 100); do eval "$1" && return 0; sleep 0.1; done; return 1; }
claimant() { cut -d' ' -f1 "$claim" 2>/dev/null; }

# (a) No incumbent: the lane is claimed at once, with no handoff.
"$BASH" -c "source '$fns'; tartci_lane_claim; echo \$\$ >'$tmp/a.pid'"
test "$(claimant)" = "$(cat "$tmp/a.pid")"
test ! -e "$handoff"
refute grep -q supervisor_handoff_wait "$tmp/events" 2>/dev/null

# (b) A live incumbent keeps the lane through the replacement's timeout: the
# replacement waits, says so, keeps .handoff, never claims and never writes
# the lane's heartbeat. The incumbent is never superseded.
: >"$tmp/events"
"$BASH" -c "source '$fns'; tartci_lane_claim; echo ready >'$tmp/incumbent'
            while sleep 0.2; do
              if tartci_lane_superseded; then echo superseded >'$tmp/incumbent'; fi
            done" &
incumbent=$!; disown "$incumbent"; pids="$pids $incumbent"
wait_for "[ -s '$tmp/incumbent' ]"
test "$(claimant)" = "$incumbent"
"$BASH" -c "source '$fns'; tartci_lane_claim; echo \$\$ >'$tmp/b.pid'" &
replacement=$!; pids="$pids $replacement"
wait_for "grep -q supervisor_handoff_timeout '$tmp/events'" \
  || { echo "no handoff timeout event" >&2; exit 1; }
sleep 1.5
test "$(claimant)" = "$incumbent"
test -e "$handoff"
test "$(cat "$tmp/incumbent")" = ready
refute test -e "$tmp/heartbeats"
kill -0 "$replacement"
# The incumbent leaves (its job done, or it stepped aside): the replacement
# takes the lane and clears .handoff.
kill "$incumbent"
wait "$replacement"
test "$(claimant)" = "$(cat "$tmp/b.pid")"
test ! -e "$handoff"
grep -q "supervisor_handoff_wait incumbent=$incumbent" "$tmp/events"
grep -q "supervisor_handoff_done incumbent=$incumbent" "$tmp/events"

# (c) A claim left by a dead supervisor is taken without waiting.
printf '%s %s\n' 999999 "Mon Sep  1 00:00:00 2026" >"$claim"
: >"$tmp/events"
"$BASH" -c "source '$fns'; tartci_lane_claim"
refute grep -q supervisor_handoff_wait "$tmp/events"

# (d) A live pid whose start time is not the claim's (pid reuse) is not an
# incumbent either.
sleep 30 & other=$!; disown "$other"; pids="$pids $other"
printf '%s %s\n' "$other" "Mon Sep  1 00:00:00 2026" >"$claim"
"$BASH" -c "source '$fns'; tartci_lane_claim"
refute grep -q supervisor_handoff_wait "$tmp/events"

# (f) A zombie (exited, not yet reaped) answers kill -0 but is no incumbent:
# its claim is taken without waiting.
python3 -c '
import os, sys, time
pid = os.fork()
if pid == 0:
    os._exit(0)
print(pid, flush=True)
time.sleep(30)' >"$tmp/zombie" &
zparent=$!; disown "$zparent"; pids="$pids $zparent"
wait_for "[ -s '$tmp/zombie' ]"
zombie="$(head -n 1 "$tmp/zombie")"
wait_for "ps -o stat= -p $zombie | grep -q Z"
printf '%s %s\n' "$zombie" "$(ps -o lstart= -p "$zombie" | sed 's/ *$//')" >"$claim"
: >"$tmp/events"
"$BASH" -c "source '$fns'; tartci_lane_claim"
refute grep -q supervisor_handoff_wait "$tmp/events"

# (e) Superseded only when the claim names another live supervisor.
"$BASH" -c "source '$fns'; tartci_lane_claim
            if tartci_lane_superseded; then echo yes; else echo no; fi" >"$tmp/self"
test "$(cat "$tmp/self")" = no
printf '%s %s\n' "$other" "$("$BASH" -c "source '$fns'; tartci_proc_start $other")" >"$claim"
"$BASH" -c "source '$fns'; if tartci_lane_superseded; then echo yes; else echo no; fi" \
  >"$tmp/other"
test "$(cat "$tmp/other")" = yes

# The loop claims before serving and steps aside only between jobs; cleanup
# leaves the runner name to the supervisor that holds the lane.
grep -q '^  tartci_lane_claim$' "$RUNNER"
grep -A1 'if \[ -z "\$CURRENT_VM" \]; then' "$RUNNER" | grep -q 'step_aside=""'
grep -q '^      elif tartci_lane_superseded; then' "$RUNNER"
grep -B1 'reclaim_runner_name "\$RUNNER_NAME" "\$CURRENT_RUNNER_API_ROOT" 1' "$RUNNER" \
  | grep -q 'if ! tartci_lane_superseded; then'
echo 'lane handoff: ok'
