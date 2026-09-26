# Exclusive event-class assignment policy for the macOS JIT supervisor.
# shellcheck shell=bash

tartci_assignment_v2_configure(){
  case "$ASSIGNMENT_MODE" in
    legacy|observe|event-class-v2) ;;
    *) die "invalid TARTCI_RUNNER_ASSIGNMENT_MODE: $ASSIGNMENT_MODE (expected legacy, observe, or event-class-v2)" ;;
  esac
  # A preference order is an event-class-v2 decision. Refuse it elsewhere rather
  # than silently ignoring it, so a mis-rendered slot cannot look configured.
  [ -z "$ASSIGNMENT_V2_TIER_ORDER" ] || [ "$ASSIGNMENT_MODE" = event-class-v2 ] \
    || die "TARTCI_ASSIGNMENT_V2_TIER_ORDER requires event-class-v2 assignment mode"
  tartci_fallback_configure
  ASSIGNMENT_V2_ORDER_LABELS="$TIER_LABELS_CONFIG"
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
  tartci_assignment_v2_configure_order
}

# Resolve the slot's class preference order. Empty keeps the configured tier
# order exactly. Otherwise it must name every configured class exactly once:
# dropping a class would idle the slot while that class waits, which is the
# opposite of the work-conserving contract this knob exists to keep.
tartci_assignment_v2_configure_order(){
  local item seen="" count=0 expected=0 tier_label
  ASSIGNMENT_V2_ORDER_LABELS="$TIER_LABELS_CONFIG"
  [ -n "$ASSIGNMENT_V2_TIER_ORDER" ] || return 0
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] && expected=$((expected + 1))
  done <<< "$TIER_LABELS_CONFIG"
  while IFS= read -r item; do
    item="$(printf '%s' "$item" | tr -d '[:space:]')"
    [ -n "$item" ] || die "TARTCI_ASSIGNMENT_V2_TIER_ORDER contains an empty class"
    printf '%s\n' "$TIER_LABELS_CONFIG" | grep -Fxq "$item" \
      || die "TARTCI_ASSIGNMENT_V2_TIER_ORDER names an unconfigured class: $item"
    if [ -n "$seen" ] && printf '%s\n' "$seen" | grep -Fxq "$item"; then
      die "TARTCI_ASSIGNMENT_V2_TIER_ORDER repeats class: $item"
    fi
    seen="${seen:+$seen
}$item"
    count=$((count + 1))
  done < <(printf '%s\n' "$ASSIGNMENT_V2_TIER_ORDER" | tr ',' '\n')
  [ "$count" -eq "$expected" ] \
    || die "TARTCI_ASSIGNMENT_V2_TIER_ORDER must name every configured class exactly once"
  ASSIGNMENT_V2_ORDER_LABELS="$seen"
}

