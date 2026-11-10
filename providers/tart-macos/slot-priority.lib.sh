# Slot priority for the macOS JIT supervisor (scripts/slot_priority.py).
# shellcheck shell=bash
#
# Every lane on a host claims a free macOS VM slot first-come, so a lane that
# just finished a job loops straight back and takes the slot it freed. With
# `[leases] slot_priority` on, a lane that would claim a free slot first asks
# the host's lease store who should have it: a merge_group job of any
# repository ahead of a pull_request job, the longest-waiting job first within
# a class. A lane that is outranked by a lane that can boot now steps aside for
# one poll instead of claiming.
#
# * off (the default): nothing here runs, no scan gets a --detail-file, and the
#   loop is byte-for-byte the first-come loop it was.
# * shadow: intents are recorded and every claim that ranking would have given
#   to another lane logs `slot_priority_shadow` ("would have given slot to
#   ..."); the lane still claims first-come.
# * on: an outranked lane logs `slot_priority_deferred` and does not claim this
#   poll. A lane that has deferred for TARTCI_SLOT_PRIORITY_MAX_DEFER_SECS
#   (default 300) in a row claims anyway (`slot_priority_defer_expired`): the
#   lane it is waiting for is not taking the slot, and a free slot must not
#   idle.
#
# Non-preemptive: this only decides who takes a FREE slot; it never touches a
# running VM, a lease or a reservation. It makes no GitHub call: the event class
# and queue age come from the scan the lane already made (--detail-file).
# Every failure claims first-come.

SLOT_PRIORITY_DEFER_SINCE=0
_tartci_slot_priority_mode=""
: "${TARTCI_SLOT_PRIORITY_MAX_DEFER_SECS:=300}"

# off|shadow|on, read once per supervisor (TARTCI_SLOT_PRIORITY overrides).
tartci_slot_priority_mode(){
  if [ -z "$_tartci_slot_priority_mode" ]; then
    case "${TARTCI_SLOT_PRIORITY:-}" in
      off|shadow|on) _tartci_slot_priority_mode="$TARTCI_SLOT_PRIORITY" ;;
      *)
        _tartci_slot_priority_mode="$(tartci_profile_value slot_priority 2>/dev/null)" \
          || _tartci_slot_priority_mode=off
        case "$_tartci_slot_priority_mode" in off|shadow|on) ;; *) _tartci_slot_priority_mode=off ;; esac
        ;;
    esac
  fi
  printf '%s\n' "$_tartci_slot_priority_mode"
}

tartci_slot_priority_enabled(){
  [ "$(tartci_slot_priority_mode)" != off ]
}

# Detail file for one demand class of this lane (`lane` for a single-label lane,
# else the class label). Only [A-Za-z0-9._-] reach the file name.
tartci_slot_priority_detail_file(){
  local key
  key="$(printf '%s' "${1:-lane}" | tr -c 'A-Za-z0-9._-' '_')"
  printf '%s/%s.slot-demand.%s.json\n' "$STATE_DIR" "$RUNNER_NAME" "$key"
}

# The demand class a selection belongs to: the class label at the selected tier,
# or `lane` when the lane has no tiers.
tartci_slot_priority_key(){
  local tier="${1:-0}" class=""
  if [ -n "${WORKFLOW_TIERS:-}" ]; then
    class="$(tartci_claim_delay_class "$tier" 2>/dev/null)" || class=""
  fi
  printf '%s\n' "${class:-lane}"
}

tartci_slot_priority_lane_id(){
  printf '%s\n' "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}"
}

# The value of key=<value> in a decide line, or `unknown`.
tartci_slot_priority_token(){
  local key="$1" token
  for token in $2; do
    case "$token" in "$key="*) printf '%s\n' "${token#*=}"; return 0 ;; esac
  done
  printf 'unknown\n'
}

tartci_slot_priority_withdraw(){
  SLOT_PRIORITY_DEFER_SINCE=0
  tartci_slot_priority_enabled || return 0
  python3 "$TARTCI_ROOT/scripts/slot_priority.py" withdraw \
    --lane "$(tartci_slot_priority_lane_id)" </dev/null >/dev/null 2>&1 || true
}

