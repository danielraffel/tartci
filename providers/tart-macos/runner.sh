#!/usr/bin/env bash
# tart-macos/runner.sh — ephemeral, per-job GitHub Actions runner on Tart macOS.
# It mints a single-job JIT config, boots a fresh clone, runs the Actions agent
# once, emits state/heartbeat/events, then tears everything down. Defaults are
# pilot-safe (`pulp-build-vm`, not required `pulp-build`).
# `--print-queue` reports queued jobs whose requested labels are satisfiable by
# the configured runner labels; it is a safe preflight for the loop gate. It prints
# the queued COUNT on a successful scan (`0` = genuinely idle), or the sentinel `ERR`
# when the gh scan itself FAILS (rate-limit / timeout / degraded token / network) — so a
# failed poll is never misread as an empty queue. On sustained blindness (ERR for
# ~TARTCI_SCAN_BLIND_MAX polls ≈ 3 min) the loop self-restarts via `exit 75` (launchd
# KeepAlive respawns → fresh auth), instead of idling silent for hours (a real 5h wedge).
# Priority-aware idle gate (opt-in): set TARTCI_YIELD_TO_WORKFLOW_NAME +
# TARTCI_YIELD_TO_LABELS to make a SECONDARY lane yield its VM slot to a
# higher-priority lane. When set, the loop boots only when that priority lane
# has NO queued/in-progress work (in addition to the usual queue + cap checks),
# so on a shared-cap host (e.g. Apple's 2-running-macOS-guest limit) a long
# advisory job can never starve the required gate. Unset = no yielding, so the
# primary gate runner and existing lanes are byte-for-byte unchanged.
# `--print-priority-demand` reports that yield count (0 when the feature is off);
# a safe preflight for the gate.
# Host-health auto-yield (opt-in): set TARTCI_HOST_VITALS_YIELD=1 to make the
# loop stop booting NEW VMs while the host is saturated (memory-pressure critical
# / fresh jetsam), reading the shared `host_vitals.sh` signal. Off by default, so
# a host that never installs host_vitals is byte-for-byte unchanged. Unlike the
# priority gate this is FAIL-OPEN: a probe error prints 0 (do not yield), so a
# missing/broken host_vitals never wedges the required gate — the worst case is
# the crash-avoidance we simply don't get, never a stalled runner. Yields only on
# CRITICAL by default; TARTCI_HOST_VITALS_YIELD_ON_WARN=1 also drains on WARN.
# `--print-host-health` reports the yield decision (0 boot / 1 yield) as a safe
# preflight, mirroring `--print-priority-demand`.
set -euo pipefail

TARTCI_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export TART_HOME="${TART_HOME:-$HOME/VMs}"
# GitHub CLI used for every API call (queue polling, JIT mint, runner reclaim,
# job/run polling, cancel). Default `gh` (the personal/host auth) keeps generic
# tartci behavior unchanged. Hosts that authenticate as a GitHub App can set
# TARTCI_GH_CLI=ghapp to move ALL provider API traffic off the personal PAT and
# onto the App's separate rate-limit bucket — the per-poll calls (every VM_POLL
# seconds × every host) are the dominant throttle source. Exported so the inline
# python pollers below inherit it.
export TARTCI_GH_CLI="${TARTCI_GH_CLI:-gh}"
GH_CLI="$TARTCI_GH_CLI"
SSH_KEY_PRIV="${TARTCI_VM_SSH_KEY:-${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}}"
VM_USER="${TARTCI_VM_USER:-${PULP_VM_USER:-admin}}"
CACHE_ROOT="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/pulp-ci}}"
GOLDEN="${TARTCI_MACOS_GOLDEN:-${PULP_RUNNER_GOLDEN:-pulp-build-runner:latest}}"
REPO="${TARTCI_RUNNER_REPO:-${PULP_RUNNER_REPO:-danielraffel/pulp}}"
LABELS="${TARTCI_RUNNER_LABELS:-${PULP_RUNNER_LABELS:-self-hosted,macOS,ARM64,pulp-build-vm}}"
RUNNER_GROUP_ID="${TARTCI_RUNNER_GROUP_ID:-${PULP_RUNNER_GROUP_ID:-1}}"
WORKFLOW_NAME="${TARTCI_RUNNER_WORKFLOW_NAME:-Build and Test}"
# Priority-aware idle gate (opt-in; see header). YIELD_WORKFLOW empty = OFF.
YIELD_WORKFLOW="${TARTCI_YIELD_TO_WORKFLOW_NAME:-}"
YIELD_LABELS="${TARTCI_YIELD_TO_LABELS:-}"
# Host-health auto-yield (opt-in; see header). Empty/0 = OFF (no host_vitals call).
HOST_VITALS_YIELD="${TARTCI_HOST_VITALS_YIELD:-}"
HOST_VITALS_BIN="${TARTCI_HOST_VITALS_BIN:-host_vitals.sh}"
HOST_VITALS_YIELD_ON_WARN="${TARTCI_HOST_VITALS_YIELD_ON_WARN:-}"
QUEUE_RUN_LIMIT="${TARTCI_QUEUE_RUN_LIMIT:-20}"
GH_TIMEOUT="${TARTCI_GH_TIMEOUT_SECS:-15}"
LOOP=0
CAP="${TARTCI_MACOS_VM_CAP:-${PULP_VM_CAP:-2}}"
POLL="${TARTCI_VM_POLL:-${PULP_VM_POLL:-20}}"; case "$POLL" in ''|*[!0-9]*|0) POLL=20;; esac  # positive int only (self-heal arithmetic)
JOB_TIMEOUT="${TARTCI_JOB_TIMEOUT_SECS:-7200}"
JOB_WARN="${TARTCI_JOB_WARN_SECS:-5400}"
IDLE_TIMEOUT="${TARTCI_RUNNER_IDLE_TIMEOUT_SECS:-900}"
STATE_DIR="${TARTCI_STATE_DIR:-$HOME/.tartci/state/macos}"
EVENT_LOG="${TARTCI_EVENT_LOG:-$STATE_DIR/events.jsonl}"
MACOS_LOGROOT="${TARTCI_MACOS_LOGS:-$HOME/VMs/logs/tartci-macos}"
RUNNER_NAME="${TARTCI_RUNNER_NAME:-${PULP_RUNNER_NAME:-}}"
RUNNER_NAME_PREFIX="${TARTCI_RUNNER_NAME_PREFIX:-${PULP_RUNNER_NAME_PREFIX:-}}"
SLOT="${TARTCI_RUNNER_SLOT:-${PULP_RUNNER_SLOT:-1}}"
PRINT_NAME=0
PRINT_QUEUE=0
PRINT_PRIORITY=0
PRINT_HOST_HEALTH=0
CURRENT_VM=""
CURRENT_RPID=""
CURRENT_RUN_ID=""
CURRENT_JOB_ID=""
CURRENT_IP=""
CLEANED_UP=0
SUPERVISOR_PID="$$"
SUPERVISOR_PID_STARTED_AT="$(ps -p "$$" -o lstart= 2>/dev/null | tr -s ' ' | sed 's/^ //;s/ $//')"
HOST_NAME="$(hostname -s 2>/dev/null || hostname)"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
now_epoch(){ date +%s; }
elapsed(){ awk -v start="$1" -v end="$2" 'BEGIN { printf "%.1f", end - start }'; }

