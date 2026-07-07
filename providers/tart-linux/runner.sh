#!/usr/bin/env bash
# tart-linux/runner.sh — ephemeral, per-job GitHub Actions runner on a Tart LINUX
# VM. The pool-serving sibling of run.sh: where run.sh does ONE on-demand
# build+ctest in-guest and exits, this mints a Just-In-Time (single-job) runner
# config, clones the golden, boots it with the host ccache mounted, runs the
# Actions agent ONCE against that JIT config, then discards the clone. The
# WORKFLOW (build.yml on GitHub) drives the build — the supervisor only supplies
# a clean VM per job. Native arm64 Ubuntu; Skia is baked in-checkout.
#
# Ported from Pulp's tools/ci/tart-runner-linux.sh (the proven supervisor) into
# the project-agnostic tartci provider shape: repo/golden/labels are env-driven.
# Defaults target danielraffel/pulp (the first consumer); override for any repo.
#
# The golden must carry the actions-runner agent at ~/actions-runner (the
# linux-arm64 install). This supervisor never registers a long-lived runner —
# JIT configs are single-job and ephemeral.
#
# CONCURRENCY: Linux guests are UNCAPPED (no 2-VM macOS kernel quota), so this can
# run several concurrent clones on one host; the --loop gate still only boots when
# there is queued work, to avoid spinning idle VMs.
#
# Pilot-safe by default: the label is `<repo>-build-linux` (NOT a required check),
# so jobs only land here when a workflow explicitly routes to it. Promote to the
# pooled label once a pilot is clean.
#
# Usage:
#   providers/tart-linux/runner.sh                 # one ephemeral job then exit (pilot default)
#   providers/tart-linux/runner.sh --loop          # keep serving jobs (LaunchAgent uses this)
#   providers/tart-linux/runner.sh --labels self-hosted,Linux,ARM64,pulp-build
set -euo pipefail
# Scan-blindness self-heal: `queued_work` prints the queued COUNT on a successful gh scan
# (`0` = genuinely idle) or `ERR` when the scan FAILS (rate-limit/timeout/degraded token),
# so a failed poll is never misread as an empty queue. After ~TARTCI_SCAN_BLIND_MAX polls
# (~3 min) of continuous blindness the loop self-restarts (exit 75 → launchd KeepAlive →
# fresh auth). See the README 'Scan-blindness self-heal' section.

