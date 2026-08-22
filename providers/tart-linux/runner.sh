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
# Defaults target Generous-Corp/pulp (the first consumer); override for any repo.
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
# shellcheck source=providers/common/pool.lib.sh
source "$TARTCI_ROOT/providers/common/pool.lib.sh"
export TART_HOME="${TART_HOME:-/Volumes/Workshop/VMs}"
SSH_KEY_PRIV="${TARTCI_VM_SSH_KEY:-${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}}"
VM_USER="${TARTCI_VM_USER:-${PULP_VM_USER:-admin}}"
CACHE_ROOT="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/tartci}}"
LOGROOT="${TARTCI_LINUX_LOGS:-${PULP_LINUX_LOGS:-$HOME/VMs/logs/tartci-linux}}"
GOLDEN="${TARTCI_LINUX_GOLDEN:-${PULP_LINUX_GOLDEN:-pulp-linux-build:latest}}"
REPO="${TARTCI_RUNNER_REPO:-${PULP_RUNNER_REPO:-Generous-Corp/pulp}}"
LABELS="${TARTCI_RUNNER_LABELS:-${PULP_RUNNER_LABELS:-self-hosted,Linux,ARM64,pulp-build-linux}}"
RUNNER_GROUP_ID="${TARTCI_RUNNER_GROUP_ID:-${PULP_RUNNER_GROUP_ID:-1}}"
# Workflow name the --loop gate counts as "queued work". Override per repo.
WORKFLOW_NAME="${TARTCI_RUNNER_WORKFLOW_NAME:-Build and Test}"
# Host-health auto-yield (opt-in): TARTCI_HOST_VITALS_YIELD=1 makes the --loop gate
# stop booting NEW VMs while the host is saturated. The decision lives in the shared
# providers/common/host-health.lib.sh (reading TARTCI_HOST_VITALS_YIELD[_ON_WARN] /
# TARTCI_HOST_VITALS_BIN), so a busy shared host — e.g. a Mac Studio running the
# macOS gate + this Linux lane — backs off ALL local lanes together, not just macOS.
# Off by default, fail-open, yields on CRITICAL (>=20) always and on WARN (>=10) only
# when TARTCI_HOST_VITALS_YIELD_ON_WARN is set. See that lib for the full contract.
# Label matching distinguishes stale workflow shells from compatible queued jobs,
# so compatible work must never age out merely because the queue is deep.
MAX_QUEUED_AGE_SECONDS="${TARTCI_RUNNER_MAX_QUEUED_AGE_SECONDS:-${PULP_RUNNER_MAX_QUEUED_AGE_SECONDS:-0}}"
# By default, only boot when a fresh queued job's requested labels can be
# satisfied by this runner's labels. This keeps the supervisor safe while repo
# defaults still route Linux to GitHub-hosted ubuntu-latest.
QUEUE_MATCH_LABELS="${TARTCI_RUNNER_QUEUE_MATCH_LABELS:-${PULP_RUNNER_QUEUE_MATCH_LABELS:-1}}"
LOOP=0
POLL="${TARTCI_VM_POLL:-${PULP_VM_POLL:-20}}"; case "$POLL" in ''|*[!0-9]*|0) POLL=20;; esac  # positive int only (self-heal arithmetic)
IDLE_TIMEOUT="${TARTCI_RUNNER_IDLE_TIMEOUT_SECS:-${PULP_RUNNER_IDLE_TIMEOUT_SECS:-900}}"
BUILD_PARALLEL_LEVEL="${TARTCI_LINUX_BUILD_PARALLEL_LEVEL:-4}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)
CURRENT_VM=""
CURRENT_VM_OWNED=0
CURRENT_RPID=""
CURRENT_RUNNER_PID=""
CURRENT_STATE_DIR=""
CURRENT_LEASE_ACTIVE=0
CURRENT_JIT_REGISTERED=0
CURRENT_CLEANED_UP=1

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
now_epoch(){ date +%s; }
elapsed(){ awk -v start="$1" -v end="$2" 'BEGIN { printf "%.1f", end - start }'; }
prefix_guest_log(){ [ -f "$1" ] && LC_ALL=C sed 's/^/[guest] /' "$1" >&2 || true; }

