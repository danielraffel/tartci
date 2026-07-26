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
#   SHIPYARD_QUEUE_CANONICAL_CONFIG=<file>     default:
#       ~/.config/shipyard/queue-tick.env. A strict, user-owned mode-600 file
#       may self-repair missing ROOT/AUTHORITY values after plist drift.
#   SHIPYARD_QUEUE_SELF_REPAIR=0|1             default 1
#   SHIPYARD_QUEUE_HEALTH_FILE=<file>           machine-readable last verdict
#   SHIPYARD_QUEUE_INVALID_LEDGER=<file>        consecutive-not-found ledger
#   SHIPYARD_QUEUE_INVALID_THRESHOLD=N          default 3; only then archive
#       a recoverable ship-state whose PR is repeatedly confirmed nonexistent.
#   SHIPYARD_QUEUE_MIN_VERSION=<semver>       default 0.79.0. The tick requires
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
MIN_VERSION="${SHIPYARD_QUEUE_MIN_VERSION:-0.79.0}"
REAP_ONLY="${SHIPYARD_TICK_REAP_ONLY:-0}"
FRESH="${SHIPYARD_TICK_HEARTBEAT_FRESH_SECS:-300}"
METHOD="${SHIPYARD_TICK_MERGE_METHOD:-merge}"
GH="ghapp"; command -v ghapp >/dev/null 2>&1 || GH="gh"
SY="$(command -v shipyard 2>/dev/null || echo "$HOME/.local/bin/shipyard")"
HOST="$(scutil --get ComputerName 2>/dev/null || hostname)"

ts()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts) [queue-tick] $*"; }
health() {
  local status="$1" reason="$2" temp="${HEALTH_FILE}.tmp.$$"
  if ! mkdir -p "$(dirname "$HEALTH_FILE")" 2>/dev/null; then
    log "$HOST: HEALTH WRITE FAILED: cannot create $(dirname "$HEALTH_FILE")"
    return 1
  fi
  if ! python3 - "$status" "$reason" "$HOST" "$(ts)" > "$temp" 2>/dev/null <<'PY'
import json,sys
print(json.dumps({
    "schema_version": 1,
    "status": sys.argv[1],
    "reason": sys.argv[2],
    "host": sys.argv[3],
    "observed_at": sys.argv[4],
}, sort_keys=True))
PY
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
  python3 - "$FRESH" "$INVALID_THRESHOLD" <<'PY' >/dev/null 2>&1 || \
    unhealthy "heartbeat freshness must be 0..604800 and invalid threshold 1..100000"
import re, sys
fresh, threshold = sys.argv[1:]
if not re.fullmatch(r"[0-9]+", fresh) or not 0 <= int(fresh) <= 604800:
    raise SystemExit(1)
if not re.fullmatch(r"[0-9]+", threshold) or not 1 <= int(threshold) <= 100000:
    raise SystemExit(1)
PY
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
      ''|'#'*) ;;
      *) unhealthy "canonical config contains unsupported key $key" ;;
    esac
  done < "$CANONICAL_CONFIG"
}
invalid_count() {
  local repo="$1" pr="$2" outcome="$3"
  mkdir -p "$(dirname "$INVALID_LEDGER")" 2>/dev/null || return 1
  python3 - "$INVALID_LEDGER" "$repo" "$pr" "$outcome" <<'PY'
import json, os, sys, tempfile
path, repo, pr, outcome = sys.argv[1:]
try:
    with open(path) as source:
        data = json.load(source)
except FileNotFoundError:
    data = {}
if not isinstance(data, dict):
    raise ValueError("invalid ledger must contain a JSON object")
if any(type(value) is not int or value < 0 for value in data.values()):
    raise ValueError("invalid ledger counters must be nonnegative integers")
key = f"{repo}#{pr}"
if outcome == "not_found":
    data[key] = data.get(key, 0) + 1
else:
    data.pop(key, None)
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, temp = tempfile.mkstemp(prefix=".queue-tick-invalid.", dir=os.path.dirname(path))
with os.fdopen(fd, "w") as out:
    json.dump(data, out, sort_keys=True)
os.replace(temp, path)
print(int(data.get(key, 0)))
PY
}
validate_invalid_ledger() {
  mkdir -p "$(dirname "$INVALID_LEDGER")" 2>/dev/null || return 1
  python3 - "$INVALID_LEDGER" <<'PY'
import json, os, sys, tempfile
path = sys.argv[1]
try:
    with open(path) as source:
        data = json.load(source)
except FileNotFoundError:
    data = {}
if not isinstance(data, dict):
    raise ValueError("invalid ledger must contain a JSON object")
if any(type(value) is not int or value < 0 for value in data.values()):
    raise ValueError("invalid ledger counters must be nonnegative integers")
directory = os.path.dirname(path)
fd, temp = tempfile.mkstemp(prefix=".queue-tick-ledger-probe.", dir=directory)
published = f"{temp}.published"
try:
    with os.fdopen(fd, "w") as out:
        json.dump(data, out, sort_keys=True)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, published)
