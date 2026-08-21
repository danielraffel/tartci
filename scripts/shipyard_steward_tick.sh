#!/usr/bin/env bash
# Deterministic, session-independent cross-repository Shipyard stewardship.
#
# This is deliberately a separate lane from shipyard_queue_tick.sh. It never
# invokes a model and never reimplements queue mutations: the installed
# Shipyard binary owns exact-head, merge-queue, retry, and cancellation policy.
set -uo pipefail

CANONICAL_CONFIG="${SHIPYARD_QUEUE_CANONICAL_CONFIG:-$HOME/.config/shipyard/queue-tick.env}"
HEALTH_FILE="${SHIPYARD_STEWARD_HEALTH_FILE:-$HOME/Library/Logs/shipyard-steward-tick.health.json}"
REPORT_FILE="${SHIPYARD_STEWARD_REPORT_FILE:-$HOME/Library/Logs/shipyard-steward-tick.json}"
TIMEOUT="${SHIPYARD_STEWARD_TIMEOUT_SECS:-120}"
MIN_VERSION="${SHIPYARD_STEWARD_MIN_VERSION:-0.97.1}"
SUPPORT="$(cd "$(dirname "$0")" && pwd)/shipyard_queue_tick_support.py"
SY="$(command -v shipyard 2>/dev/null || echo "$HOME/.local/bin/shipyard")"
HOST="$(scutil --get ComputerName 2>/dev/null || hostname)"
AUTHORITY=""
REPO_ROOT=""
GH=""
QUEUE_APPLY="${SHIPYARD_TICK_APPLY:-0}"
QUEUE_REAP_ONLY="${SHIPYARD_TICK_REAP_ONLY:-0}"

ts()  { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "$(ts) [steward-tick] $*"; }
health() {
  local status="$1" reason="$2" temp="${HEALTH_FILE}.tmp.$$"
  mkdir -p "$(dirname "$HEALTH_FILE")" 2>/dev/null || return 1
  python3 "$SUPPORT" health "$temp" "$status" "$reason" "$HOST" "$(ts)" 2>/dev/null \
    && mv "$temp" "$HEALTH_FILE"
}
unhealthy() {
  log "$HOST: UNHEALTHY: $1"
  health "unhealthy" "$1" || log "$HOST: health verdict could not be persisted"
  exit 2
}

case "$TIMEOUT" in ''|*[!0-9]*) unhealthy "SHIPYARD_STEWARD_TIMEOUT_SECS must be an integer" ;; esac
[ "$TIMEOUT" -ge 1 ] && [ "$TIMEOUT" -le 240 ] \
  || unhealthy "SHIPYARD_STEWARD_TIMEOUT_SECS must be 1..240"
[ -f "$CANONICAL_CONFIG" ] || unhealthy "canonical authority config is missing: $CANONICAL_CONFIG"
mode="$(stat -f '%Lp' "$CANONICAL_CONFIG" 2>/dev/null || stat -c '%a' "$CANONICAL_CONFIG" 2>/dev/null || echo '')"
[ "$mode" = "600" ] || unhealthy "canonical config must be mode 600"
owner="$(stat -f '%u' "$CANONICAL_CONFIG" 2>/dev/null || stat -c '%u' "$CANONICAL_CONFIG" 2>/dev/null || echo '')"
[ "$owner" = "$(id -u)" ] || unhealthy "canonical config must be owned by uid $(id -u)"
while IFS='=' read -r key value; do
  case "$key" in
    SHIPYARD_QUEUE_REPO_ROOT) REPO_ROOT="$value" ;;
    SHIPYARD_QUEUE_AUTHORITY) AUTHORITY="$value" ;;
    SHIPYARD_QUEUE_GH_CLI) GH="$value" ;;
    ''|'#'*) ;;
    *) unhealthy "canonical config contains unsupported key $key" ;;
  esac
done < "$CANONICAL_CONFIG"

case "$QUEUE_APPLY" in 0|1) ;; *) unhealthy "SHIPYARD_TICK_APPLY must be 0 or 1" ;; esac
case "$QUEUE_REAP_ONLY" in 0|1) ;; *) unhealthy "SHIPYARD_TICK_REAP_ONLY must be 0 or 1" ;; esac
if [ "$QUEUE_APPLY" != "1" ] || [ "$QUEUE_REAP_ONLY" = "1" ]; then
  health "healthy" "disabled_without_live_authority" || exit 2
  log "$HOST: stewardship disabled without live authority"
  exit 0
fi
[ "$AUTHORITY" = "1" ] \
  || unhealthy "live stewardship requires SHIPYARD_QUEUE_AUTHORITY=1"
[ -d "$REPO_ROOT" ] || unhealthy "authority repository checkout is unavailable"
remote="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
authority_repo="$(python3 "$SUPPORT" github-origin "$remote" 2>/dev/null || true)"
[ "$authority_repo" = "Generous-Corp/pulp" ] \
  || unhealthy "authority checkout must be Generous-Corp/pulp"
