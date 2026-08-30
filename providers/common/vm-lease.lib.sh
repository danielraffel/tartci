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
    tartci_observe_disk_admission "$attempt_json" "$provider" "$lane" "$runner"
    return 75
  fi
  [ -n "${TARTCI_DISK_DENIAL_RECEIPT_DIR:-}" ] && python3 "$TARTCI_ROOT/scripts/disk_denial_receipt.py" \
    --receipt-dir "$TARTCI_DISK_DENIAL_RECEIPT_DIR" --host "${TARTCI_RECEIPT_HOST_ID:-}" \
    --provider "$provider" --lane "$lane" --runner "$runner" \
    --reason "$reason" --disk-path "$path" >/dev/null 2>&1 || true
  return 75
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

tartci_vm_lease_cores(){
  local provider="$1" fallback="${2:-}" value="" key="vm_pool_cores"
  case "$provider" in
    tart-macos)
      value="${TARTCI_MACOS_VM_CORES:-${PULP_MACOS_VM_CORES:-}}"
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

# Memory (MB) a VM lease should reserve on the host memory axis. Explicit
# per-provider override wins; else a conservative per-guest default — a macOS or
# Linux CI guest wants ~8 GiB, and Windows already carries WIN_MEMORY_MB. Passing
# this (rather than letting leases.py derive cores*per-job) keeps VM accounting
# honest: a VM's real RAM footprint is its guest memory, not its vCPU count.
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
    value="${fallback:-8192}"
  fi
  tartci_positive_int_or_empty "$value" || value=8192
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
  local _ngc
  _ngc="$(tartci_profile_value non_gate_capacity_cores 2>/dev/null)"
  if tartci_vm_lease_is_non_gate_priority "$priority" \
     && tartci_positive_int_or_empty "$_ngc" && [ "$cores" -gt "$_ngc" ]; then
    tartci_vm_lease_note "clamping $kind lease cores $cores -> $_ngc (non-gate budget)"
    cores="$_ngc"
  fi
  # An explicit VM memory size charges the memory axis its real footprint;
  # omitted (empty) lets leases.py fall back to the cores*per-job estimate.
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
    [ -z "$disk_expected_mount_path" ] || disk_args+=(--disk-expected-mount-path "$disk_expected_mount_path")
  fi
  lease_id="vm-$kind-$vm_name"
  out="$(python3 "$TARTCI_ROOT/scripts/leases.py" acquire \
    --id "$lease_id" \
    --cores "$cores" \
    ${mem_args[@]+"${mem_args[@]}"} \
    ${disk_args[@]+"${disk_args[@]}"} \
    --priority "$priority" \
    --pid "$$" \
    --kind "$kind" \
    --owner "$(tartci_vm_lease_owner)" \
    --label "$labels" \
    --job-id "${GITHUB_RUN_ID:-}" \
    --vm-name "$vm_name" \
    --json 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    tartci_observe_disk_admission "$out" "$receipt_provider" "$receipt_lane" "$receipt_runner"
    tartci_vm_lease_note "lease denied for $vm_name kind=$kind cores=$cores mem_mb=${mem_mb:-auto} priority=$priority rc=$rc: $out"
    return "$rc"
  fi
  tartci_observe_disk_admission "$out" "$receipt_provider" "$receipt_lane" "$receipt_runner"
  TARTCI_ACTIVE_VM_LEASE_ID="$lease_id"
  # shellcheck disable=SC2034 # consumed by provider scripts after sourcing
  TARTCI_ACTIVE_VM_LEASE_CORES="$cores"
  tartci_start_vm_lease_heartbeat "$lease_id"
  if [ -n "$disk_path" ]; then
    disk_summary="$(printf '%s' "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin)["disk"]; gib=1024**3; print("disk_free_gib=%.1f disk_reserved_gib=%.1f disk_requested_gib=%.1f disk_required_gib=%.1f disk_device=%s" % (d["free_bytes"]/gib,d["reserved_bytes"]/gib,d["requested_bytes"]/gib,d["required_bytes"]/gib,d["device_id"]))')"
  fi
  tartci_vm_lease_note "lease acquired id=$lease_id cores=$cores mem_mb=${mem_mb:-auto} priority=$priority ${disk_summary}"
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
  return 0
}

tartci_set_tart_vm_cpu(){
  local vm_name="$1" cores="$2"
  tartci_positive_int_or_empty "$cores" || cores=1
  tart set "$vm_name" --cpu "$cores"
}