# shellcheck source=providers/common/vm-lease.lib.sh
source "$TARTCI_ROOT/providers/common/vm-lease.lib.sh"
# shellcheck source=providers/common/vm-state.lib.sh
source "$TARTCI_ROOT/providers/common/vm-state.lib.sh"
# shellcheck source=providers/common/host-health.lib.sh
source "$TARTCI_ROOT/providers/common/host-health.lib.sh"
# shellcheck source=providers/common/admission-clean.lib.sh
source "$TARTCI_ROOT/providers/common/admission-clean.lib.sh"
# shellcheck source=providers/common/runner-assignment.lib.sh
source "$TARTCI_ROOT/providers/common/runner-assignment.lib.sh"

discard_current_linux_vm(){
  tartci_pool_lock_release
  [ "$CURRENT_CLEANED_UP" = 0 ] || return 0
  CURRENT_CLEANED_UP=1
  if [ -n "$CURRENT_RUNNER_PID" ]; then
    kill -9 "$CURRENT_RUNNER_PID" 2>/dev/null || true
    wait "$CURRENT_RUNNER_PID" 2>/dev/null || true
  fi
  if [ -n "$CURRENT_RPID" ]; then
    kill -9 "$CURRENT_RPID" 2>/dev/null || true
    wait "$CURRENT_RPID" 2>/dev/null || true
  fi
  if [ "$CURRENT_VM_OWNED" = 1 ] && [ -n "$CURRENT_VM" ]; then
    tart stop "$CURRENT_VM" >/dev/null 2>&1 || true
    tart delete "$CURRENT_VM" >/dev/null 2>&1 || true
  fi
  if [ "$CURRENT_LEASE_ACTIVE" = 1 ]; then
    tartci_release_vm_lease
  fi
  if [ -n "$CURRENT_STATE_DIR" ] && [ -n "$CURRENT_VM" ]; then
    tartci_delete_vm_state "$CURRENT_VM" "$CURRENT_STATE_DIR"
  fi
  if [ "$CURRENT_JIT_REGISTERED" = 1 ] && [ -n "$CURRENT_VM" ]; then
    delete_linux_runner_registration "$CURRENT_VM" || true
  fi
  CURRENT_VM=""
  CURRENT_VM_OWNED=0
  CURRENT_RPID=""
  CURRENT_RUNNER_PID=""
  CURRENT_STATE_DIR=""
  CURRENT_LEASE_ACTIVE=0
  CURRENT_JIT_REGISTERED=0
  return 0
}

handle_linux_runner_signal(){
  discard_current_linux_vm
  trap - EXIT
  exit 143
}
trap handle_linux_runner_signal INT TERM
trap discard_current_linux_vm EXIT

linux_runner_authoritatively_busy(){
  local payload
  [ -n "$CURRENT_VM" ] || return 2
  payload="$("$GH_CLI" api --method GET "repos/$REPO/actions/runners" \
    -f per_page=10 -f "name=$CURRENT_VM" 2>/dev/null)" || return 2
  TARTCI_RUNNER_JSON="$payload" TARTCI_RUNNER_NAME="$CURRENT_VM" python3 - <<'PY'
import json
import os
import sys

try:
    runners = json.loads(os.environ["TARTCI_RUNNER_JSON"])["runners"]
except (KeyError, TypeError, ValueError):
    raise SystemExit(2)
name = os.environ["TARTCI_RUNNER_NAME"]
raise SystemExit(0 if any(r.get("name") == name and r.get("busy") is True for r in runners) else 1)
PY
}

