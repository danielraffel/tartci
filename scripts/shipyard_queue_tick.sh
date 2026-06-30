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
#   * Discards only when GitHub reports the PR MERGED/CLOSED. OPEN is always kept.
#   * Skips records owned by a live worker (fresh heartbeat).
#   * Any GitHub read failure → skip that record this pass (fail closed).
#   * DRY-RUN by default. Set SHIPYARD_TICK_APPLY=1 to take action.
#
# Tunables (env):
#   SHIPYARD_TICK_APPLY=0|1                 default 0 (dry-run)
#   SHIPYARD_TICK_HEARTBEAT_FRESH_SECS=N    default 300 (skip live workers)
#   SHIPYARD_TICK_MERGE_METHOD=merge|squash|rebase  default merge
#       (merge-commit preserves a `chore: bump versions` marker on main; squash
#        folds it and trips pulp's auto-release watchdog)
set -uo pipefail

APPLY="${SHIPYARD_TICK_APPLY:-0}"
FRESH="${SHIPYARD_TICK_HEARTBEAT_FRESH_SECS:-300}"
METHOD="${SHIPYARD_TICK_MERGE_METHOD:-merge}"
GH="ghapp"; command -v ghapp >/dev/null 2>&1 || GH="gh"
SY="$(command -v shipyard 2>/dev/null || echo "$HOME/.local/bin/shipyard")"
HOST="$(scutil --get ComputerName 2>/dev/null || hostname)"
SS=/tmp/_qt_ss.json; ROWS=/tmp/_qt_rows.txt

ts()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts) [queue-tick] $*"; }
iso2epoch() { [ -z "${1:-}" ] && { echo 0; return; }; date -u -j -f "%Y-%m-%dT%H:%M:%S" "${1%%.*}" +%s 2>/dev/null || echo 0; }

"$SY" ship-state list --json 2>/dev/null > "$SS" || { log "$HOST: shipyard ship-state unavailable"; exit 0; }

python3 - "$SS" > "$ROWS" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
for s in d.get('states',[]):
    runs=s.get('dispatched_runs') or []
    hb=max((r.get('last_heartbeat_at') or '' for r in runs), default='')
    # freshness = most recent of heartbeat / record-write / record-create, so a
    # just-created ship (runs=0, no heartbeat yet) is still treated as live.
    fresh=max([hb, s.get('updated_at') or '', s.get('created_at') or ''])
    print(f"{s['pr']}\t{s['repo']}\t{fresh}")
PY

now=$(date -u +%s)
total=$(wc -l < "$ROWS" | tr -d ' ')
log "$HOST: $total active record(s); apply=$APPLY method=$METHOD"
merged=0; reaped=0; waiting=0; stalled=0; live=0; errs=0

while IFS=$'\t' read -r pr repo hb; do
  [ -z "$pr" ] && continue
  hbe=$(iso2epoch "$hb")
  if [ "$hbe" -gt 0 ]; then
    age=$(( now - hbe ))
    if [ "$age" -lt "$FRESH" ]; then log "  $repo#$pr: live worker (hb ${age}s) — skip"; live=$((live+1)); continue; fi
  fi
  state="$($GH pr view "$pr" --repo "$repo" --json state --jq .state 2>/dev/null)"
  if [ -z "$state" ]; then log "  $repo#$pr: GitHub read failed — skip (fail closed)"; errs=$((errs+1)); continue; fi
  case "$state" in
    MERGED|CLOSED)
      if [ "$APPLY" = "1" ]; then
        if "$SY" ship-state discard "$pr" >/dev/null 2>&1; then log "  $repo#$pr: reaped ($state)"; reaped=$((reaped+1)); else log "  $repo#$pr: discard failed"; errs=$((errs+1)); fi
      else log "  $repo#$pr: would reap ($state)"; reaped=$((reaped+1)); fi ;;
    OPEN)
      info="$($GH pr view "$pr" --repo "$repo" --json mergeable,mergeStateStatus,isDraft --jq '"\(.mergeable)|\(.mergeStateStatus)|\(.isDraft)"' 2>/dev/null)"
      if [ -z "$info" ]; then log "  $repo#$pr: mergeability read failed — skip (fail closed)"; errs=$((errs+1)); continue; fi
      mergeable="${info%%|*}"; rest="${info#*|}"; mss="${rest%%|*}"; draft="${rest##*|}"
      if [ "$draft" = "true" ]; then log "  $repo#$pr: draft — skip"; waiting=$((waiting+1)); continue; fi
      if [ "$mergeable" = "CONFLICTING" ] || [ "$mss" = "DIRTY" ] || [ "$mss" = "BEHIND" ]; then
        log "  $repo#$pr: OPEN not fast-forwardable (mergeable=$mergeable status=$mss) — SURFACE, no auto-rebase"; stalled=$((stalled+1)); continue
      fi
      if [ "$APPLY" = "1" ]; then
        "$SY" ship-state reconcile "$pr" >/dev/null 2>&1
        out="$("$SY" auto-merge "$pr" --merge-method "$METHOD" --json 2>&1)"
        if echo "$out" | grep -qiE '"(event|status)"[[:space:]]*:[[:space:]]*"(merged|already-merged)"|already-merged'; then log "  $repo#$pr: merged"; merged=$((merged+1))
        else log "  $repo#$pr: not green yet / no-op"; waiting=$((waiting+1)); fi
      else log "  $repo#$pr: OPEN — would attempt shipyard auto-merge (fail-closed)"; waiting=$((waiting+1)); fi ;;
    *) log "  $repo#$pr: unexpected state '$state' — skip"; errs=$((errs+1)) ;;
  esac
done < "$ROWS"

log "$HOST: merged=$merged reaped=$reaped waiting=$waiting stalled=$stalled live=$live errs=$errs (apply=$APPLY)"