[ -n "$GH" ] && [ "$(basename "$GH")" != "gh" ] \
  || unhealthy "steward tick requires an explicit GitHub App wrapper"
command -v "$GH" >/dev/null 2>&1 || unhealthy "GitHub App wrapper is not executable: $GH"

installed="$("$SY" --version 2>/dev/null | awk '{print $2}')"
compatible="$(python3 "$SUPPORT" version-compatible "$installed" "$MIN_VERSION" 2>/dev/null || true)"
[ "$compatible" = "1" ] || unhealthy "Shipyard $MIN_VERSION or newer is required"
control="$(cd "$REPO_ROOT" && "$SY" merge-queue status --json 2>/dev/null)" \
  || unhealthy "merge-queue control unavailable"
control_flags="$(printf '%s' "$control" | python3 "$SUPPORT" control-flags 2>/dev/null)" \
  || unhealthy "merge-queue control schema malformed"
held="${control_flags%%|*}"
authority_matches="${control_flags##*|}"
[ "$held" != "1" ] || unhealthy "merge queue is held"
[ "$authority_matches" = "1" ] || unhealthy "runner tag does not match mutation_machine"
python3 "$SUPPORT" authority-read "$GH" "$authority_repo" >/dev/null 2>&1 \
  || unhealthy "GitHub App authority-repo read failed"

auth_export="$(cd "$REPO_ROOT" && "$SY" auth export --json 2>/dev/null)" \
  || unhealthy "Shipyard effective GitHub auth config is unavailable"
auth_mode="$(printf '%s' "$auth_export" | python3 "$SUPPORT" auth-mode "$GH" 2>/dev/null)" \
  || unhealthy "Shipyard auth is not bound to the configured GitHub App wrapper"
APP_TOKEN=""
if [ "$auth_mode" = "inject" ]; then
  APP_TOKEN="$(python3 "$SUPPORT" app-token "$GH" 2>/dev/null)" \
    || unhealthy "GitHub App wrapper could not provide a bounded token"
fi

mkdir -p "$(dirname "$REPORT_FILE")" || unhealthy "report directory is unavailable"
report_tmp="${REPORT_FILE}.tmp.$$"
trap 'rm -f "$report_tmp"' EXIT
health "starting" "authority_and_auth_validated" || unhealthy "could not publish starting health"
command=("$SY" runner steward
  --repo Generous-Corp/pulp
  --repo Generous-Corp/forge
  --repo Generous-Corp/vellum
  --apply --json)
if [ "$auth_mode" = "inject" ]; then
  (cd "$REPO_ROOT" && GH_TOKEN="$APP_TOKEN" \
    python3 "$SUPPORT" run-bounded "$TIMEOUT" "${command[@]}") > "$report_tmp"
else
  (cd "$REPO_ROOT" && \
    python3 "$SUPPORT" run-bounded "$TIMEOUT" "${command[@]}") > "$report_tmp"
fi
status=$?
if [ "$status" -ne 0 ]; then
  unhealthy "steward command failed or timed out (exit=$status)"
fi
python3 - "$report_tmp" <<'PY' || unhealthy "steward JSON report is malformed"
import json, sys
with open(sys.argv[1], encoding="utf-8") as source:
    value = json.load(source)
expected_repos = {
    "Generous-Corp/pulp",
    "Generous-Corp/forge",
    "Generous-Corp/vellum",
}
if (
    not isinstance(value, dict)
    or type(value.get("schema_version")) is not int
    or value["schema_version"] != 1
    or value.get("command") != "runner.steward"
    or value.get("apply") is not True
    or not isinstance(value.get("handoff_ledger"), str)
    or not value["handoff_ledger"]
    or not isinstance(value.get("repos"), list)
):
    raise SystemExit(1)
repos = value["repos"]
if len(repos) != len(expected_repos):
    raise SystemExit(1)
for repo in repos:
    if (
        not isinstance(repo, dict)
        or not isinstance(repo.get("repo"), str)
        or not isinstance(repo.get("base"), str)
        or type(repo.get("allow_auto_merge")) is not bool
        or type(repo.get("merge_queue")) is not bool
        or not isinstance(repo.get("merge_path"), str)
        or not isinstance(repo.get("required_contexts"), list)
        or not all(isinstance(item, str) for item in repo["required_contexts"])
        or not isinstance(repo.get("prs"), list)
        or not isinstance(repo.get("cancellations"), list)
        or repo.get("errors") != []
    ):
        raise SystemExit(1)
if {repo["repo"] for repo in repos} != expected_repos:
    raise SystemExit(1)
PY
mv "$report_tmp" "$REPORT_FILE" || unhealthy "could not publish steward report"
health "healthy" "pulp_forge_vellum_steward_complete" || exit 2
log "$HOST: deterministic stewardship complete"
