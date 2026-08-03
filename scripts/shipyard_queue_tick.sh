#!/usr/bin/env bash
# shipyard_queue_tick.sh — per-host Shipyard queue janitor.
#
# Drives in-flight ship-state to completion and reaps orphaned records,
# INDEPENDENT of any interactive session (cmux/Claude can die from a restart
# or quota exhaustion and the queue still advances). Reuses shipyard's OWN
# fail-closed `auto-merge` (live-head-verified, green-gated) and `ship-state`
# subcommands — it never reimplements merge logic and never edits state files.
#
# Safety invariants (see planning/2026-06-30-ship-queue-resilience-design.md):
#   * Acts only on PRs that already have a ship-state record (= the user ran
#     `shipyard pr` on them → intended-to-merge set).
#   * Merges only via `shipyard auto-merge` (no-op unless ALL targets green and
#     the live head matches the validated SHA — fail closed).
#   * Recoverably archives only MERGED/CLOSED records or a PR that is absent
#     for the configured consecutive threshold while its repository is readable.
#     OPEN is always kept.
#   * Skips records owned by a live worker (fresh heartbeat).
#   * GitHub failures fail closed. Only an explicit PR-not-found response for a
#     readable repository advances the APPLY-only quarantine ledger.
#   * DRY-RUN by default. Set SHIPYARD_TICK_APPLY=1 to take action.
#
# Tunables (env):
#   SHIPYARD_TICK_APPLY=0|1                 default 0 (dry-run)
#   SHIPYARD_QUEUE_AUTHORITY=0|1             FULL-LIVE requires explicit 1
#       unless this host is the explicitly selected queue authority.
#   SHIPYARD_QUEUE_REPO_ROOT=<checkout>       required for FULL-LIVE. Shipyard
#       loads that checkout's mutation_machine policy before GitHub access.
#   SHIPYARD_QUEUE_GH_CLI=<app-wrapper>        required in every mode. Must be
#       an explicit executable other than ambient `gh`.
#   SHIPYARD_QUEUE_CANONICAL_CONFIG=<file>     default:
#       ~/.config/shipyard/queue-tick.env. A strict, user-owned mode-600 file
#       may self-repair missing ROOT/AUTHORITY values after plist drift.
#   SHIPYARD_QUEUE_SELF_REPAIR=0|1             default 1
#   SHIPYARD_QUEUE_HEALTH_FILE=<file>           machine-readable last verdict
#   SHIPYARD_QUEUE_INVALID_LEDGER=<file>        consecutive-not-found ledger
#   SHIPYARD_QUEUE_INVALID_THRESHOLD=N          default 3; only then archive
#       a recoverable ship-state whose PR is repeatedly confirmed nonexistent.
#   SHIPYARD_QUEUE_COMMAND_TIMEOUT_SECS=N        default 45; bounds each
#       Shipyard reconcile/auto-merge subprocess to 1..300 seconds so one
#       wedged GitHub read cannot stop the five-minute queue-tick cadence.
#   SHIPYARD_QUEUE_MIN_VERSION=<semver>       default 0.80.0. The tick requires
#       Shipyard's fail-closed merge-queue control surface.
#   SHIPYARD_TICK_REAP_ONLY=0|1             default 0. With APPLY=1, act on the
#       proven reap path (discard GitHub-MERGED/CLOSED orphans) but hold the
#       auto-merge path in surface-only mode. This is the safe staged-rollout
#       gate: the reap path has been exercised; the OPEN+green auto-merge action
#       is enabled only once it has been observed firing correctly.
#   SHIPYARD_TICK_HEARTBEAT_FRESH_SECS=N    default 300 (skip live workers)
#   SHIPYARD_TICK_MERGE_METHOD=merge|squash|rebase  default merge
#       (merge-commit preserves a `chore: bump versions` marker on main; squash
#        folds it and trips pulp's auto-release watchdog)
set -uo pipefail