finally:
    for candidate in (temp, published):
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass
PY
}
validate_tunables
TMP="$(mktemp -d "${TMPDIR:-/tmp}/shipyard-queue-tick.XXXXXX")" \
  || unhealthy "could not create queue-tick scratch directory"
trap 'rm -rf "$TMP"' EXIT
SS="$TMP/ship-state.json"; ROWS="$TMP/rows.txt"

load_canonical_config
validate_invalid_ledger || unhealthy "invalid-ledger integrity/writability check failed"
installed="$("$SY" --version 2>/dev/null | awk '{print $2}')"
compatible="$(python3 - "$installed" "$MIN_VERSION" <<'PY'
import re, sys
def version(value):
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else None
installed, required = map(version, sys.argv[1:3])
print("1" if installed is not None and required is not None and installed >= required else "0")
PY
)"
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
  AUTHORITY_REPO="$(python3 - "$remote" <<'PY'
import re, sys
remote = sys.argv[1].strip()
match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", remote)
print(match.group(1) if match else "")
PY
)"
fi
if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ] && [ -z "$AUTHORITY_REPO" ]; then
  unhealthy "FULL-LIVE repo root has no GitHub origin"
fi
control="$(cd "$CONTROL_CWD" && "$SY" merge-queue status --json 2>/dev/null)" || {
  unhealthy "merge-queue control unavailable"
}
control_flags="$(printf '%s' "$control" | python3 -c '
import json, sys
value = json.load(sys.stdin)
if not isinstance(value, dict):
    raise ValueError("control must be an object")
held = value.get("held")
authority = value.get("authority_matches")
if type(held) is not bool or type(authority) is not bool:
    raise ValueError("control booleans must be exact JSON booleans")
print(f"{int(held)}|{int(authority)}")
' 2>/dev/null)" || unhealthy "merge-queue control schema malformed"
held="${control_flags%%|*}"
authority_matches="${control_flags##*|}"
if [ "$held" = "1" ]; then
  log "$HOST: local merge-queue hold active — skip entire tick before GitHub reads"
  health "healthy" "merge_queue_held" || exit 2
  exit 0
fi
if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ] && [ "$AUTHORITY" != "1" ]; then
  unhealthy "FULL-LIVE requires SHIPYARD_QUEUE_AUTHORITY=1"
fi
if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ] && [ "$authority_matches" != "1" ]; then
  unhealthy "runner tag does not match merge_queue.mutation_machine"
fi

"$SY" ship-state list --json 2>/dev/null > "$SS" || unhealthy "shipyard ship-state unavailable"

python3 - "$SS" > "$ROWS" <<'PY' || unhealthy "shipyard ship-state payload malformed"
import datetime,json,re,sys
d=json.load(open(sys.argv[1]))
if not isinstance(d, dict) or not isinstance(d.get("states"), list):
    raise ValueError("expected object with states array")
rows = []
repo_pattern = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+")
rfc3339_pattern = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})"
)

def timestamp_epoch(value, field):
    if value is None or value == "":
        return 0
    if not isinstance(value, str) or not rfc3339_pattern.fullmatch(value):
        raise ValueError(f"state {field} must be empty or RFC3339")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"state {field} must be empty or RFC3339") from error
    if parsed.tzinfo is None:
        raise ValueError(f"state {field} must carry an RFC3339 offset")
    return int(parsed.timestamp())

