#!/usr/bin/env bash
# qemu-windows/runner.sh — ephemeral, per-job GitHub Actions runner on a QEMU
# WINDOWS VM. The pool-serving sibling of run.sh: where run.sh does ONE on-demand
# build+ctest in a CoW overlay and exits, this mints a JIT (single-job) runner
# config, makes a CoW overlay off the Windows golden qcow2 on a dynamic free SSH
# port, boots it, runs the Actions agent ONCE, then discards the overlay. The
# WORKFLOW (build.yml on GitHub) drives the build — the supervisor only supplies a
# clean VM per job. Reuses the validated boot mechanics from run.sh.
#
# Ported from Pulp's tools/ci/qemu-runner-windows.sh (the proven supervisor) into
# the project-agnostic tartci provider shape: repo/golden/labels are env-driven.
# Defaults target Generous-Corp/pulp (the first consumer); override for any repo.
#
# The runner agent (actions-runner-win-arm64) is installed into C:\actions-runner
# install-if-missing, so this works whether or not the golden has it pre-baked;
# baking it into the golden later just skips the per-job download.
#
# Pilot-safe by default: label `<repo>-build-windows` (NOT a required check).
#
# Usage:
#   providers/qemu-windows/runner.sh                 # one ephemeral job then exit (pilot)
#   providers/qemu-windows/runner.sh --loop          # keep serving (LaunchAgent uses this)
#   providers/qemu-windows/runner.sh --labels self-hosted,Windows,ARM64,pulp-build
set -euo pipefail
# Scan-blindness self-heal: `queued_work` prints the queued COUNT on a successful gh scan
# (`0` = genuinely idle) or `ERR` when the scan FAILS (rate-limit/timeout/degraded token),
# so a failed poll is never misread as an empty queue. After ~TARTCI_SCAN_BLIND_MAX polls
# (~3 min) of continuous blindness the loop self-restarts (exit 75 → launchd KeepAlive →
# fresh auth). See the README 'Scan-blindness self-heal' section.

TARTCI_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=providers/common/pool.lib.sh
source "$TARTCI_ROOT/providers/common/pool.lib.sh"
GOLDEN="${TARTCI_WIN_GOLDEN:-${TARTCI_GOLDENS:-$HOME/.tartci/goldens}/pulp-windows-build-24h2-arm64-2026-06-12-cacheopt.qcow2}"
KEY="${TARTCI_WIN_SSH_KEY:-$HOME/.ssh/id_ed25519}"
WUSER="${TARTCI_WIN_SSH_USER:-admin}"
REPO="${TARTCI_RUNNER_REPO:-${PULP_RUNNER_REPO:-Generous-Corp/pulp}}"
LABELS="${TARTCI_RUNNER_LABELS:-${PULP_RUNNER_LABELS:-self-hosted,Windows,ARM64,pulp-build-windows}}"
RUNNER_GROUP_ID="${TARTCI_RUNNER_GROUP_ID:-${PULP_RUNNER_GROUP_ID:-1}}"
RUNNER_VERSION="${TARTCI_RUNNER_VERSION:-${PULP_RUNNER_VERSION:-2.335.1}}"
VCVARS_ARCH="${TARTCI_WIN_VCVARS_ARCH:-${PULP_WIN_VCVARS_ARCH:-arm64}}"
PREFLIGHT_MODE="${TARTCI_WIN_PREFLIGHT_MODE:-${PULP_WIN_PREFLIGHT_MODE:-fast}}"
WIN_CPUS="${TARTCI_WIN_CPUS:-${PULP_WIN_CPUS:-8}}"
WIN_MEMORY_MB="${TARTCI_WIN_MEMORY_MB:-${PULP_WIN_MEMORY_MB:-8192}}"
WORKROOT="${TARTCI_WIN_WORK:-${TMPDIR:-/tmp}/tartci-win}"
LOGROOT="${TARTCI_WIN_LOGS:-${PULP_WIN_LOGS:-$WORKROOT/logs}}"
# Workflow name the --loop gate counts as "queued work". Override per repo.
WORKFLOW_NAME="${TARTCI_RUNNER_WORKFLOW_NAME:-Build and Test}"
# Host-health auto-yield (opt-in): TARTCI_HOST_VITALS_YIELD=1 makes the --loop gate
# stop booting NEW VMs while the host is saturated. The decision lives in the shared
# providers/common/host-health.lib.sh (reading TARTCI_HOST_VITALS_YIELD[_ON_WARN] /
# TARTCI_HOST_VITALS_BIN), so a busy shared host backs off ALL local lanes together,
# not just macOS. Off by default, fail-open, yields on CRITICAL (>=20) always and on
# WARN (>=10) only when TARTCI_HOST_VITALS_YIELD_ON_WARN is set. See that lib.
# Label matching distinguishes stale workflow shells from compatible queued jobs,
# so compatible work must never age out merely because the queue is deep.
MAX_QUEUED_AGE_SECONDS="${TARTCI_RUNNER_MAX_QUEUED_AGE_SECONDS:-${PULP_RUNNER_MAX_QUEUED_AGE_SECONDS:-0}}"
KEEP_FAILED="${TARTCI_KEEP_FAILED:-${PULP_KEEP_FAILED:-0}}"
# By default, only boot when a fresh queued job's requested labels can be
# satisfied by this runner's labels. This keeps the supervisor safe while repo
# defaults still route Windows to GitHub-hosted `windows-latest`.
QUEUE_MATCH_LABELS="${TARTCI_RUNNER_QUEUE_MATCH_LABELS:-${PULP_RUNNER_QUEUE_MATCH_LABELS:-1}}"
LOOP=0; POLL="${TARTCI_VM_POLL:-${PULP_VM_POLL:-20}}"; case "$POLL" in ''|*[!0-9]*|0) POLL=20;; esac  # positive int only (self-heal arithmetic)
IDLE_TIMEOUT="${TARTCI_RUNNER_IDLE_TIMEOUT_SECS:-${PULP_RUNNER_IDLE_TIMEOUT_SECS:-900}}"
HOST_SLUG="$(hostname -s 2>/dev/null || hostname)"
HOST_SLUG="$(printf '%s' "$HOST_SLUG" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-*//;s/-*$//')"
RUNNER_NAME_PREFIX="${TARTCI_RUNNER_NAME_PREFIX:-${PULP_RUNNER_NAME_PREFIX:-win-ephr-${HOST_SLUG:-host}}}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o IdentitiesOnly=yes -o BatchMode=yes)
CURRENT_WIN_JOB=""
CURRENT_WIN_JOBDIR=""
CURRENT_WIN_PORT_LOCK=""
CURRENT_WIN_QPID=""
CURRENT_WIN_STATE_DIR=""
CURRENT_WIN_LEASE_ID_EXPECTED=""
CURRENT_WIN_CPUS=""
CURRENT_WIN_CLEANED_UP=1

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