APPLY="${SHIPYARD_TICK_APPLY:-0}"
AUTHORITY="${SHIPYARD_QUEUE_AUTHORITY:-}"
REPO_ROOT="${SHIPYARD_QUEUE_REPO_ROOT:-}"
CANONICAL_CONFIG="${SHIPYARD_QUEUE_CANONICAL_CONFIG:-$HOME/.config/shipyard/queue-tick.env}"
SELF_REPAIR="${SHIPYARD_QUEUE_SELF_REPAIR:-1}"
HEALTH_FILE="${SHIPYARD_QUEUE_HEALTH_FILE:-$HOME/Library/Logs/shipyard-queue-tick.health.json}"
INVALID_LEDGER="${SHIPYARD_QUEUE_INVALID_LEDGER:-$HOME/.local/state/tartci/shipyard-queue-tick-invalid.json}"
INVALID_THRESHOLD="${SHIPYARD_QUEUE_INVALID_THRESHOLD:-3}"
COMMAND_TIMEOUT="${SHIPYARD_QUEUE_COMMAND_TIMEOUT_SECS:-45}"
MIN_VERSION="${SHIPYARD_QUEUE_MIN_VERSION:-0.80.0}"
REAP_ONLY="${SHIPYARD_TICK_REAP_ONLY:-0}"
FRESH="${SHIPYARD_TICK_HEARTBEAT_FRESH_SECS:-300}"
METHOD="${SHIPYARD_TICK_MERGE_METHOD:-merge}"
GH="${SHIPYARD_QUEUE_GH_CLI:-}"
SY="$(command -v shipyard 2>/dev/null || echo "$HOME/.local/bin/shipyard")"
HOST="$(scutil --get ComputerName 2>/dev/null || hostname)"
SUPPORT="$(cd "$(dirname "$0")" && pwd)/shipyard_queue_tick_support.py"

