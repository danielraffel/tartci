#!/usr/bin/env bash
# Install the queue-tick ship-state reaper and its canonical configuration.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SUPPORT="$HERE/scripts/shipyard_queue_tick_support.py"

usage() {
  cat <<'EOF'
usage: install_shipyard_queue_tick.sh --gh-cli APP-WRAPPER
       [--mode dry-run|reap] [--repo-root PATH] [--install]

Validates and prints the install plan by default. --install writes the mode-600
canonical config, renders the LaunchAgent, bootstraps it, and verifies launchd
received the expected paths and the first tick reports healthy. The default
mode is dry-run; reap discards ship-state for merged, closed or confirmed
nonexistent pull requests. The tick never merges: landing is the GitHub merge
queue's job. --repo-root sets the checkout the merge-queue hold check and the
superseded merge-group reaper run from; without it the reaper is skipped.
Every mode requires an explicit GitHub App wrapper.
EOF
}

REPO_ROOT=""
APPLY=0
MODE="dry-run"
GH_CLI=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo-root) REPO_ROOT="${2:-}"; shift 2 ;;
    --authority)
      echo "--authority is retired: the queue tick no longer merges; drop the flag" >&2
      exit 2
      ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --gh-cli) GH_CLI="${2:-}"; shift 2 ;;
    --install) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
case "$MODE" in
  dry-run) TICK_APPLY=0 ;;
  reap|reap-only) MODE="reap"; TICK_APPLY=1 ;;
  live)
    echo "--mode live is retired: the queue tick no longer merges; use --mode reap" >&2
    exit 2
    ;;
  *) echo "invalid mode: $MODE" >&2; usage >&2; exit 2 ;;
esac
[ -n "$GH_CLI" ] && [ "$(basename "$GH_CLI")" != "gh" ] \
  && command -v "$GH_CLI" >/dev/null 2>&1 || {
  echo "all modes require --gh-cli with an executable GitHub App wrapper (not gh)" >&2
  exit 2
}

if [ -n "$REPO_ROOT" ]; then
  [ -d "$REPO_ROOT" ] || {
    echo "repo root is not a directory: $REPO_ROOT" >&2
    exit 2
  }
  REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"
fi

TEMPLATE="$HERE/launchd/com.danielraffel.shipyard.queue-tick.plist.template"
SCRIPT="$HERE/scripts/shipyard_queue_tick.sh"
# The agent runs the tick through ~/.local/bin/tartci, so it follows the
# installed generation; nothing is copied, and a self-update reaches it.
ENTRYPOINT="$HOME/.local/bin/tartci"
CONFIG="$HOME/.config/shipyard/queue-tick.env"
PLIST="$HOME/Library/LaunchAgents/com.danielraffel.shipyard.queue-tick.plist"
HEALTH="$HOME/Library/Logs/shipyard-queue-tick.health.json"
# The install waits for the first tick to FINISH, however long it runs: a tick
# reads every open pull request, and one that took longer than a fixed wait
# was read as a failed install and rolled back. HEALTH_WAIT_SECS is how long a
# tick that is NOT running may go without publishing a verdict (it has not
# started yet, or it exited without one). TICK_MAX_SECS bounds a running tick.
HEALTH_WAIT_SECS="${SHIPYARD_QUEUE_INSTALL_HEALTH_WAIT_SECS:-120}"
TICK_MAX_SECS="${SHIPYARD_QUEUE_INSTALL_TICK_MAX_SECS:-3600}"
for setting in HEALTH_WAIT_SECS TICK_MAX_SECS; do
  case "${!setting}" in
    ''|*[!0-9]*|0) echo "SHIPYARD_QUEUE_INSTALL_${setting} must be a positive integer" >&2; exit 2 ;;
  esac
  [ "${!setting}" -le 3600 ] || {
    echo "SHIPYARD_QUEUE_INSTALL_${setting} must be at most 3600" >&2
    exit 2
  }
