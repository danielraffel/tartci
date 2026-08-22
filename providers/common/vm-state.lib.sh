# Shared VM provider state and admission helpers.
# shellcheck shell=bash

: "${TARTCI_STATE_ROOT:=$HOME/.tartci/state}"
: "${TARTCI_VM_DISK_FREE_FLOOR_GB:=25}"

# shellcheck source=providers/common/disk-capacity.lib.sh
source "${BASH_SOURCE[0]%/*}/disk-capacity.lib.sh"

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

# Create a missing per-provider cache/log/work leaf without ever walking through
# a missing external mount. The nearest existing ancestor is opened first and
# all new components are created relative to that directory descriptor, keeping
# a mount disappearance from redirecting creation onto its parent filesystem.
# /Volumes/<name>/... paths additionally require <name> to be a real mount.
tartci_prepare_disk_root(){
  local path="$1" expected_mount="${2:-}" expected_device="${3:-}"
  python3 - "$path" "$expected_mount" "$expected_device" <<'PY'
import os
import pathlib
import sys

target = pathlib.Path(sys.argv[1]).expanduser()
expected_mount_text = sys.argv[2]
expected_device = sys.argv[3]
if not target.is_absolute():
    target = pathlib.Path.cwd() / target
target = pathlib.Path(os.path.abspath(os.fspath(target)))

missing = []
ancestor = target
while not ancestor.exists():
    missing.append(ancestor.name)
    if ancestor == ancestor.parent:
        raise SystemExit(f"cannot resolve existing parent for configured disk root: {target}")
    ancestor = ancestor.parent
if not ancestor.is_dir():
    raise SystemExit(f"configured disk-root parent is not a directory: {ancestor}")

parts = target.parts
mount = None
if expected_mount_text:
    mount = pathlib.Path(expected_mount_text).expanduser()
    if not mount.is_absolute():
        mount = pathlib.Path.cwd() / mount
    mount = pathlib.Path(os.path.abspath(os.fspath(mount)))
elif len(parts) >= 3 and parts[1] == "Volumes":
    mount = pathlib.Path("/Volumes") / parts[2]
if mount is not None:
    if not mount.is_dir():
        raise SystemExit(f"refusing to create disk root through missing external mount: {mount}")
    try:
        mount = mount.resolve(strict=True)
        mount_device = mount.stat().st_dev
        mount_root = mount
        while mount_root != mount_root.parent:
            if mount_root.parent.stat().st_dev != mount_device:
                break
            mount_root = mount_root.parent
        if mount_root != mount:
            raise SystemExit(f"refusing to create disk root through unmounted volume path: {mount}")
        if expected_device and str(mount_device) != expected_device:
            raise SystemExit(
                f"disk device mismatch for configured root {target}: "
                f"expected {expected_device}, got {mount_device}"
            )
    except OSError as exc:
        raise SystemExit(f"cannot validate external mount {mount}: {exc}") from exc

ancestor = ancestor.resolve(strict=True)
physical_target = ancestor.joinpath(*reversed(missing))
if mount is not None:
    try:
        physical_target.relative_to(mount)
    except ValueError as exc:
        raise SystemExit(
            f"configured disk root {target} is outside expected mount {mount}"
        ) from exc
elif expected_device and str(ancestor.stat().st_dev) != expected_device:
    raise SystemExit(
        f"disk device mismatch for configured root {target}: "
        f"expected {expected_device}, got {ancestor.stat().st_dev}"
    )

flags = os.O_RDONLY
flags |= getattr(os, "O_DIRECTORY", 0)
flags |= getattr(os, "O_NOFOLLOW", 0)
fd = os.open(ancestor, flags)
try:
    device = os.fstat(fd).st_dev
    for name in reversed(missing):
        try:
            os.mkdir(name, mode=0o755, dir_fd=fd)
        except FileExistsError:
            pass
        next_fd = os.open(name, flags, dir_fd=fd)
        os.close(fd)
        fd = next_fd
        if os.fstat(fd).st_dev != device:
            raise SystemExit(
                f"disk device changed while creating configured root: {target}"
            )
finally:
    os.close(fd)
PY
}

tartci_check_disk_floor(){
  local path="$1" floor_gb="${TARTCI_VM_DISK_FREE_FLOOR_GB:-25}" probe avail_kb floor_kb
  if ! floor_gb="$(tartci_disk_gb_or_zero TARTCI_VM_DISK_FREE_FLOOR_GB "$floor_gb" 25)"; then
    return 75
  fi
  if [ ! -d "$path" ]; then
    tartci_vm_state_note "refusing VM admission: configured disk root does not exist: $path"
    return 75
  fi
  [ "$floor_gb" -gt 0 ] || return 0
  probe="$path"
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