ts()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts) [queue-tick] $*"; }
health() {
  local status="$1" reason="$2" temp="${HEALTH_FILE}.tmp.$$"
  if ! mkdir -p "$(dirname "$HEALTH_FILE")" 2>/dev/null; then
    log "$HOST: HEALTH WRITE FAILED: cannot create $(dirname "$HEALTH_FILE")"
    return 1
  fi
  if ! python3 "$SUPPORT" health "$temp" "$status" "$reason" "$HOST" "$(ts)" 2>/dev/null
  then
    rm -f "$temp"
    log "$HOST: HEALTH WRITE FAILED: cannot encode $HEALTH_FILE"
    return 1
  fi
  if ! mv "$temp" "$HEALTH_FILE" 2>/dev/null; then
    rm -f "$temp"
    log "$HOST: HEALTH WRITE FAILED: cannot publish $HEALTH_FILE"
    return 1
  fi
}
unhealthy() {
  log "$HOST: UNHEALTHY: $1"
  if ! health "unhealthy" "$1"; then
    log "$HOST: UNHEALTHY verdict could not be persisted"
  fi
  exit 2
}
validate_tunables() {
  case "$APPLY" in 0|1) ;; *) unhealthy "SHIPYARD_TICK_APPLY must be 0 or 1" ;; esac
  case "$REAP_ONLY" in 0|1) ;; *) unhealthy "SHIPYARD_TICK_REAP_ONLY must be 0 or 1" ;; esac
  python3 "$SUPPORT" validate-tunables "$FRESH" "$INVALID_THRESHOLD" "$COMMAND_TIMEOUT" >/dev/null 2>&1 || \
    unhealthy "heartbeat freshness must be 0..604800, invalid threshold 1..100000, and command timeout 1..300 seconds"
  case "$METHOD" in
    merge|squash|rebase) ;;
    *) unhealthy "SHIPYARD_TICK_MERGE_METHOD must be merge, squash, or rebase" ;;
  esac
}
load_canonical_config() {
  [ "$SELF_REPAIR" = "1" ] || return 0
  [ -f "$CANONICAL_CONFIG" ] || return 0
  mode="$(stat -f '%Lp' "$CANONICAL_CONFIG" 2>/dev/null || stat -c '%a' "$CANONICAL_CONFIG" 2>/dev/null || echo "")"
  [ "$mode" = "600" ] || unhealthy "canonical config $CANONICAL_CONFIG must be mode 600"
  owner="$(stat -f '%u' "$CANONICAL_CONFIG" 2>/dev/null || stat -c '%u' "$CANONICAL_CONFIG" 2>/dev/null || echo "")"
  [ "$owner" = "$(id -u)" ] || unhealthy "canonical config $CANONICAL_CONFIG must be owned by uid $(id -u)"
  while IFS='=' read -r key value; do
    case "$key" in
      SHIPYARD_QUEUE_REPO_ROOT)
        [ -n "$REPO_ROOT" ] || REPO_ROOT="$value"
        ;;
      SHIPYARD_QUEUE_AUTHORITY)
        [ -n "$AUTHORITY" ] || AUTHORITY="$value"
        ;;
      SHIPYARD_QUEUE_GH_CLI)
        [ -n "$GH" ] || GH="$value"
        ;;
      ''|'#'*) ;;
      *) unhealthy "canonical config contains unsupported key $key" ;;
    esac
  done < "$CANONICAL_CONFIG"
}
invalid_count() {
  local repo="$1" pr="$2" outcome="$3"
  mkdir -p "$(dirname "$INVALID_LEDGER")" 2>/dev/null || return 1
  python3 "$SUPPORT" ledger-update "$INVALID_LEDGER" "$repo" "$pr" "$outcome"
}
validate_invalid_ledger() {
  mkdir -p "$(dirname "$INVALID_LEDGER")" 2>/dev/null || return 1
  python3 "$SUPPORT" ledger-validate "$INVALID_LEDGER"
}
validate_tunables
TMP="$(mktemp -d "${TMPDIR:-/tmp}/shipyard-queue-tick.XXXXXX")" \
  || unhealthy "could not create queue-tick scratch directory"
trap 'rm -rf "$TMP"' EXIT
SS="$TMP/ship-state.json"; ROWS="$TMP/rows.txt"

load_canonical_config
validate_invalid_ledger || unhealthy "invalid-ledger integrity/writability check failed"
installed="$("$SY" --version 2>/dev/null | awk '{print $2}')"
compatible="$(python3 "$SUPPORT" version-compatible "$installed" "$MIN_VERSION")"
if [ "$compatible" != "1" ]; then
  unhealthy "Shipyard $MIN_VERSION or newer is required"
fi
if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ] && [ ! -d "$REPO_ROOT" ]; then
  unhealthy "FULL-LIVE requires SHIPYARD_QUEUE_REPO_ROOT checkout (canonical config: $CANONICAL_CONFIG)"
fi
CONTROL_CWD="$HOME"
[ -d "$REPO_ROOT" ] && CONTROL_CWD="$REPO_ROOT"
AUTHORITY_REPO=""
if [ -d "$REPO_ROOT" ]; then
  remote="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
  AUTHORITY_REPO="$(python3 "$SUPPORT" github-origin "$remote" 2>/dev/null || true)"
fi
if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ] && [ -z "$AUTHORITY_REPO" ]; then
  unhealthy "FULL-LIVE repo root has no GitHub origin"
fi
control="$(cd "$CONTROL_CWD" && "$SY" merge-queue status --json 2>/dev/null)" || {
  unhealthy "merge-queue control unavailable"
}
control_flags="$(printf '%s' "$control" | python3 "$SUPPORT" control-flags 2>/dev/null)" \
  || unhealthy "merge-queue control schema malformed"
