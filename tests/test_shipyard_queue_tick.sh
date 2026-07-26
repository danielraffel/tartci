#!/usr/bin/env bash
# Hermetic test for scripts/shipyard_queue_tick.sh.
#
# Stubs `shipyard` and `ghapp` on PATH so the janitor's decision matrix runs
# against canned GitHub / ship-state responses — no network, no real state.
# Asserts the safety invariants that must never regress:
#   1. MERGED/CLOSED orphan  -> reaped (discard) when APPLY=1, incl. reap-only.
#   2. OPEN + green          -> auto-merge ONLY in full-live; HELD in reap-only
#                               and in dry-run.
#   3. fresh heartbeat       -> live worker skipped in every mode (no action).
#   4. GitHub read failure   -> fail-closed skip (errs++), never acts.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../scripts/shipyard_queue_tick.sh"
[ -f "$SCRIPT" ] || { echo "FAIL: script not found at $SCRIPT"; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"; mkdir -p "$BIN"
ACTIONS="$WORK/actions.log"; : > "$ACTIONS"
REPO="$WORK/repo"; git init -q "$REPO"
git -C "$REPO" remote add origin https://github.com/Generous-Corp/pulp.git

# now-ish and long-ago timestamps for heartbeat freshness control.
NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
OLD_ISO="2020-01-01T00:00:00Z"

# ── stub: shipyard ───────────────────────────────────────────────────────
cat > "$BIN/shipyard" <<STUB
#!/usr/bin/env bash
case "\$1 \$2" in
  "--version ") echo "shipyard 0.80.0" ;;
  "auth export") echo '{"schema_version":1,"command":"auth.export","bundle":{"version":2}}' ;;
  "merge-queue status")
    [ -f "$WORK/control_fail" ] && exit 1
    if [ -f "$WORK/control_malformed" ]; then echo '{'; else echo '{"held":false,"authority_matches":true}'; fi
    ;;
  "ship-state list")
    [ -f "$WORK/shipstate_fail" ] && exit 1
    cat "$WORK/states.json"
    ;;
  "ship-state discard") echo "discard \$3" >> "$ACTIONS" ;;
  "ship-state reconcile")
    echo "reconcile \$3" >> "$ACTIONS"
    printf '{"schema_version":1,"command":"ship-state:reconcile","results":[{"pr":%s,"ok":true,"changes":[]}]}\n' "\$3"
    ;;
  *) if [ "\$1" = "auto-merge" ]; then
       echo "automerge \$2" >> "$ACTIONS"
       cat "$WORK/automerge_out.json"
     fi ;;
esac
exit 0
STUB

# ── stub: ghapp (the janitor prefers ghapp when present) ─────────────────
cat > "$BIN/ghapp" <<STUB
#!/usr/bin/env bash
# args: pr view <PR> --repo <REPO> --json <FIELDS> --jq <EXPR>
[ "\${1:-} \${2:-}" = "auth token" ] && { echo "app-token"; exit 0; }
[ "\${1:-} \${2:-}" = "pr list" ] && { echo "[]"; exit 0; }
pr=""; fields=""
while [ \$# -gt 0 ]; do
  case "\$1" in
    view) pr="\$2"; shift 2; continue ;;
    --json) fields="\$2"; shift 2; continue ;;
  esac
  shift
done
case "\$fields" in
  state)
    if [ -f "$WORK/state_\${pr}.err" ]; then
      cat "$WORK/state_\${pr}.err" >&2
      exit 1
    fi
    cat "$WORK/state_\${pr}.txt" 2>/dev/null
    ;;
  *mergeable*) cat "$WORK/merge_\${pr}.txt" 2>/dev/null ;;
esac
exit 0
STUB
chmod +x "$BIN/shipyard" "$BIN/ghapp"

echo '{"schema_version":1,"command":"auto-merge","event":"merged","pr":202}' > "$WORK/automerge_out.json"