# Configured (zero-based) tier index of a class label. Tier numbers keep this one
# meaning whatever the slot's preference order, so events, runner groups and
# lease priority never change meaning under a reordered slot.
tartci_assignment_v2_tier_index(){
  local wanted="$1" tier_label tier=0
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    if [ "$tier_label" = "$wanted" ]; then
      printf '%s\n' "$tier"
      return 0
    fi
    tier=$((tier + 1))
  done <<< "$TIER_LABELS_CONFIG"
  return 1
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
#
# Classes are consulted in the slot's preference order (the configured order
# unless TARTCI_ASSIGNMENT_V2_TIER_ORDER reorders it); the printed tier is always
# the configured index. A preferred class with no demand falls through to the
# next, so a reordered slot is still work-conserving.
tartci_assignment_v2_select_live(){
  local tier_label q tier count=0
  # A fallback grant is authority for exactly the live selection that made
  # it. Drop the previous one before observing again.
  tartci_fallback_clear_grant
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    tier="$(tartci_assignment_v2_tier_index "$tier_label")" || tier="$count"
    count=$((count + 1))
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
    # No demand old enough for this lane's minimum age. A fallback lane may
    # still take young demand the preferred hosts cannot cover right now.
    if tartci_fallback_grant "$tier_label" "$tier"; then
      printf '%s|%s|%s\n' \
        "$FALLBACK_DEMAND" "$(tartci_assignment_v2_tier_labels "$tier_label")" "$tier"
      return 0
    fi
  done <<< "$ASSIGNMENT_V2_ORDER_LABELS"
  printf '0|%s|%s\n' "$ASSIGNMENT_V2_BASE_LABELS" "$count"
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

# Succeed only while the selected class still has demand and every class the
# slot PREFERS over it is empty. "Higher" is the slot's preference order, so a
# PR-first slot's PR-head mint is never denied merely because merge-group work
# exists, while its merge-group fallback still yields to PR-head arrival. Lower
# classes always re-observe exhaustively and live. A profile may let the slot's
# most-preferred class reuse its own bounded, exact-class exhaustive receipt
# because nothing can arrive above it; a stale/malformed receipt falls back to
# the live fail-closed scan.
tartci_assignment_v2_pre_mint_valid(){
  local selected_tier="$1" tier_label q tier cached cached_q cached_labels
  local cached_tier cached_extra top_label top_tier expected_labels max_age min_age
  max_age="${TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS:-0}"
  top_label="${ASSIGNMENT_V2_ORDER_LABELS%%$'\n'*}"
  top_tier="$(tartci_assignment_v2_tier_index "$top_label")" || return 1
  if [ "$selected_tier" = "$top_tier" ] && [ "$max_age" -gt 0 ]; then
    cached="$(tartci_assignment_v2_read_fresh_selection "$max_age")" || cached=""
    IFS='|' read -r cached_q cached_labels cached_tier cached_extra <<< "$cached"
    expected_labels="$(tartci_assignment_v2_tier_labels "$top_label")"
    case "$cached_q" in ''|*[!0-9]*) cached_q=0;; esac
    if [ "$cached_q" -gt 0 ] \
       && [ "$cached_labels" = "$expected_labels" ] \
       && [ "$cached_tier" = "$top_tier" ] \
       && [ -z "$cached_extra" ]; then
      event assignment_v2_pre_mint_receipt \
        "selected_tier=$selected_tier max_age_seconds=$max_age"
      return 0
    fi
  fi
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    tier="$(tartci_assignment_v2_tier_index "$tier_label")" || return 1
    # A class booted on a fallback grant is re-observed at the age the grant
    # was made at (0), so the job that justified the boot is still visible.
    min_age="$MIN_QUEUED_AGE"
    [ "$tier" != "$selected_tier" ] || min_age="$(tartci_fallback_min_age "$tier")"
    q="$(tartci_assignment_v2_tier_demand "$tier_label" 0 "$min_age")" || return 1
    printf '%s' "$q" | grep -qxE '[0-9]+' || return 1
    if [ "$tier" = "$selected_tier" ]; then
      [ "$q" -gt 0 ]
      return
    fi
    [ "$q" -eq 0 ] || return 1
  done <<< "$ASSIGNMENT_V2_ORDER_LABELS"
  return 1
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
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    tier="$(tartci_assignment_v2_tier_index "$tier_label")" || return 1
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
  done <<< "$ASSIGNMENT_V2_ORDER_LABELS"
  if [ -z "$other_label" ]; then
    event assignment_v2_idle_hold \
      "selected_tier=$selected_tier elapsed=${idle_elapsed}s reason=no_other_demand"
    return 1
  fi
  event assignment_v2_idle_retarget \
    "selected_tier=$selected_tier elapsed=${idle_elapsed}s to_tier=$other_tier to_label=$other_label queued=$other_q"
  return 0
}

# ── Fallback lane (opt-in) ──────────────────────────────────────────────────
#
# A fallback lane leaves young work to its preferred hosts. Its minimum queued
# age says how long it waits; the fallback says whether it needs to wait at
# all. For demand the age rule still hides, the lane asks every preferred host
# (`tartci pool supply` over SSH, see scripts/gate_supply.py) how many free,
# leasable gate slots it has for that class, and boots now only when queued
# demand exceeds what the preferred hosts and this host's sibling lanes already
# cover. Every uncertainty (a failed scan, an unreachable or stale peer, an
# unreadable sibling) keeps the age rule, so the lane never boots LATER than
# it would without the fallback, and never boots early on a guess.
FALLBACK_DEMAND=0
FALLBACK_GRANT_TTL=900

tartci_fallback_configure(){
  [ -n "$FALLBACK_PEERS" ] || return 0
  [ "$ASSIGNMENT_MODE" = event-class-v2 ] \
    || die "TARTCI_FALLBACK_PEERS requires event-class-v2 assignment mode"
  [ "$MIN_QUEUED_AGE" -gt 0 ] \
    || die "TARTCI_FALLBACK_PEERS requires TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS > 0 (it only shortens that wait)"
  case "$FALLBACK_PEER_MAX_AGE" in
    ''|*[!0-9]*) die "invalid TARTCI_FALLBACK_PEER_MAX_AGE_SECS: expected 30-300" ;;
  esac
  [ "$FALLBACK_PEER_MAX_AGE" -ge 30 ] && [ "$FALLBACK_PEER_MAX_AGE" -le 300 ] \
    || die "TARTCI_FALLBACK_PEER_MAX_AGE_SECS must be 30-300"
}

