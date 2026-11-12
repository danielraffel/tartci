#!/usr/bin/env bash
# shipyard_queue_tick.sh — per-host Shipyard ship-state reaper.
#
# Reaps ship-state records whose pull requests are gone, INDEPENDENT of any
# interactive session (cmux/Claude can die from a restart or quota exhaustion
# and the records still get cleaned up). It never merges, enqueues or arms a
# pull request: landing is the GitHub merge queue's job. It reuses shipyard's
# own `ship-state` subcommands and never edits state files.
#
# Safety invariants (see planning/2026-06-30-ship-queue-resilience-design.md):
#   * Acts only on PRs that already have a ship-state record.
#   * Recoverably archives only MERGED/CLOSED records or a PR that is absent
#     for the configured consecutive threshold while its repository is readable.
#     OPEN is always kept.
#   * Skips records owned by a live worker (fresh heartbeat).
#   * GitHub failures fail closed. Only an explicit PR-not-found response for a
#     readable repository advances the APPLY-only quarantine ledger.
#   * Every failed GitHub or Shipyard call logs its first stderr line.
#   * DRY-RUN by default. Set SHIPYARD_TICK_APPLY=1 to reap.
#
# Tunables (env):
#   SHIPYARD_TICK_APPLY=0|1                 default 0 (dry-run)
#   SHIPYARD_QUEUE_GH_CLI=<app-wrapper>        required in every mode. Must be
#       an explicit executable other than ambient `gh`.
#   SHIPYARD_QUEUE_REPO_ROOT=<checkout>       optional; the directory the
#       merge-queue hold check runs from (default $HOME).
#   SHIPYARD_QUEUE_CANONICAL_CONFIG=<file>     default:
#       ~/.config/shipyard/queue-tick.env. A strict, user-owned mode-600 file
#       may self-repair missing ROOT/GH_CLI values after plist drift.
#   SHIPYARD_QUEUE_SELF_REPAIR=0|1             default 1
#   SHIPYARD_QUEUE_HEALTH_FILE=<file>           machine-readable last verdict
#   SHIPYARD_QUEUE_INVALID_LEDGER=<file>        consecutive-not-found ledger
#   SHIPYARD_QUEUE_INVALID_THRESHOLD=N          default 3; only then archive
#       a recoverable ship-state whose PR is repeatedly confirmed nonexistent.
#   SHIPYARD_QUEUE_MIN_VERSION=<semver>       default 0.80.0. The tick requires
#       Shipyard's fail-closed merge-queue control surface.
#   SHIPYARD_TICK_HEARTBEAT_FRESH_SECS=N    default 300 (skip live workers)
#   SHIPYARD_QUEUE_REAP_REPOS="owner/repo ..." default Generous-Corp/pulp;
#       the repositories whose superseded merge-group runs each tick reaps.
#   SHIPYARD_QUEUE_REAP_OPT_IN=<file>          default
#       ~/.config/tartci/reap-superseded-merge-groups.enabled. While it exists
#       the reaper cancels (`--apply`); without it the reaper only reports.
#   SHIPYARD_QUEUE_REAP_LOG=<file>             default
#       ~/Library/Logs/tartci/superseded-merge-groups.log, one line per run
#       cancelled (or, in a report-only tick, that would be).
#   SHIPYARD_QUEUE_REAP_TIMEOUT_SECS=N         default 60, per repository.
#
# Retired settings: SHIPYARD_TICK_REAP_ONLY, SHIPYARD_QUEUE_AUTHORITY,
# SHIPYARD_TICK_MERGE_METHOD and SHIPYARD_QUEUE_COMMAND_TIMEOUT_SECS configured
# the removed merge path. They are still accepted so an existing install keeps
# running; when one asks for merging, every tick says so in its log and health.
set -uo pipefail

APPLY="${SHIPYARD_TICK_APPLY:-0}"
REPO_ROOT="${SHIPYARD_QUEUE_REPO_ROOT:-}"
CANONICAL_CONFIG="${SHIPYARD_QUEUE_CANONICAL_CONFIG:-$HOME/.config/shipyard/queue-tick.env}"
SELF_REPAIR="${SHIPYARD_QUEUE_SELF_REPAIR:-1}"
HEALTH_FILE="${SHIPYARD_QUEUE_HEALTH_FILE:-$HOME/Library/Logs/shipyard-queue-tick.health.json}"
INVALID_LEDGER="${SHIPYARD_QUEUE_INVALID_LEDGER:-$HOME/.local/state/tartci/shipyard-queue-tick-invalid.json}"
INVALID_THRESHOLD="${SHIPYARD_QUEUE_INVALID_THRESHOLD:-3}"
MIN_VERSION="${SHIPYARD_QUEUE_MIN_VERSION:-0.80.0}"
FRESH="${SHIPYARD_TICK_HEARTBEAT_FRESH_SECS:-300}"
GH="${SHIPYARD_QUEUE_GH_CLI:-}"
REAP_REPOS="${SHIPYARD_QUEUE_REAP_REPOS:-Generous-Corp/pulp}"
REAP_OPT_IN="${SHIPYARD_QUEUE_REAP_OPT_IN:-$HOME/.config/tartci/reap-superseded-merge-groups.enabled}"
REAP_LOG="${SHIPYARD_QUEUE_REAP_LOG:-$HOME/Library/Logs/tartci/superseded-merge-groups.log}"
REAP_TIMEOUT="${SHIPYARD_QUEUE_REAP_TIMEOUT_SECS:-60}"
# Retired merge-path settings (see header).
LEGACY_REAP_ONLY="${SHIPYARD_TICK_REAP_ONLY:-}"
LEGACY_AUTHORITY="${SHIPYARD_QUEUE_AUTHORITY:-}"
SY="$(command -v shipyard 2>/dev/null || echo "$HOME/.local/bin/shipyard")"
HOST="$(scutil --get ComputerName 2>/dev/null || hostname)"
SUPPORT="$(cd "$(dirname "$0")" && pwd)/shipyard_queue_tick_support.py"

