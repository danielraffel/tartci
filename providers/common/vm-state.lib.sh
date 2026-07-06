# Shared VM provider state and admission helpers.
# shellcheck shell=bash

: "${TARTCI_STATE_ROOT:=$HOME/.tartci/state}"
: "${TARTCI_VM_DISK_FREE_FLOOR_GB:=25}"

tartci_vm_state_note(){
  if command -v note >/dev/null 2>&1; then
    note "$*"
  else
    printf '%s\n' "$*" >&2
  fi
}

tartci_provider_state_dir(){
  local provider="$1"
  case "$provider" in
    tart-macos) printf '%s' "${TARTCI_MACOS_STATE_DIR:-${TARTCI_STATE_DIR:-$TARTCI_STATE_ROOT/macos}}" ;;
    tart-linux) printf '%s' "${TARTCI_LINUX_STATE_DIR:-$TARTCI_STATE_ROOT/linux}" ;;
    qemu-windows) printf '%s' "${TARTCI_WIN_STATE_DIR:-$TARTCI_STATE_ROOT/windows}" ;;
    *) printf '%s' "$TARTCI_STATE_ROOT/$provider" ;;
  esac
}

tartci_pid_started_at(){
  local pid="$1"
  ps -p "$pid" -o lstart= 2>/dev/null | tr -s ' ' | sed 's/^ //;s/ $//'
}

tartci_check_disk_floor(){
  local path="$1" floor_gb="${TARTCI_VM_DISK_FREE_FLOOR_GB:-25}" probe avail_kb floor_kb
  case "$floor_gb" in
    0|false|FALSE|off|OFF|no|NO|"") return 0 ;;
    *[!0-9]*) tartci_vm_state_note "invalid TARTCI_VM_DISK_FREE_FLOOR_GB=$floor_gb"; return 75 ;;
  esac
  mkdir -p "$path" 2>/dev/null || true
  probe="$path"
  while [ ! -e "$probe" ] && [ "$probe" != "/" ]; do
    probe="$(dirname "$probe")"
  done
  avail_kb="$(df -Pk "$probe" 2>/dev/null | awk 'NR==2 {print $4}')"
  case "$avail_kb" in ''|*[!0-9]*) tartci_vm_state_note "cannot read free disk for $probe"; return 75 ;; esac
  floor_kb=$((floor_gb * 1024 * 1024))
  if [ "$avail_kb" -lt "$floor_kb" ]; then
    tartci_vm_state_note "refusing VM admission: free disk $(awk -v kb="$avail_kb" 'BEGIN { printf "%.1f", kb / 1024 / 1024 }')GiB below floor ${floor_gb}GiB at $probe"
    return 75
  fi
  return 0
}

tartci_write_vm_state(){
  local provider="$1" runner="$2" vm="$3" phase="$4" lifecycle="${5:-ephemeral}" state_dir="${6:-}"
  [ -n "$state_dir" ] || state_dir="$(tartci_provider_state_dir "$provider")"
  mkdir -p "$state_dir"
  TARTCI_STATE_PROVIDER="$provider" \
  TARTCI_STATE_RUNNER="$runner" \
  TARTCI_STATE_VM="$vm" \
  TARTCI_STATE_PHASE="$phase" \
  TARTCI_STATE_LIFECYCLE="$lifecycle" \
  TARTCI_STATE_FILE="$state_dir/$runner.state.json" \
  TARTCI_STATE_LABELS="${TARTCI_STATE_LABELS:-}" \
  TARTCI_STATE_REPO="${TARTCI_STATE_REPO:-}" \
  TARTCI_STATE_SUPERVISOR_PID="${TARTCI_STATE_SUPERVISOR_PID:-}" \
  TARTCI_STATE_SUPERVISOR_PID_STARTED_AT="${TARTCI_STATE_SUPERVISOR_PID_STARTED_AT:-}" \
  TARTCI_STATE_VM_IP="${TARTCI_STATE_VM_IP:-}" \
  TARTCI_STATE_RUN_ID="${TARTCI_STATE_RUN_ID:-}" \
  TARTCI_STATE_JOB_ID="${TARTCI_STATE_JOB_ID:-}" \
  TARTCI_STATE_WORK_DIR="${TARTCI_STATE_WORK_DIR:-}" \
  TARTCI_STATE_LOG_DIR="${TARTCI_STATE_LOG_DIR:-}" \
  TARTCI_STATE_OVERLAY="${TARTCI_STATE_OVERLAY:-}" \
  TARTCI_STATE_PORT_LOCK="${TARTCI_STATE_PORT_LOCK:-}" \
  TARTCI_STATE_SSH_PORT="${TARTCI_STATE_SSH_PORT:-}" \
  TARTCI_STATE_QEMU_PID="${TARTCI_STATE_QEMU_PID:-}" \
  TARTCI_STATE_QEMU_PID_STARTED_AT="${TARTCI_STATE_QEMU_PID_STARTED_AT:-}" \
  TARTCI_STATE_KEEP_FAILED="${TARTCI_STATE_KEEP_FAILED:-}" \
  TARTCI_ACTIVE_VM_LEASE_ID="${TARTCI_ACTIVE_VM_LEASE_ID:-}" \
  python3 - <<'PY'
import datetime as dt
import json
import os
import pathlib
import socket
import tempfile

def env(name, default=""):
    return os.environ.get(name, default)

def env_bool(name):
    return env(name).lower() in {"1", "true", "yes", "on"}

data = {
    "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "provider": env("TARTCI_STATE_PROVIDER"),
    "host": socket.gethostname().split(".")[0],
    "runner": env("TARTCI_STATE_RUNNER"),
    "vm": env("TARTCI_STATE_VM"),
    "phase": env("TARTCI_STATE_PHASE"),
    "lifecycle": env("TARTCI_STATE_LIFECYCLE", "ephemeral"),
    "labels": env("TARTCI_STATE_LABELS"),
    "repo": env("TARTCI_STATE_REPO"),
    "supervisor_pid": env("TARTCI_STATE_SUPERVISOR_PID"),
    "supervisor_pid_started_at": env("TARTCI_STATE_SUPERVISOR_PID_STARTED_AT"),
}

optional = {
    "vm_ip": env("TARTCI_STATE_VM_IP"),
    "run_id": env("TARTCI_STATE_RUN_ID"),
    "job_id": env("TARTCI_STATE_JOB_ID"),
    "work_dir": env("TARTCI_STATE_WORK_DIR"),
    "log_dir": env("TARTCI_STATE_LOG_DIR"),
    "overlay": env("TARTCI_STATE_OVERLAY"),
    "port_lock": env("TARTCI_STATE_PORT_LOCK"),
    "ssh_port": env("TARTCI_STATE_SSH_PORT"),
    "qemu_pid": env("TARTCI_STATE_QEMU_PID"),
    "qemu_pid_started_at": env("TARTCI_STATE_QEMU_PID_STARTED_AT"),
    "lease_id": env("TARTCI_ACTIVE_VM_LEASE_ID"),
}
for key, value in optional.items():
    if value:
        data[key] = value
if env("TARTCI_STATE_KEEP_FAILED"):
    data["keep_failed"] = env_bool("TARTCI_STATE_KEEP_FAILED")

path = pathlib.Path(env("TARTCI_STATE_FILE"))
fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True)
        fh.write("\n")
    os.replace(tmp_name, path)
finally:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass
PY
}

tartci_delete_vm_state(){
  local runner="$1" state_dir="${2:-}"
  [ -n "$state_dir" ] || state_dir="$TARTCI_STATE_ROOT"
  rm -f "$state_dir/$runner.state.json"
}
