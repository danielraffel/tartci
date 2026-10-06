# Shared host-core lease helpers for VM provider runners.
# shellcheck shell=bash

: "${TARTCI_VM_LEASES:=1}"
: "${TARTCI_VM_LEASE_HEARTBEAT_SECS:=30}"
: "${TARTCI_VM_DISK_GROWTH_GB:=24}"
: "${TARTCI_VM_DISK_FREE_FLOOR_GB:=25}"

# Process-local admission state. Bash arrays are not inherited from the
# environment, and the unconditional reset prevents a forged scalar from being
# mistaken for authority when this helper is sourced.
unset _tartci_vm_lease_bypass_state 2>/dev/null || true
declare -a _tartci_vm_lease_bypass_state=()

# shellcheck source=providers/common/disk-capacity.lib.sh
source "${BASH_SOURCE[0]%/*}/disk-capacity.lib.sh"

tartci_vm_lease_note(){
  if command -v note >/dev/null 2>&1; then
    note "$*"
  else
    printf '%s\n' "$*" >&2
  fi
}

tartci_observe_disk_admission(){
  local attempt_json="$1" provider="$2" lane="$3" runner="$4"
  [ -n "${TARTCI_DISK_DENIAL_RECEIPT_DIR:-}" ] || return 0
  printf '%s' "$attempt_json" | python3 "$TARTCI_ROOT/scripts/disk_denial_receipt.py" \
    --receipt-dir "$TARTCI_DISK_DENIAL_RECEIPT_DIR" \
    --host "${TARTCI_RECEIPT_HOST_ID:-}" \
    --provider "$provider" --lane "$lane" --runner "$runner" >/dev/null 2>&1 || {
      tartci_vm_lease_note "disk admission receipt observer failed (ignored) provider=$provider lane=$lane runner=$runner"
      return 0
    }
}

tartci_prepare_disk_root_observed(){
  local path="$1" expected_mount="$2" expected_device="$3" provider="$4" lane="$5" runner="$6"
  tartci_prepare_disk_root "$path" "$expected_mount" "$expected_device" && return 0
  [ -n "${TARTCI_DISK_DENIAL_RECEIPT_DIR:-}" ] && python3 "$TARTCI_ROOT/scripts/disk_denial_receipt.py" \
    --receipt-dir "$TARTCI_DISK_DENIAL_RECEIPT_DIR" --host "${TARTCI_RECEIPT_HOST_ID:-}" \
    --provider "$provider" --lane "$lane" --runner "$runner" \
    --reason disk_probe_failed --disk-path "$path" >/dev/null 2>&1 || true
  return 75
}

tartci_check_disk_floor_observed(){
  local path="$1" provider="$2" lane="$3" runner="$4" floor_gb avail_kb floor_kb reason
  local probe_path="" device_id="" attempt_json=""
  TARTCI_LAST_DISK_ADMISSION_ATTEMPT_JSON=""
  if ! floor_gb="$(tartci_disk_gb_or_zero TARTCI_VM_DISK_FREE_FLOOR_GB "${TARTCI_VM_DISK_FREE_FLOOR_GB:-25}" 25)"; then
    [ -n "${TARTCI_DISK_DENIAL_RECEIPT_DIR:-}" ] && python3 "$TARTCI_ROOT/scripts/disk_denial_receipt.py" \
      --receipt-dir "$TARTCI_DISK_DENIAL_RECEIPT_DIR" --host "${TARTCI_RECEIPT_HOST_ID:-}" \
      --provider "$provider" --lane "$lane" --runner "$runner" \
      --reason disk_floor_misconfigured --disk-path "$path" >/dev/null 2>&1 || true
    return 75
  fi
  if [ ! -d "$path" ]; then
    reason=disk_probe_failed
  elif [ "$floor_gb" -eq 0 ]; then
    return 0
  else
    IFS=$'\t' read -r probe_path device_id < <(python3 - "$path" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1]).expanduser().resolve(strict=True)
print(f"{p}\t{p.stat().st_dev}")
PY
    ) || true
    if [ -z "$probe_path" ] || [ -z "$device_id" ]; then
      reason=disk_probe_failed
      avail_kb=""
    else
      avail_kb="$(df -Pk "$probe_path" 2>/dev/null | awk 'NR==2 {print $4}')"
    fi
    case "$avail_kb" in
      ''|*[!0-9]*) reason=disk_probe_failed ;;
      *)
        floor_kb=$((floor_gb * 1024 * 1024))
        [ "$avail_kb" -lt "$floor_kb" ] || return 0
        reason=disk_capacity_insufficient
        ;;
    esac
  fi
  if [ "$reason" = disk_capacity_insufficient ]; then
    attempt_json="$(python3 - "$probe_path" "$device_id" "$((avail_kb * 1024))" "$((floor_gb * 1024 * 1024 * 1024))" <<'PY'
import json, sys
path, device, free, floor = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
print(json.dumps({
    "ok": False, "reason": "disk_capacity_exceeded",
    "exceeded_axis": {"cores": False, "memory": False, "disk": True},
    "disk": {"probe_path": path, "reservation_path": path,
             "device_id": device, "free_bytes": free, "reserved_bytes": 0,
             "requested_bytes": 0, "floor_bytes": floor,
             "required_bytes": floor, "available_after_reservations_bytes": free},
}, separators=(",", ":")))
PY
    )"
    TARTCI_LAST_DISK_ADMISSION_ATTEMPT_JSON="$attempt_json"
    tartci_observe_disk_admission "$attempt_json" "$provider" "$lane" "$runner"
    return 75
  fi
  [ -n "${TARTCI_DISK_DENIAL_RECEIPT_DIR:-}" ] && python3 "$TARTCI_ROOT/scripts/disk_denial_receipt.py" \
    --receipt-dir "$TARTCI_DISK_DENIAL_RECEIPT_DIR" --host "${TARTCI_RECEIPT_HOST_ID:-}" \
    --provider "$provider" --lane "$lane" --runner "$runner" \
    --reason "$reason" --disk-path "$path" >/dev/null 2>&1 || true
  return 75
}

tartci_check_macos_disk_floor_with_cleanup_once(){
  local path="$1" lane="$2" runner="$3" rc=0
  tartci_check_disk_floor_observed "$path" tart-macos "$lane" "$runner" || rc=$?
  [ "$rc" -ne 0 ] || return 0
  [ "$path" = /Volumes/Workshop/VMs ] || return "$rc"
  [ -n "${TARTCI_LAST_DISK_ADMISSION_ATTEMPT_JSON:-}" ] || return "$rc"
  tartci_try_worktree_cleanup "$TARTCI_LAST_DISK_ADMISSION_ATTEMPT_JSON" || return "$rc"
  # One exact remeasurement only. A second denial remains authoritative.
  tartci_check_disk_floor_observed "$path" tart-macos "$lane" "$runner"
}

