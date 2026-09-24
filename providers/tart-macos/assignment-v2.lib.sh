# Exclusive event-class assignment policy for the macOS JIT supervisor.
# shellcheck shell=bash

tartci_assignment_v2_configure(){
  case "$ASSIGNMENT_MODE" in
    legacy|observe|event-class-v2) ;;
    *) die "invalid TARTCI_RUNNER_ASSIGNMENT_MODE: $ASSIGNMENT_MODE (expected legacy, observe, or event-class-v2)" ;;
  esac
  [ "$ASSIGNMENT_MODE" != legacy ] || return 0
  case "${TARTCI_ASSIGNMENT_V2_CACHE_TTL_SECS:-120}" in
    ''|*[!0-9]*) die "invalid TARTCI_ASSIGNMENT_V2_CACHE_TTL_SECS" ;;
    *) [ "${TARTCI_ASSIGNMENT_V2_CACHE_TTL_SECS:-120}" -ge 120 ] \
      || die "TARTCI_ASSIGNMENT_V2_CACHE_TTL_SECS must be at least 120" ;;
  esac
  case "${TARTCI_ASSIGNMENT_V2_OBSERVE_INTERVAL_SECS:-900}" in
    ''|*[!0-9]*) die "invalid TARTCI_ASSIGNMENT_V2_OBSERVE_INTERVAL_SECS" ;;
    *) [ "${TARTCI_ASSIGNMENT_V2_OBSERVE_INTERVAL_SECS:-900}" -ge 300 ] \
      || die "TARTCI_ASSIGNMENT_V2_OBSERVE_INTERVAL_SECS must be at least 300" ;;
  esac
  case "${TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS:-0}" in
    ''|*[!0-9]*) die "invalid TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS" ;;
    *) [ "${TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS:-0}" -le 300 ] \
      || die "TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS must be at most 300" ;;
  esac
  # Work-conserving idle retarget (opt-in; 0 = off). Bounded below so an idle
  # runner is never re-observed faster than GitHub ordinarily assigns a job to
  # a fresh registration, and never more often than one exhaustive scan per
  # minute per idle slot against the host-global observation lock.
  case "$ASSIGNMENT_V2_IDLE_RETARGET_SECS" in
    ''|*[!0-9]*) die "invalid TARTCI_ASSIGNMENT_V2_IDLE_RETARGET_SECS: expected 0 or 60-3600" ;;
  esac
  if [ "$ASSIGNMENT_V2_IDLE_RETARGET_SECS" -ne 0 ]; then
    [ "$ASSIGNMENT_V2_IDLE_RETARGET_SECS" -ge 60 ] \
      && [ "$ASSIGNMENT_V2_IDLE_RETARGET_SECS" -le 3600 ] \
      || die "TARTCI_ASSIGNMENT_V2_IDLE_RETARGET_SECS must be 0 or 60-3600"
  fi
  [ -n "$WORKFLOW_TIERS" ] \
    || die "$ASSIGNMENT_MODE assignment mode requires TARTCI_RUNNER_WORKFLOW_TIERS"
  ASSIGNMENT_V2_BASE_LABELS="$(python3 - "$LABELS" "$ASSIGNMENT_V2_OMIT_LABELS" "$ASSIGNMENT_V2_CLASS_LABELS" <<'PY'
import sys

configured = [item.strip() for item in sys.argv[1].split(",") if item.strip()]
omitted = {item.strip().lower() for item in sys.argv[2].split(",") if item.strip()}
classes = {item.strip().lower() for item in sys.argv[3].split(",") if item.strip()}
print(",".join(item for item in configured if item.lower() not in omitted | classes))
PY
)"
  [ -n "$ASSIGNMENT_V2_BASE_LABELS" ] \
    || die "V2 assignment omitted every configured runner label"
  while IFS= read -r required_omit; do
    [ -n "$required_omit" ] || continue
    case ",$ASSIGNMENT_V2_BASE_LABELS," in
      *",$required_omit,"*)
        die "V2 assignment retained required legacy selector label: $required_omit"
        ;;
    esac
  done < <(printf '%s\n' "$ASSIGNMENT_V2_REQUIRED_OMIT_LABELS" | tr ',' '\n')
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    case ",$ASSIGNMENT_V2_CLASS_LABELS," in
      *",$tier_label,"*) ;;
      *) die "V2 workflow tier is not an allowed assignment class: $tier_label" ;;
    esac
  done <<< "$TIER_LABELS_CONFIG"
}

