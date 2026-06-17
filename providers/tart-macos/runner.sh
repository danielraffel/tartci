#!/usr/bin/env bash
# tart-macos/runner.sh — ephemeral, per-job GitHub Actions runner on Tart macOS.
# It mints a single-job JIT config, boots a fresh clone, runs the Actions agent
# once, emits state/heartbeat/events, then tears everything down. Defaults are
# pilot-safe (`pulp-build-vm`, not required `pulp-build`).
# `--print-queue` reports queued jobs whose requested labels are satisfiable by
# the configured runner labels; it is a safe preflight for the loop gate.
# Priority-aware idle gate (opt-in): set TARTCI_YIELD_TO_WORKFLOW_NAME +
# TARTCI_YIELD_TO_LABELS to make a SECONDARY lane yield its VM slot to a
# higher-priority lane. When set, the loop boots only when that priority lane
# has NO queued/in-progress work (in addition to the usual queue + cap checks),
# so on a shared-cap host (e.g. Apple's 2-running-macOS-guest limit) a long
# advisory job can never starve the required gate. Unset = no yielding, so the
# primary gate runner and existing lanes are byte-for-byte unchanged.
# `--print-priority-demand` reports that yield count (0 when the feature is off);
# a safe preflight for the gate.
set -euo pipefail

TARTCI_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export TART_HOME="${TART_HOME:-$HOME/VMs}"
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
QUEUE_RUN_LIMIT="${TARTCI_QUEUE_RUN_LIMIT:-20}"
GH_TIMEOUT="${TARTCI_GH_TIMEOUT_SECS:-15}"
LOOP=0
CAP="${TARTCI_MACOS_VM_CAP:-${PULP_VM_CAP:-2}}"
POLL="${TARTCI_VM_POLL:-${PULP_VM_POLL:-20}}"
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
  --print-queue) PRINT_QUEUE=1; shift;;
  --print-priority-demand) PRINT_PRIORITY=1; shift;;
  --yield-to-workflow) YIELD_WORKFLOW="$2"; shift 2;;
  --yield-to-labels) YIELD_LABELS="$2"; shift 2;;
  -h|--help) usage; exit 0;;
  *) die "unknown arg: $1";;
esac; done

RUNNER_NAME="$(derive_runner_name)"
[ "$PRINT_NAME" = 1 ] && { printf '%s\n' "$RUNNER_NAME"; exit 0; }

command -v tart >/dev/null 2>&1 || die "tart not installed"
command -v gh >/dev/null 2>&1 || die "gh not installed / authed (need repo admin to mint JIT config)"
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
{"ts":"$ts","provider":"tart-macos","host":"$(json_sanitize "$HOST_NAME")","runner":"$RUNNER_NAME","vm":"${CURRENT_VM:-}","vm_ip":"$(json_sanitize "${CURRENT_IP:-}")","phase":"$(json_sanitize "$phase")","labels":"$(json_sanitize "$LABELS")","repo":"$(json_sanitize "$REPO")","run_id":"$(json_sanitize "${CURRENT_RUN_ID:-}")","job_id":"$(json_sanitize "${CURRENT_JOB_ID:-}")","supervisor_pid":"$SUPERVISOR_PID","supervisor_pid_started_at":"$(json_sanitize "$SUPERVISOR_PID_STARTED_AT")"}
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
  tart list --format json 2>/dev/null | python3 -c '
import json, subprocess, sys
try:
    vms = json.load(sys.stdin)
except Exception:
    print(0)
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
' 2>/dev/null || echo 0
}

queued_work(){
  python3 - "$REPO" "$WORKFLOW_NAME" "$LABELS" "$QUEUE_RUN_LIMIT" "$GH_TIMEOUT" <<'PY'
import json
import subprocess
import sys

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
            ["gh", "api", path],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
    )


runs = []
seen = set()
try:
    for status in ("queued", "in_progress"):
        data = gh_json(f"repos/{repo}/actions/runs?status={status}&per_page={limit}")
        for run in data.get("workflow_runs", []):
            run_id = run.get("id")
            if run_id in seen:
                continue
            seen.add(run_id)
            runs.append(run)
except Exception:
    print(0)
    raise SystemExit

matches = 0
for run in runs:
    if run.get("name") != workflow_name:
        continue
    run_id = run.get("id")
    if not run_id:
        continue
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
  local label_json count=0 run_id matches
  label_json="$(YL="$YIELD_LABELS" python3 -c 'import json, os; print(json.dumps([x.strip() for x in os.environ["YL"].split(",") if x.strip()]))')"
  while IFS= read -r run_id; do
    [ -n "$run_id" ] || continue
    matches="$(
      gh api "repos/$REPO/actions/runs/$run_id/jobs?filter=latest&per_page=100" \
        --jq '.jobs[] | select(.status == "queued" or .status == "in_progress") | .labels | @json' 2>/dev/null |
      LABEL_JSON="$label_json" python3 -c '
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
'
    )" || matches=0
    count=$((count + ${matches:-0}))
  done < <(
    for st in queued in_progress; do
      gh api "repos/$REPO/actions/runs?status=$st&per_page=$QUEUE_RUN_LIMIT" \
        --jq ".workflow_runs[] | select(.name == \"${YIELD_WORKFLOW}\") | .id" 2>/dev/null || true
    done
  )
  printf '%s\n' "$count"
}