tartci_prepare_and_check_disk_root_observed(){
  tartci_prepare_disk_root_observed "$@" || return $?
  tartci_check_disk_floor_observed "$1" "$4" "$5" "$6"
}

tartci_vm_leases_enabled(){
  case "${TARTCI_VM_LEASES:-1}" in
    0|false|FALSE|off|OFF|no|NO) return 1 ;;
    *) return 0 ;;
  esac
}

tartci_profile_value(){
  local key="$1"
  python3 - "$TARTCI_ROOT" "$key" <<'PY'
import json
import subprocess
import sys

root, key = sys.argv[1], sys.argv[2]
profile = json.loads(subprocess.check_output([sys.executable, f"{root}/scripts/host_profile.py", "--json"], text=True))
print(profile[key])
PY
}

tartci_positive_int_or_empty(){
  case "${1:-}" in
    ''|*[!0-9]*) return 1 ;;
    *) [ "$1" -gt 0 ] 2>/dev/null ;;
  esac
}

# Per-slot VM cores for a lane sized from this host's gate reserve
# (vm_cores_from = "gate-reserve"): gate_reserve_fit.share_cores is the one
# derivation, shared with the gate-supply and gate-reserve checks.
tartci_gate_reserve_share_cores(){
  python3 "$TARTCI_ROOT/scripts/gate_reserve_fit.py" share-cores --slots "${1:-1}"
}

tartci_vm_lease_cores(){
  local provider="$1" fallback="${2:-}" value="" key="vm_pool_cores"
  case "$provider" in
    tart-macos)
      value="${TARTCI_MACOS_VM_CORES:-${PULP_MACOS_VM_CORES:-}}"
      if [ -z "$value" ] && [ "${TARTCI_MACOS_VM_CORES_FROM:-}" = "gate-reserve" ]; then
        value="$(tartci_gate_reserve_share_cores "${TARTCI_MACOS_VM_CORES_SLOTS:-1}" 2>/dev/null || true)"
        tartci_positive_int_or_empty "$value" \
          || echo "tartci: gate-reserve VM size unavailable; using vm_pool_cores" >&2
      fi
      ;;
    tart-linux)
      value="${TARTCI_LINUX_VM_CORES:-${PULP_LINUX_VM_CORES:-}}"
      ;;
    qemu-windows)
      value="${TARTCI_WIN_VM_CORES:-${PULP_WIN_VM_CORES:-${fallback:-}}}"
      ;;
  esac
  if ! tartci_positive_int_or_empty "$value"; then
    if tartci_positive_int_or_empty "$fallback"; then
      value="$fallback"
    else
      value="$(tartci_profile_value "$key")"
    fi
  fi
  tartci_positive_int_or_empty "$value" || value=1
  printf '%s' "$value"
}

# Memory (MB) a VM lease should reserve on the host memory axis. An explicit
# per-provider override wins and is used verbatim; otherwise this returns EMPTY
# and tartci_acquire_vm_lease derives the size from the lease's finally-granted
# core count (see tartci_vm_lease_derived_mem_mb). The derivation cannot happen
# here because the core count is not final until after the non-gate clamp inside
# acquisition. qemu-windows keeps its caller-supplied WIN_MEMORY_MB fallback:
# that lane sizes its own guest and is not vCPU-derived.
# Passing a real number (rather than letting leases.py derive cores*per-job)
# keeps VM accounting honest: a VM's real RAM footprint is its guest memory,
# not its vCPU count.
tartci_vm_lease_mem_mb(){
  local provider="$1" fallback="${2:-}" value=""
  case "$provider" in
    tart-macos)
      value="${TARTCI_MACOS_VM_MEM_MB:-${PULP_MACOS_VM_MEM_MB:-}}"
      ;;
    tart-linux)
      value="${TARTCI_LINUX_VM_MEM_MB:-${PULP_LINUX_VM_MEM_MB:-}}"
      ;;
    qemu-windows)
      value="${TARTCI_WIN_MEMORY_MB:-${PULP_WIN_MEMORY_MB:-${fallback:-}}}"
      ;;
  esac
  if ! tartci_positive_int_or_empty "$value"; then
    value="${fallback:-}"
  fi
  tartci_positive_int_or_empty "$value" || value=""
  printf '%s' "$value"
}

# Guest memory (MB) for a VM lease of $1 granted cores — the amount charged on
# the host memory axis AND applied to the guest, which must be the same number.
#
# C vCPUs are worth (4/3 * C * per_compile_job_mem_mb): the exact inverse of the
# guest-side governor's own bound in Pulp's tools/ci/governed-build.sh, which
# computes jobs = mem_mb * 3 / 4 / 1536 and takes min(cores, that). The 3/4 and
# the 1536 are a CROSS-REPO CONTRACT with that script — change one, change both.
# per_compile_job_mem_mb is read from the host profile rather than hardcoded so
# the two stay tied to a single definition of "one compile job".
#
# Sized for C-1 jobs, not C. The exact inverse leaves the guest zero slack: it
# would spend its whole compile budget on compile jobs while the guest's own
# link/LTO peak, its runner agent and its OS are unaccounted for. The host
# profile subtracts a flat link_lto_reserve_mem_mb for exactly this reason and
# the guest formula has no equivalent, so the slack has to come from here.
# Guest-internal swap is invisible to every host-side signal, so this errs small.
#
# Floor and ceiling are deliberate. The floor keeps small lanes (a 3-core m1
# guest) from shrinking below the size they boot at today. The ceiling is a
# staged-rollout limit, not a derived quantity: measured per-Virtualization
# process RSS runs well above configured guest memory, so two concurrent guests
# at a higher size can approach host RAM. Raise it only against a fresh
# RSS-to-configured measurement at the current size.
tartci_vm_lease_derived_mem_mb(){
  local cores="$1" per_job="" floor ceiling value
  tartci_positive_int_or_empty "$cores" || cores=1
  floor="${TARTCI_VM_LEASE_MIN_MEM_MB:-8192}"
  ceiling="${TARTCI_VM_LEASE_MAX_MEM_MB:-16384}"
  tartci_positive_int_or_empty "$floor" || floor=8192
  tartci_positive_int_or_empty "$ceiling" || ceiling=16384
  # Tolerate a profile hiccup rather than aborting a VM boot over it: the
  # caller runs under `set -e`, where a bare command substitution that exits
  # non-zero would end the acquisition before the fallback below could apply.
  # per_compile_job_mem_mb is a fixed constant in host_profile.py, so the
  # fallback is that same value and not a guess.
  per_job="$(tartci_profile_value per_compile_job_mem_mb 2>/dev/null)" || per_job=""
  tartci_positive_int_or_empty "$per_job" || per_job=1536
  local jobs=$(( cores - 1 ))
  [ "$jobs" -ge 1 ] || jobs=1
  value=$(( jobs * per_job * 4 / 3 ))
  [ "$value" -ge "$floor" ] || value="$floor"
  [ "$ceiling" -ge "$floor" ] || ceiling="$floor"
  [ "$value" -le "$ceiling" ] || value="$ceiling"
  printf '%s' "$value"
}