# shellcheck source=providers/common/vm-lease.lib.sh
source "$TARTCI_ROOT/providers/common/vm-lease.lib.sh"
# shellcheck source=providers/common/vm-state.lib.sh
source "$TARTCI_ROOT/providers/common/vm-state.lib.sh"

usage(){ sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; }

derive_runner_name(){
  if [ -n "$RUNNER_NAME" ]; then printf '%s' "$RUNNER_NAME"; return; fi
  local prefix="$RUNNER_NAME_PREFIX" class="" l
  if [ -z "$prefix" ]; then
    IFS=',' read -r -a label_parts <<< "$LABELS"
    for l in "${label_parts[@]}"; do case "$l" in pulp-build-?*) class="${l#pulp-build-}";; esac; done
    if [ -n "$class" ]; then
      prefix="pulp-$class"
    else
      prefix="pulp-$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-*//;s/-*$//')"
    fi
  fi
  printf '%s-%02d' "$prefix" "$((10#$SLOT))"
}

# Per-BOOT GitHub runner registration name: <lane>-<supervisor pid>-<boot index>,
# mirroring the qemu-windows lane (`${RUNNER_NAME_PREFIX}-$$-$i`). This must be
# unique for every boot and never reused. Rationale: a fixed static name (the bare
# $RUNNER_NAME, e.g. `pulp-vm-01`) is reused across boots AND supervisor restarts,
# so a SIGKILL'd VM (kickstart / yield / crash) orphans a GitHub runner registration
# stuck "offline but running a job". The next boot then collides on that name
# (`generate-jitconfig` → HTTP 409 "already exists"), and reclaim_runner_name can't
# clear it without repo-admin — wedging the ENTIRE macOS gate until an admin deletes
# the ghost by hand (pulp-runner-ops "Sixth symptom", 2026-07-06). An
# never-reused name makes the collision impossible: a dead VM's registration just
# ages out. $$ is the supervisor PID even inside command substitution (bash keeps it
# the top-level shell's PID); $1 is the monotonic per-boot index.
ephemeral_boot_name(){ printf '%s-%s-%s' "$RUNNER_NAME" "$$" "$1"; }

while [ $# -gt 0 ]; do case "$1" in
  --loop) LOOP=1; shift;;
  --once) LOOP=0; shift;;
  --golden) GOLDEN="$2"; shift 2;;
  --labels) LABELS="$2"; shift 2;;
  --repo) REPO="$2"; shift 2;;
  --cap) CAP="$2"; shift 2;;
  --name) RUNNER_NAME="$2"; shift 2;;
  --name-prefix) RUNNER_NAME_PREFIX="$2"; shift 2;;
  --slot) SLOT="$2"; shift 2;;
  --state-dir) STATE_DIR="$2"; EVENT_LOG="$STATE_DIR/events.jsonl"; shift 2;;
  --print-name) PRINT_NAME=1; shift;;
  --print-boot-name) PRINT_BOOT_NAME="$2"; shift 2;;  # debug/test: emit ephemeral_boot_name <i>
  --print-queue) PRINT_QUEUE=1; shift;;
  --print-priority-demand) PRINT_PRIORITY=1; shift;;
  --print-host-health) PRINT_HOST_HEALTH=1; shift;;
  --yield-to-workflow) YIELD_WORKFLOW="$2"; shift 2;;
  --yield-to-labels) YIELD_LABELS="$2"; shift 2;;
  -h|--help) usage; exit 0;;
  *) die "unknown arg: $1";;
