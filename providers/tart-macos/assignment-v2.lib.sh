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
tartci_assignment_v2_tier_demand(){
  local tier_label="$1" workflow tier_args=() selected_labels error_file detail rc
  selected_labels="$(tartci_assignment_v2_tier_labels "$tier_label")"
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
    --min-age-seconds "$MIN_QUEUED_AGE" \
    --gh-cli "$GH_CLI" 2>"$error_file"; then
    rc=0
  else
    rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    detail="$(tail -n 1 "$error_file" | cut -c1-512)"
    event assignment_scan_error \
      "tier=$tier_label scanner_rc=$rc detail=${detail:-no scanner detail}"
  fi
  rm -f "$error_file"
  return "$rc"
}

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

tartci_assignment_v2_select(){
  local force_refresh="${1:-0}" ttl now cache_file cached_at cached_value tmp
  ttl="${TARTCI_ASSIGNMENT_V2_CACHE_TTL_SECS:-120}"
  now="$(date +%s)"
  cache_file="$STATE_DIR/$RUNNER_NAME.assignment-v2-selection.cache"
  if [ "$force_refresh" != 1 ] && [ -r "$cache_file" ]; then
    IFS=$'\t' read -r cached_at cached_value < "$cache_file" || true
    case "$cached_at" in
      ''|*[!0-9]*) ;;
      *)
        if [ $((now - cached_at)) -lt "$ttl" ] && [ -n "$cached_value" ]; then
          printf '%s\n' "$cached_value"
          return 0
        fi
        ;;
    esac
  fi
  cached_value="$(tartci_assignment_v2_select_live)"
  # TTL begins when the exhaustive observation completes, not before lock
  # contention and API pagination. Backdating this stamp can make a fresh
  # snapshot immediately expire and recreate the scan burst it should prevent.
  now="$(date +%s)"
  mkdir -p "$STATE_DIR"
  if tmp="$(mktemp "$cache_file.tmp.XXXXXX")"; then
    printf '%s\t%s\n' "$now" "$cached_value" > "$tmp"
    mv -f "$tmp" "$cache_file"
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
    q="$(tartci_assignment_v2_tier_demand "$tier_label")" || {
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
# is empty. Every invocation is exhaustive and live; cancellation, class-label
# drift, API failure, or truncation denies JIT minting.
tartci_assignment_v2_pre_mint_valid(){
  local selected_tier="$1" tier_label q tier=0
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