runtime_emit_complete(){
  [ "${TARTCI_RUNTIME_MEASURE:-0}" = 1 ] || return 0
  local status="$1" failure_class="$2" exit_code="$3" runner_name="$4" timing_path="$5" log_dir="$6"
  python3 "$TARTCI_ROOT/scripts/runtime_measure.py" complete \
    --repo "$REPO" \
    --workflow "$WORKFLOW_NAME" \
    --provider qemu-windows \
    --platform windows \
    --arch arm64 \
    --runner-name "$runner_name" \
    --vm-name "$runner_name" \
    --labels "$LABELS" \
    --golden "$GOLDEN" \
    --cache-mode unknown \
    --cache-mode-source unknown \
    --cpu-count "${CURRENT_WIN_CPUS:-$WIN_CPUS}" \
    --ram-mb "$WIN_MEMORY_MB" \
    --status "$status" \
    --failure-class "$failure_class" \
    --exit-code "$exit_code" \
    --timing-path "$timing_path" \
    --log-dir "$log_dir" \
    --gh-enrich \
    --json >/dev/null 2>&1 || note "runtime measurement emit failed (ignored)"
}
command -v qemu-system-aarch64 >/dev/null 2>&1 || die "qemu not installed"
# GitHub CLI for all API calls. Default `gh`; hosts authenticating as a GitHub
# App set TARTCI_GH_CLI=ghapp to move provider API traffic off the personal PAT
# (the per-poll calls are the dominant throttle). Exported so the inline python
# poller inherits it.
export TARTCI_GH_CLI="${TARTCI_GH_CLI:-gh}"
GH_CLI="$TARTCI_GH_CLI"
command -v "$GH_CLI" >/dev/null 2>&1 || die "GitHub CLI '$GH_CLI' (TARTCI_GH_CLI) not installed / authed (need admin to mint JIT)"

allocate_ssh_port(){
  python3 - "$WORKROOT/port-locks" <<'PY'
import os
import random
import shutil
import socket
import sys
import time

root = sys.argv[1]
os.makedirs(root, exist_ok=True)
now = time.time()
for name in os.listdir(root):
    path = os.path.join(root, name)
    try:
        if os.path.isdir(path) and now - os.stat(path).st_mtime > 24 * 60 * 60:
            shutil.rmtree(path)
    except OSError:
        pass

for _ in range(200):
    port = random.randint(20000, 60999)
    lock = os.path.join(root, f"{port}.lock")
    try:
        os.mkdir(lock)
    except FileExistsError:
        continue
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        shutil.rmtree(lock, ignore_errors=True)
        continue
    finally:
        sock.close()
    print(port, lock)
    raise SystemExit(0)

raise SystemExit("no available SSH port after 200 attempts")
PY
}

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

# Preflight probe — safe to run without a golden (mirrors tart-macos ordering:
# print-exits precede any golden/VM requirement).
[ "$PRINT_HOST_HEALTH" = 1 ] && { tartci_host_health_yield; exit 0; }
tartci_validate_admission_clean_config "$REPO" "$LABELS" \
  || die "invalid required Shipyard admission-clean configuration"