tartci_fallback_enabled(){
  [ -n "$FALLBACK_PEERS" ] && [ "$ASSIGNMENT_MODE" = event-class-v2 ] \
    && [ "$MIN_QUEUED_AGE" -gt 0 ]
}

tartci_fallback_grant_file(){
  printf '%s/%s.fallback-grant\n' "$STATE_DIR" "$RUNNER_NAME"
}

tartci_fallback_clear_grant(){
  rm -f "$(tartci_fallback_grant_file)"
}

# Minimum queued age for the pre-mint recheck of <tier>: 0 while a fresh
# fallback grant for exactly that tier stands, else the lane's own minimum.
tartci_fallback_min_age(){
  local tier="$1" file granted_tier granted_at now
  file="$(tartci_fallback_grant_file)"
  if tartci_fallback_enabled && [ -r "$file" ] \
     && read -r granted_tier granted_at < "$file"; then
    now="$(date +%s)"
    case "$granted_at" in ''|*[!0-9]*) granted_at=0 ;; esac
    if [ "$granted_tier" = "$tier" ] && [ $((now - granted_at)) -le "$FALLBACK_GRANT_TTL" ]; then
      printf '0\n'
      return 0
    fi
  fi
  printf '%s\n' "$MIN_QUEUED_AGE"
}

# Print `<verdict> <detail>` for young demand of one class, where verdict is
# grant, hold or unknown; `none` when there is no young demand and `off` when
# the policy is disabled. Sets FALLBACK_DEMAND to the young demand count.
tartci_fallback_decision(){
  local tier_label="$1" young line
  FALLBACK_DEMAND=0
  tartci_fallback_enabled || { printf 'off\n'; return 0; }
  # Exhaustive and age-agnostic: the decision compares a MAGNITUDE of demand
  # with the peers' free slots, which the early-stopping scan cannot supply.
  if ! young="$(tartci_assignment_v2_tier_demand "$tier_label" 1 0)" \
     || ! printf '%s' "$young" | grep -qxE '[0-9]+'; then
    printf 'unknown young-demand scan failed\n'
    return 0
  fi
  [ "$young" -gt 0 ] || { printf 'none no young demand\n'; return 0; }
  FALLBACK_DEMAND="$young"
  line="$(python3 "$TARTCI_ROOT/scripts/gate_supply.py" decide \
    --repo "$REPO" --class "$tier_label" --demand "$young" \
    --peers "$FALLBACK_PEERS" --slot "$SLOT" --state-dir "$STATE_DIR" \
    --max-age-seconds "$FALLBACK_PEER_MAX_AGE" 2>/dev/null)" || line=""
  case "$line" in
    grant\ *|hold\ *|unknown\ *) printf '%s\n' "$line" ;;
    *) printf 'unknown decision helper failed\n' ;;
  esac
}

tartci_fallback_decision_for_tier(){
  local wanted="$1" tier_label tier=0
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    if [ "$tier" = "$wanted" ]; then
      tartci_fallback_decision "$tier_label"
      return 0
    fi
    tier=$((tier + 1))
  done <<< "$TIER_LABELS_CONFIG"
  printf 'unknown no tier %s\n' "$wanted"
}

# Succeed (and record the grant) when this lane should boot now for young
# demand of <tier_label>. Every non-grant keeps the minimum-age rule.
tartci_fallback_grant(){
  local tier_label="$1" tier="$2" line verdict detail tmp file
  tartci_fallback_enabled || return 1
  line="$(tartci_fallback_decision "$tier_label")"
  verdict="${line%% *}"
  detail="${line#* }"
  case "$verdict" in
    grant)
      file="$(tartci_fallback_grant_file)"
      mkdir -p "$STATE_DIR"
      if tmp="$(mktemp "$file.tmp.XXXXXX")"; then
        printf '%s %s\n' "$tier" "$(date +%s)" > "$tmp"
        mv -f "$tmp" "$file"
      fi
      FALLBACK_DEMAND="$(printf '%s' "$detail" | sed -n 's/.*demand=\([0-9][0-9]*\).*/\1/p')"
      case "$FALLBACK_DEMAND" in ''|*[!0-9]*|0) FALLBACK_DEMAND=1 ;; esac
      event fallback_grant "tier=$tier label=$tier_label min_queued_age=${MIN_QUEUED_AGE}s $detail"
      return 0
      ;;
    hold) event fallback_hold "tier=$tier label=$tier_label $detail" ;;
    unknown) event fallback_unknown "tier=$tier label=$tier_label $detail" ;;
  esac
  return 1
}
