#!/usr/bin/env bash
# A host profile's github_scan_app must leave the runner able to scan.
#
# Runs the runner's own scan-CLI selection block with only the variables the
# fleet renderer exports for [github_scan_app], then drives the production
# current-job scan through the real scripts/gh-scan-app. The repository is
# deliberately absent from the ambient environment (no SHIPYARD_GH_APP_REPO,
# no GH_REPO): the runner itself must bind the wrapper to the lane repository,
# or the wrapper exits 75 before the first API call.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail(){ printf 'scan app runner selection: FAIL: %s\n' "$*" >&2; exit 1; }

export TARTCI_QUEUE_OBSERVATION_LOCK_FILE="$TMP/queue-observation.lock"
unset SHIPYARD_GH_APP_REPO SHIPYARD_GHAPP_REPO GH_REPO TARTCI_SCAN_GH_CLI \
  TARTCI_SCAN_APP_REPO GITHUB_TOKEN GH_TOKEN

EXPIRES="$(/usr/bin/python3 -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)+d.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
cat >"$TMP/mint" <<SH
#!/bin/sh
echo "\$@" >> "$TMP/mints"
printf '{"token":"scan-app-token","expires_at":"$EXPIRES"}'
SH
# Writes stay on GH_CLI; a scan that reaches it is on the wrong credential.
cat >"$TMP/write-cli" <<SH
#!/bin/sh
echo "\$@" >> "$TMP/write-calls"
exit 1
SH
cat >"$TMP/fake-gh" <<'PY'
#!/usr/bin/env python3
import json, os, sys
with open(os.environ["SCAN_LEDGER"], "a", encoding="utf-8") as stream:
    stream.write(f"token={os.environ.get('GH_TOKEN', '')} {sys.argv[-1]}\n")
if os.environ.get("GH_TOKEN") != "scan-app-token":
    sys.stderr.write("fake gh: not the scan App token\n")
    raise SystemExit(1)
path = sys.argv[-1]
if path.endswith("/actions/workflows?per_page=100&page=1"):
    print(json.dumps({"total_count": 1, "workflows": [{"id": 99, "name": "Build and Test"}]}))
elif path.endswith("/actions/jobs/444"):
    print(json.dumps({"id": 444, "status": "in_progress", "conclusion": None,
                      "runner_name": "runner-1"}))
elif path.endswith("/actions/runs/333"):
    print(json.dumps({"id": 333, "workflow_id": 99, "name": "Build and Test",
                      "status": "in_progress", "conclusion": None}))
elif "/actions/runs?status=in_progress" in path:
    print(json.dumps({"total_count": 1, "workflow_runs": [
        {"id": 333, "workflow_id": 99, "name": "Build and Test", "status": "in_progress"}]}))
elif "/actions/runs/333/jobs?filter=latest" in path:
    print(json.dumps({"total_count": 1, "jobs": [
        {"id": 444, "status": "in_progress", "runner_name": "runner-1"}]}))
else:
    print(json.dumps({"total_count": 0, "jobs": [], "workflow_runs": []}))
PY
chmod +x "$TMP/mint" "$TMP/write-cli" "$TMP/fake-gh"
: >"$TMP/reader.pem"

# Exactly what scripts/macos_fleet_lanes.py renders for [github_scan_app].
export TARTCI_SCAN_APP_ID=5665767
export TARTCI_SCAN_APP_PRIVATE_KEY_PATH="$TMP/reader.pem"
export TARTCI_SCAN_APP_CACHE_DIR="$TMP/cache"
# Test seams: stub the mint and point the wrapper at a gh it may exec.
export TARTCI_SCAN_APP_GHAPP="$TMP/mint" TARTCI_SCAN_APP_GH="$TMP/fake-gh"
export SCAN_LEDGER="$TMP/scan-calls"

export TARTCI_ROOT="$ROOT" REPO="Generous-Corp/pulp" GH_CLI="$TMP/write-cli"
selection="$(sed -n '/^# scan-cli-selection: begin$/,/^# scan-cli-selection: end$/p' \
  "$ROOT/providers/tart-macos/runner.sh")"
# Control: the markers still bracket the real selection (a literal match).
# shellcheck disable=SC2016
case "$selection" in
  *'SCAN_GH_CLI="$TARTCI_ROOT/scripts/gh-scan-app"'*) ;;
  *) fail "scan-cli-selection block not found in runner.sh" ;;
esac
eval "$selection"
[ "$SCAN_GH_CLI" = "$ROOT/scripts/gh-scan-app" ] || fail "selected $SCAN_GH_CLI"

eval "$(sed -n '/^handle_supervisor_signal(){/,/^ensure_runner_version(){/p' \
  "$ROOT/providers/tart-macos/runner.sh" | sed '$d')"
export STATE_DIR="$TMP/state" WORKFLOW_CONFIG="Build and Test"
export CURRENT_REGISTERED_RUNNER="runner-1" RUNNER_NAME="lane-1"
mkdir -p "$STATE_DIR"
EVENTS="$TMP/events"
: >"$EVENTS"
export EVENT_LOG="$EVENTS"
event(){ printf '%s|%s\n' "$1" "${2:-}" >>"$EVENTS"; }
heartbeat(){ :; }

run_discover(){
  # Consumed by production functions loaded through eval above.
  # shellcheck disable=SC2034
  {
    CURRENT_RUN_ID="" CURRENT_JOB_ID="" CURRENT_WORKFLOW_NAME=""
    CURRENT_JOB_CAPTURE_STATUS="" CURRENT_JOB_RECEIPT=""
    CURRENT_JOB_SCAN_SPENT=0 CURRENT_JOB_SCAN_FAILURES=0 CURRENT_JOB_SCAN_NEXT_AT=0
    CURRENT_CANCEL_DISCOVERY_SCAN_SPENT=0 CURRENT_CANCEL_REVALIDATION_SCAN_SPENT=0
    CURRENT_CANCEL_TERMINAL_SCAN_SPENT=0 CURRENT_ASSIGNMENT_QUARANTINE=none
    CURRENT_SCAN_PID="" CURRENT_SCAN_TMP=""
  }
  : >"$TMP/scan-calls"; : >"$TMP/mints"; rm -rf "$TMP/cache"
  capture_current_job discover \
    || { sed 's/^/  /' "$EVENTS" >&2; fail "discover scan failed through gh-scan-app"; }
}

run_discover
[ "$CURRENT_JOB_CAPTURE_STATUS" = active ] || fail "status $CURRENT_JOB_CAPTURE_STATUS"
[ "$CURRENT_RUN_ID" = 333 ] && [ "$CURRENT_JOB_ID" = 444 ] || fail "ids $CURRENT_RUN_ID/$CURRENT_JOB_ID"
grep -q "^token=scan-app-token .*repos/$REPO/actions/" "$TMP/scan-calls" \
  || fail "first scan API call did not run on the scan App token"
grep -q -- "--repo $REPO\$" "$TMP/mints" || fail "mint was not bound to $REPO"
[ ! -e "$TMP/write-calls" ] || fail "a scan reached the write CLI"

# The lane's repository wins over an unrelated ambient binding, so a token is
# never minted for a repository other than the one being scanned.
export SHIPYARD_GH_APP_REPO="Generous-Corp/elsewhere" GH_REPO="Generous-Corp/elsewhere"
run_discover
grep -qx -- "token --app-id 5665767 --private-key $TMP/reader.pem --repo $REPO" "$TMP/mints" \
  || fail "ambient repository overrode the lane repository: $(cat "$TMP/mints")"

echo "scan app runner selection: OK"
