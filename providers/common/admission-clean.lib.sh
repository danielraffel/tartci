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
tartci_admission_clean() {
  local repo="$1" labels="$2"
  python3 "$TARTCI_ROOT/scripts/provider_admission_clean.py" \
    --shipyard "$TARTCI_SHIPYARD_CLI" \
    --repo "$repo" \
    --base "$TARTCI_ADMISSION_CLEAN_BASE" \
    --labels "$labels"
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

# Emit `admission_contention_waited` when an admission envelope records that
# the wrapper waited out another caller's contention defer before its final
# verdict. The wait count and duration otherwise live only in the envelope
# file, which the next attempt overwrites. No-op when the provider defines no
# `event` function or the envelope records no wait. $2 names the stage.
tartci_admission_contention_event() {
  local envelope="${1:-}" stage="${2:-unknown}" parsed
  declare -F event >/dev/null 2>&1 || return 0
  parsed="$(printf '%s' "$envelope" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    raise SystemExit(1)
if not isinstance(d, dict):
    raise SystemExit(1)
waits = d.get("tartci_contention_waits")
if type(waits) is not int or waits <= 0:
    raise SystemExit(1)
secs = d.get("tartci_contention_wait_secs")
secs = secs if isinstance(secs, (int, float)) and not isinstance(secs, bool) else ""
verdict = str(d.get("verdict") or "unknown").replace(" ", "_")
reason = str(d.get("reason") or "unknown").replace(" ", "_")
print("waits=%d secs=%s verdict=%s reason=%s" % (waits, secs, verdict, reason))
' 2>/dev/null)" || return 0
  local fields=()
  read -r -a fields <<< "$parsed stage=$stage"
  event admission_contention_waited "$parsed stage=$stage" ${fields[@]+"${fields[@]}"}
}

# Emit `admission_reused` when the adapter admitted on Shipyard's recent admit
# because a re-read failed (provider_admission_clean.py, REUSABLE_ERROR_REASONS).
# The reuse is otherwise indistinguishable from a fresh admit in the provider's
# events. No-op when the provider defines no `event` function or the envelope
# records no reuse. $2 names the stage.
tartci_admission_reuse_event() {
  local envelope="${1:-}" stage="${2:-unknown}" parsed
  declare -F event >/dev/null 2>&1 || return 0
  parsed="$(printf '%s' "$envelope" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
except ValueError:
    raise SystemExit(1)
if not isinstance(d, dict) or d.get("tartci_reused_admit") is not True:
    raise SystemExit(1)
age = d.get("tartci_reused_admit_age_secs")
age = age if type(age) is int else ""
reason = str(d.get("tartci_reused_for_reason") or "unknown").replace(" ", "_")
print("age_secs=%s reason=%s" % (age, reason))
' 2>/dev/null)" || return 0
  local fields=()
  read -r -a fields <<< "$parsed stage=$stage"
  event admission_reused "$parsed stage=$stage" ${fields[@]+"${fields[@]}"}
}