esac; done

RUNNER_NAME="$(derive_runner_name)"
[ "$PRINT_NAME" = 1 ] && { printf '%s\n' "$RUNNER_NAME"; exit 0; }
[ -n "${PRINT_BOOT_NAME:-}" ] && { printf '%s\n' "$(ephemeral_boot_name "$PRINT_BOOT_NAME")"; exit 0; }

command -v tart >/dev/null 2>&1 || die "tart not installed"
command -v "$GH_CLI" >/dev/null 2>&1 || die "GitHub CLI '$GH_CLI' (TARTCI_GH_CLI) not installed / authed (need repo admin to mint JIT config)"
mkdir -p "$STATE_DIR"

json_sanitize(){ printf '%s' "$1" | tr '\n\r\t"' '    '; }
event(){
  local kind="$1" detail="${2:-}" ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '{"ts":"%s","event":"%s","runner":"%s","vm":"%s","detail":"%s"}\n' \
    "$ts" "$(json_sanitize "$kind")" "$(json_sanitize "$RUNNER_NAME")" \
    "$(json_sanitize "${CURRENT_VM:-}")" "$(json_sanitize "$detail")" >>"$EVENT_LOG"
}

heartbeat(){
  local phase="$1" ts state_file tmp_file
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  state_file="$STATE_DIR/$RUNNER_NAME.state.json"
  tmp_file="$(mktemp "$state_file.tmp.XXXXXX")" || return 1
  if cat >"$tmp_file" <<EOF
{"ts":"$ts","provider":"tart-macos","host":"$(json_sanitize "$HOST_NAME")","runner":"$RUNNER_NAME","vm":"${CURRENT_VM:-}","vm_ip":"$(json_sanitize "${CURRENT_IP:-}")","phase":"$(json_sanitize "$phase")","lifecycle":"ephemeral","labels":"$(json_sanitize "$LABELS")","repo":"$(json_sanitize "$REPO")","run_id":"$(json_sanitize "${CURRENT_RUN_ID:-}")","job_id":"$(json_sanitize "${CURRENT_JOB_ID:-}")","supervisor_pid":"$SUPERVISOR_PID","supervisor_pid_started_at":"$(json_sanitize "$SUPERVISOR_PID_STARTED_AT")"}
EOF
  then
    mv -f "$tmp_file" "$state_file"
  else
    rm -f "$tmp_file"
    return 1
  fi
}

runtime_emit_complete(){
  [ "${TARTCI_RUNTIME_MEASURE:-0}" = 1 ] || return 0
  local status="$1" failure_class="$2" exit_code="$3" timing_path="$4" log_dir="$5"
  python3 "$TARTCI_ROOT/scripts/runtime_measure.py" complete \
    --repo "$REPO" \
    --workflow "$WORKFLOW_NAME" \
    --provider tart-macos \
    --platform macos \
    --arch arm64 \
    --runner-name "$RUNNER_NAME" \
    --vm-name "${CURRENT_VM:-$RUNNER_NAME}" \
    --labels "$LABELS" \
    --run-id "${CURRENT_RUN_ID:-}" \
    --job-id "${CURRENT_JOB_ID:-}" \
    --golden "$GOLDEN" \
    --cache-mode unknown \
    --cache-mode-source unknown \
    --status "$status" \
    --failure-class "$failure_class" \
    --exit-code "$exit_code" \
    --timing-path "$timing_path" \
    --log-dir "$log_dir" \
    --json >/dev/null 2>&1 || note "runtime measurement emit failed (ignored)"
}

running_macos_vms(){
  local payload fail_closed
  fail_closed="${TARTCI_MACOS_HARD_MAX:-2}"
  if ! payload="$(tart list --format json 2>/dev/null)"; then
    printf '%s\n' "$fail_closed"
    return 0
  fi
  TARTCI_TART_LIST_JSON="$payload" python3 - "$fail_closed" <<'PY' || printf '%s\n' "$fail_closed"
import json, os, subprocess, sys
try:
    vms = json.loads(os.environ["TARTCI_TART_LIST_JSON"])
except Exception:
    print(sys.argv[1])
    raise SystemExit
n = 0
for vm in vms if isinstance(vms, list) else []:
    if not str(vm.get("State", vm.get("state", ""))).lower().startswith("run"):
        continue
    name = vm.get("Name") or vm.get("name")
    os_name = ""
    if name:
        try:
            out = subprocess.check_output(["tart", "get", str(name), "--format", "json"], text=True, stderr=subprocess.DEVNULL)
            os_name = str(json.loads(out).get("OS", "")).lower()
        except Exception:
            os_name = ""
    # Missing OS is conservative: count it against the macOS cap.
    if os_name in ("", "darwin", "macos"):
        n += 1
print(n)
PY
}

