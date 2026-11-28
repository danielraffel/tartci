# Yield PR-class claims to a queued merge-group job (scripts/pr_class_yield.py).
# shellcheck shell=bash
#
# A merge-group job can land up to five pull requests, and a running VM cannot
# be preempted. The lane's scan is a snapshot, and a PR-class claim is selected
# exactly when the last snapshot counted no merge-group work, so a merge-group
# job queued in the meantime waits behind a 30-minute PR proof. With a lane's
# `pr_class_yield_to_merge_group = true` (TARTCI_PR_CLASS_YIELD=1), a lane about
# to start a PR-class VM makes ONE fresh GitHub read (the lane's own `gh api`,
# no cache) of its repository's queued merge_group runs. If one is queued the
# claim yields and the free slot is held for `merge_group_grace_secs`
# (TARTCI_PR_CLASS_YIELD_GRACE_SECS, default 120, bounded 30-600) by a
# placeholder reservation that every lane's slot claim counts. The hold ends
# when the run is no longer queued, when this repository's merge-group tier is
# claimed on this host, or when the grace period ends. A run id holds at most
# once per host.
#
# * unset (the default): nothing runs and no GitHub call is made.
# * PR-class tiers are the tiers ranked below the lane's `*-merge-group` tier,
#   the suffix convention refresh_after_serve uses. Merge-group, sterile and
#   every other repository are never held.
# * Fail open: any read error, timeout, rate limit or helper failure claims.
# * Cost: one call per PR-class claim attempt that finds a free slot, plus one
#   per poll while a hold is active (at most grace/poll), plus one when a hold
#   starts (job ids for the event).

TARTCI_PR_CLASS_YIELD="${TARTCI_PR_CLASS_YIELD:-0}"
case "$TARTCI_PR_CLASS_YIELD" in 1) ;; *) TARTCI_PR_CLASS_YIELD=0 ;; esac
TARTCI_PR_CLASS_YIELD_GRACE_SECS="${TARTCI_PR_CLASS_YIELD_GRACE_SECS:-120}"
case "$TARTCI_PR_CLASS_YIELD_GRACE_SECS" in
  ''|*[!0-9]*) TARTCI_PR_CLASS_YIELD_GRACE_SECS=120 ;;
esac

tartci_pr_class_yield_enabled(){
  [ "$TARTCI_PR_CLASS_YIELD" = 1 ] && [ -n "${WORKFLOW_TIERS:-}" ] \
    && [ "${ASSIGNMENT_MODE:-legacy}" != event-class-v2 ]
}

# The lane's merge-group tier label (first label ending -merge-group), or nothing.
tartci_pr_class_yield_mg_label(){
  local label
  while IFS= read -r label; do
    case "$label" in *-merge-group) printf '%s\n' "$label"; return 0 ;; esac
  done <<< "$TIER_LABELS_CONFIG"
  return 1
}

# The PR-class tier labels (the tiers after the merge-group tier), comma-joined.
tartci_pr_class_yield_pr_labels(){
  local label seen=0 out=""
  while IFS= read -r label; do
    [ -n "$label" ] || continue
    if [ "$seen" = 1 ]; then out="${out:+$out,}$label"; fi
    case "$label" in *-merge-group) seen=1 ;; esac
  done <<< "$TIER_LABELS_CONFIG"
  printf '%s\n' "$out"
}

_tartci_pr_class_yield_holder(){
  printf '%s/resv.yield.%s\n' "${TARTCI_MACOS_RESV_DIR:-$HOME/.config/tartci/macos-vm-reservations}" \
    "$(printf '%s' "$REPO" | tr -c 'A-Za-z0-9._-' '_')"
}

# Log what a helper line says ended, then the verdict's own event.
_tartci_pr_class_yield_events(){
  local line="$1" fields=() run job
  read -r -a fields <<< "${line#* }"
  run="$(tartci_slot_priority_token ended_run_id "$line")"
  job="$(tartci_slot_priority_token ended_job_ids "$line")"
  case " $line " in
    *" ended=served "*)
      event pr_class_yield_served "merge-group run $run left the queue (claimed); PR-class claims resume" \
        repo="$REPO" host="$(hostname -s 2>/dev/null || echo unknown)" run_id="$run" job_ids="$job" \
        via="$(tartci_slot_priority_token via "$line")" ;;
    *" ended=expired "*)
      event pr_class_yield_expired "merge-group run $run still queued after the grace period; PR-class claims resume" \
        repo="$REPO" host="$(hostname -s 2>/dev/null || echo unknown)" run_id="$run" job_ids="$job" ;;
  esac
  case "$line" in
    yield\ *)
      event pr_class_yield "PR-class claim yielded to queued merge-group work: ${line#* }" \
        ${fields[@]+"${fields[@]}"}
      # Make the hold active: the next selection is uncached and reads the held
      # run's jobs first (the refresh_after_serve path), so the lane's own
      # merge-group scan sees the job inside the grace period. Set here, in the
      # supervisor's own shell (the gate is never run in $(...)).
      # shellcheck disable=SC2034 # read by the supervisor loop in runner.sh
      SCAN_REFRESH_NEXT=1
      # shellcheck disable=SC2034
      SCAN_REFRESH_RUN="$(tartci_slot_priority_token run_id "$line")"
      # shellcheck disable=SC2034
      SCAN_REFRESH_CLASS=pr_class_yield
      ;;
    claim\ *)
      case "$line" in
        *" reason=read_error"*|*" reason=state_error"*|*" reason=helper_failed"*)
          _tartci_pr_class_yield_read_error "${line#* }" ;;
      esac ;;
  esac
}