tartci_assignment_v2_tier_labels(){
  local tier_label="$1"
  printf '%s,%s\n' "$ASSIGNMENT_V2_BASE_LABELS" "$tier_label"
}

# Assignment admission needs a complete current view. The dedicated scanner
# consumes every run/job page and fails on API uncertainty or truncation. Its
# explicit require-label predicate rejects generic-only jobs.
# The scanner stops at the first matching job and reports 1, because every
# admission decision only asks whether demand exists. Pass exhaustive=1 to buy
# the true magnitude instead; only reporting needs it, and it costs a full scan.
tartci_assignment_v2_tier_demand(){
  local tier_label="$1" exhaustive="${2:-0}" min_age="${3:-$MIN_QUEUED_AGE}"
  local workflow tier_args=() selected_labels
  local error_file detail rc count_args=() evidence
  selected_labels="$(tartci_assignment_v2_tier_labels "$tier_label")"
  # bash 3.2 (the macOS system shell) treats an empty "${a[@]}" as an unbound
  # variable under `set -u`, so the expansion must be guarded, not just quoted.
  [ "$exhaustive" = 1 ] && count_args=(--exhaustive-count)
  while IFS= read -r workflow; do
    [ -n "$workflow" ] && tier_args+=(--workflow "$workflow")
  done < <(tier_workflow_args "$tier_label")
  [ "${#tier_args[@]}" -gt 0 ] || return 1
  mkdir -p "$STATE_DIR"
  error_file="$(mktemp "$STATE_DIR/$RUNNER_NAME.assignment-scan.XXXXXX")" || return 1
  if python3 "$TARTCI_ROOT/scripts/assignment_scan.py" \
    --repo "$REPO" \
    "${tier_args[@]}" \
    --labels "$selected_labels" \
    --require-label "$tier_label" \
    --min-age-seconds "$min_age" \
    ${count_args[@]+"${count_args[@]}"} \
    --gh-cli "$GH_CLI" 2>"$error_file"; then
    rc=0
  else
    rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    # Keep BOTH ends: a wrapper prints the underlying cause BEFORE its own
    # summary, so any tail-only rule discards the line that identifies the real
    # fault and keeps the one that misattributes it. The head/tail budget is
    # sized so the 512-byte event field cannot chop the summary back off.
    # Publish the same text for the supervisor's blind path to report.
    detail="$(scan_diagnostic_digest "$error_file" 2 2 110 | tr '\n' '|' \
      | sed 's/|$//' | cut -c1-512)"
    record_scan_error "$detail"
    event assignment_scan_error \
      "tier=$tier_label scanner_rc=$rc detail=${detail:-no scanner detail}"
    if [ "$exhaustive" != 1 ] \
       && tartci_assignment_feed_rescue "$tier_label" "$selected_labels" "$min_age"; then
      rm -f "$error_file"
      return 0
    fi
  fi
  # The scanner's stderr is captured so it cannot pollute the demand count on
  # stdout, and then deleted. Evidence written there is therefore invisible
  # unless it is lifted out here: on the SUCCESS path the file is discarded
  # entirely, which is exactly the path a stale run is detected on. Promote each
  # stale-demand line to a typed event before the file goes away.
  while IFS= read -r evidence; do
    [ -n "$evidence" ] || continue
    event assignment_stale_demand \
      "tier=$tier_label detail=$(printf '%s' "$evidence" | cut -c1-512)"
  done < <(grep '^stale-demand: ' "$error_file" 2>/dev/null | sed 's/^stale-demand: //')
  rm -f "$error_file"
  return "$rc"
}