TARTCI_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export TART_HOME="${TART_HOME:-/Volumes/Workshop/VMs}"
SSH_KEY_PRIV="${TARTCI_VM_SSH_KEY:-${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}}"
VM_USER="${TARTCI_VM_USER:-${PULP_VM_USER:-admin}}"
CACHE_ROOT="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/tartci}}"
LOGROOT="${TARTCI_LINUX_LOGS:-${PULP_LINUX_LOGS:-$HOME/VMs/logs/tartci-linux}}"
GOLDEN="${TARTCI_LINUX_GOLDEN:-${PULP_LINUX_GOLDEN:-pulp-linux-build:latest}}"
REPO="${TARTCI_RUNNER_REPO:-${PULP_RUNNER_REPO:-danielraffel/pulp}}"
LABELS="${TARTCI_RUNNER_LABELS:-${PULP_RUNNER_LABELS:-self-hosted,Linux,ARM64,pulp-build-linux}}"
RUNNER_GROUP_ID="${TARTCI_RUNNER_GROUP_ID:-${PULP_RUNNER_GROUP_ID:-1}}"
# Workflow name the --loop gate counts as "queued work". Override per repo.
WORKFLOW_NAME="${TARTCI_RUNNER_WORKFLOW_NAME:-Build and Test}"
# Host-health auto-yield (opt-in): TARTCI_HOST_VITALS_YIELD=1 makes the --loop gate
# stop booting NEW VMs while the host is saturated (reads the shared host_vitals.sh
# signal). Off by default, so a host that never installs host_vitals is byte-for-byte
# unchanged. FAIL-OPEN: a missing/broken/erroring probe prints 0 (boot), so it can
# never wedge this lane — worst case is the crash-avoidance we simply don't get.
# Yields on CRITICAL (>=20) always; TARTCI_HOST_VITALS_YIELD_ON_WARN=1 also drains on
# WARN (>=10). Mirrors the tart-macos provider so a busy shared host (e.g. a Mac
# Studio running the macOS gate + this Linux lane) backs off ALL local lanes, not
# just macOS — the durable guard against oversubscribing one box.
HOST_VITALS_YIELD="${TARTCI_HOST_VITALS_YIELD:-}"
HOST_VITALS_BIN="${TARTCI_HOST_VITALS_BIN:-host_vitals.sh}"
HOST_VITALS_YIELD_ON_WARN="${TARTCI_HOST_VITALS_YIELD_ON_WARN:-}"
# Ignore stale queued workflow shells by default. Without this guard, old queued
# runs with no matching self-hosted Linux job can keep waking Tart forever.
MAX_QUEUED_AGE_SECONDS="${TARTCI_RUNNER_MAX_QUEUED_AGE_SECONDS:-${PULP_RUNNER_MAX_QUEUED_AGE_SECONDS:-21600}}"
# By default, only boot when a fresh queued job's requested labels can be
# satisfied by this runner's labels. This keeps the supervisor safe while repo
# defaults still route Linux to GitHub-hosted ubuntu-latest.
QUEUE_MATCH_LABELS="${TARTCI_RUNNER_QUEUE_MATCH_LABELS:-${PULP_RUNNER_QUEUE_MATCH_LABELS:-1}}"
LOOP=0
POLL="${TARTCI_VM_POLL:-${PULP_VM_POLL:-20}}"; case "$POLL" in ''|*[!0-9]*|0) POLL=20;; esac  # positive int only (self-heal arithmetic)
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
now_epoch(){ date +%s; }
elapsed(){ awk -v start="$1" -v end="$2" 'BEGIN { printf "%.1f", end - start }'; }
prefix_guest_log(){ [ -f "$1" ] && LC_ALL=C sed 's/^/[guest] /' "$1" >&2 || true; }

# shellcheck source=providers/common/vm-lease.lib.sh
source "$TARTCI_ROOT/providers/common/vm-lease.lib.sh"
# shellcheck source=providers/common/vm-state.lib.sh
source "$TARTCI_ROOT/providers/common/vm-state.lib.sh"
runtime_emit_complete(){
  [ "${TARTCI_RUNTIME_MEASURE:-0}" = 1 ] || return 0
  local status="$1" failure_class="$2" exit_code="$3" runner_name="$4" vm_name="$5" timing_path="$6" log_dir="$7"
  python3 "$TARTCI_ROOT/scripts/runtime_measure.py" complete \
    --repo "$REPO" \
    --workflow "$WORKFLOW_NAME" \
    --provider tart-linux \
    --platform linux \
    --arch arm64 \
    --runner-name "$runner_name" \
    --vm-name "$vm_name" \
    --labels "$LABELS" \
    --golden "$GOLDEN" \
    --cache-mode unknown \
    --cache-mode-source unknown \
    --status "$status" \
    --failure-class "$failure_class" \
    --exit-code "$exit_code" \
    --timing-path "$timing_path" \
    --log-dir "$log_dir" \
    --gh-enrich \
    --json >/dev/null 2>&1 || note "runtime measurement emit failed (ignored)"
}
command -v tart >/dev/null 2>&1 || die "tart not installed"
# GitHub CLI for all API calls. Default `gh`; hosts authenticating as a GitHub
# App set TARTCI_GH_CLI=ghapp to move provider API traffic off the personal PAT
# (the per-poll calls are the dominant throttle). Exported so the inline python
# poller inherits it.
export TARTCI_GH_CLI="${TARTCI_GH_CLI:-gh}"
GH_CLI="$TARTCI_GH_CLI"
command -v "$GH_CLI" >/dev/null 2>&1 || die "GitHub CLI '$GH_CLI' (TARTCI_GH_CLI) not installed / authed (need admin to mint JIT config)"