[ -f "$GOLDEN" ] || die "golden not found: $GOLDEN (set TARTCI_WIN_GOLDEN)"
FW=""; for c in /opt/homebrew/share/qemu/edk2-aarch64-code.fd /Applications/UTM.app/Contents/Resources/qemu/edk2-aarch64-code.fd; do [ -f "$c" ] && FW="$c" && break; done
[ -n "$FW" ] || die "no edk2-aarch64-code.fd"
VARS_TPL=""; for v in /opt/homebrew/share/qemu/edk2-aarch64-vars.fd /opt/homebrew/share/qemu/edk2-arm-vars.fd; do [ -f "$v" ] && VARS_TPL="$v" && break; done
[ -n "$VARS_TPL" ] || die "no edk2 vars template"
case "$MAX_QUEUED_AGE_SECONDS" in ''|*[!0-9]*) MAX_QUEUED_AGE_SECONDS=0;; esac
case "$PREFLIGHT_MODE" in fast|full) ;; *) die "invalid TARTCI_WIN_PREFLIGHT_MODE='$PREFLIGHT_MODE' (fast|full)";; esac
case "$WIN_CPUS" in ''|*[!0-9]*) die "invalid TARTCI_WIN_CPUS='$WIN_CPUS'";; esac
case "$WIN_MEMORY_MB" in ''|*[!0-9]*) die "invalid TARTCI_WIN_MEMORY_MB='$WIN_MEMORY_MB'";; esac

delete_runner_registration(){
  local name="$1" ids id tries=0
  while [ "$tries" -lt 6 ]; do
    tries=$((tries + 1))
    ids="$("$GH_CLI" api "repos/$REPO/actions/runners" --paginate \
      --jq ".runners[] | select(.name==\"$name\" and .busy==false) | .id" 2>/dev/null || true)"
    if [ -n "$ids" ]; then
      for id in $ids; do
        note "deleting stale runner registration name=$name id=$id"
        "$GH_CLI" api -X DELETE "repos/$REPO/actions/runners/$id" >/dev/null 2>&1 || true
      done
    fi
    ids="$("$GH_CLI" api "repos/$REPO/actions/runners" --paginate \
      --jq ".runners[] | select(.name==\"$name\") | .id" 2>/dev/null || true)"
    [ -z "$ids" ] && return 0
    sleep 2
  done
}

cleanup_active_windows_job(){
  [ "$CURRENT_WIN_CLEANED_UP" = 0 ] || return 0
  CURRENT_WIN_CLEANED_UP=1
  if [ -n "$CURRENT_WIN_QPID" ]; then
    kill -9 "$CURRENT_WIN_QPID" 2>/dev/null || true
    wait "$CURRENT_WIN_QPID" 2>/dev/null || true
  fi
  [ -z "$CURRENT_WIN_JOBDIR" ] || rm -rf "$CURRENT_WIN_JOBDIR"
  [ -z "$CURRENT_WIN_PORT_LOCK" ] || rm -rf "$CURRENT_WIN_PORT_LOCK"
  if [ -n "$CURRENT_WIN_LEASE_ID_EXPECTED" ] \
    && [ "${TARTCI_ACTIVE_VM_LEASE_ID:-}" = "$CURRENT_WIN_LEASE_ID_EXPECTED" ]; then
    tartci_release_vm_lease
  fi
  if [ -n "$CURRENT_WIN_STATE_DIR" ] && [ -n "$CURRENT_WIN_JOB" ]; then
    tartci_delete_vm_state "$CURRENT_WIN_JOB" "$CURRENT_WIN_STATE_DIR"
  fi
  [ -z "$CURRENT_WIN_JOB" ] \
    || delete_runner_registration "$CURRENT_WIN_JOB" || true
  CURRENT_WIN_JOB=""
  CURRENT_WIN_JOBDIR=""
  CURRENT_WIN_PORT_LOCK=""
  CURRENT_WIN_QPID=""
  CURRENT_WIN_STATE_DIR=""
  CURRENT_WIN_LEASE_ID_EXPECTED=""
  CURRENT_WIN_CPUS=""
  return 0
}

handle_windows_runner_signal(){
  cleanup_active_windows_job
  trap - EXIT
  exit 143
}
trap handle_windows_runner_signal INT TERM
trap cleanup_active_windows_job EXIT

queued_work(){
  python3 "$TARTCI_ROOT/scripts/queue_scan.py" \
    --repo "$REPO" \
    --workflow "$WORKFLOW_NAME" \
    --labels "$LABELS" \
    --provider qemu-windows \
    --lane-id "${TARTCI_QUEUE_LANE_ID:-qemu-windows-${TARTCI_RUNNER_SLOT:-$$}}" \
    --state-file "${TARTCI_STATE_DIR:-$WORKROOT/state}/queue-scan.json" \
    --shared-cache-file "${TARTCI_SHARED_QUEUE_CACHE:-$HOME/.tartci/state/queue-discovery.json}" \
    --max-age-seconds "$MAX_QUEUED_AGE_SECONDS" \
    --match-labels "$QUEUE_MATCH_LABELS" 2>/dev/null || echo ERR
}

