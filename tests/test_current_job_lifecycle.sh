#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Exercise the production shell functions directly while replacing only their
# external GitHub/event/heartbeat/cleanup boundaries.
eval "$(sed -n '/^handle_supervisor_signal(){/,/^ensure_runner_version(){/p' \
  "$ROOT/providers/tart-macos/runner.sh" | sed '$d')"

cat >"$TMP/fake-gh" <<'PY'
#!/usr/bin/env python3
import json, os, sys, time
state = os.environ["LIFECYCLE_STATE"]
args = sys.argv[1:]
path = args[-1]
if os.environ.get("LIFECYCLE_HANG") == "1":
    time.sleep(20)
if "-X" in args and "POST" in args:
    with open(os.environ["LIFECYCLE_POST_LOG"], "a", encoding="utf-8") as stream:
        stream.write("POST\n")
    open(state, "w", encoding="utf-8").write("cancelled")
    print("{}")
elif path.endswith("/actions/workflows?per_page=100&page=1"):
    print(json.dumps({"total_count":1,"workflows":[{"id":99,"name":"Build and Test"}]}))
elif path.endswith("/actions/jobs/444"):
    terminal = open(state, encoding="utf-8").read().strip() == "cancelled"
    print(json.dumps({"id":444,"status":"completed" if terminal else "in_progress",
                      "conclusion":"cancelled" if terminal else None,"runner_name":"runner-1"}))
elif path.endswith("/actions/runs/333"):
    terminal = open(state, encoding="utf-8").read().strip() == "cancelled"
    print(json.dumps({"id":333,"workflow_id":99,"name":"Build and Test",
                      "status":"completed" if terminal else "in_progress",
                      "conclusion":"cancelled" if terminal else None}))
elif "/actions/runs?status=in_progress" in path:
    print(json.dumps({"total_count":1,"workflow_runs":[
        {"id":333,"workflow_id":99,"name":"Build and Test","status":"in_progress"}]}))
elif "/actions/runs/333/jobs?filter=latest" in path:
    print(json.dumps({"total_count":1,"jobs":[
        {"id":444,"status":"in_progress","runner_name":"runner-1"}]}))
else:
    print(json.dumps({"total_count":0,"jobs":[]}))
PY
chmod +x "$TMP/fake-gh"

export TARTCI_ROOT="$ROOT" STATE_DIR="$TMP/state" REPO="Generous-Corp/pulp"
export GH_CLI="$TMP/fake-gh" WORKFLOW_CONFIG="Build and Test"
export CURRENT_REGISTERED_RUNNER="runner-1" RUNNER_NAME="lane-1"
export LIFECYCLE_STATE="$TMP/api-state" LIFECYCLE_HANG=0
export LIFECYCLE_POST_LOG="$TMP/posts"
mkdir -p "$STATE_DIR"
printf active >"$LIFECYCLE_STATE"
: >"$LIFECYCLE_POST_LOG"
EVENTS="$TMP/events"
: >"$EVENTS"
export EVENT_LOG="$EVENTS"
event(){ printf '%s|%s\n' "$1" "${2:-}" >>"$EVENTS"; }
heartbeat(){ printf 'heartbeat|%s\n' "$1" >>"$EVENTS"; }
cleanup(){
  [ -z "${CURRENT_SCAN_PID:-}" ] || kill "$CURRENT_SCAN_PID" 2>/dev/null || true
  [ -z "${CURRENT_SCAN_TMP:-}" ] || rm -f "$CURRENT_SCAN_TMP" 2>/dev/null || true
  printf 'cleanup|%s\n' "${CURRENT_ASSIGNMENT_QUARANTINE:-}" >>"$EVENTS"
}

reset_state(){
  # Consumed by production functions loaded through eval above.
  # shellcheck disable=SC2034
  CURRENT_RUN_ID=333 CURRENT_JOB_ID=444 CURRENT_WORKFLOW_NAME="Build and Test"
  CURRENT_JOB_CAPTURE_STATUS=active CURRENT_JOB_RECEIPT='{"kind":"active"}'
  # shellcheck disable=SC2034
  CURRENT_JOB_SCAN_SPENT=30 CURRENT_JOB_SCAN_FAILURES=0 CURRENT_JOB_SCAN_NEXT_AT=0
  CURRENT_CANCEL_DISCOVERY_SCAN_SPENT=0
  CURRENT_CANCEL_REVALIDATION_SCAN_SPENT=0
  CURRENT_CANCEL_TERMINAL_SCAN_SPENT=0 CURRENT_ASSIGNMENT_QUARANTINE=none
  CURRENT_SCAN_PID="" CURRENT_SCAN_TMP=""
  : >"$EVENTS"
}

post_count(){
  wc -l <"$LIFECYCLE_POST_LOG" | tr -d '[:space:]'
}