# Worst-case writable growth charged to the VM/overlay store. Pulp's observed
# full macOS gate grew its store by about 19 GiB; 24 GiB is the conservative
# fleet default, while provider/host overrides keep lighter or heavier lanes
# configurable without changing runner identity or routing.
tartci_vm_lease_disk_growth_gb(){
  local provider="$1" value=""
  case "$provider" in
    tart-macos) value="${TARTCI_MACOS_VM_DISK_GROWTH_GB:-}" ;;
    tart-linux) value="${TARTCI_LINUX_VM_DISK_GROWTH_GB:-}" ;;
    qemu-windows) value="${TARTCI_WIN_VM_DISK_GROWTH_GB:-}" ;;
  esac
  [ -n "$value" ] || value="${TARTCI_VM_DISK_GROWTH_GB:-24}"
  tartci_disk_gb_or_zero TARTCI_VM_DISK_GROWTH_GB "$value" 24
}

tartci_vm_lease_disk_expected_device_id(){
  local provider="$1" value="${TARTCI_VM_DISK_EXPECTED_DEVICE_ID:-}"
  case "$provider" in
    tart-macos) value="${TARTCI_MACOS_VM_DISK_EXPECTED_DEVICE_ID:-$value}" ;;
    tart-linux) value="${TARTCI_LINUX_VM_DISK_EXPECTED_DEVICE_ID:-$value}" ;;
    qemu-windows) value="${TARTCI_WIN_VM_DISK_EXPECTED_DEVICE_ID:-$value}" ;;
  esac
  printf '%s' "$value"
}