held="${control_flags%%|*}"
authority_matches="${control_flags##*|}"
if [ "$held" = "1" ]; then
  log "$HOST: local merge-queue hold active — skip entire tick before GitHub reads"
  health "degraded" "merge_queue_held" || exit 2
  exit 0
fi
if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ] && [ "$AUTHORITY" != "1" ]; then
  unhealthy "FULL-LIVE requires SHIPYARD_QUEUE_AUTHORITY=1"
fi
if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ] && [ "$authority_matches" != "1" ]; then
  unhealthy "runner tag does not match merge_queue.mutation_machine"
fi
[ -n "$GH" ] || unhealthy "queue tick requires SHIPYARD_QUEUE_GH_CLI GitHub App wrapper"
[ "$(basename "$GH")" != "gh" ] \
  || unhealthy "queue tick refuses ambient gh; configure a GitHub App wrapper"
command -v "$GH" >/dev/null 2>&1 \
  || unhealthy "configured GitHub App wrapper is not executable: $GH"
# Installation readiness is distinct from completion: publish it after all
# local/configuration checks, before bounded or queue-size-dependent network I/O.
health "starting" "local_prerequisites_validated" || exit 2
if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ]; then
  auth_export="$(cd "$REPO_ROOT" && "$SY" auth export --json 2>/dev/null)" \
    || unhealthy "Shipyard effective GitHub auth config is unavailable"
  shipyard_auth_mode="$(printf '%s' "$auth_export" | python3 "$SUPPORT" auth-mode "$GH" 2>/dev/null)" \
    || unhealthy "Shipyard auth is not bound to configured GitHub App wrapper"
  APP_TOKEN=""
  if [ "$shipyard_auth_mode" = "inject" ]; then
    APP_TOKEN="$(python3 "$SUPPORT" app-token "$GH" 2>/dev/null)" \
      || unhealthy "GitHub App wrapper could not provide a bounded token"
  fi
  shipyard_with_app_auth() {
    if [ "$shipyard_auth_mode" = "inject" ]; then
      GH_TOKEN="$APP_TOKEN" "$SY" "$@"
    else
      "$SY" "$@"
    fi
  }
  bounded_shipyard_with_app_auth() {
    if [ "$shipyard_auth_mode" = "inject" ]; then
      GH_TOKEN="$APP_TOKEN" python3 "$SUPPORT" run-bounded "$COMMAND_TIMEOUT" "$SY" "$@"
    else
      python3 "$SUPPORT" run-bounded "$COMMAND_TIMEOUT" "$SY" "$@"
    fi
  }
  python3 "$SUPPORT" authority-read "$GH" "$AUTHORITY_REPO" >/dev/null 2>&1 \
    || unhealthy "GitHub App authority-repo read failed"
fi

"$SY" ship-state list --json 2>/dev/null > "$SS" || unhealthy "shipyard ship-state unavailable"

python3 "$SUPPORT" state-rows "$SS" > "$ROWS" \
  || unhealthy "shipyard ship-state payload malformed"

now=$(date -u +%s)
total=$(wc -l < "$ROWS" | tr -d ' ')
MODE="dry-run"; [ "$APPLY" = "1" ] && MODE="live"; [ "$APPLY" = "1" ] && [ "$REAP_ONLY" = "1" ] && MODE="reap-only"
log "$HOST: $total active record(s); mode=$MODE method=$METHOD"
merged=0; reaped=0; waiting=0; stalled=0; live=0; errs=0

