#!/usr/bin/env bash
# Shared Shipyard admission gate for JIT provider supervisors.
#
# `disabled` preserves backward compatibility during the coordinated rollout.
# `required` fails closed: only a typed Shipyard `admit` verdict returns zero.
# Shipyard owns every observation/cancellation decision; this helper never calls
# GitHub and never interprets individual runs.

TARTCI_ADMISSION_CLEAN_MODE="${TARTCI_ADMISSION_CLEAN_MODE:-disabled}"
TARTCI_ADMISSION_CLEAN_BASE="${TARTCI_ADMISSION_CLEAN_BASE:-main}"
TARTCI_SHIPYARD_CLI="${TARTCI_SHIPYARD_CLI:-shipyard}"

tartci_validate_admission_clean_config() {
  local repo="${1:-}" labels="${2:-}"
  case "$TARTCI_ADMISSION_CLEAN_MODE" in
    disabled|required) ;;
    *)
      printf '%s\n' \
        "TARTCI_ADMISSION_CLEAN_MODE must be disabled or required" >&2
      return 2
      ;;
  esac
  [ "$TARTCI_ADMISSION_CLEAN_MODE" = disabled ] && return 0
  command -v "$TARTCI_SHIPYARD_CLI" >/dev/null 2>&1 || {
    printf "required admission-clean Shipyard CLI is unavailable: %s\n" \
      "$TARTCI_SHIPYARD_CLI" >&2
    return 2
  }
  python3 "$TARTCI_ROOT/scripts/provider_admission_clean.py" \
    --shipyard "$TARTCI_SHIPYARD_CLI" \
    --repo "$repo" \
    --base "$TARTCI_ADMISSION_CLEAN_BASE" \
    --labels "$labels" \
    --validate-only \
    || return $?
  return 0
}

tartci_admission_clean_enabled() {
  [ "$TARTCI_ADMISSION_CLEAN_MODE" = required ]
}

# Returns Shipyard's typed mapping: 0 admit, 3 defer, 1 operational/contract
# error. Stdout is the validated JSON envelope and may be persisted in provider
# diagnostics; stderr carries only bounded adapter errors.
tartci_admission_clean() {
  local repo="$1" labels="$2"
  python3 "$TARTCI_ROOT/scripts/provider_admission_clean.py" \
    --shipyard "$TARTCI_SHIPYARD_CLI" \
    --repo "$repo" \
    --base "$TARTCI_ADMISSION_CLEAN_BASE" \
    --labels "$labels"
}