delete_linux_runner_registration(){
  local name="$1" ids id attempt delete_failed
  for attempt in 1 2 3; do
    if ids="$("$GH_CLI" api --method GET "repos/$REPO/actions/runners" \
      -f per_page=10 -f "name=$name" \
      --jq ".runners[] | select(.name==\"$name\" and .busy==false) | .id" \
      2>/dev/null)"; then
      delete_failed=0
      for id in $ids; do
        "$GH_CLI" api -X DELETE "repos/$REPO/actions/runners/$id" \
          >/dev/null 2>&1 || delete_failed=1
      done
      if [ "$delete_failed" = 0 ]; then
        printf 'TARTCI_DIAG runner_registration_cleanup=confirmed name=%s attempt=%s\n' \
          "$name" "$attempt" >&2
        return 0
      fi
    fi
    printf 'TARTCI_DIAG runner_registration_cleanup=retry name=%s attempt=%s\n' \
      "$name" "$attempt" >&2
    [ "$attempt" = 3 ] || sleep 1
  done
  printf 'TARTCI_DIAG runner_registration_cleanup=exhausted name=%s attempts=3\n' \
    "$name" >&2
  return 1
}

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
case "$MAX_QUEUED_AGE_SECONDS" in ''|*[!0-9]*) MAX_QUEUED_AGE_SECONDS=0;; esac

# Count fresh queued jobs whose labels this runner can satisfy. 0 on any gh
# failure, treating a flaky API as "no work" so it does not spin VMs.
queued_work(){
  python3 "$TARTCI_ROOT/scripts/queue_scan.py" \
    --repo "$REPO" \
    --workflow "$WORKFLOW_NAME" \
    --labels "$LABELS" \
    --provider tart-linux \
    --lane-id "${TARTCI_QUEUE_LANE_ID:-tart-linux-${TARTCI_RUNNER_SLOT:-$$}}" \
    --state-file "${TARTCI_STATE_DIR:-$HOME/.tartci/state/linux}/queue-scan.json" \
    --shared-cache-file "${TARTCI_SHARED_QUEUE_CACHE:-$HOME/.tartci/state/queue-discovery.json}" \
    --max-age-seconds "$MAX_QUEUED_AGE_SECONDS" \
    --match-labels "$QUEUE_MATCH_LABELS" 2>/dev/null || echo ERR
}