run_one(){ # $1=iteration index
  local i="$1" jit="" job="${RUNNER_NAME_PREFIX}-$$-$1" lease_cores lease_priority
  local effective_win_cpus
  local t_start t_booted t_preflight t_runner_done t_done
  local state_dir qemu_started=""
  t_start="$(now_epoch)"
  tartci_check_disk_floor "$WORKROOT" || return $?
  tartci_check_disk_floor "$LOGROOT" || return $?

  local port port_lock jobdir logdir overlay efivars qpid
  read -r port port_lock < <(allocate_ssh_port)
  [ -n "$port" ] && [ -n "$port_lock" ] || die "failed to allocate SSH port"
  jobdir="$WORKROOT/$job"
  CURRENT_WIN_JOB="$job"
  CURRENT_WIN_JOBDIR="$jobdir"
  CURRENT_WIN_PORT_LOCK="$port_lock"
  CURRENT_WIN_QPID=""
  CURRENT_WIN_STATE_DIR=""
  CURRENT_WIN_LEASE_ID_EXPECTED=""
  CURRENT_WIN_CPUS=""
  CURRENT_WIN_CLEANED_UP=0
  mkdir -p "$jobdir"
  logdir="$LOGROOT/$job"; mkdir -p "$logdir"
  overlay="$jobdir/overlay.qcow2"; efivars="$jobdir/efivars.fd"
  state_dir="$(tartci_provider_state_dir qemu-windows)"
  CURRENT_WIN_STATE_DIR="$state_dir"
  write_state(){
    TARTCI_STATE_LABELS="$LABELS" \
    TARTCI_STATE_REPO="$REPO" \
    TARTCI_STATE_SUPERVISOR_PID="$$" \
    TARTCI_STATE_SUPERVISOR_PID_STARTED_AT="$(tartci_pid_started_at "$$")" \
    TARTCI_STATE_WORK_DIR="$jobdir" \
    TARTCI_STATE_LOG_DIR="$logdir" \
    TARTCI_STATE_OVERLAY="$overlay" \
    TARTCI_STATE_PORT_LOCK="$port_lock" \
    TARTCI_STATE_SSH_PORT="$port" \
    TARTCI_STATE_QEMU_PID="${qpid:-}" \
    TARTCI_STATE_QEMU_PID_STARTED_AT="$qemu_started" \
    TARTCI_STATE_KEEP_FAILED="${2:-}" \
    tartci_write_vm_state qemu-windows "$job" "$job" "$1" ephemeral "$state_dir"
  }
  lease_cores="$(tartci_vm_lease_cores qemu-windows "$WIN_CPUS")"
  lease_mem="$(tartci_vm_lease_mem_mb qemu-windows "$WIN_MEMORY_MB")"
  lease_priority="$(tartci_vm_lease_priority "$LABELS")"
  # Claim release responsibility before the foreground acquisition so a signal
  # delivered immediately after success cannot strand the new lease.
  CURRENT_WIN_LEASE_ID_EXPECTED="vm-qemu-windows-vm-$job"
  tartci_acquire_vm_lease "$job" "$lease_cores" "qemu-windows-vm" "$lease_priority" "$LABELS" "$lease_mem" "$WORKROOT" || {
    local lease_rc=$?
    cleanup_active_windows_job
    return "$lease_rc"
  }
  effective_win_cpus="${TARTCI_ACTIVE_VM_LEASE_CORES:-$lease_cores}"
  CURRENT_WIN_CPUS="$effective_win_cpus"
  write_state preparing

  note "[$i] CoW overlay off $(basename "$GOLDEN") + boot (ssh 127.0.0.1:$port)"
  if ! tartci_vm_lease_guard_run qemu-img create -f qcow2 -b "$GOLDEN" -F qcow2 "$overlay" >/dev/null; then
    runtime_emit_complete fail boot_failed 1 "$job" "" "$logdir"
    cleanup_active_windows_job
    return 1
  fi
  if ! cp "$VARS_TPL" "$efivars"; then
    runtime_emit_complete fail boot_failed 1 "$job" "" "$logdir"
    cleanup_active_windows_job
    return 1
  fi
  tartci_vm_lease_guard_exec qemu-system-aarch64 -name "$job" -accel hvf -machine virt,highmem=on -cpu host -smp "$effective_win_cpus" -m "$WIN_MEMORY_MB" \
    -drive if=pflash,format=raw,readonly=on,file="$FW" -drive if=pflash,format=raw,file="$efivars" \
    -device ramfb -device qemu-xhci,id=usb -device usb-kbd -device usb-tablet \
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$port-:22" -device virtio-net-pci,netdev=net0 \
    -drive file="$overlay",if=none,id=nvm,format=qcow2 -device nvme,drive=nvm,serial=pulpwin \
    -display none >"$logdir/qemu.log" 2>&1 & CURRENT_WIN_QPID=$!
  qpid="$CURRENT_WIN_QPID"
  qemu_started="$(tartci_pid_started_at "$qpid")"
  write_state booting

  cleanup_job(){
    tartci_pool_lock_release
    local outcome="${1:-success}"
    delete_runner_registration "$job" || true
    if [ "$outcome" = "failure" ] && [ "$KEEP_FAILED" = 1 ]; then
      note "[$i] keeping failed VM for inspection: job=$job qemu_pid=$qpid ssh_port=$port dir=$jobdir"
      write_state kept-failed 1
      CURRENT_WIN_JOB=""
      CURRENT_WIN_JOBDIR=""
      CURRENT_WIN_PORT_LOCK=""
      CURRENT_WIN_QPID=""
      CURRENT_WIN_STATE_DIR=""
      CURRENT_WIN_LEASE_ID_EXPECTED=""
      CURRENT_WIN_CPUS=""
      CURRENT_WIN_CLEANED_UP=1
      return 0
    fi
    note "[$i] host diagnostics: $logdir"
    cleanup_active_windows_job
  }

  wsh(){ ssh "${SSH_OPTS[@]}" -i "$KEY" -p "$port" "$WUSER@127.0.0.1" "$@"; }
  # Wait for SSH, but bail the moment QEMU dies — that's how a free-port TOCTOU
  # (another process grabbed $port between the probe close and QEMU's bind)
  # surfaces: qemu exits instantly. Without this check the wait would burn the
  # full ~10min before failing. Caller (--loop) retries with a fresh port.
  local up=0 qemu_died=0; local _
  for _ in $(seq 1 150); do
    kill -0 "$qpid" 2>/dev/null || { qemu_died=1; note "[$i] qemu exited early (well before the SSH window) — port $port likely grabbed (TOCTOU); see $logdir/qemu.log"; break; }
    wsh 'echo ok' >/dev/null 2>&1 && { up=1; break; }; sleep 4
  done
  if [ "$up" != 1 ]; then
    # qemu-death already logged the accurate cause above; only emit the generic
    # "waited the full window" message when QEMU stayed up but no SSH.
    [ "$qemu_died" = 1 ] || note "[$i] no SSH after ~10min (qemu alive but unreachable; see $logdir/qemu.log)"
    runtime_emit_complete fail "$([ "$qemu_died" = 1 ] && printf boot_failed || printf ssh_failed)" 1 "$job" "" "$logdir"
    cleanup_job failure; return 1
  fi
  t_booted="$(now_epoch)"
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
      cleanup_job success
      return "$admission_rc"
    fi
  fi

  note "[$i] admission clean — minting JIT runner config (labels=$LABELS, ephemeral)"
  local label_args=(); local l; IFS=',' read -ra _ls <<< "$LABELS"
  for l in "${_ls[@]}"; do label_args+=(-f "labels[]=$l"); done
  if ! tartci_pool_lock_acquire; then
    note "[$i] pool transition busy before JIT mint — discarding unassigned VM"
    cleanup_job success
    return 75
  fi
  if ! tartci_pool_admission_open; then
    tartci_pool_lock_release
    note "[$i] pool $(tartci_pool_read_state) before JIT mint — discarding unassigned VM"
    cleanup_job success
    return 75
  fi
  jit="$("$GH_CLI" api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
        -f "name=$job" -F "runner_group_id=$RUNNER_GROUP_ID" "${label_args[@]}" \
        --jq '.encoded_jit_config')" || {
    tartci_pool_lock_release
    note "[$i] JIT mint failed — discarding VM"
    cleanup_job success
    return 1
  }
  if [ -z "$jit" ]; then
    tartci_pool_lock_release
    note "[$i] empty JIT config — discarding VM"
    cleanup_job success
    return 1
  fi
  note "[$i] vm $job up — ensure runner version + run JIT agent (one job)"
  write_state running
  run_guest_ps_file(){
    local remote_path="$1" script="$2" upload_enc
    upload_enc="$(printf '%s' '$ErrorActionPreference="Stop"
$p="'"$remote_path"'"
$script=[Console]::In.ReadToEnd()
Set-Content -LiteralPath $p -Value $script -Encoding UTF8
& powershell -NoProfile -ExecutionPolicy Bypass -File $p
exit $LASTEXITCODE' | iconv -t UTF-16LE | base64)"
    printf '%s' "$script" | wsh "powershell -NoProfile -EncodedCommand $upload_enc"
  }
  local host_utc enc_clock
  host_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  enc_clock="$(printf '%s' '$hostUtc="'"$host_utc"'"