ts()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts) [queue-tick] $*"; }
# First non-empty line of a captured stderr file, for a one-line reason.
first_line() {
  local line
  line="$(grep -m 1 -v '^[[:space:]]*$' "$1" 2>/dev/null | cut -c1-240)"
  printf '%s' "${line:-no stderr}"
}
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
  python3 "$SUPPORT" validate-tunables "$FRESH" "$INVALID_THRESHOLD" >/dev/null 2>&1 || \
    unhealthy "heartbeat freshness must be 0..604800 and invalid threshold 1..100000"
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
        [ -n "$LEGACY_AUTHORITY" ] || LEGACY_AUTHORITY="$value"
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
# The merge path is gone; an install that still asks for it is told so on
# every tick rather than silently getting a reaper.
LEGACY_NOTE=""
if { [ "$APPLY" = "1" ] && [ "$LEGACY_REAP_ONLY" = "0" ]; } || [ "$LEGACY_AUTHORITY" = "1" ]; then
  LEGACY_NOTE="legacy_full_live_ignored"
fi
validate_invalid_ledger || unhealthy "invalid-ledger integrity/writability check failed"
installed="$("$SY" --version 2>/dev/null | awk '{print $2}')"
compatible="$(python3 "$SUPPORT" version-compatible "$installed" "$MIN_VERSION")"
if [ "$compatible" != "1" ]; then
  unhealthy "Shipyard $MIN_VERSION or newer is required"
fi
CONTROL_CWD="$HOME"
[ -n "$REPO_ROOT" ] && [ -d "$REPO_ROOT" ] && CONTROL_CWD="$REPO_ROOT"
CONTROL_ERROR="$TMP/control.err"
control="$(cd "$CONTROL_CWD" && "$SY" merge-queue status --json 2>"$CONTROL_ERROR")" || {
  unhealthy "merge-queue control unavailable: $(first_line "$CONTROL_ERROR")"
}
control_flags="$(printf '%s' "$control" | python3 "$SUPPORT" control-flags 2>/dev/null)" \
  || unhealthy "merge-queue control schema malformed"
held="${control_flags%%|*}"
if [ "$held" = "1" ]; then
  log "$HOST: local merge-queue hold active — skip entire tick before GitHub reads"
  health "degraded" "merge_queue_held" || exit 2
  exit 0
fi
[ -n "$GH" ] || unhealthy "queue tick requires SHIPYARD_QUEUE_GH_CLI GitHub App wrapper"
[ "$(basename "$GH")" != "gh" ] \
  || unhealthy "queue tick refuses ambient gh; configure a GitHub App wrapper"
command -v "$GH" >/dev/null 2>&1 \
  || unhealthy "configured GitHub App wrapper is not executable: $GH"
# Installation readiness is distinct from completion: publish it after all
# local/configuration checks, before queue-size-dependent network I/O.
health "starting" "local_prerequisites_validated" || exit 2

# Superseded merge-group runs: a `merge_group` run whose gh-readonly-queue ref
# is gone validates a dequeued batch and only holds a gate VM. Shipyard's own
# reaper decides (it trusts only a ref listing that carries HEAD, and skips
# runs that hold no runner); this tick only runs it. It never fails the tick,
# and the support helper bounds it to REAP_TIMEOUT seconds per repository.
# Shipyard resolves its GitHub credential from the checkout it runs in, so the
# reaper runs from SHIPYARD_QUEUE_REPO_ROOT; from $HOME it fails with
# "token_command placeholder requires remote.origin.url to be a GitHub remote".
REAP_APPLY=0; [ -e "$REAP_OPT_IN" ] && REAP_APPLY=1
# One checkout resolves one repository's credential, so a root cannot serve
# several reaped repositories.
reap_count=0
for reap_repo in $REAP_REPOS; do reap_count=$((reap_count+1)); done
if [ "$reap_count" -gt 1 ]; then
  log "$HOST: reap-superseded: skipped: one repo root cannot serve $reap_count repositories" \
      "($REAP_REPOS); set SHIPYARD_QUEUE_REAP_REPOS to the root's repository"
  REAP_REPOS=""