assert_pre_cancel_invalid(){
  local variable="$1" value="$2" detail="$3" quarantine="$4" clear_ids="$5"
  local posts_before
  reset_state
  if [ "$clear_ids" = 1 ]; then
    CURRENT_RUN_ID="" CURRENT_JOB_ID=""
  fi
  printf active >"$LIFECYCLE_STATE"
  posts_before="$(post_count)"
  export "$variable=$value"
  if cancel_current_run; then
    unset "$variable"
    exit 1
  fi
  unset "$variable"
  [ "$(post_count)" = "$posts_before" ]
  [ "$CURRENT_JOB_CAPTURE_STATUS" = invalid_budget ]
  [ "$CURRENT_JOB_RECEIPT" = "{\"kind\":\"invalid_budget\",\"detail\":\"$detail\"}" ]
  [ "$CURRENT_ASSIGNMENT_QUARANTINE" = "$quarantine" ]
  grep -q '^run_cancel_suppressed|.*rerun_eligible=false' "$EVENTS"
}

assert_post_cancel_invalid(){
  local value="$1" posts_before
  reset_state
  printf active >"$LIFECYCLE_STATE"
  posts_before="$(post_count)"
  export "TARTCI_CANCEL_TERMINAL_OBSERVATION_BUDGET_SECS=$value"
  if cancel_current_run; then
    unset TARTCI_CANCEL_TERMINAL_OBSERVATION_BUDGET_SECS
    exit 1
  fi
  unset TARTCI_CANCEL_TERMINAL_OBSERVATION_BUDGET_SECS
  [ "$(post_count)" = "$((posts_before + 1))" ]
  [ "$CURRENT_JOB_CAPTURE_STATUS" = invalid_budget ]
  [ "$CURRENT_JOB_RECEIPT" = '{"kind":"invalid_budget","detail":"cancel_terminal_observation_budget"}' ]
  [ "$CURRENT_ASSIGNMENT_QUARANTINE" = cancel_terminal_unknown ]
  grep -q '^run_cancel_requested|.*rerun_eligible=pending-terminal' "$EVENTS"
  grep -q '^run_cancel_unknown|.*observation=invalid_budget.*rerun_eligible=false' "$EVENTS"
  if grep -q '^run_cancel_terminal|' "$EVENTS"; then exit 1; fi
}

# Every timeout that reaches shell arithmetic is a canonical positive decimal.
for value in 1 8 10 300; do
  tartci_is_canonical_positive_decimal "$value" || exit 1
done
for value in "" 0 00 09 +1 -1 " 1" "1 " 1:2 1a; do
  if tartci_is_canonical_positive_decimal "$value"; then exit 1; fi
done

# Discovery exhaustion must not consume mandatory pre-cancel authority.
reset_state
TARTCI_CAPTURE_CURRENT_JOB_LIFECYCLE_BUDGET_SECS=30 \
TARTCI_CANCEL_REVALIDATION_BUDGET_SECS=10 capture_current_job revalidate
[ "$CURRENT_JOB_CAPTURE_STATUS" = active ]
[ "$CURRENT_JOB_SCAN_SPENT" = 30 ]
[ "$CURRENT_CANCEL_REVALIDATION_SCAN_SPENT" -gt 0 ]

# Timeout cancellation gets the protected cancel budget even when ordinary
# discovery exhausted before it learned the run and job IDs.
reset_state
CURRENT_RUN_ID="" CURRENT_JOB_ID=""
CURRENT_JOB_CAPTURE_STATUS=budget_exhausted
CURRENT_JOB_RECEIPT='{"kind":"budget_exhausted"}'
printf active >"$LIFECYCLE_STATE"
posts_before="$(post_count)"
TARTCI_CAPTURE_CURRENT_JOB_LIFECYCLE_BUDGET_SECS=30 \
TARTCI_CANCEL_DISCOVERY_BUDGET_SECS=1 \
TARTCI_CANCEL_REVALIDATION_BUDGET_SECS=1 \
TARTCI_CANCEL_TERMINAL_OBSERVATION_BUDGET_SECS=1 \
TARTCI_CANCEL_TERMINAL_TIMEOUT_SECS=10 cancel_current_run
[ "$CURRENT_RUN_ID" = 333 ]
[ "$CURRENT_JOB_ID" = 444 ]
[ "$CURRENT_CANCEL_DISCOVERY_SCAN_SPENT" = 1 ]
[ "$CURRENT_CANCEL_REVALIDATION_SCAN_SPENT" = 1 ]
[ "$CURRENT_CANCEL_TERMINAL_SCAN_SPENT" = 1 ]
[ "$(post_count)" = "$((posts_before + 1))" ]
grep -q '^run_cancel_terminal|.*rerun_eligible=true' "$EVENTS"