PRINT_HOST_HEALTH=0
while [ $# -gt 0 ]; do case "$1" in
  --loop) LOOP=1; shift;;
  --once) LOOP=0; shift;;
  --golden) GOLDEN="$2"; shift 2;;
  --labels) LABELS="$2"; shift 2;;
  --repo) REPO="$2"; shift 2;;
  --print-host-health) PRINT_HOST_HEALTH=1; shift;;
  -h|--help) sed -n '2,30p' "$0"; exit 0;;
  *) die "unknown arg: $1";;
esac; done
case "$MAX_QUEUED_AGE_SECONDS" in ''|*[!0-9]*) MAX_QUEUED_AGE_SECONDS=21600;; esac

# Host-health auto-yield. Prints 1 when the loop should STOP booting new VMs
# because the host is saturated, 0 when it is safe to boot. Opt-in via
# TARTCI_HOST_VITALS_YIELD; off by default so hosts without host_vitals installed
# are unaffected. FAIL OPEN: if the host_vitals probe is missing, unexecutable, or
# errors, print 0 (boot) — host-health yield is a crash-avoidance nicety, not a
# correctness gate, and a broken probe must never wedge this lane. host_vitals.sh
# exit codes: 0 green, 10 warn, 20 critical. Yield on >=20 always, and on >=10 when
# TARTCI_HOST_VITALS_YIELD_ON_WARN is set. (Identical policy to the tart-macos
# provider so the whole host backs off together.)
host_health_yield(){
  [ -n "$HOST_VITALS_YIELD" ] && [ "$HOST_VITALS_YIELD" != 0 ] || { printf '%s\n' 0; return 0; }
  command -v "$HOST_VITALS_BIN" >/dev/null 2>&1 || { printf '%s\n' 0; return 0; }
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

# Count fresh queued jobs whose labels this runner can satisfy. 0 on any gh
# failure, treating a flaky API as "no work" so it does not spin VMs.
queued_work(){
  python3 - "$REPO" "$WORKFLOW_NAME" "$MAX_QUEUED_AGE_SECONDS" "$LABELS" "$QUEUE_MATCH_LABELS" <<'PY' 2>/dev/null || echo ERR   # ERR (not 0) on gh-scan failure: blind != empty
import datetime as dt
import json
import subprocess
import sys

repo, workflow_name, max_age_raw, labels_csv, match_labels_raw = sys.argv[1:6]
try:
    max_age = int(max_age_raw)
except ValueError:
    max_age = 21600
wanted = {label.strip().lower() for label in labels_csv.split(",") if label.strip()}
match_labels = match_labels_raw.strip().lower() not in {"0", "false", "no"}
now = dt.datetime.now(dt.timezone.utc)

def gh(path):
    import os
    cli = os.environ.get("TARTCI_GH_CLI") or "gh"
    try:
        timeout = max(1, int(os.environ.get("TARTCI_GH_TIMEOUT_SECS", "15")))
    except ValueError:
        timeout = 15
    # timeout is load-bearing for the scan-blindness self-heal: a HUNG gh (stalled TLS / half-open
    # socket) would otherwise block queued_work forever, so the loop never returns to increment
    # `blind` and never self-restarts. A timeout kills the child and raises → non-zero exit → `echo ERR`.
    return json.loads(subprocess.check_output([cli, "api", path], text=True, timeout=timeout))

def is_fresh(created_at):
    if max_age <= 0:
        return True
    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now - created).total_seconds() <= max_age

# Scan our workflow's runs, not the GLOBAL runs list: under multi-workflow load other workflows
# crowd our build runs out of the newest-`per_page` window, hiding queued build legs the fleet
# should serve (VM-lane starvation under load). Resolve the workflow id and hit workflows/{id}/runs.
runs = []
seen = set()
wf_id = None
for wf in gh(f"repos/{repo}/actions/workflows?per_page=100").get("workflows", []):
    if wf.get("name") == workflow_name:
        wf_id = wf.get("id")
        break
if wf_id is not None:
    run_paths = [f"repos/{repo}/actions/workflows/{wf_id}/runs?status={s}&per_page=100" for s in ("queued", "in_progress")]
else:
    run_paths = [f"repos/{repo}/actions/runs?status={s}&per_page=100" for s in ("queued", "in_progress")]
for _path in run_paths:
    for run in gh(_path).get("workflow_runs", []):
        run_id = run.get("id")
        if run_id in seen:
            continue
        seen.add(run_id)
        runs.append(run)

runs.sort(key=lambda r: r.get("created_at") or "")   # oldest-first: fairness + urgency under a deep queue
_MAX_JOB_FETCHES = 30
count = 0
_fetched = 0
for run in runs:
    if run.get("name") != workflow_name:
        continue
    if _fetched >= _MAX_JOB_FETCHES:
        break
    _fetched += 1
    jobs = gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100").get("jobs", [])
    for job in jobs:
        if job.get("status") != "queued":
            continue
        job_freshness_ts = (
            job.get("created_at")
            or job.get("started_at")
            or run.get("updated_at")
            or run.get("created_at")
            or ""
        )
        if not is_fresh(job_freshness_ts):
            continue
        labels = {str(label).lower() for label in job.get("labels", [])}
        if match_labels and (not labels or not labels.issubset(wanted)):
            continue
        if not match_labels or labels:
            count += 1
    if count > 0:
        break   # boot gate only needs ">= 1 servable job"; GitHub assigns the oldest match
print(count)
PY
}