# Second opinion for a scan that already failed closed, from a source that is
# not the GitHub REST API: the local Shipyard daemon's webhook push feed.
#
# It runs ONLY after the scan failed, so a healthy lane never reaches it and no
# feed defect can regress one. It can only ever turn a blind poll into "there
# is demand" -- the feed cannot observe absence (a severed feed and an empty
# queue are the same silence), so a refusal leaves the blind result exactly as
# the scanner left it and the supervisor's existing scan-blind handling runs
# unchanged. The exhaustive caller is excluded because it wants a magnitude,
# and a replay ring is not a queue census.
#
# Every outcome is announced. A feed that is failing must not look like a lane
# that is merely quiet.
tartci_assignment_feed_rescue(){
  local tier_label="$1" selected_labels="$2" min_age="${3:-$MIN_QUEUED_AGE}"
  local out err_file reason rc socket_arg=()
  [ "${TARTCI_ASSIGNMENT_FEED_RESCUE:-0}" = 1 ] || return 1
  if [ -n "${TARTCI_SHIPYARD_DAEMON_SOCKET:-}" ]; then
    socket_arg=(--socket "$TARTCI_SHIPYARD_DAEMON_SOCKET")
  fi
  mkdir -p "$STATE_DIR"
  err_file="$(mktemp "$STATE_DIR/$RUNNER_NAME.feed-rescue.XXXXXX")" || return 1
  if out="$(python3 "$TARTCI_ROOT/scripts/shipyard_event_feed.py" \
    --repo "$REPO" \
    --require-label "$tier_label" \
    --labels "$selected_labels" \
    --min-observed-age-seconds "$min_age" \
    --ledger "$STATE_DIR/$RUNNER_NAME.feed-ledger.json" \
    ${socket_arg[@]+"${socket_arg[@]}"} 2>"$err_file")"; then
    rc=0
  else
    rc=$?
  fi
  reason="$(tail -n 1 "$err_file" | cut -c1-512)"
  rm -f "$err_file"
  if [ "$rc" -ne 0 ] || ! printf '%s' "$out" | grep -qxE '[1-9][0-9]*'; then
    event assignment_feed_degraded \
      "tier=$tier_label feed_rc=$rc detail=${reason:-no feed detail}"
    return 1
  fi
  event assignment_feed_rescue \
    "tier=$tier_label detail=${reason:-feed observed demand}"
  printf '%s\n' "$out"
  return 0
}

# Print `count|registration labels|zero-based tier`. A scan error at any tier is
# fail-closed: never skip a blind higher class and hand its capacity to a lower
# one. A failed scan leaves that class's demand UNKNOWN, not zero, and the two
# must not converge here -- electing a lower class would mint a runner that
# cannot serve the blind one, and the numeric verdict would clear the
# supervisor's scan-blind counter, disabling the very self-heal that recovers
# the blind scan. `ERR` carries the uncertainty out intact instead.
tartci_assignment_v2_select_live(){
  local tier_label q tier=0
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    if ! q="$(tartci_assignment_v2_tier_demand "$tier_label")"; then
      printf 'ERR|%s|%s\n' "$ASSIGNMENT_V2_BASE_LABELS" "$tier"
      return 0
    fi
    printf '%s' "$q" | grep -qxE '[0-9]+' || {
      printf 'ERR|%s|%s\n' "$ASSIGNMENT_V2_BASE_LABELS" "$tier"
      return 0
    }
    if [ "$q" -gt 0 ]; then
      printf '%s|%s|%s\n' \
        "$q" "$(tartci_assignment_v2_tier_labels "$tier_label")" "$tier"
      return 0
    fi
    tier=$((tier + 1))
  done <<< "$TIER_LABELS_CONFIG"
  printf '0|%s|%s\n' "$ASSIGNMENT_V2_BASE_LABELS" "$tier"
}

tartci_assignment_v2_read_fresh_selection(){
  local max_age="$1" now cache_file cached_at cached_value age
  [ "$max_age" -gt 0 ] || return 1
  now="$(date +%s)"
  cache_file="$STATE_DIR/$RUNNER_NAME.assignment-v2-selection.cache"
  [ -r "$cache_file" ] || return 1
  IFS=$'\t' read -r cached_at cached_value < "$cache_file" || return 1
  case "$cached_at" in ''|*[!0-9]*) return 1;; esac
  age=$((now - cached_at))
  [ "$age" -ge 0 ] && [ "$age" -lt "$max_age" ] && [ -n "$cached_value" ] \
    || return 1
  printf '%s\n' "$cached_value"
}