tartci_vm_lease_disk_expected_mount_path(){
  local provider="$1" disk_path="$2" value="${TARTCI_VM_DISK_EXPECTED_MOUNT_PATH:-}"
  case "$provider" in
    tart-macos) value="${TARTCI_MACOS_VM_DISK_EXPECTED_MOUNT_PATH:-$value}" ;;
    tart-linux) value="${TARTCI_LINUX_VM_DISK_EXPECTED_MOUNT_PATH:-$value}" ;;
    qemu-windows) value="${TARTCI_WIN_VM_DISK_EXPECTED_MOUNT_PATH:-$value}" ;;
  esac
  # A missing /Volumes/<name> mount must never spill onto the internal Data
  # volume. Infer the declared external mount when the host did not provide an
  # even stricter persisted device/mount identity.
  if [ -z "$value" ]; then
    case "$disk_path" in
      /Volumes/*)
        local volume_tail="${disk_path#/Volumes/}"
        value="/Volumes/${volume_tail%%/*}"
        ;;
    esac
  fi
  printf '%s' "$value"
}

tartci_vm_lease_priority(){
  local labels="${1:-}"
  case ",$labels," in
    *,pulp-build-merge-group,*pulp-build-pr-head,*|*,pulp-build-pr-head,*pulp-build-merge-group,*)
      printf '%s' vm
      return 0
      ;;
  esac
  if [ -n "${TARTCI_VM_LEASE_PRIORITY:-}" ]; then
    printf '%s' "$TARTCI_VM_LEASE_PRIORITY"
    return 0
  fi
  case ",$labels," in
    *,pulp-build-merge-group,*)
      # Both event classes retain gate-reserved capacity, while merge-group
      # demand sorts above PR-head demand in status/admission ordering.
      printf '%s' 110
      return 0
      ;;
    *,pulp-build-pr-head,*)
      printf '%s' 100
      return 0
      ;;
  esac
  # Release classes minted by an event-class-v2 gate lane, whose registrations
  # carry the gate base label pulp-build-vm (the legacy pulp-release lane carries
  # pulp-build-vm-release instead and keeps its gate/vm classes below). Tagged
  # releases sort above merge-group (120 > 110) so a release boot is admitted
  # from gate-reserved capacity ahead of queued gate work. The release-path PR
  # gate sits between them (115): it is gate class, so it admits exactly like
  # PR-head (a slot that boots it holds what a gate guest on that slot would,
  # and an ordinary build holding the non-gate budget cannot lock it out), and
  # where ranked VM lease waiters are on it outranks merge-group, PR-head and
  # the other lanes' `gate` class (100) instead of tying with them. Each
  # supervisor slot holds at most one lease, so this cannot take a second
  # slot's reserve.
  case ",$labels," in
    *,pulp-release-tagged,*pulp-release-pr-gate,*|*,pulp-release-pr-gate,*pulp-release-tagged,*) ;;
    *,pulp-build-vm,*)
      case ",$labels," in
        *,pulp-release-tagged,*)
          printf '%s' 120
          return 0
          ;;
        *,pulp-release-pr-gate,*)
          printf '%s' 115
          return 0
          ;;
      esac
      ;;
  esac
  case ",$labels," in
    *,pulp-release-pr-gate,*) printf '%s' vm ;;
    *,pulp-build,*|*,pulp-release-tagged,*) printf '%s' gate ;;
    *) printf '%s' vm ;;
  esac
}

# Is this lease priority NON-gate? Accepts the class names tartci_vm_lease_priority
# emits ("gate"/"vm") or a numeric priority (gate class is >= 100). A non-gate VM
# lane is subject to the host's non-gate core budget (lease_capacity -
# reserved_gate); a gate lane is not and must never be clamped.
tartci_vm_lease_is_non_gate_priority(){
  local p="${1:-vm}"
  case "$p" in
    gate) return 1 ;;
    ''|*[!0-9]*) return 0 ;;          # any non-numeric class other than "gate"
    *) [ "$p" -ge 100 ] && return 1 || return 0 ;;
  esac
}

tartci_vm_lease_owner(){
  local host
  host="$(hostname -s 2>/dev/null || hostname 2>/dev/null || printf unknown)"
  printf '%s@%s' "${USER:-unknown}" "$host"
}

# Only a freshly returned, exact disk-axis lease denial can trigger cleanup.
tartci_try_worktree_cleanup(){
  local attempt_json="$1" free_bytes="" required_bytes="" apply_args=()
  [ "${TARTCI_WORKTREE_CLEANUP_APPLY:-0}" = 1 ] || return 1
  [ "${TARTCI_WORKTREE_CLEANUP_PROVIDER:-}" = merged-main-v1 ] || return 1
  [ "${TARTCI_WORKTREE_CLEANUP_REPO:-}" = Generous-Corp/pulp ] || return 1
  [ "${TARTCI_RUNNER_REPO:-}" = Generous-Corp/pulp ] || return 1
  [ "${TARTCI_RECEIPT_HOST_ID:-}" = studio ] || return 1
  case "${TARTCI_QUEUE_LANE_ID:-}" in
    studio-pulp-gate|studio-pulp-gate-slot2) ;;
    *) return 1 ;;
  esac
  IFS=$'\t' read -r free_bytes required_bytes < <(printf '%s' "$attempt_json" | /usr/bin/python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception: raise SystemExit(1)
disk=d.get("disk"); axis=d.get("exceeded_axis")
if d.get("ok") is not False or d.get("reason")!="disk_capacity_exceeded" or axis!={"cores":False,"memory":False,"disk":True} or not isinstance(disk,dict): raise SystemExit(1)
free=disk.get("free_bytes"); required=disk.get("required_bytes")
if type(free) is not int or type(required) is not int: raise SystemExit(1)
print(f"{free}\t{required}")
') || return 1
  case "$free_bytes:$required_bytes" in *[!0-9:]*) return 1;; esac
  apply_args=(--apply)
  /usr/bin/python3 "$TARTCI_ROOT/scripts/worktree_cleanup.py" \
    --provider "$TARTCI_WORKTREE_CLEANUP_PROVIDER" --repo "$TARTCI_WORKTREE_CLEANUP_REPO" \
    --primary "$TARTCI_WORKTREE_CLEANUP_PRIMARY" --prefix "$TARTCI_WORKTREE_CLEANUP_PREFIX" \
    --main-ref "$TARTCI_WORKTREE_CLEANUP_MAIN_REF" \
    --receipt "$TARTCI_DISK_DENIAL_RECEIPT_DIR/worktree-cleanup.json" \
    --lock "$TARTCI_DISK_DENIAL_RECEIPT_DIR/worktree-cleanup.lock" \
    --before-free-bytes "$free_bytes" --required-bytes "$required_bytes" \
    --max-trees "${TARTCI_WORKTREE_CLEANUP_MAX_TREES:-8}" \
    --max-bytes "$(( ${TARTCI_WORKTREE_CLEANUP_MAX_GIB:-512} * 1024 * 1024 * 1024 ))" \
    --timeout "${TARTCI_WORKTREE_CLEANUP_TIMEOUT_SECS:-300}" \
    --cooldown "${TARTCI_WORKTREE_CLEANUP_COOLDOWN_SECS:-3600}" \
    ${apply_args[@]+"${apply_args[@]}"}
}

tartci_record_worktree_cleanup_retry(){
  local attempt_json="$1" retry_rc="$2" receipt="$TARTCI_DISK_DENIAL_RECEIPT_DIR/worktree-cleanup.json"
  [ -f "$receipt" ] || return 0
  TARTCI_RETRY_ATTEMPT_JSON="$attempt_json" /usr/bin/python3 - "$receipt" "$retry_rc" <<'PY' || true
import hashlib, json, os, sys, tempfile
from pathlib import Path
path, rc = Path(sys.argv[1]), int(sys.argv[2])
try:
    record = json.loads(path.read_text())
except OSError:
    raise SystemExit(0)
raw = os.environ["TARTCI_RETRY_ATTEMPT_JSON"]
try:
    attempt = json.loads(raw)
except ValueError:
    attempt = {"malformed": True, "raw_sha256": hashlib.sha256(raw.encode()).hexdigest()}
record["retry"] = {"rc": rc, "ok": rc == 0, "attempt": attempt}
fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle, indent=2, sort_keys=True); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.replace(name, path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
finally:
    try: os.unlink(name)
    except FileNotFoundError: pass
PY
}

tartci_start_vm_lease_heartbeat(){
  local lease_id="$1"
  (
    while :; do
      sleep "$TARTCI_VM_LEASE_HEARTBEAT_SECS"
      python3 "$TARTCI_ROOT/scripts/leases.py" heartbeat --id "$lease_id" --json >/dev/null 2>&1 || exit 0
    done
  ) &
  TARTCI_ACTIVE_VM_LEASE_HEARTBEAT_PID="$!"
}

tartci_stop_vm_lease_heartbeat(){
  if [ -n "${TARTCI_ACTIVE_VM_LEASE_HEARTBEAT_PID:-}" ]; then
    kill "$TARTCI_ACTIVE_VM_LEASE_HEARTBEAT_PID" 2>/dev/null || true
    wait "$TARTCI_ACTIVE_VM_LEASE_HEARTBEAT_PID" 2>/dev/null || true
    TARTCI_ACTIVE_VM_LEASE_HEARTBEAT_PID=""
  fi
}

# A structured `lease_denied` event for the supervisor's events.jsonl, when the
# sourcing provider defines `event` (the macOS runner does). A denial used to
# reach only the note stream, so a host that refused every lease for an hour
# left no event behind. axis is the exceeded capacity axes joined with "+"
# (cores, memory, disk), or "none" for a denial that is not a capacity
# verdict (legacy accounting, disk root unavailable, ...).
tartci_vm_lease_denied_event(){
  local out="$1" rc="$2" kind="$3" cores="$4" mem_mb="$5" priority="$6" parsed
  declare -F event >/dev/null 2>&1 || return 0
  parsed="$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    d = {}
if not isinstance(d, dict):
    d = {}
axis = d.get("exceeded_axis") if isinstance(d.get("exceeded_axis"), dict) else {}
axes = "+".join(k for k in ("cores", "memory", "disk") if axis.get(k) is True) or "none"
disk = d.get("disk") if isinstance(d.get("disk"), dict) else {}
home = d.get("home_volume") if isinstance(d.get("home_volume"), dict) else {}
def num(v):
    return v if type(v) is int else ""
extra = ""
if home.get("state") == "below":
    extra = " volume=home free=%s floor=%s" % (num(home.get("free_bytes")), num(home.get("floor_bytes")))
print("axis=%s reason=%s requested_cores=%s requested_mem_mb=%s requested_disk_bytes=%s disk_free_bytes=%s disk_required_bytes=%s%s" % (
    axes, str(d.get("reason") or "unreadable").replace(" ", "_"),
    num(d.get("requested_cores")), num(d.get("requested_mem_mb")),
    num(disk.get("requested_bytes")), num(disk.get("free_bytes")),
    num(disk.get("required_bytes")), extra))
' 2>/dev/null)" || parsed="axis=none reason=unreadable"
  local fields=()
  read -r -a fields <<< "$parsed rc=$rc kind=$kind lease_cores=$cores lease_mem_mb=${mem_mb:-auto} priority=$priority"
  event lease_denied "$parsed rc=$rc kind=$kind" ${fields[@]+"${fields[@]}"}
}

# Events for a grant the home-volume floor did not refuse: `disk_axis_unread`
# when the volume could not be read (the axis fails open, so the event keeps it
# from failing silent), and `home_volume_would_refuse` when report mode admitted
# a lease that refuse mode would have denied (the data that decides the flip).
tartci_vm_lease_home_unread_event(){
  local out="$1" line name fields
  declare -F event >/dev/null 2>&1 || return 0
  line="$(printf '%s' "$out" | python3 -c 'import json,sys
try:
    h = json.load(sys.stdin).get("home_volume") or {}
except ValueError:
    h = {}
if not isinstance(h, dict):
    h = {}
if h.get("state") == "unread":
    print("disk_axis_unread volume=home reason=" + str(h.get("reason") or "unknown").replace(" ", "_"))
elif h.get("would_refuse"):
    print("home_volume_would_refuse volume=home free=%s floor=%s" % (h.get("free_bytes"), h.get("floor_bytes")))' 2>/dev/null || true)"
  [ -n "$line" ] || return 0
  name="${line%% *}"
  fields=()
  read -r -a fields <<< "${line#* }"
  event "$name" "${line#* }" ${fields[@]+"${fields[@]}"}
}

# The cores a VM lease of $1 at priority $2 is granted: a non-gate lane is
# clamped to the host's non-gate budget (see tartci_acquire_vm_lease).
tartci_vm_lease_granted_cores(){
  local cores="$1" priority="$2" ngc
  tartci_positive_int_or_empty "$cores" || cores=1
  ngc="$(tartci_profile_value non_gate_capacity_cores 2>/dev/null)" || ngc=""
  if tartci_vm_lease_is_non_gate_priority "$priority" \
     && tartci_positive_int_or_empty "$ngc" && [ "$cores" -gt "$ngc" ]; then
    cores="$ngc"
  fi
  printf '%s' "$cores"
}

# --- ranked VM lease waiters (opt-in: [leases] rank_vm_waiters) ---------------
#
# A lane registers as a waiter once it has taken a job and wants a VM, BEFORE
# its admission precheck, so a lower-priority lane whose acquire lands first is
# deferred rather than handed the cores (scripts/leases.py). The store refreshes
# the waiter on every denied acquire and withdraws it on a grant; the lane
# withdraws it when it stops wanting a VM. A lane whose acquire was denied keeps
# its waiter while it waits for capacity (tartci_vm_lease_waiter_hold), bounded
# by TARTCI_VM_WAITER_HOLD_SECS so a lane that never gets to re-check demand
# cannot hold it forever. Every call fails open: a waiter that cannot be
# written only means this lane is ranked first-come, exactly as without it.
: "${TARTCI_VM_WAITER_HOLD_SECS:=900}"
TARTCI_VM_WAITER_ID=""
TARTCI_VM_WAITER_SINCE=0
declare -a _tartci_vm_waiter_args=()
_tartci_vm_waiters_knob=""

# The knob is read once per supervisor process (a profile change reaches the
# supervisor with its next restart; the lease store itself reads it per call,
# and either side alone being on changes nothing).
tartci_vm_lease_waiters_enabled(){
  tartci_vm_leases_enabled || return 1
  if [ -z "$_tartci_vm_waiters_knob" ]; then
    _tartci_vm_waiters_knob="$(tartci_profile_value rank_vm_waiters 2>/dev/null)" \
      || _tartci_vm_waiters_knob=False
  fi
  [ "$_tartci_vm_waiters_knob" = True ]
}

# $1 lane id, $2 requested cores, $3 kind, $4 lease priority, $5 labels, $6 mem MB.
tartci_vm_lease_waiter_register(){
  local lane="$1" cores="$2" kind="$3" priority="$4" labels="${5:-}" mem_mb="${6:-}"
  tartci_vm_lease_waiters_enabled || return 0
  cores="$(tartci_vm_lease_granted_cores "$cores" "$priority")"
  tartci_positive_int_or_empty "$mem_mb" || mem_mb="$(tartci_vm_lease_derived_mem_mb "$cores")"
  _tartci_vm_waiter_args=(
    --id "waiter-$lane" --cores "$cores" --mem-mb "$mem_mb" --priority "$priority"
    --kind "$kind" --pid "$$" --lane "$lane" --label "$labels"
  )
  if python3 "$TARTCI_ROOT/scripts/leases.py" wait "${_tartci_vm_waiter_args[@]}" \
       --json >/dev/null 2>&1; then
    [ "$TARTCI_VM_WAITER_ID" = "waiter-$lane" ] || TARTCI_VM_WAITER_SINCE="$(date +%s)"
    TARTCI_VM_WAITER_ID="waiter-$lane"
  else
    tartci_vm_lease_note "VM lease waiter registration failed for $lane (ignored: first-come)"
    TARTCI_VM_WAITER_ID=""
  fi
  return 0
}

# Keep a denied lane's waiter alive while it waits for capacity.
tartci_vm_lease_waiter_hold(){
  [ -n "$TARTCI_VM_WAITER_ID" ] || return 0
  if [ $(( $(date +%s) - TARTCI_VM_WAITER_SINCE )) -ge "$TARTCI_VM_WAITER_HOLD_SECS" ]; then
    tartci_vm_lease_waiter_withdraw
    return 0
  fi
  python3 "$TARTCI_ROOT/scripts/leases.py" wait "${_tartci_vm_waiter_args[@]}" \
    --json >/dev/null 2>&1 || true
}

tartci_vm_lease_waiter_withdraw(){
  [ -n "$TARTCI_VM_WAITER_ID" ] || return 0
  python3 "$TARTCI_ROOT/scripts/leases.py" withdraw --id "$TARTCI_VM_WAITER_ID" \
    --json >/dev/null 2>&1 || true
  TARTCI_VM_WAITER_ID=""
  TARTCI_VM_WAITER_SINCE=0
}

# A deferral is not a capacity denial: it gets its own event naming the waiter
# it yielded to, and no lease_denied (whose axis=cores count is the canary's
# proxy for the race this prevents).
tartci_vm_lease_deferred_event(){
  local out="$1" kind="$2" cores="$3" priority="$4" parsed
  declare -F event >/dev/null 2>&1 || return 0
  parsed="$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    w = json.loads(sys.stdin.read()).get("waiter") or {}
except (ValueError, AttributeError):
    w = {}
def v(x):
    return str(x if x not in (None, "") else "unknown").replace(" ", "_")
print("waiter_lane=%s waiter_priority=%s waiter_cores=%s waiter_since=%s" % (
    v(w.get("lane")), v(w.get("priority")), v(w.get("cores")), v(w.get("waiting_since"))))
' 2>/dev/null)" || parsed="waiter_lane=unknown"
  local fields=()
  read -r -a fields <<< "$parsed kind=$kind lease_cores=$cores priority=$priority"
  event lease_deferred_to_waiter "$parsed priority=$priority" ${fields[@]+"${fields[@]}"}
}

tartci_acquire_vm_lease(){
  local vm_name="$1" cores="$2" kind="$3" priority="$4" labels="${5:-}" mem_mb="${6:-}" disk_path="${7:-}"
  local receipt_provider="${8:-unknown}" receipt_lane="${9:-unknown}" receipt_runner="${10:-unknown}" lease_id rc=0 out
  tartci_positive_int_or_empty "$cores" || cores=1
  if [ -n "${TARTCI_ACTIVE_VM_LEASE_ID:-}" ]; then
    tartci_observe_disk_admission '{"ok":false,"reason":"active_lease_exists"}' "$receipt_provider" "$receipt_lane" "$receipt_runner"
    tartci_vm_lease_note "refusing to acquire $kind lease for $vm_name while ${TARTCI_ACTIVE_VM_LEASE_ID} is active"
    return 75
  fi
  # Authorization to run without a guardian is issued only by this admission
  # entry point. Guard helpers must not turn a later mutation of the public
  # environment knob into an ungoverned writer bypass.
  _tartci_vm_lease_bypass_state=()
  if ! tartci_vm_leases_enabled; then
    TARTCI_ACTIVE_VM_LEASE_ID=""
    # shellcheck disable=SC2034 # consumed by provider scripts after sourcing
    TARTCI_ACTIVE_VM_LEASE_CORES="$cores"
    # Break-glass still sizes the guest: the invariant is "the guest boots at
    # what was charged", and a disabled lease store charges nothing but still
    # has to hand the provider a size. Derived from the unclamped request,
    # because with leases off there is no clamp.
    if ! tartci_positive_int_or_empty "$mem_mb"; then
      mem_mb="$(tartci_vm_lease_derived_mem_mb "$cores")"
    fi
    # shellcheck disable=SC2034 # consumed by provider scripts after sourcing
    TARTCI_ACTIVE_VM_LEASE_MEM_MB="$mem_mb"
    _tartci_vm_lease_bypass_state=(authorized)
    tartci_observe_disk_admission '{"ok":true,"reason":"leases_disabled"}' "$receipt_provider" "$receipt_lane" "$receipt_runner"
    return 0
  fi
  # Clamp a NON-GATE VM lane to the host's non-gate core budget
  # (lease_capacity - reserved_gate). A non-gate lane can never lease more than
  # that — leases.py denies it — and on a builder+gate host a mis-sized
  # vm_pool_cores (e.g. dedicated-builder's 14 vs a 12-core non-gate budget) would
  # otherwise make the lane un-leasable and force a hand-set per-host override.
  # Clamping here makes any over-sized request safe by construction, so no
  # override is load-bearing and no VM lane can encroach on the gate reserve.
  # The gate lane runs at gate priority and is intentionally NOT clamped.
  local _granted
  _granted="$(tartci_vm_lease_granted_cores "$cores" "$priority")"
  if [ "$_granted" != "$cores" ]; then
    tartci_vm_lease_note "clamping $kind lease cores $cores -> $_granted (non-gate budget)"
    cores="$_granted"
  fi
  # Size the guest from the cores this lease will actually be granted — i.e.
  # AFTER the clamp above. Deriving from the requested count would charge a
  # clamped non-gate lane for cores it never gets (a 14-core request clamped to
  # 12 would still be billed for 14).
  if ! tartci_positive_int_or_empty "$mem_mb"; then
    mem_mb="$(tartci_vm_lease_derived_mem_mb "$cores")"
  fi
  # The VM memory size charges the memory axis its real footprint. It is also
  # the size the guest is booted at (tartci_set_tart_vm_size): what admission
  # charges and what the guest boots with must be the same number.
  local mem_args=()
  if tartci_positive_int_or_empty "$mem_mb" && [ -n "$mem_mb" ]; then
    mem_args=(--mem-mb "$mem_mb")
  fi
  local disk_args=() disk_growth_gb disk_floor_gb disk_summary="" disk_provider=""
  local disk_expected_device_id="" disk_expected_mount_path=""
  if [ -n "$disk_path" ]; then
    case "$kind" in
      tart-macos-vm) disk_provider=tart-macos ;;
      tart-linux-vm) disk_provider=tart-linux ;;
      qemu-windows-vm) disk_provider=qemu-windows ;;
      *) disk_provider=unknown ;;
    esac
    if [ "$disk_provider" = unknown ]; then
      if ! disk_growth_gb="$(tartci_disk_gb_or_zero TARTCI_VM_DISK_GROWTH_GB "${TARTCI_VM_DISK_GROWTH_GB:-24}" 24)"; then
        tartci_observe_disk_admission '{"ok":false,"reason":"disk_growth_misconfigured"}' "$receipt_provider" "$receipt_lane" "$receipt_runner"
        return 75
      fi
    elif ! disk_growth_gb="$(tartci_vm_lease_disk_growth_gb "$disk_provider")"; then
      tartci_observe_disk_admission '{"ok":false,"reason":"disk_growth_misconfigured"}' "$receipt_provider" "$receipt_lane" "$receipt_runner"
      return 75
    fi
    if ! disk_floor_gb="$(tartci_disk_gb_or_zero TARTCI_VM_DISK_FREE_FLOOR_GB "${TARTCI_VM_DISK_FREE_FLOOR_GB:-25}" 25)"; then
      out="{\"ok\":false,\"reason\":\"disk_floor_misconfigured\",\"disk_path\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$disk_path") }"
      tartci_observe_disk_admission "$out" "$receipt_provider" "$receipt_lane" "$receipt_runner"
      return 75
    fi
    disk_expected_device_id="$(tartci_vm_lease_disk_expected_device_id "$disk_provider")"
    disk_expected_mount_path="$(tartci_vm_lease_disk_expected_mount_path "$disk_provider" "$disk_path")"
    disk_args=(
      --disk-path "$disk_path"
      --disk-growth-mb "$((disk_growth_gb * 1024))"
      --disk-floor-mb "$((disk_floor_gb * 1024))"
    )
    [ -z "$disk_expected_device_id" ] || disk_args+=(--disk-expected-device-id "$disk_expected_device_id")
    # The home volume holds the supervisors' temp files and the build trees; a
    # VM lease also refuses a new clone while it is below its per-host floor
    # (scripts/home_volume_floor.py). The host profile's home_volume_floor_mode
    # (TARTCI_HOME_VOLUME_FLOOR_MODE) is `report` until a day of would-refuse
    # data shows no false refusals; TARTCI_HOME_VOLUME_FLOOR=0 turns it off.
    if [ "${TARTCI_HOME_VOLUME_FLOOR:-1}" = 1 ] && [ -n "${HOME:-}" ]; then
      disk_args+=(--home-floor-path "$HOME" --home-floor-hours "${TARTCI_HOME_VOLUME_FLOOR_HOURS:-1}"
                  --home-floor-mode "${TARTCI_HOME_VOLUME_FLOOR_MODE:-report}")
    fi
    [ -z "$disk_expected_mount_path" ] || disk_args+=(--disk-expected-mount-path "$disk_expected_mount_path")
  fi
  lease_id="vm-$kind-$vm_name"
  # A parked warm VM (TARTCI_VM_LEASE_MEMORY_ONLY=1) holds its memory and disk
  # but no cores until tartci_resize_vm_lease upgrades it at hand-off.
  local core_args=(--cores "$cores")
  [ "${TARTCI_VM_LEASE_MEMORY_ONLY:-0}" != 1 ] || core_args=(--cores 0 --memory-only)
  TARTCI_LAST_VM_LEASE_DENIAL=""
  local waiter_args=()
  [ -z "$TARTCI_VM_WAITER_ID" ] || waiter_args=(--waiter-id "$TARTCI_VM_WAITER_ID")
  out="$(python3 "$TARTCI_ROOT/scripts/leases.py" acquire \
    --id "$lease_id" \
    "${core_args[@]}" \
    ${mem_args[@]+"${mem_args[@]}"} \
    ${disk_args[@]+"${disk_args[@]}"} \
    --priority "$priority" \
    --pid "$$" \
    --kind "$kind" \
    --owner "$(tartci_vm_lease_owner)" \
    --label "$labels" \
    --job-id "${GITHUB_RUN_ID:-}" \
    --vm-name "$vm_name" \
    ${waiter_args[@]+"${waiter_args[@]}"} \
    --json 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    tartci_observe_disk_admission "$out" "$receipt_provider" "$receipt_lane" "$receipt_runner"
    if tartci_try_worktree_cleanup "$out"; then
      rc=0
      out="$(python3 "$TARTCI_ROOT/scripts/leases.py" acquire \
        --id "$lease_id" --cores "$cores" ${mem_args[@]+"${mem_args[@]}"} \
        ${disk_args[@]+"${disk_args[@]}"} --priority "$priority" --pid "$$" \
        --kind "$kind" --owner "$(tartci_vm_lease_owner)" --label "$labels" \
        --job-id "${GITHUB_RUN_ID:-}" --vm-name "$vm_name" \
        ${waiter_args[@]+"${waiter_args[@]}"} --json 2>&1)" || rc=$?
      tartci_observe_disk_admission "$out" "$receipt_provider" "$receipt_lane" "$receipt_runner"
      tartci_record_worktree_cleanup_retry "$out" "$rc"
    fi
  fi
  if [ "$rc" -ne 0 ]; then
    # shellcheck disable=SC2034 # read by the provider (warm-VM yield signal)
    TARTCI_LAST_VM_LEASE_DENIAL="$out"
    tartci_vm_lease_note "lease denied for $vm_name kind=$kind cores=$cores mem_mb=${mem_mb:-auto} priority=$priority rc=$rc: $out"
    case "$out" in
      *'"reason": "deferred_to_waiter"'*)
        tartci_vm_lease_deferred_event "$out" "$kind" "$cores" "$priority" ;;
      *)
        tartci_vm_lease_denied_event "$out" "$rc" "$kind" "$cores" "${mem_mb:-}" "$priority" ;;
    esac
    return "$rc"
  fi
  # The store withdrew this lane's waiter with the grant.
  TARTCI_VM_WAITER_ID=""
  TARTCI_VM_WAITER_SINCE=0
  tartci_observe_disk_admission "$out" "$receipt_provider" "$receipt_lane" "$receipt_runner"
  TARTCI_ACTIVE_VM_LEASE_ID="$lease_id"
  # shellcheck disable=SC2034 # consumed by provider scripts after sourcing
  TARTCI_ACTIVE_VM_LEASE_CORES="$cores"
  # shellcheck disable=SC2034 # consumed by provider scripts after sourcing
  TARTCI_ACTIVE_VM_LEASE_MEM_MB="$mem_mb"
  tartci_start_vm_lease_heartbeat "$lease_id"
  if [ -n "$disk_path" ]; then
    disk_summary="$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin)["disk"]; gib=1024**3; print("disk_free_gib=%.1f disk_reserved_gib=%.1f disk_requested_gib=%.1f disk_required_gib=%.1f disk_device=%s" % (d["free_bytes"]/gib,d["reserved_bytes"]/gib,d["requested_bytes"]/gib,d["required_bytes"]/gib,d["device_id"]))')"
  fi
  tartci_vm_lease_note "lease acquired id=$lease_id cores=$cores mem_mb=${mem_mb:-auto} priority=$priority ${disk_summary}"
  tartci_vm_lease_home_unread_event "$out"
  if declare -F event >/dev/null 2>&1; then
    event lease_acquired "kind=$kind priority=$priority lease_cores=$cores" \
      "kind=$kind" "priority=$priority" "lease_cores=$cores" "lease_mem_mb=${mem_mb:-auto}"
  fi
  return 0
}

# Upgrade the active lease in place (leases.py resize): a parked warm VM's
# memory-only lease becomes a full core lease at hand-off. Atomic under the
# lease-store lock, so there is no released window another lease can take, and
# the guardian and disk reservation are unchanged. Returns 75 on a capacity
# denial with the lease left exactly as it was.
tartci_resize_vm_lease(){
  local cores="$1" mem_mb="$2" priority="$3" labels="${4:-}" lease_id="${TARTCI_ACTIVE_VM_LEASE_ID:-}" out rc=0
  TARTCI_LAST_VM_LEASE_DENIAL=""
  tartci_positive_int_or_empty "$cores" || return 1
  if [ -z "$lease_id" ]; then
    # Break-glass (leases disabled) has no record to resize; the size is
    # simply what the provider applies. Anything else without a lease is wrong.
    [ "${_tartci_vm_lease_bypass_state[0]:-}" = authorized ] || return 1
    TARTCI_ACTIVE_VM_LEASE_CORES="$cores"
    TARTCI_ACTIVE_VM_LEASE_MEM_MB="$mem_mb"
    return 0
  fi
  local waiter_args=()
  [ -z "$TARTCI_VM_WAITER_ID" ] || waiter_args=(--waiter-id "$TARTCI_VM_WAITER_ID")
  out="$(python3 "$TARTCI_ROOT/scripts/leases.py" resize --id "$lease_id" \
    --cores "$cores" --mem-mb "$mem_mb" --priority "$priority" --label "$labels" \
    ${waiter_args[@]+"${waiter_args[@]}"} --json 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    # shellcheck disable=SC2034 # read by the provider
    TARTCI_LAST_VM_LEASE_DENIAL="$out"
    tartci_vm_lease_note "lease resize denied for $lease_id cores=$cores mem_mb=$mem_mb priority=$priority rc=$rc: $out"
    case "$out" in
      *'"reason": "deferred_to_waiter"'*)
        tartci_vm_lease_deferred_event "$out" resize "$cores" "$priority" ;;
    esac
    return "$rc"
  fi
  TARTCI_VM_WAITER_ID=""
  TARTCI_VM_WAITER_SINCE=0
  # shellcheck disable=SC2034 # consumed by provider scripts after sourcing
  TARTCI_ACTIVE_VM_LEASE_CORES="$cores"
  # shellcheck disable=SC2034 # consumed by provider scripts after sourcing
  TARTCI_ACTIVE_VM_LEASE_MEM_MB="$mem_mb"
  tartci_vm_lease_note "lease resized id=$lease_id cores=$cores mem_mb=$mem_mb priority=$priority"
  return 0
}

# Start the actual VM writer as the exact lease guardian. The backgrounded
# function process is replaced first by leases.py and then by the requested
# Tart/QEMU process, so $! remains the same PID across the atomic record update
# and exec. There is no parent-starts-child/attaches-child crash gap.
tartci_vm_lease_guard_exec(){
  local lease_id="${TARTCI_ACTIVE_VM_LEASE_ID:-}"
  [ -n "$lease_id" ] || {
    # The documented operator-only break-glass mode has no durable lease to
    # guard. Bypass only after tartci_acquire_vm_lease authoritatively observed
    # that explicit disable; rereading a mutable environment knob is not proof.
    if [ "${_tartci_vm_lease_bypass_state[0]:-}" = authorized ] \
      && ! tartci_vm_leases_enabled; then
      exec "$@"
    fi
    tartci_vm_lease_note "cannot start VM guardian without an active lease"
    return 75
  }
  exec python3 "$TARTCI_ROOT/scripts/leases.py" guard-exec --id "$lease_id" -- "$@"
}

# Finite clone/overlay writers need the same crash-safe handoff as the VM, but
# ownership must return to the provider supervisor after the command exits.
tartci_vm_lease_guard_run(){
  local lease_id="${TARTCI_ACTIVE_VM_LEASE_ID:-}"
  [ -n "$lease_id" ] || {
    # See guard_exec: this is the finite-writer half of the same acquisition-
    # authorized break-glass contract, not a fallback after a lease failure.
    if [ "${_tartci_vm_lease_bypass_state[0]:-}" = authorized ] \
      && ! tartci_vm_leases_enabled; then
      "$@"
      return $?
    fi
    tartci_vm_lease_note "cannot start guarded VM writer without an active lease"
    return 75
  }
  python3 "$TARTCI_ROOT/scripts/leases.py" guard-run --id "$lease_id" -- "$@"
}

tartci_release_vm_lease(){
  local lease_id="${TARTCI_ACTIVE_VM_LEASE_ID:-}" rc=0
  if [ -z "$lease_id" ]; then
    _tartci_vm_lease_bypass_state=()
    return 0
  fi
  tartci_stop_vm_lease_heartbeat
  # An acquired lease remains authoritative even if configuration changes.
  # Always release by exact ID; the current public enable knob is irrelevant.
  python3 "$TARTCI_ROOT/scripts/leases.py" release --id "$lease_id" --json >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 0 ] || tartci_vm_lease_note "lease release reported rc=$rc for $lease_id"
  TARTCI_ACTIVE_VM_LEASE_ID=""
  _tartci_vm_lease_bypass_state=()
  # shellcheck disable=SC2034 # consumed by provider scripts after sourcing
  TARTCI_ACTIVE_VM_LEASE_CORES=""
  # shellcheck disable=SC2034 # consumed by provider scripts after sourcing
  TARTCI_ACTIVE_VM_LEASE_MEM_MB=""
  return 0
}

# Apply the lease's granted size to the clone. Both axes, in one call: a clone
# inherits its golden's CPU *and* memory, so setting only --cpu leaves the guest
# booting at the golden's baked memory however much the lease charged for. That
# silently breaks the plumbing invariant — the guest's own build governor sizes
# itself from the memory it can see, so it would derive its job count from the
# golden's number while the host reserved a different one.
tartci_set_tart_vm_size(){
  local vm_name="$1" cores="$2" mem_mb="${3:-}"
  tartci_positive_int_or_empty "$cores" || cores=1
  if tartci_positive_int_or_empty "$mem_mb"; then
    tart set "$vm_name" --cpu "$cores" --memory "$mem_mb"
  else
    tart set "$vm_name" --cpu "$cores"
  fi
}

# Retained name for callers that only size the CPU axis.
tartci_set_tart_vm_cpu(){
  tartci_set_tart_vm_size "$1" "$2" ""
}