queued_work(){
  python3 - "$REPO" "$WORKFLOW_NAME" "$LABELS" "$QUEUE_RUN_LIMIT" "$GH_TIMEOUT" <<'PY'
import json
import os
import subprocess
import sys

# Honor TARTCI_GH_CLI (inherited from the exported env) so polling rides the
# same App bucket as the bash calls; default `gh`.
GH_CLI = os.environ.get("TARTCI_GH_CLI") or "gh"
repo, workflow_name, labels_csv, queue_limit, gh_timeout = sys.argv[1:]
runner_labels = {label.strip() for label in labels_csv.split(",") if label.strip()}
try:
    limit = max(1, min(100, int(queue_limit)))
except ValueError:
    limit = 20
try:
    timeout = max(1, int(gh_timeout))
except ValueError:
    timeout = 15


def gh_json(path):
    return json.loads(
        subprocess.check_output(
            [GH_CLI, "api", path],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    )


# Scan ONLY our workflow's runs, not the global runs list. The global
# `runs?status=queued` endpoint returns the newest `per_page` runs across ALL workflows; under
# multi-workflow / multi-PR load those newest runs are dominated by OTHER workflows, so our
# Build-and-Test runs get crowded out of the window and the fleet can't see queued build legs it
# should serve — VM-lane starvation under exactly the heavy load we care about. Resolving the
# workflow id and hitting `workflows/{id}/runs` keeps the window filled with only our runs, so a
# deep queue of build jobs stays visible. per_page is bumped to 100 (the endpoint max) so a deep
# backlog is covered in one page.
_WF_PER_PAGE = 100
runs = []
seen = set()
try:
    wf_id = None
    wfs = gh_json(f"repos/{repo}/actions/workflows?per_page=100")
    for wf in wfs.get("workflows", []):
        if wf.get("name") == workflow_name:
            wf_id = wf.get("id")
            break
    if wf_id is not None:
        for status in ("queued", "in_progress"):
            data = gh_json(
                f"repos/{repo}/actions/workflows/{wf_id}/runs?status={status}&per_page={_WF_PER_PAGE}"
            )
            for run in data.get("workflow_runs", []):
                run_id = run.get("id")
                if run_id in seen:
                    continue
                seen.add(run_id)
                runs.append(run)
    else:
        # Workflow not found by name (renamed / not yet created) → degrade to the global scan so
        # behavior is never worse than before.
        for status in ("queued", "in_progress"):
            data = gh_json(f"repos/{repo}/actions/runs?status={status}&per_page={limit}")
            for run in data.get("workflow_runs", []):
                run_id = run.get("id")
                if run_id in seen:
                    continue
                seen.add(run_id)
                runs.append(run)
except Exception:
    # A scan FAILURE (gh-api timeout / rate-limit / auth degradation / network) is NOT the same as
    # "no queued work". Emit a distinct sentinel so the loop stays blind-AWARE and can self-restart
    # to re-establish auth, instead of silently idling as if the queue were empty — the multi-hour
    # supervisor wedge this guards against (an alive loop printing `queued=0` while jobs pile up).
    print("ERR")
    raise SystemExit

# Oldest-first so the longest-starved run is checked first (fairness + urgency under a deep queue).
# The loop only needs to know whether >= 1 servable job exists to boot ONE VM this iteration, so we
# SHORT-CIRCUIT at the first match instead of fetching jobs for every run — a `jobs` call per run
# turns a deep backlog into a 60s+ scan that can't keep up with the poll interval. We still bound the
# no-match case (`_MAX_JOB_FETCHES`) so a large queue of non-servable runs can't stall the scan.
runs.sort(key=lambda r: r.get("created_at") or "")
_MAX_JOB_FETCHES = 30
matches = 0
fetched = 0
for run in runs:
    if run.get("name") != workflow_name:
        continue
    run_id = run.get("id")
    if not run_id:
        continue
    if fetched >= _MAX_JOB_FETCHES:
        break
    fetched += 1
    try:
        jobs = gh_json(f"repos/{repo}/actions/runs/{run_id}/jobs")
    except Exception:
        continue
    for job in jobs.get("jobs", []):
        if job.get("status") != "queued":
            continue
        job_labels = {str(label) for label in job.get("labels", []) if str(label)}
        if job_labels and job_labels.issubset(runner_labels):
            matches += 1
    if matches > 0:
        break  # >= 1 servable job is all the boot gate needs; GitHub assigns the oldest match.
print(matches)
PY
}

# priority_demand — how many jobs a higher-priority lane currently has WAITING or
# RUNNING. Used by the opt-in idle gate so a secondary lane yields its VM slot.
#
# Returns 0 (and never calls gh) when the feature is OFF (YIELD_WORKFLOW empty),
# so the primary gate runner / release lane are unaffected. When ON, it counts
# queued + in_progress jobs of the YIELD_WORKFLOW whose requested labels are a
# SUBSET of the priority lane's labels (YIELD_LABELS) — GitHub's assignment rule:
# a runner serves a job iff it advertises every label the job requests. We scan
# BOTH queued and in_progress because a priority run can flip to in_progress
# (its GitHub-hosted resolver/classify job) before its self-hosted leg is queued.
#
# The pure subset matcher below is intentionally identical in shape to the one
# used elsewhere (reads LABEL_JSON env + job-label JSON lines on stdin, prints a
# count) so it can be extracted and unit-tested without gh/tart. Non-zero output
# means "a priority job needs a slot — do not boot the secondary VM".
priority_demand(){
  [ -n "$YIELD_WORKFLOW" ] || { printf '%s\n' 0; return 0; }
  # FAIL CLOSED. If we cannot read priority-lane demand, assume there IS demand
  # (print 1 → the loop gate yields) rather than booting blind. gh errors
  # (rate-limit / 5xx) cluster during exactly the load spikes when the gate most
  # needs its slot, so a fail-OPEN guard would let the secondary grab the gate's
  # slot precisely when that is most harmful. Worst case of fail-closed is an
  # advisory lane that idles during a gh outage — strictly safer than risking the
  # required gate. (`local x=$(...)` masks the substitution's exit code, so vars
  # are declared first and assigned separately so `||` actually fires.)
  local label_json count=0 run_id matches ids run_ids="" st
  label_json="$(YL="$YIELD_LABELS" python3 -c 'import json, os; print(json.dumps([x.strip() for x in os.environ["YL"].split(",") if x.strip()]))')"
  for st in queued in_progress; do
    ids="$("$GH_CLI" api "repos/$REPO/actions/runs?status=$st&per_page=$QUEUE_RUN_LIMIT" \
      --jq ".workflow_runs[] | select(.name == \"${YIELD_WORKFLOW}\") | .id" 2>/dev/null)" \
      || { printf '%s\n' 1; return 0; }
    [ -n "$ids" ] && run_ids="$run_ids$ids"$'\n'
  done
  while IFS= read -r run_id; do
    [ -n "$run_id" ] || continue
    ids="$("$GH_CLI" api "repos/$REPO/actions/runs/$run_id/jobs?filter=latest&per_page=100" \
      --jq '.jobs[] | select(.status == "queued" or .status == "in_progress") | .labels | @json' 2>/dev/null)" \
      || { printf '%s\n' 1; return 0; }
    matches="$(printf '%s' "$ids" | LABEL_JSON="$label_json" python3 -c '
import json, os, sys
want = {s.lower() for s in json.loads(os.environ["LABEL_JSON"])}
n = 0
for line in sys.stdin:
    try:
        labels = {s.lower() for s in json.loads(line)}
    except Exception:
        continue
    # A priority job that requests `labels` would land on the priority runner
    # iff that runner advertises every requested label (labels ⊆ priority set).
    # Such a job competes for the shared VM cap, so the secondary lane must
    # stand down while any exist.
    if labels and labels.issubset(want):
        n += 1
print(n)
')" || { printf '%s\n' 1; return 0; }
    count=$((count + ${matches:-0}))
  done <<< "$run_ids"
  printf '%s\n' "$count"
}

# Host-health auto-yield. Prints 1 when the loop should STOP booting new VMs
# because the host is saturated, 0 when it is safe to boot. Opt-in via
# TARTCI_HOST_VITALS_YIELD; off by default so hosts without host_vitals installed
# are unaffected.
#
# FAIL OPEN (the deliberate opposite of priority_demand's fail-closed): if the
# host_vitals probe is missing, unexecutable, or errors, print 0 (boot). Rationale
# — host-health yield is a crash-avoidance *nicety*, not a correctness gate. A
# broken probe must never wedge the required macOS runner; the worst case of
# fail-open is that we forgo the avoidance we can't measure, which is exactly where
# we were before this feature. (Priority demand gates a shared VM cap and so must
# fail closed; these are different risk classes on purpose.)
#
# host_vitals.sh exit codes: 0 green, 10 warn, 20 critical. Yield on >=20 always,
# and on >=10 when TARTCI_HOST_VITALS_YIELD_ON_WARN is set.
host_health_yield(){
  [ -n "$HOST_VITALS_YIELD" ] && [ "$HOST_VITALS_YIELD" != 0 ] || { printf '%s\n' 0; return 0; }
  command -v "$HOST_VITALS_BIN" >/dev/null 2>&1 || { printf '%s\n' 0; return 0; }
  # `local code=0; ... || code=$?` keeps set -e from aborting on a non-zero exit,
  # and declaring first avoids `local x=$(...)` masking the command's status.
  local code=0
  "$HOST_VITALS_BIN" >/dev/null 2>&1 || code=$?
  if [ "$code" -ge 20 ]; then
    printf '%s\n' 1
  elif [ "$code" -ge 10 ] && [ -n "$HOST_VITALS_YIELD_ON_WARN" ] && [ "$HOST_VITALS_YIELD_ON_WARN" != 0 ]; then
    printf '%s\n' 1
  else
    printf '%s\n' 0
  fi
}

reclaim_runner_name(){
  local name="$1" id attempt
  for attempt in $(seq 1 18); do
    id="$("$GH_CLI" api "repos/$REPO/actions/runners" --paginate \
          --jq ".runners[] | select(.name==\"$name\") | .id" 2>/dev/null | head -n1 || true)"
    [ -n "$id" ] || break
    note "reclaiming static name '$name': deleting stale runner registration (id=$id attempt=$attempt)"
    "$GH_CLI" api -X DELETE "repos/$REPO/actions/runners/$id" >/dev/null 2>&1 && break
    sleep 10
  done
  tart delete "$name" >/dev/null 2>&1 || true
}

discard_current_vm(){
  [ -n "$CURRENT_VM" ] || return 0
  note "stopping — tearing down in-flight VM $CURRENT_VM"
  [ -n "$CURRENT_RPID" ] && kill -9 "$CURRENT_RPID" 2>/dev/null || true
  tart stop "$CURRENT_VM" >/dev/null 2>&1 || true
  tart delete "$CURRENT_VM" >/dev/null 2>&1 || true
  CURRENT_VM=""
  CURRENT_RPID=""
  CURRENT_IP=""
}

cleanup(){
  [ "$CLEANED_UP" = 1 ] && return 0
  discard_current_vm
  tartci_release_vm_lease
  [ -n "${CURRENT_RESV:-}" ] && rm -f "$CURRENT_RESV" 2>/dev/null || true
  reclaim_runner_name "$RUNNER_NAME" 2>/dev/null || true
  CLEANED_UP=1
  heartbeat stopped
}
trap 'event supervisor_signal "INT/TERM"; cleanup; trap - EXIT; exit 143' INT TERM
trap 'cleanup' EXIT

capture_current_job(){
  local run_id job_id
  CURRENT_RUN_ID=""
  CURRENT_JOB_ID=""
  while IFS= read -r run_id; do
    [ -n "$run_id" ] || continue
    job_id="$("$GH_CLI" api "repos/$REPO/actions/runs/$run_id/jobs" \
      --jq ".jobs[] | select(.runner_name==\"$RUNNER_NAME\") | select(.status==\"in_progress\") | .id" \
      2>/dev/null | head -n1 || true)"
    if [ -n "$job_id" ]; then
      CURRENT_RUN_ID="$run_id"
      CURRENT_JOB_ID="$job_id"
      return 0
    fi
  done < <("$GH_CLI" api "repos/$REPO/actions/runs?per_page=100" \
    --jq ".workflow_runs[] | select(.name == \"$WORKFLOW_NAME\") | .id" 2>/dev/null || true)
  return 1
}

cancel_current_run(){
  [ -n "$CURRENT_RUN_ID" ] || capture_current_job || true
  if [ -n "$CURRENT_RUN_ID" ]; then
    event run_cancel "run_id=$CURRENT_RUN_ID job_id=${CURRENT_JOB_ID:-} reason=timeout"
    "$GH_CLI" api -X POST "repos/$REPO/actions/runs/$CURRENT_RUN_ID/cancel" >/dev/null 2>&1 || true
  fi
}

run_runner_until_done(){
  local vm="$1" ip="$2" jit="$3"
  local runner_log="$STATE_DIR/$vm.actions-runner.log"
  local ssh_pid start assigned_at=0 now idle_elapsed job_elapsed assigned=0 warned=0 rc=0
  : >"$runner_log"
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "mkdir -p ~/Library/Caches ~/.ccache-tmp && ln -sfn '/Volumes/My Shared Files/ccache' ~/Library/Caches/ccache && \
     printf '%s' '$jit' > ~/jit.cfg && eval \"\$(/opt/homebrew/bin/brew shellenv)\" && cd ~/actions-runner && ./run.sh --jitconfig \"\$(cat ~/jit.cfg)\"" \
    >"$runner_log" 2>&1 & ssh_pid=$!
  start="$(date +%s)"
  while kill -0 "$ssh_pid" 2>/dev/null; do
    now="$(date +%s)"
    idle_elapsed=$((now - start))
    if [ "$assigned" = 0 ] && grep -q 'Running job:' "$runner_log" 2>/dev/null; then
      assigned=1
      assigned_at="$now"
      for _ in $(seq 1 6); do
        capture_current_job && break
        sleep 2
      done
      event job_assigned "$(grep 'Running job:' "$runner_log" | tail -1)"
      heartbeat job-running
    fi
    if [ "$assigned" = 0 ] && [ "$idle_elapsed" -ge "$IDLE_TIMEOUT" ]; then
      event idle_timeout "elapsed=${idle_elapsed}s rerun_eligible=false"
      kill "$ssh_pid" 2>/dev/null || true
      wait "$ssh_pid" 2>/dev/null || true
      sed 's/^/[actions-runner] /' "$runner_log" >&2 || true
      return 124
    fi
    if [ "$assigned" = 1 ]; then
      job_elapsed=$((now - assigned_at))
      [ -n "$CURRENT_RUN_ID" ] || capture_current_job || true
      if [ "$warned" = 0 ] && [ "$job_elapsed" -ge "$JOB_WARN" ]; then
        warned=1
        event job_warn "elapsed=${job_elapsed}s"
      fi
      if [ "$job_elapsed" -ge "$JOB_TIMEOUT" ]; then
        event job_timeout "elapsed=${job_elapsed}s run_id=${CURRENT_RUN_ID:-} job_id=${CURRENT_JOB_ID:-} rerun_eligible=true"
        cancel_current_run
        for _ in $(seq 1 30); do
          kill -0 "$ssh_pid" 2>/dev/null || break
          sleep 2
        done
        kill -0 "$ssh_pid" 2>/dev/null && kill "$ssh_pid" 2>/dev/null || true
        wait "$ssh_pid" 2>/dev/null || true
        sed 's/^/[actions-runner] /' "$runner_log" >&2 || true
        return 124
      fi
    fi
    heartbeat "$([ "$assigned" = 1 ] && printf job-running || printf idle-wait)"
    sleep 5
  done
  wait "$ssh_pid" || rc=$?
  sed 's/^/[actions-runner] /' "$runner_log" >&2 || true
  return "$rc"
}

run_one(){
  # Per-boot EPHEMERAL registration name (see ephemeral_boot_name) — never the bare
  # static $RUNNER_NAME, which would collide with an orphaned registration and wedge
  # the gate. $RUNNER_NAME stays the stable lane identity for state/heartbeat.
  local i="$1" vm; vm="$(ephemeral_boot_name "$i")"
  local jit label_args=() l boot_log rpid ip="" rc=0
  local lease_cores lease_priority
  local t_start t_booted t_runner_done t_done logdir=""
  t_start="$(now_epoch)"
  if [ "${TARTCI_RUNTIME_MEASURE:-0}" = 1 ]; then
    logdir="$MACOS_LOGROOT/$vm"
    mkdir -p "$logdir"
  fi
  tartci_check_disk_floor "$TART_HOME" || return $?
  tartci_check_disk_floor "$CACHE_ROOT" || return $?
  CLEANED_UP=0
  CURRENT_RUN_ID=""
  CURRENT_JOB_ID=""
  reclaim_runner_name "$vm"
  lease_cores="$(tartci_vm_lease_cores tart-macos)"
  lease_mem="$(tartci_vm_lease_mem_mb tart-macos)"
  lease_priority="$(tartci_vm_lease_priority "$LABELS")"
  tartci_acquire_vm_lease "$vm" "$lease_cores" "tart-macos-vm" "$lease_priority" "$LABELS" "$lease_mem" || return $?
  heartbeat minting-jit
  event mint_jit "labels=$LABELS"
  IFS=',' read -r -a labels_split <<< "$LABELS"
  for l in "${labels_split[@]}"; do label_args+=(-f "labels[]=$l"); done
  jit="$("$GH_CLI" api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
        -f "name=$vm" -F "runner_group_id=$RUNNER_GROUP_ID" "${label_args[@]}" \
        --jq '.encoded_jit_config')" || { tartci_release_vm_lease; die "JIT config mint failed (need repo admin)"; }
  [ -n "$jit" ] || { tartci_release_vm_lease; die "empty JIT config"; }

  note "[$i] clone $GOLDEN → $vm (CoW) + boot with host ccache mounted"
  event clone_start "golden=$GOLDEN"
  if ! tart clone "$GOLDEN" "$vm"; then
    tartci_release_vm_lease
    runtime_emit_complete fail boot_failed 1 "" "$logdir"
    return 1
  fi
  CURRENT_VM="$vm"
  if ! tartci_set_tart_vm_cpu "$vm" "$lease_cores"; then
    note "[$i] failed to set $vm CPU count to lease cores=$lease_cores"
    discard_current_vm
    tartci_release_vm_lease
    runtime_emit_complete fail boot_failed 1 "" "$logdir"
    return 1
  fi
  mkdir -p "$CACHE_ROOT/ccache"
  boot_log="$(mktemp -t "tart-run-$vm")"
  tart run --no-graphics --dir="ccache:$CACHE_ROOT/ccache" "$vm" >"$boot_log" 2>&1 & rpid=$!
  CURRENT_RPID="$rpid"
  heartbeat booting

  for _ in $(seq 1 60); do ip="$(tart ip "$vm" 2>/dev/null || true)"; [ -n "$ip" ] && break; sleep 2; done
  if [ -z "$ip" ]; then
    note "[$i] no IP after 120s — last tart run lines:"; tail -10 "$boot_log" >&2 2>/dev/null || true
    rm -f "$boot_log"; event boot_failed "no_ip"; runtime_emit_complete fail boot_failed 1 "" "$logdir"
    discard_current_vm
    tartci_release_vm_lease
    return 1
  fi
  CURRENT_IP="$ip"
  rm -f "$boot_log"
  for _ in $(seq 1 90); do ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" true 2>/dev/null && break; sleep 2; done
  t_booted="$(now_epoch)"
  note "[$i] vm $vm up at $ip — launching JIT runner (idle_timeout=${IDLE_TIMEOUT}s job_timeout=${JOB_TIMEOUT}s)"
  event boot_ok "ip=$ip"
  heartbeat idle-wait

  run_runner_until_done "$vm" "$ip" "$jit" || rc=$?
  t_runner_done="$(now_epoch)"
  if [ "$rc" -ne 0 ]; then note "[$i] runner exited non-zero rc=$rc — VM will be discarded"; fi

  note "[$i] discarding ephemeral VM $vm"
  event teardown "rc=$rc"
  tart stop "$vm" >/dev/null 2>&1 || true
  kill "$rpid" 2>/dev/null || true
  sleep 2
  tart delete "$vm" >/dev/null 2>&1 || true
  tartci_release_vm_lease
  t_done="$(now_epoch)"
  if [ "${TARTCI_RUNTIME_MEASURE:-0}" = 1 ]; then
    {
      printf 'phase\tseconds\n'
      printf 'boot_to_ssh\t%s\n' "$(elapsed "$t_start" "$t_booted")"
      printf 'runner_process\t%s\n' "$(elapsed "$t_booted" "$t_runner_done")"
      printf 'cleanup\t%s\n' "$(elapsed "$t_runner_done" "$t_done")"
      printf 'total\t%s\n' "$(elapsed "$t_start" "$t_done")"
    } >"$logdir/timing.tsv"
    if [ "$rc" -eq 0 ]; then
      runtime_emit_complete pass unknown 0 "$logdir/timing.tsv" "$logdir"
    elif [ "$rc" -eq 124 ]; then
      runtime_emit_complete fail runner_timeout "$rc" "$logdir/timing.tsv" "$logdir"
    else
      runtime_emit_complete fail source_failure "$rc" "$logdir/timing.tsv" "$logdir"
    fi
  fi
  CURRENT_VM=""
  CURRENT_RPID=""
  CURRENT_IP=""
  CURRENT_RUN_ID=""
  CURRENT_JOB_ID=""
  reclaim_runner_name "$vm"
  heartbeat stopped
  CLEANED_UP=1
  return 0
}