for s in d["states"]:
    if not isinstance(s, dict):
        raise ValueError("state entry must be an object")
    pr = s.get("pr")
    if isinstance(pr, bool) or not (
        isinstance(pr, int) and pr > 0
        or isinstance(pr, str) and re.fullmatch(r"[1-9][0-9]*", pr)
    ):
        raise ValueError("state pr must be a positive integer")
    pr = str(pr)
    repo = s.get("repo")
    if not isinstance(repo, str) or not repo_pattern.fullmatch(repo):
        raise ValueError("state repo must be a canonical owner/name slug")
    runs = s.get("dispatched_runs")
    if runs is None:
        runs = []
    if not isinstance(runs, list) or any(not isinstance(run, dict) for run in runs):
        raise ValueError("state dispatched_runs must be an array of objects")
    heartbeats = []
    for run in runs:
        heartbeats.append(
            timestamp_epoch(run.get("last_heartbeat_at"), "heartbeat")
        )
    hb=max(heartbeats, default=0)
    timestamps = []
    for field in ("updated_at", "created_at"):
        timestamps.append(timestamp_epoch(s.get(field), field))
    # freshness = most recent of heartbeat / record-write / record-create, so a
    # just-created ship (runs=0, no heartbeat yet) is still treated as live.
    fresh=max([hb, *timestamps])
    rows.append((pr, repo, str(fresh)))
for row in rows:
    print("\t".join(row))
PY

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
      info="$($GH pr view "$pr" --repo "$repo" --json mergeable,mergeStateStatus,isDraft --jq '"\(.mergeable)|\(.mergeStateStatus)|\(.isDraft)"' 2>/dev/null)"
      info_status=$?
      if [ "$info_status" -ne 0 ] || [ -z "$info" ]; then
        log "  $repo#$pr: mergeability read failed — skip (fail closed)"
        errs=$((errs+1))
        continue
      fi
      mergeable="${info%%|*}"; rest="${info#*|}"; mss="${rest%%|*}"; draft="${rest##*|}"
      if [ "$draft" = "true" ]; then log "  $repo#$pr: draft — skip"; waiting=$((waiting+1)); continue; fi
      if [ "$mergeable" = "CONFLICTING" ] || [ "$mss" = "DIRTY" ] || [ "$mss" = "BEHIND" ]; then
        log "  $repo#$pr: OPEN not fast-forwardable (mergeable=$mergeable status=$mss) — SURFACE, no auto-rebase"; stalled=$((stalled+1)); continue
      fi
      if [ "$APPLY" = "1" ] && [ "$REAP_ONLY" != "1" ]; then
        reconcile_out="$(cd "$REPO_ROOT" && "$SY" ship-state reconcile "$pr" --json 2>/dev/null)"
        reconcile_status=$?
        reconcile_ok="$(printf '%s' "$reconcile_out" | python3 -c '
import json, sys
expected = int(sys.argv[1])
value = json.load(sys.stdin)
results = value.get("results") if isinstance(value, dict) else None
ok = (
    isinstance(results, list)
    and len(results) == 1
    and results[0].get("pr") == expected
    and results[0].get("ok") is True
)
print("1" if ok else "0")
' "$pr" 2>/dev/null)"
        if [ "$reconcile_status" -ne 0 ] || [ "$reconcile_ok" != "1" ]; then
          log "  $repo#$pr: ship-state reconcile failed — skip auto-merge"
          errs=$((errs+1))
          continue
        fi
        AUTO_MERGE_ERROR="$TMP/auto-merge-$pr.err"
        out="$(cd "$REPO_ROOT" && "$SY" auto-merge "$pr" --merge-method "$METHOD" --json 2>"$AUTO_MERGE_ERROR")"
        auto_merge_status=$?
        if [ "$auto_merge_status" -eq 0 ]; then
          if grep -qiE '"(event|status)"[[:space:]]*:[[:space:]]*"(merged|already-merged)"|already-merged' <<<"$out"; then
            log "  $repo#$pr: merged"
            merged=$((merged+1))
          else
            log "  $repo#$pr: auto-merge returned success without a merged verdict"
            errs=$((errs+1))
          fi
        elif [ "$auto_merge_status" -eq 3 ]; then
          log "  $repo#$pr: not green yet / in flight"
          waiting=$((waiting+1))
        else
          auto_merge_event="$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except Exception:
    print("")
else:
    event = value.get("event") if isinstance(value, dict) else None
    print(event if isinstance(event, str) else "")
' 2>/dev/null)"
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