try {
  Set-Date -Date ([DateTimeOffset]::Parse($hostUtc).LocalDateTime) | Out-Null
  Write-Output "TARTCI_DIAG early-clock-sync=$hostUtc"
} catch {
  Write-Output "TARTCI_DIAG early-clock-sync-failed=$($_.Exception.Message)"
}' | iconv -t UTF-16LE | base64)"
  mkdir -p "$logdir"
  wsh "powershell -NoProfile -EncodedCommand $enc_clock" >"$logdir/early-clock.log" 2>&1 \
    || note "[$i] early clock sync failed"

  # The runner agent + JIT run, in three small ssh calls. The JIT blob is
  # multi-KB; it must NEVER ride a command line — embedding it in a PowerShell
  # -EncodedCommand or passing it as a cmd arg blows cmd.exe's 8191-char limit
  # through the ssh→cmd→powershell chain ("The command line is too long").
  # So: (1) ensure the agent binary version [no blob], (2) STREAM the blob into a
  # file via ssh STDIN [unbounded], (3) run the agent reading that file [no blob].
  local enc_install ps_preflight ps_run enc_after
  enc_install="$(printf '%s' '$ProgressPreference="SilentlyContinue"
$dir="C:\actions-runner"
$runnerVersion="'"$RUNNER_VERSION"'"
$listener="$dir\bin\Runner.Listener.exe"
$currentVersion=""
if (Test-Path $listener) {
  try { $currentVersion = ((& $listener --version 2>$null | Select-Object -First 1).Trim()) } catch { $currentVersion = "" }
}
if ($currentVersion -ne $runnerVersion) {
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $dir
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $url="https://github.com/actions/runner/releases/download/v$runnerVersion/actions-runner-win-arm64-$runnerVersion.zip"
  Invoke-WebRequest -Uri $url -OutFile "$dir\r.zip"
  Expand-Archive -Path "$dir\r.zip" -DestinationPath $dir -Force
  Remove-Item "$dir\r.zip"
}
# Goldens may carry stale runner registration files from an older proof. JIT
# configs are single-use; leave only the runner binaries before each fresh boot.
Remove-Item -Force -ErrorAction SilentlyContinue "$dir\.runner","$dir\.credentials","$dir\.credentials_rsaparams","$dir\.env","$dir\.path","$dir\jit.cfg"
# Integrity gate: the agent binary must exist after install. The download is
# over authenticated HTTPS and Expand-Archive rejects a corrupt/truncated zip,
# but this catches a partial extract loudly rather than failing opaquely at run.
if (-not (Test-Path "$dir\bin\Runner.Listener.exe")) { Write-Error "Runner.Listener.exe missing after install (corrupt/truncated download?)"; exit 1 }' | iconv -t UTF-16LE | base64)"
  wsh "powershell -NoProfile -EncodedCommand $enc_install" \
    || { note "[$i] runner install failed"; runtime_emit_complete fail jit_failed 1 "$job" "" "$logdir"; cleanup_job failure; return 1; }

  # (2) stream the JIT config in via stdin → file (no command-line length limit).
  # Guard the pipeline: under `set -euo pipefail` a dropped SSH / PowerShell error
  # here would otherwise exit the whole supervisor BEFORE the cleanup below,
  # leaking the QEMU process + overlay for a launchd --loop runner to trip over.
  printf '%s' "$jit" | wsh "powershell -NoProfile -Command \"[Console]::In.ReadToEnd() | Out-File -FilePath C:\\actions-runner\\jit.cfg -Encoding ascii -NoNewline\"" \
    || { note "[$i] JIT config upload failed — discarding overlay"; runtime_emit_complete fail jit_failed 1 "$job" "" "$logdir"; cleanup_job failure; return 1; }

  # The JIT token is time-sensitive. QEMU Windows overlays can wake with stale
  # clocks, so sync the throwaway guest to the host UTC and emit lightweight
  # reachability diagnostics before the agent tries to create its session.
  host_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  ps_preflight='$ErrorActionPreference="Continue"