i=0
[ "$PRINT_QUEUE" = 1 ] && { queued_work; exit 0; }
[ "$PRINT_PRIORITY" = 1 ] && { priority_demand; exit 0; }
[ "$PRINT_HOST_HEALTH" = 1 ] && { host_health_yield; exit 0; }

# Part F — host-wide macOS VM cap (live, GUI-adjustable) + cross-lane mutex.
# shellcheck source=providers/tart-macos/macos-vm-cap.lib.sh
source "${BASH_SOURCE[0]%/*}/macos-vm-cap.lib.sh"

if [ "$LOOP" = 1 ]; then
  note "ephemeral macOS runner LOOP; golden=$GOLDEN labels=$LABELS cap=$CAP yield_to=${YIELD_WORKFLOW:-<off>} host_vitals_yield=${HOST_VITALS_YIELD:-<off>}"
  # Scan-blindness self-heal: `queued_work` prints `ERR` (not a count) when the gh queue scan fails.
  # Treating that as 0 silently idles the supervisor while jobs pile up (the observed multi-hour
  # wedge). Count consecutive blind polls; after ~this many seconds of continuous blindness,
  # self-restart so launchd (KeepAlive) respawns a fresh process with fresh gh auth — the exact
  # manual recovery, automated. A blip self-heals on the next successful poll (blind resets to 0).
  blind=0
  BLIND_MAX="${TARTCI_SCAN_BLIND_MAX:-$(( (180 + POLL - 1) / POLL ))}"
  heartbeat loop
  while true; do
    q="$(queued_work)"; cap="$(tartci_effective_cap)"; r="$(running_macos_vms)"
    # Blind-aware: a non-numeric `q` (ERR) means the gh queue scan FAILED — do NOT treat it as an
    # empty queue. Count consecutive blind polls; after a sustained window, self-restart for fresh
    # gh auth (the supervisor is idle at the loop top — run_one blocks — so cleanup discards no live
    # VM). Any successful poll resets the counter, so a transient blip costs nothing.
    if ! printf '%s' "$q" | grep -qxE '[0-9]+'; then
      blind=$((blind + 1))
      note "SCAN BLIND (gh queue scan failed) ${blind}/${BLIND_MAX} — NOT idling as empty (running_macos_vms=$r/$cap)"
      event scan_blind "consecutive=$blind running=$r/$cap"
      heartbeat scan_blind
      if [ "$blind" -ge "$BLIND_MAX" ]; then
        note "SCAN BLIND ~$((blind * POLL))s — self-restarting the supervisor for fresh gh auth (launchd KeepAlive respawns)"
        event scan_blind_restart "seconds=$((blind * POLL))"
        exit 75
      fi
      sleep "$POLL"; continue
    fi
    blind=0
    # Only probe priority demand when THIS lane actually has work — no point
    # spending a gh round-trip (and the API quota the secondary-rate-limit cares
    # about) to decide whether to yield a slot we wouldn't use anyway. Stays 0
    # when there's no work, and the feature is off entirely for the gate runner.
    p=0
    [ "${q:-0}" -gt 0 ] && p="$(priority_demand)"
    # Host-health yield: only worth probing when we actually have work to boot.
    # Cheap local check (no gh call), fail-open, and 0 when the feature is off.
    hh=0
    [ "${q:-0}" -gt 0 ] && hh="$(host_health_yield)"
    # Idle gate: boot only when (1) this lane has work, (2) a VM slot is free,
    # (3) no higher-priority lane is waiting/running, and (4) the host is healthy.
    # (3) is always satisfied when the priority feature is off (priority_demand
    # returns 0) and (4) when host-health yield is off (host_health_yield returns
    # 0), so this is a no-op for a runner with neither feature enabled.
    if [ "${q:-0}" -gt 0 ] && [ "${p:-0}" -eq 0 ] && [ "${hh:-0}" -eq 0 ] && resv="$(tartci_claim_macos_slot "$cap")" && [ -n "$resv" ]; then
      CURRENT_RESV="$resv"
      i=$((i+1)); note "[$i] queued=$q running_macos_vms=$r/$cap priority_demand=$p host_health_yield=$hh → booting ephemeral VM"
      run_one "$i" || sleep "$POLL"
      rm -f "$resv" 2>/dev/null || true; CURRENT_RESV=""
    elif [ "${q:-0}" -gt 0 ] && [ "${hh:-0}" -gt 0 ]; then
      note "yielding ${POLL}s (queued=$q host_health_yield=$hh running_macos_vms=$r/$cap) — host saturated, deferring new VM boot"
      event yielded_host_health "queued=$q host_health_yield=$hh running=$r/$cap"
      heartbeat yielding
      sleep "$POLL"
    elif [ "${q:-0}" -gt 0 ] && [ "${p:-0}" -gt 0 ]; then
      note "yielding ${POLL}s (queued=$q priority_demand=$p running_macos_vms=$r/$cap) — priority lane '${YIELD_WORKFLOW}' has the slot"
      event yielded_to_priority "workflow=$YIELD_WORKFLOW queued=$q priority_demand=$p running=$r/$cap"
      heartbeat yielding
      sleep "$POLL"
    else
      note "waiting ${POLL}s (queued=$q running_macos_vms=$r/$cap priority_demand=$p)"
      heartbeat waiting
      sleep "$POLL"
    fi
  done
else
  note "ephemeral macOS runner ONCE; golden=$GOLDEN labels=$LABELS"
  run_one 1
fi