# Every malformed class reaches each actual configuration path. Discovery,
# mandatory revalidation, per-attempt, and terminal-deadline failures precede
# POST; terminal-observation failure follows exactly one POST and stays
# non-rerunnable.
for malformed in 0 +1 -1 " 1" "1 " 1:2 09 ""; do
  assert_pre_cancel_invalid TARTCI_CANCEL_DISCOVERY_BUDGET_SECS \
    "$malformed" cancel_discovery_budget pre_cancel_discovery_invalid_budget 1
  assert_pre_cancel_invalid TARTCI_CANCEL_REVALIDATION_BUDGET_SECS \
    "$malformed" cancel_revalidation_budget pre_cancel_invalid_budget 0
  assert_pre_cancel_invalid TARTCI_CAPTURE_CURRENT_JOB_ATTEMPT_TIMEOUT_SECS \
    "$malformed" attempt_timeout pre_cancel_invalid_budget 0
  assert_pre_cancel_invalid TARTCI_CANCEL_TERMINAL_TIMEOUT_SECS \
    "$malformed" cancel_terminal_timeout pre_cancel_invalid_budget 0
  assert_post_cancel_invalid "$malformed"
done

# A setup failure overwrites a stale active receipt and fails closed.
reset_state
CURRENT_JOB_SCAN_SPENT=0
mktemp(){ return 1; }
if capture_current_job discover; then exit 1; fi
unset -f mktemp
[ "$CURRENT_JOB_CAPTURE_STATUS" = setup_error ]
[ "$CURRENT_JOB_RECEIPT" = '{"kind":"setup_error","detail":"mktemp_failed"}' ]
[ "$CURRENT_ASSIGNMENT_QUARANTINE" = observation_setup_error ]

# POST is not success: only the subsequent exact terminal receipt makes the
# timed-out workflow rerun-eligible.
reset_state
printf active >"$LIFECYCLE_STATE"
posts_before="$(post_count)"
TARTCI_CANCEL_REVALIDATION_BUDGET_SECS=20 \
TARTCI_CANCEL_TERMINAL_TIMEOUT_SECS=10 cancel_current_run
[ "$(post_count)" = "$((posts_before + 1))" ]
grep -q '^run_cancel_requested|.*rerun_eligible=pending-terminal' "$EVENTS"
grep -q '^run_cancel_terminal|.*rerun_eligible=true.*"run_conclusion":"cancelled"' "$EVENTS"
[ "$CURRENT_JOB_CAPTURE_STATUS" = terminal ]
[ "$CURRENT_ASSIGNMENT_QUARANTINE" = none ]

# Listener teardown telemetry cannot call an active/error observation terminal.
CURRENT_JOB_CAPTURE_STATUS=active
CURRENT_JOB_RECEIPT='{"kind":"active"}'
record_terminal_job_receipt 1
[ "$CURRENT_ASSIGNMENT_QUARANTINE" = listener_exited_workflow_active ]
CURRENT_JOB_CAPTURE_STATUS=observation_error
CURRENT_JOB_RECEIPT='{"kind":"observation_error"}'
record_terminal_job_receipt 1
[ "$CURRENT_ASSIGNMENT_QUARANTINE" = listener_exit_terminal_unknown ]
grep -q '^job_lifecycle_quarantine|.*observation=active.*listener_exited_workflow_active' "$EVENTS"
grep -q '^job_lifecycle_quarantine|.*observation=observation_error.*listener_exit_terminal_unknown' "$EVENTS"

# A listener that exits after assignment but before ID discovery is not allowed
# to skip the terminal receipt/quarantine transaction.
reset_state
CURRENT_RUN_ID="" CURRENT_JOB_ID=""
CURRENT_JOB_CAPTURE_STATUS=not-attempted
CURRENT_JOB_RECEIPT='{"kind":"not-attempted"}'
finalize_listener_receipt 1 1
[ "$CURRENT_ASSIGNMENT_QUARANTINE" = listener_exit_terminal_unknown ]
grep -q '^job_lifecycle_quarantine|.*observation=not-attempted.*listener_exit_terminal_unknown' "$EVENTS"

# A signal during a live API observation owns the scanner child/temp file and
# leaves a typed terminal-unknown quarantine instead of stale active telemetry.
reset_state
CURRENT_RUN_ID="" CURRENT_JOB_ID=""
printf active >"$LIFECYCLE_STATE"
LIFECYCLE_HANG=1
export LIFECYCLE_HANG
(
  trap 'handle_supervisor_signal' TERM
  capture_current_job cancel_discover
) & signal_pid=$!
sleep 1
kill -TERM "$signal_pid"
set +e
wait "$signal_pid"
signal_rc=$?
set -e
[ "$signal_rc" = 143 ]
grep -q '^supervisor_signal|INT/TERM quarantine=signal_teardown_unknown' "$EVENTS"
grep -q '^cleanup|signal_teardown_unknown' "$EVENTS"

printf 'current job lifecycle: ok\n'