# ── fixtures: merged, open-green, live, and a foreign-repo record ──
cat > "$WORK/states.json" <<JSON
{"states":[
  {"pr":"101","repo":"Generous-Corp/pulp","created_at":"$OLD_ISO","updated_at":"$OLD_ISO","dispatched_runs":[]},
  {"pr":"202","repo":"Generous-Corp/pulp","created_at":"$OLD_ISO","updated_at":"$OLD_ISO","dispatched_runs":[]},
  {"pr":"303","repo":"Generous-Corp/pulp","created_at":"$OLD_ISO","updated_at":"$OLD_ISO","dispatched_runs":[{"last_heartbeat_at":"$NOW_ISO"}]},
  {"pr":"404","repo":"owner/other","created_at":"$OLD_ISO","updated_at":"$OLD_ISO","dispatched_runs":[]},
  {"pr":"505","repo":"Generous-Corp/pulp","created_at":"$OLD_ISO","updated_at":"$OLD_ISO","dispatched_runs":[]}
]}
JSON
echo "MERGED"       > "$WORK/state_101.txt"   # orphan → reap
echo "OPEN"         > "$WORK/state_202.txt"   # open-green → auto-merge/held
echo "OPEN"         > "$WORK/state_303.txt"   # open but live worker → skip
echo "OPEN"         > "$WORK/state_404.txt"   # foreign repo → never mutate
echo "OPEN"         > "$WORK/state_505.txt"   # inert draft until not-found test
echo '{"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":false}' > "$WORK/merge_202.txt"
echo '{"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":false}' > "$WORK/merge_303.txt"
echo '{"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":false}' > "$WORK/merge_404.txt"
echo '{"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":true}'  > "$WORK/merge_505.txt"

run() {
  : > "$ACTIONS"
  env \
    -u SHIPYARD_QUEUE_AUTHORITY \
    -u SHIPYARD_QUEUE_REPO_ROOT \
    -u SHIPYARD_QUEUE_CANONICAL_CONFIG \
    -u SHIPYARD_QUEUE_INVALID_THRESHOLD \
    -u SHIPYARD_QUEUE_MIN_VERSION \
    -u SHIPYARD_TICK_APPLY \
    -u SHIPYARD_TICK_REAP_ONLY \
    -u SHIPYARD_TICK_MERGE_METHOD \
    -u SHIPYARD_TICK_HEARTBEAT_FRESH_SECS \
    -u TARTCI_VM_LEASE_PRIORITY \
    PATH="$BIN:$PATH" \
    HOME="$WORK" \
    SHIPYARD_QUEUE_GH_CLI=ghapp \
    SHIPYARD_QUEUE_SELF_REPAIR=0 \
    SHIPYARD_QUEUE_HEALTH_FILE="$WORK/health.json" \
    SHIPYARD_QUEUE_INVALID_LEDGER="$WORK/invalid.json" \
    "$@" bash "$SCRIPT" 2>&1
}
fail() { echo "FAIL: $1"; exit 1; }
has()  { grep -q "$1" "$ACTIONS" || fail "expected action '$1' — got: $(tr '\n' ';' < "$ACTIONS")"; }
hasnt(){ grep -q "$1" "$ACTIONS" && fail "unexpected action '$1' — got: $(tr '\n' ';' < "$ACTIONS")"; return 0; }

echo "== dry-run: no actions at all =="
out="$(run SHIPYARD_TICK_APPLY=0)"
[ -s "$ACTIONS" ] && fail "dry-run took actions: $(cat "$ACTIONS")"
grep -q "101: would reap" <<<"$out" || fail "dry-run should log would-reap for 101"
grep -q "303: live worker" <<<"$out" || fail "should skip live worker 303"
echo "  ok"

echo "== reap-only: reap merged, HOLD auto-merge, skip live =="
run SHIPYARD_TICK_APPLY=1 SHIPYARD_TICK_REAP_ONLY=1 >/dev/null
has  "discard 101"
hasnt "automerge 202"
hasnt "discard 303"; hasnt "automerge 303"
echo "  ok"

echo "== full-live: reap merged AND auto-merge open-green, skip live =="
out="$(run SHIPYARD_TICK_APPLY=1 SHIPYARD_QUEUE_AUTHORITY=1 SHIPYARD_QUEUE_REPO_ROOT="$REPO")"
has  "discard 101"
has  "automerge 202"
hasnt "automerge 404"
hasnt "discard 303"; hasnt "automerge 303"
grep -q "202: merged" <<<"$out" || fail "202 should report merged"
grep -q "owner/other#404: outside authority repo Generous-Corp/pulp — skip" <<<"$out" || fail "foreign repo should be skipped"
echo "  ok"

echo "== fail-closed: GitHub state read empty -> skip, no action =="
echo "" > "$WORK/state_101.txt"   # simulate read failure for 101
out="$(run SHIPYARD_TICK_APPLY=1 SHIPYARD_QUEUE_AUTHORITY=1 SHIPYARD_QUEUE_REPO_ROOT="$REPO")"
hasnt "discard 101"
grep -q "101: GitHub read failed — skip (fail closed)" <<<"$out" || fail "101 should fail closed"
echo "MERGED" > "$WORK/state_101.txt"   # restore
echo "  ok"

echo "ALL PASS"