fi
for reap_repo in $REAP_REPOS; do
  if [ -z "$REPO_ROOT" ] || [ ! -d "$REPO_ROOT" ]; then
    log "$HOST: reap-superseded $reap_repo: skipped: no repo root configured" \
        "(install the tick with --repo-root <checkout>)"
    continue
  fi
  reap_line="$(cd "$REPO_ROOT" && python3 "$SUPPORT" reap-superseded "$SY" "$reap_repo" "$REAP_APPLY" \
    "$REAP_LOG" "$HOST" "$(ts)" --timeout "$REAP_TIMEOUT" 2>&1 | head -n 1)"
  log "$HOST: ${reap_line:-reap-superseded $reap_repo: no output}"
done

LIST_ERROR="$TMP/ship-state-list.err"
"$SY" ship-state list --json 2>"$LIST_ERROR" > "$SS" \
  || unhealthy "shipyard ship-state unavailable: $(first_line "$LIST_ERROR")"

python3 "$SUPPORT" state-rows "$SS" > "$ROWS" \
  || unhealthy "shipyard ship-state payload malformed"

now=$(date -u +%s)
total=$(wc -l < "$ROWS" | tr -d ' ')
MODE="dry-run"; [ "$APPLY" = "1" ] && MODE="reap"
log "$HOST: $total active record(s); mode=$MODE${LEGACY_NOTE:+ ($LEGACY_NOTE: SHIPYARD_TICK_REAP_ONLY=0 / SHIPYARD_QUEUE_AUTHORITY=1 no longer merge anything; this tick only reaps)}"
reaped=0; open=0; stalled=0; live=0; errs=0

# Reap one record; a failed discard names its reason. The record is named by
# repo and PR: from Shipyard 0.254.0 a bare PR number that two repositories
# share is refused, and with --repo only that repository's record is archived.
# A Shipyard that predates --repo rejects the flag as an unexpected argument;
# only then is the bare form used, which is what it accepted before.
discard() {
  local pr="$1" repo="$2" error="$TMP/discard-$1.err"
  if "$SY" ship-state discard "$pr" --repo "$repo" >/dev/null 2>"$error"; then
    return 0
  fi
  if grep -q "unexpected argument '--repo'" "$error" 2>/dev/null \
    && "$SY" ship-state discard "$pr" >/dev/null 2>"$error"; then
    return 0
  fi
  DISCARD_REASON="$(first_line "$error")"
  return 1
}

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
        if discard "$pr" "$repo"; then
          invalid_count "$repo" "$pr" reset >/dev/null 2>&1 \
            || unhealthy "invalid-ledger reset failed after durable discard for $repo#$pr"
          log "  $repo#$pr: quarantined recoverably after $count confirmed not-found reads"
          reaped=$((reaped+1))
        else
          log "  $repo#$pr: quarantine discard failed ($DISCARD_REASON); confirmation ledger preserved"
          errs=$((errs+1))
        fi
      else
        log "  $repo#$pr: confirmed nonexistent ($count/$INVALID_THRESHOLD) — hold before quarantine"
        stalled=$((stalled+1))
      fi
    else
      invalid_count "$repo" "$pr" reset >/dev/null 2>&1 \
        || unhealthy "invalid-ledger reset failed for $repo#$pr"
      log "  $repo#$pr: GitHub read failed (exit $state_status: $(first_line "$PR_ERROR")) — skip (fail closed)"
      errs=$((errs+1))
    fi
    continue
  fi
  invalid_count "$repo" "$pr" reset >/dev/null 2>&1 \
    || unhealthy "invalid-ledger reset failed for $repo#$pr"
  case "$state" in
    MERGED|CLOSED)
      if [ "$APPLY" = "1" ]; then
        if discard "$pr" "$repo"; then
          log "  $repo#$pr: reaped ($state)"
          reaped=$((reaped+1))
        else
          log "  $repo#$pr: discard failed ($DISCARD_REASON)"
          errs=$((errs+1))
        fi
      else
        log "  $repo#$pr: would reap ($state)"
        reaped=$((reaped+1))
      fi ;;
    OPEN)
      log "  $repo#$pr: open — kept; landing is the merge queue's job"
      open=$((open+1)) ;;
    *) log "  $repo#$pr: unexpected state '$state' — skip"; errs=$((errs+1)) ;;
  esac
done < "$ROWS"

summary="mode=$MODE reaped=$reaped open=$open stalled=$stalled live=$live errs=$errs${LEGACY_NOTE:+ $LEGACY_NOTE}"
log "$HOST: $summary"
if [ "$errs" -gt 0 ]; then
  health "degraded" "$summary" || exit 2
  exit 1
fi
health "healthy" "$summary" || exit 2