done
LABEL="com.danielraffel.shipyard.queue-tick"
DOMAIN="gui/$(id -u)"
# Exit status when the install failed AND the tick is left unloaded: the host
# has no queue tick at all until someone bootstraps it.
EXIT_LEFT_UNLOADED=4

job_loaded() {
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1
}

job_state() {
  launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
    | awk -F' = ' '$1 ~ /^[[:space:]]*state$/ { print $2; exit }'
}

# `launchctl bootout` returns before the job is gone, and a bootstrap that
# races it fails with "Bootstrap failed: 5: Input/output error". That is how
# a rollback once left the tick silently unloaded. Wait for the old job to go,
# retry, and report whether it is loaded now.
bootstrap_reliably() {
  local plist="$1" tries=0
  while job_loaded && [ "$tries" -lt 20 ]; do
    sleep 0.5
    tries=$((tries + 1))
  done
  tries=0
  while [ "$tries" -lt 6 ]; do
    launchctl bootstrap "$DOMAIN" "$plist" >/dev/null 2>&1 || true
    job_loaded && return 0
    sleep $((tries + 1))
    tries=$((tries + 1))
  done
  return 1
}

publish_unloaded_health() {
  mkdir -p "$(dirname "$HEALTH")" 2>/dev/null || return 0
  printf '{"schema_version": 1, "status": "unhealthy", "reason": "agent_unloaded: %s", "observed_at": "%s"}\n' \
    "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$HEALTH" 2>/dev/null || true
}

[ -f "$TEMPLATE" ] && [ -f "$SCRIPT" ] && [ -f "$SUPPORT" ] || {
  echo "installer must run from a complete tartci checkout" >&2
  exit 2
}

echo "queue tick install plan:"
echo "  repo_root=${REPO_ROOT:-unset (hold check runs from \$HOME)}"
echo "  mode=$MODE"
echo "  gh_cli=${GH_CLI:-unset}"
echo "  executable=$ENTRYPOINT queue-tick (the installed generation)"
echo "  canonical_config=$CONFIG (mode 600)"
echo "  launch_agent=$PLIST"
if [ "$APPLY" != "1" ]; then
  echo "  action=dry-run (pass --install to apply)"
  exit 0
fi

mkdir -p "$HOME/.config/shipyard" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
CONFIG_TMP=""
PLIST_TMP=""
BACKUP=""
PRIOR_LOADED=0
SWITCH_STARTED=0
COMMITTED=0
rollback_and_cleanup() {
  rc=$?
  trap - EXIT
  if [ "$SWITCH_STARTED" = "1" ] && [ "$COMMITTED" != "1" ]; then
    set +e
    echo "install failed; rolling back prior queue-tick installation" >&2
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1
    for entry in config plist; do
      case "$entry" in
        config) target="$CONFIG" ;;
        plist) target="$PLIST" ;;
      esac
      if [ -f "$BACKUP/$entry.present" ]; then
        cp -p "$BACKUP/$entry" "$target"
      else
        rm -f "$target"
      fi
    done
    if [ "$PRIOR_LOADED" = "1" ] && [ -f "$PLIST" ]; then
      if ! bootstrap_reliably "$PLIST"; then
        echo "ROLLBACK FAILED: $LABEL is NOT LOADED; this host has no queue tick." >&2
        echo "  restore it with: launchctl bootstrap $DOMAIN $PLIST" >&2
        publish_unloaded_health "rollback could not re-bootstrap $PLIST"
        rc=$EXIT_LEFT_UNLOADED
      else
        # A RunAtLoad launch is speculative and launchd can defer it
        # indefinitely on a busy host; kickstart makes it an on-demand spawn.
        launchctl kickstart "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
        echo "rolled back: the prior $LABEL is loaded again" >&2
      fi
    fi
  fi
  rm -f "$CONFIG_TMP" "$PLIST_TMP"
  [ -z "$BACKUP" ] || rm -rf "$BACKUP"
  exit "$rc"
}
trap rollback_and_cleanup EXIT
CONFIG_TMP="$(mktemp "$HOME/.config/shipyard/.queue-tick.env.XXXXXX")"
PLIST_TMP="$(mktemp "$HOME/Library/LaunchAgents/.queue-tick.plist.XXXXXX")"
BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/queue-tick-install-backup.XXXXXX")"
umask 077
{
  printf 'SHIPYARD_QUEUE_REPO_ROOT=%s\n' "$REPO_ROOT"
  printf 'SHIPYARD_QUEUE_GH_CLI=%s\n' "$GH_CLI"
} > "$CONFIG_TMP"
chmod 600 "$CONFIG_TMP"