run_one(){ # $1=iteration index (unique VM name without Date.now/rand)
  local i="$1" vm="linux-ephr-$$-$1" jit="" lease_cores lease_priority
  local build_parallel_effective
  local t_start t_booted t_runner_done t_done logdir run_status=0
  local state_dir rpid="" ip=""
  t_start="$(now_epoch)"
  tartci_check_disk_floor "$TART_HOME" || return $?
  tartci_prepare_disk_root "$LOGROOT" || return $?
  tartci_check_disk_floor "$LOGROOT" || return $?
  logdir="$LOGROOT/$vm"
  tartci_prepare_disk_root "$logdir" || return $?
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
  mark_runner_assigned(){ tartci_pool_lock_release; write_state job-running; }
  lease_cores="$(tartci_vm_lease_cores tart-linux)"
  lease_mem="$(tartci_vm_lease_mem_mb tart-linux)"
  lease_priority="$(tartci_vm_lease_priority "$LABELS")"
  CURRENT_VM="$vm"
  CURRENT_STATE_DIR="$state_dir"
  CURRENT_LEASE_ACTIVE=1
  CURRENT_CLEANED_UP=0
  tartci_acquire_vm_lease "$vm" "$lease_cores" "tart-linux-vm" "$lease_priority" "$LABELS" "$lease_mem" "$TART_HOME" || {
    local lease_rc=$?
    discard_current_linux_vm
    return "$lease_rc"
  }
  lease_cores="${TARTCI_ACTIVE_VM_LEASE_CORES:-$lease_cores}"
  build_parallel_effective="$BUILD_PARALLEL_LEVEL"
  if [ "$build_parallel_effective" -gt "$lease_cores" ]; then
    build_parallel_effective="$lease_cores"
  fi
  write_state preparing

  note "[$i] clone $GOLDEN → $vm (CoW) + boot with host ccache mounted"
  # The name is unique to this supervisor/iteration. Claim it before the
  # foreground clone so a TERM delivered at clone completion cannot strand it.
  CURRENT_VM_OWNED=1
  if ! tartci_vm_lease_guard_run tart clone "$GOLDEN" "$vm"; then
    discard_current_linux_vm
    runtime_emit_complete fail boot_failed 1 "$vm" "$vm" "" "$logdir"
    return 1
  fi
  if ! tartci_set_tart_vm_cpu "$vm" "$lease_cores"; then
    note "[$i] failed to set $vm CPU count to lease cores=$lease_cores"
    discard_current_linux_vm
    runtime_emit_complete fail boot_failed 1 "$vm" "$vm" "" "$logdir"
    return 1
  fi
  tartci_prepare_disk_root "$CACHE_ROOT/ccache-linux" || {
    discard_current_linux_vm
    runtime_emit_complete fail cache_setup_failed 1 "$vm" "$vm" "" "$logdir"
    return 1
  }
  local boot_log; boot_log="$logdir/tart-run.log"
  tartci_vm_lease_guard_exec tart run --no-graphics --dir="ccache:$CACHE_ROOT/ccache-linux" "$vm" >"$boot_log" 2>&1 & rpid=$!
  CURRENT_RPID="$rpid"
  write_state booting

  for _ in $(seq 1 60); do ip="$(tart ip "$vm" 2>/dev/null || true)"; [ -n "$ip" ] && break; sleep 2; done
  if [ -z "$ip" ]; then
    note "[$i] no IP after 120s — last lines of \`tart run\` ($boot_log):"; tail -3 "$boot_log" >&2 2>/dev/null || true
    discard_current_linux_vm
    runtime_emit_complete fail boot_failed 1 "$vm" "$vm" "" "$logdir"
    return 1
  fi
  write_state booted
  local sshok=0
  for _ in $(seq 1 90); do ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" true 2>/dev/null && { sshok=1; break; }; sleep 2; done
  if [ "$sshok" != 1 ]; then
    note "[$i] no SSH on $vm after 180s — discarding (won't run a job on an unreachable VM)"
    discard_current_linux_vm
    runtime_emit_complete fail ssh_failed 1 "$vm" "$vm" "" "$logdir"
    return 1
  fi
  t_booted="$(now_epoch)"
  write_state cache-setup
  if ! ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "sudo mkdir -p /mnt/host && \
     (sudo mount -t virtiofs com.apple.virtio-fs.automount /mnt/host 2>/dev/null || mountpoint -q /mnt/host) && \
     bash -s -- /mnt/host/ccache" \
    <"$TARTCI_ROOT/providers/tart-linux/prepare-ccache.sh" \
    >"$logdir/ccache-setup.log" 2>&1; then
    note "[$i] host ccache binding failed — refusing to launch a silently cold JIT runner"
    prefix_guest_log "$logdir/ccache-setup.log"
    discard_current_linux_vm
    runtime_emit_complete fail cache_setup_failed 1 "$vm" "$vm" "" "$logdir"
    return 1
  fi
  prefix_guest_log "$logdir/ccache-setup.log"

  if tartci_admission_clean_enabled; then
    local admission_json="" admission_rc=0
    write_state admission-check
    if admission_json="$(tartci_admission_clean "$REPO" "$LABELS")"; then
      admission_rc=0
    else
      admission_rc=$?
    fi
    [ -z "$admission_json" ] \
      || printf '%s\n' "$admission_json" >"$logdir/admission-clean.json"
    if [ "$admission_rc" -ne 0 ]; then
      write_state "$([ "$admission_rc" -eq 3 ] && printf admission-deferred || printf admission-error)"
      note "[$i] Shipyard admission $([ "$admission_rc" -eq 3 ] && printf deferred || printf failed) — discarding unregistered VM and backing off"
      discard_current_linux_vm
      return "$admission_rc"
    fi
  fi

  if ! tartci_pool_lock_acquire; then
    note "[$i] pool transition busy before JIT mint — discarding unassigned VM"
    discard_current_linux_vm
    return 75
  fi
  if ! tartci_pool_admission_open; then
    tartci_pool_lock_release
    note "[$i] pool $(tartci_pool_read_state) before JIT mint — discarding unassigned VM"
    discard_current_linux_vm
    return 75
  fi
  note "[$i] admission clean — minting JIT runner config (labels=$LABELS, ephemeral)"
  local label_args=(); local l; IFS=',' read -ra _ls <<< "$LABELS"
  for l in "${_ls[@]}"; do label_args+=(-f "labels[]=$l"); done
  # Claim uncertain-outcome cleanup before the state-creating POST. If GitHub
  # creates the registration but the response is lost, teardown still queries
  # and removes this exact non-busy name.
  CURRENT_JIT_REGISTERED=1
  jit="$("$GH_CLI" api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
        -f "name=$vm" -F "runner_group_id=$RUNNER_GROUP_ID" "${label_args[@]}" \
        --jq '.encoded_jit_config')" || {
    tartci_pool_lock_release
    note "[$i] JIT config mint failed — discarding VM"
    discard_current_linux_vm
    return 1
  }
  if [ -z "$jit" ]; then
    tartci_pool_lock_release
    note "[$i] empty JIT config — discarding VM"
    discard_current_linux_vm
    return 1
  fi
  note "[$i] vm $vm up at $ip — launching JIT runner (assignment_timeout=${IDLE_TIMEOUT}s, build_parallel=${build_parallel_effective}, one job)"
  write_state idle-wait

  # Write the JIT config and run the agent once. A JIT runner processes exactly
  # one job and deregisters. The host cache binding above is mandatory so a
  # mount regression cannot silently turn every ephemeral job cold.
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "printf '%s' '$jit' > ~/jit.cfg && cd ~/actions-runner && \
     export CCACHE_DIR=\"\$HOME/.ccache\" && \
     export CMAKE_BUILD_PARALLEL_LEVEL='$build_parallel_effective' && \
     printf 'TARTCI_DIAG ccache_dir=%s\n' \"\$CCACHE_DIR\" && \
     printf 'TARTCI_DIAG cmake_build_parallel_level=%s\n' \"\$CMAKE_BUILD_PARALLEL_LEVEL\" && \
     umask 0022 && runner_umask=\"\$(umask)\" && printf 'TARTCI_DIAG runner_umask=%s\n' \"\$runner_umask\" && \
     [ \"\$runner_umask\" = 0022 ] && ./run.sh --jitconfig \"\$(cat ~/jit.cfg)\"" \
    >"$logdir/runner-output.log" 2>&1 &
  CURRENT_RUNNER_PID=$!
  if tartci_monitor_runner_assignment \
    "$CURRENT_RUNNER_PID" "$logdir/runner-output.log" "$IDLE_TIMEOUT" \
    discard_current_linux_vm 5 mark_runner_assigned \
    linux_runner_authoritatively_busy; then
    run_status=0
  else
    run_status=$?
  fi
  tartci_pool_lock_release
  t_runner_done="$(now_epoch)"
  prefix_guest_log "$logdir/runner-output.log"

  if [ "$run_status" -eq 124 ] && [ "$TARTCI_RUNNER_WAS_ASSIGNED" = 0 ]; then
    note "[$i] runner assignment timeout — unassigned JIT runner and VM discarded"
  elif [ "$run_status" -ne 0 ]; then
    note "[$i] runner exited non-zero (job failure or no job) — VM discarded"
  fi
  t_done="$(now_epoch)"
  {
    printf 'phase\tseconds\n'
    printf 'boot_to_ssh\t%s\n' "$(elapsed "$t_start" "$t_booted")"
    printf 'runner_process\t%s\n' "$(elapsed "$t_booted" "$t_runner_done")"
    printf 'cleanup\t%s\n' "$(elapsed "$t_runner_done" "$t_done")"
    printf 'total\t%s\n' "$(elapsed "$t_start" "$t_done")"
  } >"$logdir/timing.tsv"
  note "[$i] timing: boot=$(elapsed "$t_start" "$t_booted")s runner=$(elapsed "$t_booted" "$t_runner_done")s total=$(elapsed "$t_start" "$t_done")s diagnostics=$logdir"
  if [ "$run_status" -eq 124 ] && [ "$TARTCI_RUNNER_WAS_ASSIGNED" = 0 ]; then
    runtime_emit_complete fail idle_timeout "$run_status" "$vm" "$vm" "$logdir/timing.tsv" "$logdir"
  elif [ "$run_status" -eq 0 ]; then
    runtime_emit_complete pass unknown 0 "$vm" "$vm" "$logdir/timing.tsv" "$logdir"
  else
    runtime_emit_complete fail source_failure "$run_status" "$vm" "$vm" "$logdir/timing.tsv" "$logdir"
  fi
  return "$run_status"
}