run_one(){ # $1=iteration index (unique VM name without Date.now/rand)
  local i="$1" vm="linux-ephr-$$-$1" jit lease_cores lease_priority
  local t_start t_booted t_runner_done t_done logdir run_status=0
  local state_dir rpid="" ip=""
  t_start="$(now_epoch)"
  tartci_check_disk_floor "$TART_HOME" || return $?
  tartci_check_disk_floor "$LOGROOT" || return $?
  logdir="$LOGROOT/$vm"; mkdir -p "$logdir"
  state_dir="$(tartci_provider_state_dir tart-linux)"
  write_state(){
    TARTCI_STATE_LABELS="$LABELS" \
    TARTCI_STATE_REPO="$REPO" \
    TARTCI_STATE_SUPERVISOR_PID="$$" \
    TARTCI_STATE_SUPERVISOR_PID_STARTED_AT="$(tartci_pid_started_at "$$")" \
    TARTCI_STATE_VM_IP="$ip" \
    TARTCI_STATE_LOG_DIR="$logdir" \
    TARTCI_STATE_QEMU_PID="$rpid" \
    TARTCI_STATE_QEMU_PID_STARTED_AT="$(if [ -n "$rpid" ]; then tartci_pid_started_at "$rpid"; fi)" \
    tartci_write_vm_state tart-linux "$vm" "$vm" "$1" ephemeral "$state_dir"
  }
  delete_state(){ tartci_delete_vm_state "$vm" "$state_dir"; }
  lease_cores="$(tartci_vm_lease_cores tart-linux)"
  lease_mem="$(tartci_vm_lease_mem_mb tart-linux)"
  lease_priority="$(tartci_vm_lease_priority "$LABELS")"
  tartci_acquire_vm_lease "$vm" "$lease_cores" "tart-linux-vm" "$lease_priority" "$LABELS" "$lease_mem" || return $?
  write_state preparing
  note "[$i] minting JIT runner config (labels=$LABELS, ephemeral)"
  local label_args=(); local l; IFS=',' read -ra _ls <<< "$LABELS"
  for l in "${_ls[@]}"; do label_args+=(-f "labels[]=$l"); done
  jit="$("$GH_CLI" api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
        -f "name=$vm" -F "runner_group_id=$RUNNER_GROUP_ID" "${label_args[@]}" \
        --jq '.encoded_jit_config')" || { tartci_release_vm_lease; die "JIT config mint failed (need repo admin)"; }
  [ -n "$jit" ] || { tartci_release_vm_lease; die "empty JIT config"; }

  note "[$i] clone $GOLDEN → $vm (CoW) + boot with host ccache mounted"
  if ! tart clone "$GOLDEN" "$vm"; then
    tartci_release_vm_lease
    delete_state
    runtime_emit_complete fail boot_failed 1 "$vm" "$vm" "" "$logdir"
    return 1
  fi
  if ! tartci_set_tart_vm_cpu "$vm" "$lease_cores"; then
    note "[$i] failed to set $vm CPU count to lease cores=$lease_cores"
    tart delete "$vm" >/dev/null 2>&1 || true
    tartci_release_vm_lease
    delete_state
    runtime_emit_complete fail boot_failed 1 "$vm" "$vm" "" "$logdir"
    return 1
  fi
  mkdir -p "$CACHE_ROOT/ccache-linux"
  local boot_log; boot_log="$logdir/tart-run.log"
  tart run --no-graphics --dir="ccache:$CACHE_ROOT/ccache-linux" "$vm" >"$boot_log" 2>&1 & rpid=$!
  write_state booting

  for _ in $(seq 1 60); do ip="$(tart ip "$vm" 2>/dev/null || true)"; [ -n "$ip" ] && break; sleep 2; done
  if [ -z "$ip" ]; then
    note "[$i] no IP after 120s — last lines of \`tart run\` ($boot_log):"; tail -3 "$boot_log" >&2 2>/dev/null || true
    tart stop "$vm" >/dev/null 2>&1||true; kill "$rpid" 2>/dev/null||true; tart delete "$vm" >/dev/null 2>&1||true
    tartci_release_vm_lease
    delete_state
    runtime_emit_complete fail boot_failed 1 "$vm" "$vm" "" "$logdir"
    return 1
  fi
  write_state booted
  local sshok=0
  for _ in $(seq 1 90); do ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" true 2>/dev/null && { sshok=1; break; }; sleep 2; done
  if [ "$sshok" != 1 ]; then
    note "[$i] no SSH on $vm after 180s — discarding (won't run a job on an unreachable VM)"
    tart stop "$vm" >/dev/null 2>&1 || true; kill "$rpid" 2>/dev/null || true; tart delete "$vm" >/dev/null 2>&1 || true
    tartci_release_vm_lease
    delete_state
    runtime_emit_complete fail ssh_failed 1 "$vm" "$vm" "" "$logdir"
    return 1
  fi
  t_booted="$(now_epoch)"
  note "[$i] vm $vm up at $ip — mounting ccache + launching JIT runner (one job)"
  write_state running

  # Best-effort host ccache via virtio-fs (named "ccache" subdir is the rw one).
  # Then write the JIT config and run the agent once — a JIT runner processes
  # exactly one job and deregisters. CCACHE_* come from the baked
  # ~/actions-runner/.env so job steps inherit warm-cache settings.
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "sudo mkdir -p /mnt/host 2>/dev/null; sudo mount -t virtiofs com.apple.virtio-fs.automount /mnt/host 2>/dev/null || true; \
     if [ -d /mnt/host/ccache ] && [ -w /mnt/host/ccache ]; then mkdir -p ~/.ccache && ln -sfn /mnt/host/ccache ~/.ccache; fi; \
     printf '%s' '$jit' > ~/jit.cfg && cd ~/actions-runner && ./run.sh --jitconfig \"\$(cat ~/jit.cfg)\"" \
    >"$logdir/runner-output.log" 2>&1 \
    || { run_status=$?; note "[$i] runner exited non-zero (job failure or no job) — VM will be discarded regardless"; }
  t_runner_done="$(now_epoch)"
  prefix_guest_log "$logdir/runner-output.log"

  note "[$i] discarding ephemeral VM $vm"
  tart stop "$vm" >/dev/null 2>&1 || true; kill "$rpid" 2>/dev/null || true; sleep 2
  tart delete "$vm" >/dev/null 2>&1 || true
  tartci_release_vm_lease
  delete_state
  t_done="$(now_epoch)"
  {
    printf 'phase\tseconds\n'
    printf 'boot_to_ssh\t%s\n' "$(elapsed "$t_start" "$t_booted")"
    printf 'runner_process\t%s\n' "$(elapsed "$t_booted" "$t_runner_done")"
    printf 'cleanup\t%s\n' "$(elapsed "$t_runner_done" "$t_done")"
    printf 'total\t%s\n' "$(elapsed "$t_start" "$t_done")"
  } >"$logdir/timing.tsv"
  note "[$i] timing: boot=$(elapsed "$t_start" "$t_booted")s runner=$(elapsed "$t_booted" "$t_runner_done")s total=$(elapsed "$t_start" "$t_done")s diagnostics=$logdir"
  if [ "$run_status" -eq 0 ]; then
    runtime_emit_complete pass unknown 0 "$vm" "$vm" "$logdir/timing.tsv" "$logdir"
  else
    runtime_emit_complete fail source_failure "$run_status" "$vm" "$vm" "$logdir/timing.tsv" "$logdir"
  fi
  return "$run_status"
}