tartci_assignment_v2_select(){
  local force_refresh="${1:-0}" ttl now cache_file cached_value tmp
  ttl="${TARTCI_ASSIGNMENT_V2_CACHE_TTL_SECS:-120}"
  cache_file="$STATE_DIR/$RUNNER_NAME.assignment-v2-selection.cache"
  if [ "$force_refresh" != 1 ] \
     && cached_value="$(tartci_assignment_v2_read_fresh_selection "$ttl")"; then
    printf '%s\n' "$cached_value"
    return 0
  fi
  cached_value="$(tartci_assignment_v2_select_live)"
  # Publish only a real observation. A blind selection (`ERR`) is the ABSENCE of
  # an observation, never an observation of absence, so it must not enter the
  # cache: a cached blind verdict is replayed for the whole TTL, during which the
  # lane makes no GitHub call at all and therefore cannot recover on the next
  # poll, and a supervisor that restarts for fresh credentials re-reads the same
  # stale verdict from disk. Leaving the cache untouched keeps the fail-closed
  # answer for this poll while letting the next one re-observe.
  # TTL begins when the exhaustive observation completes, not before lock
  # contention and API pagination. Backdating this stamp can make a fresh
  # snapshot immediately expire and recreate the scan burst it should prevent.
  if printf '%s' "${cached_value%%|*}" | grep -qxE '[0-9]+'; then
    now="$(date +%s)"
    mkdir -p "$STATE_DIR"
    if tmp="$(mktemp "$cache_file.tmp.XXXXXX")"; then
      printf '%s\t%s\n' "$now" "$cached_value" > "$tmp"
      mv -f "$tmp" "$cache_file"
    fi
  fi
  printf '%s\n' "$cached_value"
}

tartci_assignment_v2_invalidate_selection(){
  rm -f "$STATE_DIR/$RUNNER_NAME.assignment-v2-selection.cache"
}

tartci_assignment_v2_observe(){
  local interval now stamp_file last=0 tmp selection
  interval="${TARTCI_ASSIGNMENT_V2_OBSERVE_INTERVAL_SECS:-900}"
  now="$(date +%s)"
  stamp_file="$STATE_DIR/$RUNNER_NAME.assignment-v2-observe.last"
  [ ! -r "$stamp_file" ] || read -r last < "$stamp_file" || last=0
  case "$last" in ''|*[!0-9]*) last=0;; esac
  [ $((now - last)) -ge "$interval" ] || return 0
  mkdir -p "$STATE_DIR"
  if tmp="$(mktemp "$stamp_file.tmp.XXXXXX")"; then
    printf '%s\n' "$now" > "$tmp"
    mv -f "$tmp" "$stamp_file"
  fi
  selection="$(tartci_assignment_v2_select 1)"
  printf '%s\n' "$selection"
}

tartci_assignment_v2_total_demand(){
  local tier_label q total=0
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    q="$(tartci_assignment_v2_tier_demand "$tier_label" 1)" || {
      printf 'ERR\n'
      return 0
    }
    printf '%s' "$q" | grep -qxE '[0-9]+' || {
      printf 'ERR\n'
      return 0
    }
    total=$((total + q))
  done <<< "$TIER_LABELS_CONFIG"
  printf '%s\n' "$total"
}

tartci_assignment_v2_parity(){
  local legacy v2
  legacy="$(ASSIGNMENT_MODE=legacy; select_work)"
  v2="$(tartci_assignment_v2_select 1)"
  printf 'legacy=%s\tv2=%s\n' "$legacy" "$v2"
}

# Succeed only while the selected class still has demand and every higher class
# is empty. Lower tiers always re-observe exhaustively and live. A profile may
# let tier zero reuse its own bounded, exact-class exhaustive receipt because no
# higher class can arrive above it; a stale/malformed receipt falls back to the
# live fail-closed scan.
tartci_assignment_v2_pre_mint_valid(){
  local selected_tier="$1" tier_label q tier=0 cached cached_q cached_labels
  local cached_tier cached_extra top_label expected_labels max_age
  max_age="${TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS:-0}"
  if [ "$selected_tier" = 0 ] && [ "$max_age" -gt 0 ]; then
    cached="$(tartci_assignment_v2_read_fresh_selection "$max_age")" || cached=""
    IFS='|' read -r cached_q cached_labels cached_tier cached_extra <<< "$cached"
    top_label="${TIER_LABELS_CONFIG%%$'\n'*}"
    expected_labels="$(tartci_assignment_v2_tier_labels "$top_label")"
    case "$cached_q" in ''|*[!0-9]*) cached_q=0;; esac
    if [ "$cached_q" -gt 0 ] \
       && [ "$cached_labels" = "$expected_labels" ] \
       && [ "$cached_tier" = 0 ] \
       && [ -z "$cached_extra" ]; then
      event assignment_v2_pre_mint_receipt \
        "selected_tier=0 max_age_seconds=$max_age"
      return 0
    fi
  fi
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    [ "$tier" -le "$selected_tier" ] || break
    q="$(tartci_assignment_v2_tier_demand "$tier_label")" || return 1
    printf '%s' "$q" | grep -qxE '[0-9]+' || return 1
    if [ "$tier" -lt "$selected_tier" ]; then
      [ "$q" -eq 0 ] || return 1
    else
      [ "$q" -gt 0 ] || return 1
    fi
    tier=$((tier + 1))
  done <<< "$TIER_LABELS_CONFIG"
  [ "$tier" -gt "$selected_tier" ]
}