i=0
[ "$PRINT_HOST_HEALTH" = 1 ] && { tartci_host_health_yield; exit 0; }
tartci_validate_runner_idle_timeout "$IDLE_TIMEOUT" \
  || die "invalid Linux runner assignment timeout configuration"
tartci_validate_bounded_positive_integer \
  TARTCI_LINUX_BUILD_PARALLEL_LEVEL "$BUILD_PARALLEL_LEVEL" 64 \
  || die "invalid Linux build parallelism configuration"
tartci_validate_admission_clean_config "$REPO" "$LABELS" \
  || die "invalid required Shipyard admission-clean configuration"

if [ "$LOOP" = 1 ]; then
  note "ephemeral Linux runner LOOP (Ctrl-C to stop); golden=$GOLDEN labels=$LABELS assignmentTimeout=${IDLE_TIMEOUT}s buildParallel=${BUILD_PARALLEL_LEVEL} maxQueuedAge=${MAX_QUEUED_AGE_SECONDS}s queueMatchLabels=$QUEUE_MATCH_LABELS host_vitals_yield=${TARTCI_HOST_VITALS_YIELD:-<off>}"
  # Scan-blindness self-heal: `queued_work` prints ERR when the gh queue scan fails; treating
  # that as 0 silently idles the supervisor while jobs pile up. Count consecutive blind polls
  # and self-restart after a sustained window so launchd (KeepAlive) respawns with fresh gh
  # auth (the loop is idle at the top — run_one blocks — so nothing in flight is lost).
  blind=0
  BLIND_MAX="${TARTCI_SCAN_BLIND_MAX:-$(( (180 + POLL - 1) / POLL ))}"
  while true; do
    if ! tartci_pool_admission_open; then
      note "pool $(tartci_pool_read_state) — no new Linux admission; waiting ${POLL}s"
      sleep "$POLL"
      continue
    fi
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
    [ "${q:-0}" -gt 0 ] && hh="$(tartci_host_health_yield)"
    if [ "${q:-0}" -gt 0 ] && [ "${hh:-0}" -eq 0 ]; then
      i=$((i+1)); note "[$i] queued=$q host_health_yield=$hh → booting ephemeral Linux VM"; run_one "$i" || sleep "$POLL"
    elif [ "${q:-0}" -gt 0 ]; then
      note "host saturated (host_health_yield=1) — deferring boot ${POLL}s (queued=$q)"; sleep "$POLL"
    else
      note "waiting ${POLL}s (queued=$q — no '$WORKFLOW_NAME' work)"; sleep "$POLL"
    fi
  done
else
  tartci_pool_admission_open || die "pool $(tartci_pool_read_state): refusing one-shot admission"
  note "ephemeral Linux runner ONCE; golden=$GOLDEN labels=$LABELS"
  run_one 1
fi