i=0
[ "$PRINT_HOST_HEALTH" = 1 ] && { host_health_yield; exit 0; }

if [ "$LOOP" = 1 ]; then
  note "ephemeral Linux runner LOOP (Ctrl-C to stop); golden=$GOLDEN labels=$LABELS maxQueuedAge=${MAX_QUEUED_AGE_SECONDS}s queueMatchLabels=$QUEUE_MATCH_LABELS host_vitals_yield=${HOST_VITALS_YIELD:-<off>}"
  # Scan-blindness self-heal: `queued_work` prints ERR when the gh queue scan fails; treating
  # that as 0 silently idles the supervisor while jobs pile up. Count consecutive blind polls
  # and self-restart after a sustained window so launchd (KeepAlive) respawns with fresh gh
  # auth (the loop is idle at the top — run_one blocks — so nothing in flight is lost).
  blind=0
  BLIND_MAX="${TARTCI_SCAN_BLIND_MAX:-$(( (180 + POLL - 1) / POLL ))}"
  while true; do
    q="$(queued_work)"
    if ! printf '%s' "$q" | grep -qxE '[0-9]+'; then
      blind=$((blind + 1))
      note "SCAN BLIND (gh queue scan failed) ${blind}/${BLIND_MAX} — NOT idling as empty"
      if [ "$blind" -ge "$BLIND_MAX" ]; then
        note "SCAN BLIND ~$((blind * POLL))s — self-restarting for fresh gh auth (launchd KeepAlive respawns)"
        exit 75
      fi
      sleep "$POLL"; continue
    fi
    blind=0
    # Host-health yield: only worth probing when we actually have work to boot.
    # Cheap local check (no gh call), fail-open, and 0 when the feature is off.
    hh=0
    [ "${q:-0}" -gt 0 ] && hh="$(host_health_yield)"
    if [ "${q:-0}" -gt 0 ] && [ "${hh:-0}" -eq 0 ]; then
      i=$((i+1)); note "[$i] queued=$q host_health_yield=$hh → booting ephemeral Linux VM"; run_one "$i" || sleep "$POLL"
    elif [ "${q:-0}" -gt 0 ]; then
      note "host saturated (host_health_yield=1) — deferring boot ${POLL}s (queued=$q)"; sleep "$POLL"
    else
      note "waiting ${POLL}s (queued=$q — no '$WORKFLOW_NAME' work)"; sleep "$POLL"
    fi
  done
else
  note "ephemeral Linux runner ONCE; golden=$GOLDEN labels=$LABELS"
  run_one 1
fi