$ProgressPreference="SilentlyContinue"
$hostUtc="'"$host_utc"'"
$preflightMode="'"$PREFLIGHT_MODE"'"
Write-Output "TARTCI_DIAG preflight-mode=$preflightMode"
try {
  Set-Date -Date ([DateTimeOffset]::Parse($hostUtc).LocalDateTime) | Out-Null
  Write-Output "TARTCI_DIAG clock-sync=$hostUtc"
} catch {
  Write-Output "TARTCI_DIAG clock-sync-failed=$($_.Exception.Message)"
}
Write-Output ("TARTCI_DIAG guest-time=" + (Get-Date -Format o))
if ($preflightMode -eq "full") {
try {
  Set-ExecutionPolicy -Scope LocalMachine -ExecutionPolicy RemoteSigned -Force
  Write-Output "TARTCI_DIAG execution-policy-localmachine=RemoteSigned"
} catch {
  Write-Output "TARTCI_DIAG execution-policy-failed=$($_.Exception.Message)"
}
try {
  Get-ExecutionPolicy -List | ForEach-Object { Write-Output ("TARTCI_DIAG execution-policy {0}={1}" -f $_.Scope, $_.ExecutionPolicy) }
} catch {
  Write-Output "TARTCI_DIAG execution-policy-list-failed=$($_.Exception.Message)"
}
$runnerPathAdd = @(
  "C:\Program Files\Git\cmd",
  "C:\Program Files\Git\bin",
  "C:\Program Files\Git\usr\bin",
  "C:\ProgramData\chocolatey\bin"
)
$env:Path = (($runnerPathAdd + @($env:Path -split ";")) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique) -join ";"
foreach ($cmd in @("git", "bash", "choco", "ccache")) {
  $found = Get-Command $cmd -ErrorAction SilentlyContinue
  if ($found) {
    Write-Output ("TARTCI_DIAG command {0}={1}" -f $cmd, $found.Source)
  } else {
    Write-Output ("TARTCI_DIAG command {0}=missing" -f $cmd)
  }
}
$vcvarsArch="'"$VCVARS_ARCH"'"
$vcvars = Get-ChildItem "C:\Program Files\Microsoft Visual Studio" -Recurse -Filter vcvarsall.bat -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match "BuildTools" } | Select-Object -First 1 -ExpandProperty FullName
if ($vcvars) {
  Write-Output ("TARTCI_DIAG vcvars={0} arch={1}" -f $vcvars, $vcvarsArch)
  $tmp = Join-Path $env:TEMP ("tartci-vcvars-" + [guid]::NewGuid().ToString("N") + ".cmd")
  try {
    "@echo off",("call ""{0}"" {1} >nul" -f $vcvars, $vcvarsArch),"set" | Set-Content -Path $tmp -Encoding ASCII
    $lines = & cmd.exe /d /c $tmp
    foreach ($line in $lines) {
      if ($line -match "^(.*?)=(.*)$") {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
      }
    }
  } finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $tmp
  }
  $cl = Get-Command cl -ErrorAction SilentlyContinue
  if ($cl) { Write-Output ("TARTCI_DIAG command cl={0}" -f $cl.Source) } else { Write-Output "TARTCI_DIAG command cl=missing-after-vcvars" }
} else {
  Write-Output "TARTCI_DIAG vcvars=missing"
}
try {
  w32tm /query /status | ForEach-Object { Write-Output ("TARTCI_DIAG w32tm " + $_) }
} catch {
  Write-Output "TARTCI_DIAG w32tm-failed=$($_.Exception.Message)"
}
foreach ($target in @("github.com", "broker.actions.githubusercontent.com")) {
  try {
    $tcp = Test-NetConnection $target -Port 443 -WarningAction SilentlyContinue
    Write-Output ("TARTCI_DIAG tcp-443 {0}={1}" -f $target, $tcp.TcpTestSucceeded)
  } catch {
    Write-Output ("TARTCI_DIAG tcp-443 {0}=error:{1}" -f $target, $_.Exception.Message)
  }
}
try {
  $resp = Invoke-WebRequest -Uri "https://github.com" -Method Head -UseBasicParsing -TimeoutSec 20
  Write-Output ("TARTCI_DIAG github-head-status={0}" -f $resp.StatusCode)
} catch {
  Write-Output "TARTCI_DIAG github-head-failed=$($_.Exception.Message)"
}
}
$listener="C:\actions-runner\bin\Runner.Listener.exe"
if (Test-Path $listener) {
  try { Write-Output ("TARTCI_DIAG listener-version=" + ((& $listener --version 2>$null | Select-Object -First 1).Trim())) } catch { Write-Output "TARTCI_DIAG listener-version-failed=$($_.Exception.Message)" }
}
$jitPath="C:\actions-runner\jit.cfg"
  if (Test-Path $jitPath) {
  Write-Output ("TARTCI_DIAG jit-cfg-bytes=" + (Get-Item $jitPath).Length)
}'
  mkdir -p "$logdir"
  run_guest_ps_file "C:\actions-runner\tartci-preflight.ps1" "$ps_preflight" >"$logdir/preflight.log" 2>&1 \
    || note "[$i] preflight diagnostics failed"
  t_preflight="$(now_epoch)"
  prefix_guest_log "$logdir/preflight.log"

  # (3) run the agent reading the jit FILE — small PS, no blob on the wire.
  # Use Runner.Listener.exe directly (not run.cmd) so the huge JIT config is not
  # expanded through cmd.exe's 8191-character command-line limit.
  ps_run='$runnerPathAdd = @(
  "C:\Program Files\Git\cmd",
  "C:\Program Files\Git\bin",
  "C:\Program Files\Git\usr\bin",
  "C:\ProgramData\chocolatey\bin"
)
$env:Path = (($runnerPathAdd + @($env:Path -split ";")) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique) -join ";"
$vcvarsArch="'"$VCVARS_ARCH"'"
$vcvars = Get-ChildItem "C:\Program Files\Microsoft Visual Studio" -Recurse -Filter vcvarsall.bat -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match "BuildTools" } | Select-Object -First 1 -ExpandProperty FullName
if ($vcvars) {
  Write-Output ("TARTCI_DIAG runner_vcvars={0} arch={1}" -f $vcvars, $vcvarsArch)
  $tmp = Join-Path $env:TEMP ("tartci-vcvars-" + [guid]::NewGuid().ToString("N") + ".cmd")
  try {
    "@echo off",("call ""{0}"" {1} >nul" -f $vcvars, $vcvarsArch),"set" | Set-Content -Path $tmp -Encoding ASCII
    $lines = & cmd.exe /d /c $tmp
    foreach ($line in $lines) {
      if ($line -match "^(.*?)=(.*)$") {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
      }
    }
  } finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $tmp
  }
  $cl = Get-Command cl -ErrorAction SilentlyContinue
  if ($cl) { Write-Output ("TARTCI_DIAG runner_command cl={0}" -f $cl.Source) } else { Write-Output "TARTCI_DIAG runner_command cl=missing-after-vcvars" }
} else {
  Write-Output "TARTCI_DIAG runner_vcvars=missing"
}
Set-Location C:\actions-runner
& "C:\actions-runner\bin\Runner.Listener.exe" run --jitconfig (Get-Content "C:\actions-runner\jit.cfg")
exit $LASTEXITCODE'
  local run_status=0
  local runner_output="$logdir/runner-output.log"
  local runner_pid runner_start runner_assigned=0 runner_timed_out=0 now idle_elapsed
  mkdir -p "$logdir"
  run_guest_ps_file "C:\actions-runner\tartci-runner.ps1" "$ps_run" >"$runner_output" 2>&1 &
  runner_pid=$!
  runner_start="$(now_epoch)"
  while kill -0 "$runner_pid" 2>/dev/null; do
    if [ "$runner_assigned" = 0 ] && grep -q 'Running job:' "$runner_output" 2>/dev/null; then
      runner_assigned=1
      tartci_pool_lock_release
    fi
    if [ "$runner_assigned" = 0 ]; then
      now="$(now_epoch)"
      idle_elapsed=$((now - runner_start))
      if [ "$idle_elapsed" -ge "$IDLE_TIMEOUT" ]; then
        runner_timed_out=1
        note "[$i] runner idle timeout after ${idle_elapsed}s without claiming a job"
        kill "$runner_pid" 2>/dev/null || true
        break
      fi
    fi
    sleep 5
  done
  tartci_pool_lock_release
  wait "$runner_pid" || run_status=$?
  if [ "$runner_timed_out" = 1 ]; then
    run_status=124
  elif [ "$run_status" -ne 0 ]; then
    note "[$i] runner exited non-zero (job failure or no job)"
  fi
  t_runner_done="$(now_epoch)"
  prefix_guest_log "$runner_output"
  if grep -qi 'runner registration has been deleted\|Failed to create a session' "$runner_output"; then
    run_status=1
    note "[$i] runner session failed before claiming a job"
  fi

  enc_after="$(printf '%s' '$ErrorActionPreference="Continue"