# tartci_slot_priority_gate <cap> <running> <selected_labels> <selected_tier>
# Returns 0 to claim (first-come, ranked first, shadow, or any failure) and 75
# when, in mode `on`, a lane ranked ahead can take the free slot. With every
# slot taken it only refreshes this lane's intent and returns 0 (the claim then
# finds the host full exactly as before).
tartci_slot_priority_gate(){
  local cap="$1" running="$2" labels="$3" tier="${4:-0}" reserved held free
  local cores mem priority line verdict now detail_file sp_fields=()
  tartci_slot_priority_enabled || return 0
  case "$cap" in ''|*[!0-9]*) return 0 ;; esac
  case "$running" in ''|*[!0-9]*) return 0 ;; esac
  reserved="$(tartci_count_reservations 2>/dev/null)" || reserved=0
  case "$reserved" in ''|*[!0-9]*) reserved=0 ;; esac
  held="$running"
  [ "$reserved" -le "$held" ] || held="$reserved"
  free=$((cap - held))
  [ "$free" -ge 0 ] || free=0
  priority="$(tartci_vm_lease_priority "$labels")"
  if ! cores="$(tartci_vm_lease_cores tart-macos 2>/dev/null)" \
     || ! tartci_positive_int_or_empty "$cores"; then
    # Without a size the store cannot tell whether this lane would boot, so it
    # is not ranked at all.
    tartci_slot_priority_withdraw
    return 0
  fi
  cores="$(tartci_vm_lease_granted_cores "$cores" "$priority")"
  mem="$(tartci_vm_lease_mem_mb tart-macos 2>/dev/null)" || mem=""
  detail_file="$(tartci_slot_priority_detail_file "$(tartci_slot_priority_key "$tier")")"
  local mem_args=()
  tartci_positive_int_or_empty "$mem" && mem_args=(--mem-mb "$mem")
  line="$(python3 "$TARTCI_ROOT/scripts/slot_priority.py" decide \
    --lane "$(tartci_slot_priority_lane_id)" --repo "$REPO" --labels "$labels" \
    --free-slots "$free" --cores "$cores" ${mem_args[@]+"${mem_args[@]}"} \
    --priority "$priority" --pid "$$" --demand-file "$detail_file" \
    </dev/null 2>/dev/null)" || line=""
  verdict="${line%% *}"
  [ -n "$line" ] || { verdict=claim; line="claim reason=helper_failed"; }
  read -r -a sp_fields <<< "${line#* }"
  case "$verdict" in
    defer)
      now="$(date +%s)"
      [ "$SLOT_PRIORITY_DEFER_SINCE" -gt 0 ] || SLOT_PRIORITY_DEFER_SINCE="$now"
      if [ $((now - SLOT_PRIORITY_DEFER_SINCE)) -ge "$TARTCI_SLOT_PRIORITY_MAX_DEFER_SECS" ]; then
        event slot_priority_defer_expired \
          "deferred $((now - SLOT_PRIORITY_DEFER_SINCE))s without the outranking lane taking the slot; claiming ${line#* }" \
          ${sp_fields[@]+"${sp_fields[@]}"} deferred_secs="$((now - SLOT_PRIORITY_DEFER_SINCE))"
        SLOT_PRIORITY_DEFER_SINCE=0
        return 0
      fi
      event slot_priority_deferred "gave the free slot to a higher-ranked lane: ${line#* }" \
        ${sp_fields[@]+"${sp_fields[@]}"} labels="$labels"
      return 75
      ;;
    claim)
      SLOT_PRIORITY_DEFER_SINCE=0
      case " $line " in
        *" reason=shadow_would_defer "*)
          event slot_priority_shadow "would have given slot to $(tartci_slot_priority_token winner_lane "$line") ($(tartci_slot_priority_token winner_repo "$line") $(tartci_slot_priority_token winner_class "$line")); claiming first-come: ${line#* }" \
            ${sp_fields[@]+"${sp_fields[@]}"} labels="$labels"
          ;;
      esac
      return 0
      ;;
    *)
      # `full`, or anything unexpected: no decision to act on.
      return 0
      ;;
  esac
}