reclaim_runner_name(){
  local name="$1" id attempt
  for attempt in $(seq 1 18); do
    id="$(gh api "repos/$REPO/actions/runners" --paginate \
          --jq ".runners[] | select(.name==\"$name\") | .id" 2>/dev/null | head -n1 || true)"
    [ -n "$id" ] || break
    note "reclaiming static name '$name': deleting stale runner registration (id=$id attempt=$attempt)"
    gh api -X DELETE "repos/$REPO/actions/runners/$id" >/dev/null 2>&1 && break
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
    job_id="$(gh api "repos/$REPO/actions/runs/$run_id/jobs" \
      --jq ".jobs[] | select(.runner_name==\"$RUNNER_NAME\") | select(.status==\"in_progress\") | .id" \
      2>/dev/null | head -n1 || true)"
    if [ -n "$job_id" ]; then
      CURRENT_RUN_ID="$run_id"
      CURRENT_JOB_ID="$job_id"
      return 0
    fi
  done < <(gh api "repos/$REPO/actions/runs?per_page=100" \
    --jq ".workflow_runs[] | select(.name == \"$WORKFLOW_NAME\") | .id" 2>/dev/null || true)
  return 1
}

cancel_current_run(){
  [ -n "$CURRENT_RUN_ID" ] || capture_current_job || true
  if [ -n "$CURRENT_RUN_ID" ]; then
    event run_cancel "run_id=$CURRENT_RUN_ID job_id=${CURRENT_JOB_ID:-} reason=timeout"
    gh api -X POST "repos/$REPO/actions/runs/$CURRENT_RUN_ID/cancel" >/dev/null 2>&1 || true
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
  local i="$1" vm="$RUNNER_NAME" jit label_args=() l boot_log rpid ip="" rc=0
  local t_start t_booted t_runner_done t_done logdir=""
  t_start="$(now_epoch)"
  if [ "${TARTCI_RUNTIME_MEASURE:-0}" = 1 ]; then
    logdir="$MACOS_LOGROOT/$vm"
    mkdir -p "$logdir"
  fi
  CLEANED_UP=0
  CURRENT_RUN_ID=""
  CURRENT_JOB_ID=""
  reclaim_runner_name "$vm"
  heartbeat minting-jit
  event mint_jit "labels=$LABELS"
  IFS=',' read -r -a labels_split <<< "$LABELS"
  for l in "${labels_split[@]}"; do label_args+=(-f "labels[]=$l"); done
  jit="$(gh api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
        -f "name=$vm" -F "runner_group_id=$RUNNER_GROUP_ID" "${label_args[@]}" \
        --jq '.encoded_jit_config')" || die "JIT config mint failed (need repo admin)"
  [ -n "$jit" ] || die "empty JIT config"

  note "[$i] clone $GOLDEN → $vm (CoW) + boot with host ccache mounted"
  event clone_start "golden=$GOLDEN"
  tart clone "$GOLDEN" "$vm"
  CURRENT_VM="$vm"
  mkdir -p "$CACHE_ROOT/ccache"
  boot_log="$(mktemp -t "tart-run-$vm")"
  tart run --no-graphics --dir="ccache:$CACHE_ROOT/ccache" "$vm" >"$boot_log" 2>&1 & rpid=$!
  CURRENT_RPID="$rpid"
  heartbeat booting

  for _ in $(seq 1 60); do ip="$(tart ip "$vm" 2>/dev/null || true)"; [ -n "$ip" ] && break; sleep 2; done
  if [ -z "$ip" ]; then
    note "[$i] no IP after 120s — last tart run lines:"; tail -10 "$boot_log" >&2 2>/dev/null || true
    rm -f "$boot_log"; event boot_failed "no_ip"; runtime_emit_complete fail boot_failed 1 "" "$logdir"; return 1
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

if [ "$LOOP" = 1 ]; then
  note "ephemeral macOS runner LOOP; golden=$GOLDEN labels=$LABELS cap=$CAP yield_to=${YIELD_WORKFLOW:-<off>}"
  heartbeat loop
  while true; do
    q="$(queued_work)"; r="$(running_macos_vms)"
    # Only probe priority demand when THIS lane actually has work — no point
    # spending a gh round-trip (and the API quota the secondary-rate-limit cares
    # about) to decide whether to yield a slot we wouldn't use anyway. Stays 0
    # when there's no work, and the feature is off entirely for the gate runner.
    p=0
    [ "${q:-0}" -gt 0 ] && p="$(priority_demand)"
    # Idle gate: boot only when (1) this lane has work, (2) a VM slot is free,
    # and (3) no higher-priority lane is waiting/running. (3) is always satisfied
    # when the feature is off (priority_demand returns 0), so this is a no-op for
    # the primary gate runner. For a secondary lane it guarantees the priority
    # gate keeps its slot — the failure that backed out the coverage lane.
    if [ "${q:-0}" -gt 0 ] && [ "${r:-0}" -lt "$CAP" ] && [ "${p:-0}" -eq 0 ]; then
      i=$((i+1)); note "[$i] queued=$q running_macos_vms=$r/$CAP priority_demand=$p → booting ephemeral VM"
      run_one "$i" || true
    elif [ "${q:-0}" -gt 0 ] && [ "${p:-0}" -gt 0 ]; then
      note "yielding ${POLL}s (queued=$q priority_demand=$p running_macos_vms=$r/$CAP) — priority lane '${YIELD_WORKFLOW}' has the slot"
      event yielded_to_priority "workflow=$YIELD_WORKFLOW queued=$q priority_demand=$p running=$r/$CAP"
      heartbeat yielding
      sleep "$POLL"
    else
      note "waiting ${POLL}s (queued=$q running_macos_vms=$r/$CAP priority_demand=$p)"
      heartbeat waiting
      sleep "$POLL"
    fi
  done
else
  note "ephemeral macOS runner ONCE; golden=$GOLDEN labels=$LABELS"
  run_one 1
fi
