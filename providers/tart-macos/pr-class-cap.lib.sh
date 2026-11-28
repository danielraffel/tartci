# PR-class cap while the merge group is queued (scripts/pr_class_cap.py).
# shellcheck shell=bash
#
# A merge-group job can land up to five pull requests, and a running VM cannot
# be preempted, so the only way to get merge-group work onto a host sooner is
# to not START another PR-class VM there. With a lane's
# `pr_class_cap_while_merge_group_queued = N` (TARTCI_PR_CLASS_CAP_WHILE_MG), a
# lane about to claim a slot for a PR-class tier asks the helper first. It is
# capped only when the lane's merge-group tier scan (its detail file) counted
# queued merge_group jobs and the host already runs N or more live VM leases for
# this repository's PR-class tiers. Merge-group, every other tier and every
# other repository are never capped.
#
# * unset or 0 (the default): nothing runs, no scan gets a detail file for it,
#   and the loop takes exactly the decisions it always did.
# * PR-class tiers are the tiers ranked below the lane's `*-merge-group` tier,
#   the same suffix convention refresh_after_serve uses.
# * Fail open: an unreadable or stale detail file, an unreadable lease store or
#   any helper failure claims. Merge-group demand queued longer than
#   TARTCI_PR_CLASS_CAP_MAX_WAIT_SECS (default 3600) stops capping, so a run
#   that can never run cannot hold the cap forever.
# * Non-preemptive: it never touches a running VM, a lease or a reservation,
#   and makes no GitHub call.

TARTCI_PR_CLASS_CAP_WHILE_MG="${TARTCI_PR_CLASS_CAP_WHILE_MG:-0}"
case "$TARTCI_PR_CLASS_CAP_WHILE_MG" in
  ''|*[!0-9]*) TARTCI_PR_CLASS_CAP_WHILE_MG=0 ;;
esac

tartci_pr_class_cap_enabled(){
  [ "$TARTCI_PR_CLASS_CAP_WHILE_MG" -gt 0 ] && [ -n "${WORKFLOW_TIERS:-}" ] \
    && [ "${ASSIGNMENT_MODE:-legacy}" != event-class-v2 ]
}

# The lane's merge-group tier label (first label ending -merge-group), or nothing.
tartci_pr_class_cap_mg_label(){
  local label
  while IFS= read -r label; do
    case "$label" in *-merge-group) printf '%s\n' "$label"; return 0 ;; esac
  done <<< "$TIER_LABELS_CONFIG"
  return 1
}

# The PR-class tier labels (the tiers after the merge-group tier), comma-joined.
tartci_pr_class_cap_pr_labels(){
  local label seen=0 out=""
  while IFS= read -r label; do
    [ -n "$label" ] || continue
    if [ "$seen" = 1 ]; then out="${out:+$out,}$label"; fi
    case "$label" in *-merge-group) seen=1 ;; esac
  done <<< "$TIER_LABELS_CONFIG"
  printf '%s\n' "$out"
}

# tartci_pr_class_cap_gate <selected_labels> <selected_tier>
# Returns 75 when the claim for a PR-class tier is capped, else 0.
tartci_pr_class_cap_gate(){
  local labels="$1" tier="${2:-0}" mg_label pr_labels tier_label line verdict fields=()
  tartci_pr_class_cap_enabled || return 0
  mg_label="$(tartci_pr_class_cap_mg_label)" || return 0
  pr_labels="$(tartci_pr_class_cap_pr_labels)"
  tier_label="$(tartci_claim_delay_class "$tier" 2>/dev/null)" || return 0
  case ",$pr_labels," in *",$tier_label,"*) ;; *) return 0 ;; esac
  line="$(python3 "$TARTCI_ROOT/scripts/pr_class_cap.py" \
    --repo "$REPO" --host "$(hostname -s 2>/dev/null || echo unknown)" \
    --cap "$TARTCI_PR_CLASS_CAP_WHILE_MG" --pr-labels "$pr_labels" \
    --mg-detail-file "$(tartci_slot_priority_detail_file "$mg_label")" \
    --max-wait-secs "${TARTCI_PR_CLASS_CAP_MAX_WAIT_SECS:-3600}" \
    </dev/null 2>/dev/null)" || line=""
  verdict="${line%% *}"
  [ "$verdict" = cap ] || return 0
  read -r -a fields <<< "${line#* }"
  event pr_class_capped "PR-class VM not started while merge-group work is queued: ${line#* }" \
    ${fields[@]+"${fields[@]}"} labels="$labels"
  return 75
}
