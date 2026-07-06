# Shared host-core lease helpers for VM provider runners.
# shellcheck shell=bash

: "${TARTCI_VM_LEASES:=1}"
: "${TARTCI_VM_LEASE_HEARTBEAT_SECS:=30}"

tartci_vm_lease_note(){
  if command -v note >/dev/null 2>&1; then
    note "$*"
  else
    printf '%s\n' "$*" >&2
  fi
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

tartci_vm_lease_priority(){
  local labels="${1:-}"
  if [ -n "${TARTCI_VM_LEASE_PRIORITY:-}" ]; then
    printf '%s' "$TARTCI_VM_LEASE_PRIORITY"
    return 0
  fi
  case ",$labels," in
    *,pulp-build,*) printf '%s' gate ;;
    *) printf '%s' vm ;;
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
  local vm_name="$1" cores="$2" kind="$3" priority="$4" labels="${5:-}" lease_id rc=0 out
  if ! tartci_vm_leases_enabled; then
    TARTCI_ACTIVE_VM_LEASE_ID=""
    return 0
  fi
  if [ -n "${TARTCI_ACTIVE_VM_LEASE_ID:-}" ]; then
    tartci_vm_lease_note "refusing to acquire $kind lease for $vm_name while ${TARTCI_ACTIVE_VM_LEASE_ID} is active"
    return 75
  fi
  tartci_positive_int_or_empty "$cores" || cores=1
  lease_id="vm-$kind-$vm_name"
  out="$(python3 "$TARTCI_ROOT/scripts/leases.py" acquire \
    --id "$lease_id" \
    --cores "$cores" \
    --priority "$priority" \
    --pid "$$" \
    --kind "$kind" \
    --owner "$(tartci_vm_lease_owner)" \
    --label "$labels" \
    --job-id "${GITHUB_RUN_ID:-}" \
    --vm-name "$vm_name" \
    --json 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    tartci_vm_lease_note "lease denied for $vm_name kind=$kind cores=$cores priority=$priority rc=$rc: $out"
    return "$rc"
  fi
  TARTCI_ACTIVE_VM_LEASE_ID="$lease_id"
  tartci_start_vm_lease_heartbeat "$lease_id"
  tartci_vm_lease_note "lease acquired id=$lease_id cores=$cores priority=$priority"
  return 0
}

tartci_release_vm_lease(){
  local lease_id="${TARTCI_ACTIVE_VM_LEASE_ID:-}" rc=0
  [ -n "$lease_id" ] || return 0
  tartci_stop_vm_lease_heartbeat
  if tartci_vm_leases_enabled; then
    python3 "$TARTCI_ROOT/scripts/leases.py" release --id "$lease_id" --json >/dev/null 2>&1 || rc=$?
    [ "$rc" -eq 0 ] || tartci_vm_lease_note "lease release reported rc=$rc for $lease_id"
  fi
  TARTCI_ACTIVE_VM_LEASE_ID=""
  return 0
}

tartci_set_tart_vm_cpu(){
  local vm_name="$1" cores="$2"
  tartci_positive_int_or_empty "$cores" || cores=1
  tart set "$vm_name" --cpu "$cores"
}