# A denied pre-mint check proves the cached selection is no longer authority:
# the selected job was claimed/cancelled, a higher class arrived, or GitHub was
# uncertain. Drop that cache before returning so the supervisor's next pass
# performs a live scan and can immediately fall through to another eligible
# class instead of repeatedly booting for the stale class until the TTL expires.
tartci_assignment_v2_pre_mint_admit(){
  local selected_tier="$1"
  if tartci_assignment_v2_pre_mint_valid "$selected_tier"; then
    return 0
  fi
  tartci_assignment_v2_invalidate_selection
  return 1
}

# Work-conserving idle retarget. A registered JIT runner advertises exactly one
# event class and can serve nothing else, so a runner that GitHub has not
# assigned within the interval is either about to receive a job of its own
# class or is holding a governed slot for a class nobody is waiting in. The
# static split alone cannot tell those apart, and the observed cost is a slot
# idle for the full idle timeout while the other class queues behind it.
#
# Succeed (retarget) only on POSITIVE evidence of both halves: the runner's own
# class has NO queued job at all, and some other class has admissible demand.
# The own-class probe is deliberately age-agnostic -- the lane's minimum queued
# age is a boot-delay policy, not an assignment restriction, and GitHub will
# hand a young job of the runner's class to this already-registered runner in
# seconds, which is strictly better than discarding it. Any scan uncertainty
# holds: a runner discarded on a blind reading could strand the very work it
# was minted for, and a held runner still reaches the bounded idle timeout.
#
# Class preference is not decided here. Discarding returns the supervisor to
# its ordered selection, so when both classes wait the top tier still wins.
tartci_assignment_v2_idle_retarget(){
  local selected_tier="$1" idle_elapsed="${2:-0}" tier_label q tier=0 own_label=""
  local other_tier="" other_label="" other_q=""
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    if [ "$tier" -eq "$selected_tier" ]; then
      own_label="$tier_label"
      break
    fi
    tier=$((tier + 1))
  done <<< "$TIER_LABELS_CONFIG"
  [ -n "$own_label" ] || return 1
  if ! q="$(tartci_assignment_v2_tier_demand "$own_label" 0 0)" \
     || ! printf '%s' "$q" | grep -qxE '[0-9]+'; then
    event assignment_v2_idle_hold \
      "selected_tier=$selected_tier elapsed=${idle_elapsed}s reason=own_class_uncertain"
    return 1
  fi
  if [ "$q" -gt 0 ]; then
    event assignment_v2_idle_hold \
      "selected_tier=$selected_tier elapsed=${idle_elapsed}s reason=own_class_demand"
    return 1
  fi
  tier=0
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    if [ "$tier" -ne "$selected_tier" ]; then
      if ! q="$(tartci_assignment_v2_tier_demand "$tier_label")" \
         || ! printf '%s' "$q" | grep -qxE '[0-9]+'; then
        event assignment_v2_idle_hold \
          "selected_tier=$selected_tier elapsed=${idle_elapsed}s reason=other_class_uncertain tier=$tier"
        return 1
      fi
      if [ "$q" -gt 0 ] && [ -z "$other_label" ]; then
        other_tier="$tier"; other_label="$tier_label"; other_q="$q"
      fi
    fi
    tier=$((tier + 1))
  done <<< "$TIER_LABELS_CONFIG"
  if [ -z "$other_label" ]; then
    event assignment_v2_idle_hold \
      "selected_tier=$selected_tier elapsed=${idle_elapsed}s reason=no_other_demand"
    return 1
  fi
  event assignment_v2_idle_retarget \
    "selected_tier=$selected_tier elapsed=${idle_elapsed}s to_tier=$other_tier to_label=$other_label queued=$other_q"
  return 0
}