$diagDir="C:\actions-runner\_diag"
if (Test-Path $diagDir) {
  Get-ChildItem $diagDir -Filter "*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 3 | ForEach-Object {
    Write-Output ("TARTCI_DIAG runner-log=" + $_.FullName)
    Get-Content $_.FullName -Tail 140
  }
} else {
  Write-Output "TARTCI_DIAG no-runner-diag-dir"
}' | iconv -t UTF-16LE | base64)"
  mkdir -p "$logdir"
  wsh "powershell -NoProfile -EncodedCommand $enc_after" >"$logdir/runner-diag.log" 2>&1 || true
  t_done="$(now_epoch)"
  prefix_guest_log "$logdir/runner-diag.log"
  {
    printf 'phase\tseconds\n'
    printf 'boot_to_ssh\t%s\n' "$(elapsed "$t_start" "$t_booted")"
    printf 'preflight\t%s\n' "$(elapsed "$t_booted" "$t_preflight")"
    printf 'runner_process\t%s\n' "$(elapsed "$t_preflight" "$t_runner_done")"
    printf 'post_diag\t%s\n' "$(elapsed "$t_runner_done" "$t_done")"
    printf 'total\t%s\n' "$(elapsed "$t_start" "$t_done")"
  } >"$logdir/timing.tsv"
  note "[$i] timing: boot=$(elapsed "$t_start" "$t_booted")s preflight=$(elapsed "$t_booted" "$t_preflight")s runner=$(elapsed "$t_preflight" "$t_runner_done")s total=$(elapsed "$t_start" "$t_done")s"

  if [ "$run_status" -ne 0 ]; then
    if [ "$run_status" -eq 124 ]; then
      runtime_emit_complete fail idle_timeout "$run_status" "$job" "$logdir/timing.tsv" "$logdir"
    else
      runtime_emit_complete fail source_failure "$run_status" "$job" "$logdir/timing.tsv" "$logdir"
    fi
    cleanup_job failure
    return "$run_status"
  else
    runtime_emit_complete pass unknown 0 "$job" "$logdir/timing.tsv" "$logdir"
    note "[$i] discarding ephemeral overlay $job"
    cleanup_job success
  fi
}