# A feature that always fails to read must not stay inert and invisible: one
# pr_class_yield_read_error event per TARTCI_PR_CLASS_YIELD_ERROR_LOG_SECS
# (default 300) per lane.
_tartci_pr_class_yield_read_error(){
  local detail="$1" stamp now last=0
  stamp="${STATE_DIR:-${TMPDIR:-/tmp}}/pr-class-yield-read-error.stamp"
  now="$(date +%s)"
  [ -r "$stamp" ] && read -r last < "$stamp" 2>/dev/null || last=0
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  [ $((now - last)) -ge "${TARTCI_PR_CLASS_YIELD_ERROR_LOG_SECS:-300}" ] || return 0
  printf '%s\n' "$now" > "$stamp" 2>/dev/null || true
  event pr_class_yield_read_error "PR-class yield could not read merge-group demand (claiming as before): $detail" \
    repo="$REPO" host="$(hostname -s 2>/dev/null || echo unknown)" \
    reason="$(tartci_slot_priority_token reason "claim $detail")"
}

# tartci_pr_class_yield_gate <selected_labels> <selected_tier> <cap> <running>
# Returns 75 when the PR-class claim must wait (a hold starts or continues),
# else 0. On the merge-group tier it only releases a hold this host has.
tartci_pr_class_yield_gate(){
  local tier="${2:-0}" cap="${3:-}" running="${4:-}" mg_label pr_labels tier_label
  local holder reserved held free line workflow args=()
  tartci_pr_class_yield_enabled || return 0
  mg_label="$(tartci_pr_class_yield_mg_label)" || return 0
  tier_label="$(tartci_claim_delay_class "$tier" 2>/dev/null)" || return 0
  holder="$(_tartci_pr_class_yield_holder)"
  pr_labels="$(tartci_pr_class_yield_pr_labels)"
  case ",$pr_labels," in
    *",$tier_label,"*) ;;
    *)
    # Any tier that is not PR-class (the merge-group tier, or a higher one such
    # as a future sterile-consumer label) is about to claim: give the held slot
    # back first.
    [ -e "$holder" ] || return 0
    line="$(python3 "$TARTCI_ROOT/scripts/pr_class_yield.py" release --repo "$REPO" \
      --resv-dir "$(dirname "$holder")" --host "$(hostname -s 2>/dev/null || echo unknown)" </dev/null 2>/dev/null)" || line=""
    _tartci_pr_class_yield_events "${line:-claim}"
    return 0
    ;;
  esac
  if [ ! -e "$holder" ]; then
    # Only a claim that would find a free slot is worth a GitHub read.
    case "$cap" in ''|*[!0-9]*) return 0 ;; esac
    case "$running" in ''|*[!0-9]*) return 0 ;; esac
    reserved="$(tartci_count_reservations 2>/dev/null)" || reserved=0
    case "$reserved" in ''|*[!0-9]*) reserved=0 ;; esac
    held="$running"
    [ "$reserved" -le "$held" ] || held="$reserved"
    free=$((cap - held))
    [ "$free" -gt 0 ] || return 0
  fi
  while IFS= read -r workflow; do
    [ -n "$workflow" ] && args+=(--workflow "$workflow")
  done < <(tier_workflow_args "$mg_label" 2>/dev/null)
  line="$(python3 "$TARTCI_ROOT/scripts/pr_class_yield.py" check --repo "$REPO" \
    --resv-dir "$(dirname "$holder")" --resv-ttl "${TARTCI_MACOS_RESV_TTL:-7200}" --host "$(hostname -s 2>/dev/null || echo unknown)" \
    --grace-secs "$TARTCI_PR_CLASS_YIELD_GRACE_SECS" --owner-pid "$$" \
    ${args[@]+"${args[@]}"} </dev/null 2>/dev/null)" || line=""
  [ -n "$line" ] || line="claim reason=helper_failed:no_output"
  _tartci_pr_class_yield_events "$line"
  case "$line" in yield\ *|hold\ *) return 75 ;; esac
  return 0
}
