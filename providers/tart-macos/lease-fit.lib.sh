# Skip the queue scan and Shipyard admission while this lane's VM lease cannot
# be granted (scripts/lease_fit.py).
# shellcheck shell=bash
#
# A lane used to scan the queue, ask Shipyard for an admission verdict and
# only then learn from the lease store that its VM does not fit. On m5 the
# second gate lane does that every minute whenever the first holds a VM: a
# 12-core VM in a 14-core lease universe leaves 2, and 257 denials in one day
# each cost a GitHub scan and a Shipyard observation that contend with the
# lane that could actually serve.
#
# The check is local and read-only. It answers with the lease store's own
# capacity model, never acquires, and fails OPEN: an unreadable store or
# profile lets the lane proceed to the real acquisition exactly as before.
#
#   not now -> wait a poll, then look again. Nothing is wrong.
#   never   -> the VM is larger than the lease budget it is admitted against,
#              so it would be denied on an idle host. That is configuration,
#              not load: the lane stops polling and reports it (once per
#              change) instead of retrying forever. `tartci doctor fleet`
#              reports the same fact per lane.
#
# TARTCI_LEASE_FIT_GATE=0 disables the check.

LEASE_FIT_LAST_VERDICT=""

tartci_lease_fit_enabled(){
  [ "${TARTCI_LEASE_FIT_GATE:-1}" = 1 ] && tartci_vm_leases_enabled
}

tartci_lease_fit_validate(){
  case "${TARTCI_LEASE_FIT_GATE:-1}" in 0|1) ;; *)
    printf 'invalid TARTCI_LEASE_FIT_GATE: expected 0 or 1\n' >&2; return 2 ;;
  esac
}

# Every set of labels this lane can register with. Each maps to a lease
# priority; the lane fits if its most permissive class fits.
tartci_lease_fit_candidate_labels(){
  local tier_label
  if [ -z "$TIER_LABELS_CONFIG" ]; then
    printf '%s\n' "$LABELS"
    return 0
  fi
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    if [ "$ASSIGNMENT_MODE" = event-class-v2 ]; then
      tartci_assignment_v2_tier_labels "$tier_label"
    else
      printf '%s,%s\n' "$LABELS" "$tier_label"
    fi
  done <<< "$TIER_LABELS_CONFIG"
}

# Prints the verdict JSON; returns lease_fit.py's code (0 now, 3 not now,
# 4 never, 1 unknown).
tartci_lease_fit_probe(){
  local cores mem ngc labels priority_args=() cap_args=() rc=0
  cores="$(tartci_vm_lease_cores tart-macos)"
  mem="$(tartci_vm_lease_mem_mb tart-macos)"
  while IFS= read -r labels; do
    [ -n "$labels" ] || continue
    priority_args+=(--priority "$(tartci_vm_lease_priority "$labels")")
  done < <(tartci_lease_fit_candidate_labels)
  # Acquisition clamps a non-gate VM to the non-gate budget and derives memory
  # from the granted cores; the probe asks about that same lease.
  ngc="$(tartci_profile_value non_gate_capacity_cores 2>/dev/null)" || ngc=""
  ! tartci_positive_int_or_empty "$ngc" || cap_args=(--non-gate-cap "$ngc")
  tartci_positive_int_or_empty "$mem" || mem="$(tartci_vm_lease_derived_mem_mb "$cores")"
  python3 "$TARTCI_ROOT/scripts/lease_fit.py" --cores "$cores" --mem-mb "$mem" \
    ${priority_args[@]+"${priority_args[@]}"} ${cap_args[@]+"${cap_args[@]}"} \
    --record "$STATE_DIR/$RUNNER_NAME.lease-fit.json" --lane "$RUNNER_NAME" || rc=$?
  return "$rc"
}

# 0: go on and poll. 1: this lane cannot lease a VM now (or ever); the caller
# waits a poll without scanning or asking Shipyard.
tartci_lease_fit_gate(){
  local out rc=0 verdict detail
  tartci_lease_fit_enabled || return 0
  out="$(tartci_lease_fit_probe 2>/dev/null)" || rc=$?
  case "$rc" in
    0) verdict=fits_now ;;
    3) verdict=not_now ;;
    4) verdict=never ;;
    *) verdict=unknown ;;
  esac
  detail="$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("detail=unreadable"); raise SystemExit
print("cores=%s budget=%s used=%s max_concurrent=%s" % (
    d.get("requested_cores"), d.get("core_budget"), d.get("used_cores"),
    d.get("max_concurrent")))
' 2>/dev/null)" || detail="detail=unreadable"
  if [ "$verdict" != "$LEASE_FIT_LAST_VERDICT" ]; then
    case "$verdict" in
      never)
        note "CONFIGURATION: this lane's VM lease can never be granted on this host ($detail) — not polling for work; see tartci doctor fleet"
        event lease_never_fits "$detail"
        ;;
      not_now)
        note "VM lease does not fit right now ($detail) — waiting without scanning or asking Shipyard"
        event lease_unfit_now "$detail"
        ;;
      *)
        [ -z "$LEASE_FIT_LAST_VERDICT" ] || event lease_fit_restored "verdict=$verdict $detail"
        ;;
    esac
    LEASE_FIT_LAST_VERDICT="$verdict"
  fi
  case "$verdict" in
    never) heartbeat lease-never-fits; return 1 ;;
    not_now) heartbeat lease-wait; return 1 ;;
  esac
  return 0
}