sed -e "s|\$HOME|$HOME|g" "$TEMPLATE" > "$PLIST_TMP"
python3 - "$PLIST_TMP" "$TICK_APPLY" <<'PY'
import plistlib, sys
path, apply = sys.argv[1:]
with open(path, "rb") as source:
    value = plistlib.load(source)
environment = value["EnvironmentVariables"]
environment["SHIPYARD_TICK_APPLY"] = apply
with open(path, "wb") as destination:
    plistlib.dump(value, destination, sort_keys=False)
PY
plutil -lint "$PLIST_TMP" >/dev/null

for entry in config plist; do
  case "$entry" in
    config) target="$CONFIG" ;;
    plist) target="$PLIST" ;;
  esac
  if [ -e "$target" ]; then
    cp -p "$target" "$BACKUP/$entry"
    : > "$BACKUP/$entry.present"
  fi
done
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  PRIOR_LOADED=1
fi

SWITCH_STARTED=1
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
mv "$CONFIG_TMP" "$CONFIG"
mv "$PLIST_TMP" "$PLIST"

rm -f "$HEALTH"
bootstrap_reliably "$PLIST" || {
  echo "LaunchAgent $LABEL did not load from $PLIST" >&2
  exit 1
}
launchctl kickstart -k "$DOMAIN/$LABEL"

PRINTED="$(launchctl print "gui/$(id -u)/$LABEL")"
grep -Fq "$HOME/.config/shipyard/queue-tick.env" <<<"$PRINTED" || {
  echo "LaunchAgent did not receive canonical config path" >&2
  exit 1
}
grep -Fq "$ENTRYPOINT" <<<"$PRINTED" || {
  echo "LaunchAgent does not run the tick through $ENTRYPOINT" >&2
  exit 1
}
# A verdict file the new tick wrote: healthy commits, anything else fails now.
verdict() {
  python3 - "$HEALTH" <<'PY' 2>/dev/null
import json, sys
try:
    with open(sys.argv[1]) as source:
        value = json.load(source)
except (OSError, ValueError):
    raise SystemExit(0)
if isinstance(value, dict):
    print(value.get("status") or "unknown", value.get("reason") or "")
PY
}
started="$(date +%s)"
idle_since="$started"
last_note="$started"
while :; do
  now="$(date +%s)"
  read -r status reason <<<"$(verdict)" || true
  if [ "$status" = "healthy" ]; then
    break
  elif [ -n "$status" ]; then
    echo "queue tick published an unhealthy verdict: $status ${reason:-}" >&2
    exit 1
  fi
  if [ "$(job_state)" = "running" ]; then
    idle_since="$now"
    if [ $((now - started)) -ge "$TICK_MAX_SECS" ]; then
      echo "queue tick still running after ${TICK_MAX_SECS}s without a verdict: $HEALTH" >&2
      exit 1
    fi
    if [ $((now - last_note)) -ge 60 ]; then
      echo "  waiting for the first tick to finish ($((now - started))s so far)"
      last_note="$now"
    fi
  elif [ $((now - idle_since)) -ge "$HEALTH_WAIT_SECS" ]; then
    echo "queue tick did not publish a fresh healthy verdict: $HEALTH" \
      "(not running for ${HEALTH_WAIT_SECS}s)" >&2
    exit 1
  fi
  sleep 1
done
COMMITTED=1
echo "installed and started $LABEL in $MODE mode; fresh health verdict is healthy"