while IFS=$'\t' read -r pr repo hbe; do
  [ -z "$pr" ] && continue
  if [ "$hbe" -gt 0 ]; then
    age=$(( now - hbe ))
    if [ "$age" -lt "$FRESH" ]; then
      invalid_count "$repo" "$pr" reset >/dev/null 2>&1 \
        || unhealthy "invalid-ledger reset failed for $repo#$pr"
      log "  $repo#$pr: live worker (hb ${age}s) — skip"
      live=$((live+1))
      continue
    fi
  fi
  PR_ERROR="$TMP/pr-$pr.err"
  state="$($GH pr view "$pr" --repo "$repo" --json state --jq .state 2>"$PR_ERROR")"
  state_status=$?
  if [ "$state_status" -ne 0 ] || [ -z "$state" ]; then
    if [ "$state_status" -ne 0 ] \
      && grep -qiE 'HTTP 404|Could not resolve to a PullRequest|pull request not found' "$PR_ERROR"; then
      if ! "$GH" pr list --repo "$repo" --limit 1 --json number >/dev/null 2>&1; then
        invalid_count "$repo" "$pr" reset >/dev/null 2>&1 \
          || unhealthy "invalid-ledger reset failed for $repo#$pr"
        log "  $repo#$pr: PR not-found was not confirmed by a readable repository — skip"
        errs=$((errs+1))
        continue
      fi
      if [ "$APPLY" != "1" ]; then
        log "  $repo#$pr: PR not found — dry-run does not advance quarantine confirmation"
        stalled=$((stalled+1))
        continue
      fi
      count="$(invalid_count "$repo" "$pr" not_found 2>/dev/null)" \
        || unhealthy "invalid-ledger not-found update failed for $repo#$pr"
      if [ "$count" -ge "$INVALID_THRESHOLD" ] 2>/dev/null; then
        if [ "$APPLY" = "1" ]; then
          if "$SY" ship-state discard "$pr" >/dev/null 2>&1; then
            invalid_count "$repo" "$pr" reset >/dev/null 2>&1 \
              || unhealthy "invalid-ledger reset failed after durable discard for $repo#$pr"
            log "  $repo#$pr: quarantined recoverably after $count confirmed not-found reads"
            reaped=$((reaped+1))
          else
            log "  $repo#$pr: quarantine discard failed; confirmation ledger preserved"
            errs=$((errs+1))
          fi
        else
          log "  $repo#$pr: confirmed nonexistent $count times — would quarantine ship-state"
          stalled=$((stalled+1))
        fi
      else
        log "  $repo#$pr: confirmed nonexistent ($count/$INVALID_THRESHOLD) — hold before quarantine"
        stalled=$((stalled+1))
      fi
    else
      invalid_count "$repo" "$pr" reset >/dev/null 2>&1 \
        || unhealthy "invalid-ledger reset failed for $repo#$pr"
      log "  $repo#$pr: GitHub read failed — skip (fail closed)"
      errs=$((errs+1))
    fi
    continue
  fi
  invalid_count "$repo" "$pr" reset >/dev/null 2>&1 \
    || unhealthy "invalid-ledger reset failed for $repo#$pr"
  case "$state" in
    MERGED|CLOSED)
      if [ "$APPLY" = "1" ]; then
        if "$SY" ship-state discard "$pr" >/dev/null 2>&1; then
          log "  $repo#$pr: reaped ($state)"
          reaped=$((reaped+1))
        else
          log "  $repo#$pr: discard failed"
          errs=$((errs+1))
        fi
      else
        log "  $repo#$pr: would reap ($state)"
        reaped=$((reaped+1))
      fi ;;
    OPEN)
      if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ] && [ "$repo" != "$AUTHORITY_REPO" ]; then
        log "  $repo#$pr: outside authority repo $AUTHORITY_REPO — skip"
        waiting=$((waiting+1))
        continue
      fi
      info="$($GH pr view "$pr" --repo "$repo" --json mergeable,mergeStateStatus,isDraft 2>/dev/null)"
      info_status=$?
      if [ "$info_status" -ne 0 ] || [ -z "$info" ]; then
        log "  $repo#$pr: mergeability read failed — skip (fail closed)"
        errs=$((errs+1))
        continue
      fi
      info_fields="$(printf '%s' "$info" | python3 "$SUPPORT" mergeability 2>/dev/null)" || {
        log "  $repo#$pr: mergeability schema malformed — skip (fail closed)"
        errs=$((errs+1))
        continue
      }
      mergeable="${info_fields%%|*}"; rest="${info_fields#*|}"; mss="${rest%%|*}"; draft="${rest##*|}"
      if [ "$draft" = "true" ]; then log "  $repo#$pr: draft — skip"; waiting=$((waiting+1)); continue; fi
      if [ "$mergeable" = "CONFLICTING" ] || [ "$mss" = "DIRTY" ] || [ "$mss" = "BEHIND" ]; then
        log "  $repo#$pr: OPEN not fast-forwardable (mergeable=$mergeable status=$mss) — SURFACE, no auto-rebase"; stalled=$((stalled+1)); continue
      fi
      if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ]; then
        reconcile_out="$(cd "$REPO_ROOT" && bounded_shipyard_with_app_auth ship-state reconcile "$pr" --json 2>/dev/null)"
        reconcile_status=$?
        reconcile_ok="$(printf '%s' "$reconcile_out" | python3 "$SUPPORT" reconcile-ok "$pr" 2>/dev/null)"
        if [ "$reconcile_status" -ne 0 ] || [ "$reconcile_ok" != "1" ]; then
          log "  $repo#$pr: ship-state reconcile failed — skip auto-merge"
          errs=$((errs+1))
          continue
        fi
        AUTO_MERGE_ERROR="$TMP/auto-merge-$pr.err"
        out="$(cd "$REPO_ROOT" && bounded_shipyard_with_app_auth auto-merge "$pr" --merge-method "$METHOD" --json 2>"$AUTO_MERGE_ERROR")"
        auto_merge_status=$?
        auto_merge_event="$(printf '%s' "$out" | python3 "$SUPPORT" auto-merge-event "$pr" 2>/dev/null)" \
          || auto_merge_event=""
        if [ "$auto_merge_status" -eq 0 ]; then
          if [ "$auto_merge_event" = "merged" ] || [ "$auto_merge_event" = "already-merged" ]; then
            log "  $repo#$pr: merged"
            merged=$((merged+1))
          else
            log "  $repo#$pr: auto-merge returned success without a merged verdict"
            errs=$((errs+1))
          fi
        elif [ "$auto_merge_status" -eq 3 ] \
          && { [ "$auto_merge_event" = "in-flight" ] || [ "$auto_merge_event" = "enqueued" ]; }; then
          log "  $repo#$pr: not green yet / in flight"
          waiting=$((waiting+1))
        else
          case "$auto_merge_event" in
            target-failed)
              log "  $repo#$pr: required target failed — waiting for a new green head"
              waiting=$((waiting+1)) ;;
            superseded-sha)
              log "  $repo#$pr: ship evidence was superseded — stalled pending re-validation"
              stalled=$((stalled+1)) ;;
            *)
              log "  $repo#$pr: auto-merge failed (exit $auto_merge_status)"
              errs=$((errs+1)) ;;
          esac
        fi
      elif [ "$APPLY" = "1" ]; then
        log "  $repo#$pr: OPEN green — reap-only mode, would attempt shipyard auto-merge (held)"; waiting=$((waiting+1))
      else log "  $repo#$pr: OPEN — would attempt shipyard auto-merge (fail-closed)"; waiting=$((waiting+1)); fi ;;
    *) log "  $repo#$pr: unexpected state '$state' — skip"; errs=$((errs+1)) ;;
  esac
done < "$ROWS"

log "$HOST: merged=$merged reaped=$reaped waiting=$waiting stalled=$stalled live=$live errs=$errs (mode=$MODE)"
if [ "$errs" -gt 0 ]; then
  health "degraded" "github_or_mutation_errors=$errs" || exit 2
  exit 1
fi
health "healthy" "mode=$MODE merged=$merged reaped=$reaped waiting=$waiting stalled=$stalled" || exit 2