i=0
if [ "$LOOP" = 1 ]; then
  note "ephemeral Windows runner LOOP (Ctrl-C to stop); golden=$(basename "$GOLDEN") labels=$LABELS preflight=$PREFLIGHT_MODE cpus=$WIN_CPUS mem=${WIN_MEMORY_MB}MB maxQueuedAge=${MAX_QUEUED_AGE_SECONDS}s queueMatchLabels=$QUEUE_MATCH_LABELS host_vitals_yield=${TARTCI_HOST_VITALS_YIELD:-<off>}"
  # Scan-blindness self-heal: `queued_work` prints ERR when the gh queue scan fails; treating
  # that as 0 silently idles the supervisor while jobs pile up. Count consecutive blind polls
  # and self-restart after a sustained window so launchd (KeepAlive) respawns with fresh gh
  # auth (the loop is idle at the top — run_one blocks — so nothing in flight is lost).
  blind=0
  BLIND_MAX="${TARTCI_SCAN_BLIND_MAX:-$(( (180 + POLL - 1) / POLL ))}"
  while true; do
    if ! tartci_pool_admission_open; then
      note "pool $(tartci_pool_read_state) — no new Windows admission; waiting ${POLL}s"
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
      i=$((i+1)); note "[$i] queued=$q host_health_yield=$hh → booting ephemeral Windows VM"; run_one "$i" || sleep "$POLL"
    elif [ "${q:-0}" -gt 0 ]; then
      note "host saturated (host_health_yield=1) — deferring boot ${POLL}s (queued=$q)"; sleep "$POLL"
    else
      note "waiting ${POLL}s (queued=$q — no '$WORKFLOW_NAME' work)"; sleep "$POLL"
    fi
  done
else
  tartci_pool_admission_open || die "pool $(tartci_pool_read_state): refusing one-shot admission"
  note "ephemeral Windows runner ONCE; golden=$(basename "$GOLDEN") labels=$LABELS preflight=$PREFLIGHT_MODE cpus=$WIN_CPUS mem=${WIN_MEMORY_MB}MB"
  run_one 1
fi
