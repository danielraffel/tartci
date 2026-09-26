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
TARTCI_ADMISSION_CLEAN_ERROR_CHARS="${TARTCI_ADMISSION_CLEAN_ERROR_CHARS:-120}"

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
#
# Extra arguments are forwarded to the adapter. A caller holding a booted VM
# passes --wait-in-progress so a sibling lane's in-flight observation of the
# same target is waited out (bounded by
# TARTCI_ADMISSION_CLEAN_IN_PROGRESS_WAIT_SECS) instead of discarding the VM.
tartci_admission_clean() {
  local repo="$1" labels="$2"
  shift 2
  python3 "$TARTCI_ROOT/scripts/provider_admission_clean.py" \
    --shipyard "$TARTCI_SHIPYARD_CLI" \
    --repo "$repo" \
    --base "$TARTCI_ADMISSION_CLEAN_BASE" \
    --labels "$labels" \
    "$@"
}

# Render an admission envelope as a bounded single-line detail for a provider
# event or log line: the typed reason plus the head of the underlying error.
# Without it a refusal reports only its exit code, and the reason is reachable
# only by finding the per-VM envelope on disk.
#
# Fails open to a fixed marker. A diagnostic must never be able to break the
# failure path it is describing, so a missing python3, a rotated envelope and
# malformed JSON all render rather than abort.
tartci_admission_clean_detail() {
  local envelope="${1:-}" rendered=""
  if rendered="$(printf '%s' "$envelope" \
    | python3 "$TARTCI_ROOT/scripts/admission_clean_detail.py" \
      --max-error-chars "$TARTCI_ADMISSION_CLEAN_ERROR_CHARS" 2>/dev/null)"; then
    printf '%s' "$rendered"
  else
    printf '%s' "reason=unreadable"
  fi
}
