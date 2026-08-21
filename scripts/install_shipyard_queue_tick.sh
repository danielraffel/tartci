#!/usr/bin/env bash
# Install the queue tick plus its trusted canonical authority configuration.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SUPPORT="$HERE/scripts/shipyard_queue_tick_support.py"

usage() {
  cat <<'EOF'
usage: install_shipyard_queue_tick.sh --repo-root PATH [--authority]
       [--mode dry-run|reap-only|live] [--gh-cli APP-WRAPPER] [--install]

Validates and prints the install plan by default. --install writes the mode-600
canonical config, renders the LaunchAgent, bootstraps it, and verifies launchd
received the expected paths and both first-tick lanes report healthy. Exactly one
fleet host should use --authority. The default mode is dry-run; live requires
--authority. Every mode requires an explicit GitHub App wrapper.
EOF
}

REPO_ROOT=""
AUTHORITY=0
APPLY=0
MODE="dry-run"
GH_CLI=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo-root) REPO_ROOT="${2:-}"; shift 2 ;;
    --authority) AUTHORITY=1; shift ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --gh-cli) GH_CLI="${2:-}"; shift 2 ;;
    --install) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done
case "$MODE" in
  dry-run) TICK_APPLY=0; REAP_ONLY=0 ;;
  reap-only) TICK_APPLY=1; REAP_ONLY=1 ;;
  live)
    [ "$AUTHORITY" = "1" ] || {
      echo "--mode live requires --authority" >&2
      exit 2
    }
    TICK_APPLY=1
    REAP_ONLY=0
    ;;
  *) echo "invalid mode: $MODE" >&2; usage >&2; exit 2 ;;
esac
[ -n "$GH_CLI" ] && [ "$(basename "$GH_CLI")" != "gh" ] \
  && command -v "$GH_CLI" >/dev/null 2>&1 || {
  echo "all modes require --gh-cli with an executable GitHub App wrapper (not gh)" >&2
  exit 2
}

[ -d "$REPO_ROOT/.git" ] || git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1 || {
  echo "repo root is not a Git checkout: $REPO_ROOT" >&2
  exit 2
}
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"
REMOTE="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
if ! python3 "$SUPPORT" github-origin "$REMOTE" >/dev/null
then
  echo "repo root has no supported GitHub origin: $REPO_ROOT" >&2
  exit 2
fi

TEMPLATE="$HERE/launchd/com.danielraffel.shipyard.queue-tick.plist.template"
SCRIPT="$HERE/scripts/shipyard_queue_tick.sh"
SERVICE="$HERE/scripts/shipyard_queue_service_tick.sh"
SERVICE_SUPPORT="$HERE/scripts/shipyard_queue_service_tick.py"
STEWARD="$HERE/scripts/shipyard_steward_tick.sh"
INSTALL_DIR="$HOME/.local/share/tartci/scripts"
INSTALLED_SCRIPT="$INSTALL_DIR/shipyard_queue_tick.sh"
INSTALLED_SERVICE="$INSTALL_DIR/shipyard_queue_service_tick.sh"
INSTALLED_SERVICE_SUPPORT="$INSTALL_DIR/shipyard_queue_service_tick.py"
INSTALLED_STEWARD="$INSTALL_DIR/shipyard_steward_tick.sh"
INSTALLED_SUPPORT="$INSTALL_DIR/shipyard_queue_tick_support.py"
CONFIG="$HOME/.config/shipyard/queue-tick.env"
PLIST="$HOME/Library/LaunchAgents/com.danielraffel.shipyard.queue-tick.plist"
HEALTH="$HOME/Library/Logs/shipyard-queue-tick.health.json"
STEWARD_HEALTH="$HOME/Library/Logs/shipyard-steward-tick.health.json"
HEALTH_WAIT_SECS="${SHIPYARD_QUEUE_INSTALL_HEALTH_WAIT_SECS:-300}"
case "$HEALTH_WAIT_SECS" in
  ''|*[!0-9]*|0) echo "SHIPYARD_QUEUE_INSTALL_HEALTH_WAIT_SECS must be a positive integer" >&2; exit 2 ;;
esac
[ "$HEALTH_WAIT_SECS" -le 3600 ] || {
  echo "SHIPYARD_QUEUE_INSTALL_HEALTH_WAIT_SECS must be at most 3600" >&2
  exit 2
}
LABEL="com.danielraffel.shipyard.queue-tick"

[ -f "$TEMPLATE" ] && [ -f "$SCRIPT" ] && [ -f "$SERVICE" ] && [ -f "$SERVICE_SUPPORT" ] \
  && [ -f "$STEWARD" ] && [ -f "$SUPPORT" ] || {
  echo "installer must run from a complete tartci checkout" >&2
  exit 2
}

echo "queue tick install plan:"
echo "  repo_root=$REPO_ROOT"
echo "  authority=$AUTHORITY"
echo "  mode=$MODE"
echo "  gh_cli=${GH_CLI:-unset}"
echo "  executable=$INSTALLED_SCRIPT (mode 755)"
echo "  service=$INSTALLED_SERVICE (mode 755)"
echo "  service_support=$INSTALLED_SERVICE_SUPPORT (mode 644)"
echo "  steward=$INSTALLED_STEWARD (mode 755)"
echo "  support=$INSTALLED_SUPPORT (mode 644)"
echo "  canonical_config=$CONFIG (mode 600)"
echo "  launch_agent=$PLIST"
if [ "$APPLY" != "1" ]; then
  echo "  action=dry-run (pass --install to apply)"
  exit 0
fi

mkdir -p "$INSTALL_DIR" "$HOME/.config/shipyard" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
SCRIPT_TMP=""
SERVICE_TMP=""
SERVICE_SUPPORT_TMP=""
STEWARD_TMP=""
SUPPORT_TMP=""
CONFIG_TMP=""
PLIST_TMP=""
BACKUP=""
PRIOR_LOADED=0
SWITCH_STARTED=0
COMMITTED=0
queue_writer_active() {
  command -v pgrep >/dev/null 2>&1 || return 0
  pgrep -f "$INSTALLED_SCRIPT" >/dev/null 2>&1 \
    || pgrep -f "$INSTALLED_SERVICE" >/dev/null 2>&1 \
    || pgrep -f "$INSTALLED_STEWARD" >/dev/null 2>&1 \
    || pgrep -f 'shipyard (auto-merge|ship-state (discard|reconcile)|runner steward)' >/dev/null 2>&1
}
rollback_and_cleanup() {
  rc=$?
  trap - EXIT
  if [ "$SWITCH_STARTED" = "1" ] && [ "$COMMITTED" != "1" ]; then
    set +e
    echo "install failed; rolling back prior queue-tick installation" >&2
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1
    for entry in script service service_support steward support config plist; do
      case "$entry" in
        script) target="$INSTALLED_SCRIPT" ;;
        service) target="$INSTALLED_SERVICE" ;;
        service_support) target="$INSTALLED_SERVICE_SUPPORT" ;;
        steward) target="$INSTALLED_STEWARD" ;;
        support) target="$INSTALLED_SUPPORT" ;;
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
      quiescence_attempt=0
      while queue_writer_active && [ "$quiescence_attempt" -lt 5 ]; do
        sleep 1
        quiescence_attempt=$((quiescence_attempt + 1))
      done
      if queue_writer_active; then
        echo "rollback warning: candidate writer remains active; prior LaunchAgent left unloaded" >&2
      else
        launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || \
          echo "rollback warning: prior LaunchAgent could not be re-bootstrapped" >&2
      fi
    fi
  fi
  rm -f "$SCRIPT_TMP" "$SERVICE_TMP" "$SERVICE_SUPPORT_TMP" "$STEWARD_TMP" "$SUPPORT_TMP" "$CONFIG_TMP" "$PLIST_TMP"
  [ -z "$BACKUP" ] || rm -rf "$BACKUP"
  exit "$rc"
}
trap rollback_and_cleanup EXIT
SCRIPT_TMP="$(mktemp "$INSTALL_DIR/.shipyard_queue_tick.sh.XXXXXX")"
SERVICE_TMP="$(mktemp "$INSTALL_DIR/.shipyard_queue_service_tick.sh.XXXXXX")"
SERVICE_SUPPORT_TMP="$(mktemp "$INSTALL_DIR/.shipyard_queue_service_tick.py.XXXXXX")"
STEWARD_TMP="$(mktemp "$INSTALL_DIR/.shipyard_steward_tick.sh.XXXXXX")"
SUPPORT_TMP="$(mktemp "$INSTALL_DIR/.shipyard_queue_tick_support.py.XXXXXX")"
CONFIG_TMP="$(mktemp "$HOME/.config/shipyard/.queue-tick.env.XXXXXX")"
PLIST_TMP="$(mktemp "$HOME/Library/LaunchAgents/.queue-tick.plist.XXXXXX")"
BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/queue-tick-install-backup.XXXXXX")"
umask 077
install -m 755 "$SCRIPT" "$SCRIPT_TMP"
[ -x "$SCRIPT_TMP" ] && cmp -s "$SCRIPT" "$SCRIPT_TMP" || {
  echo "staged queue tick executable failed verification" >&2
  exit 1
}
install -m 755 "$SERVICE" "$SERVICE_TMP"
[ -x "$SERVICE_TMP" ] && cmp -s "$SERVICE" "$SERVICE_TMP" || {
  echo "staged queue service supervisor failed verification" >&2
  exit 1
}
install -m 644 "$SERVICE_SUPPORT" "$SERVICE_SUPPORT_TMP"
[ -r "$SERVICE_SUPPORT_TMP" ] && cmp -s "$SERVICE_SUPPORT" "$SERVICE_SUPPORT_TMP" || {
  echo "staged queue service support failed verification" >&2
  exit 1
}
install -m 755 "$STEWARD" "$STEWARD_TMP"
[ -x "$STEWARD_TMP" ] && cmp -s "$STEWARD" "$STEWARD_TMP" || {
  echo "staged steward tick executable failed verification" >&2
  exit 1
}
install -m 644 "$SUPPORT" "$SUPPORT_TMP"
[ -r "$SUPPORT_TMP" ] && cmp -s "$SUPPORT" "$SUPPORT_TMP" || {
  echo "staged queue tick support module failed verification" >&2
  exit 1
}
{
  printf 'SHIPYARD_QUEUE_REPO_ROOT=%s\n' "$REPO_ROOT"
  printf 'SHIPYARD_QUEUE_AUTHORITY=%s\n' "$AUTHORITY"
  printf 'SHIPYARD_QUEUE_GH_CLI=%s\n' "$GH_CLI"
} > "$CONFIG_TMP"
chmod 600 "$CONFIG_TMP"

sed -e "s|\$HOME|$HOME|g" "$TEMPLATE" > "$PLIST_TMP"
python3 - "$PLIST_TMP" "$TICK_APPLY" "$REAP_ONLY" <<'PY'
import plistlib, sys
path, apply, reap_only = sys.argv[1:]
with open(path, "rb") as source:
    value = plistlib.load(source)
environment = value["EnvironmentVariables"]
environment["SHIPYARD_TICK_APPLY"] = apply
environment["SHIPYARD_TICK_REAP_ONLY"] = reap_only
with open(path, "wb") as destination:
    plistlib.dump(value, destination, sort_keys=False)
PY
plutil -lint "$PLIST_TMP" >/dev/null

for entry in script service service_support steward support config plist; do
  case "$entry" in
    script) target="$INSTALLED_SCRIPT" ;;
    service) target="$INSTALLED_SERVICE" ;;
    service_support) target="$INSTALLED_SERVICE_SUPPORT" ;;
    steward) target="$INSTALLED_STEWARD" ;;
    support) target="$INSTALLED_SUPPORT" ;;
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

if [ "$PRIOR_LOADED" = "1" ]; then
  PRIOR_SPEC="$(launchctl print "gui/$(id -u)/$LABEL")"
  if grep -Eq '^[[:space:]]*state = running$' <<<"$PRIOR_SPEC"; then
    echo "queue-tick service is running; wait for this tick to finish and rerun" >&2
    exit 1
  fi
  SWITCH_STARTED=1
  launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || {
    echo "could not boot out the prior queue-tick LaunchAgent" >&2
    exit 1
  }
  if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    echo "prior queue-tick LaunchAgent is still loaded after bootout" >&2
    exit 1
  fi
  command -v pgrep >/dev/null 2>&1 || {
    echo "pgrep is required to verify queue-writer quiescence" >&2
    exit 1
  }
  if queue_writer_active; then
    # Leave the prior LaunchAgent unloaded: RunAtLoad must not start a second
    # writer while the detected process is still live.
    PRIOR_LOADED=0
    echo "prior queue writer is still active after bootout; LaunchAgent left unloaded — wait for quiescence and rerun" >&2
    exit 1
  fi
fi
[ "$PRIOR_LOADED" = "1" ] || {
  if queue_writer_active; then
    echo "queue writer is active while LaunchAgent is unloaded; wait for quiescence and rerun" >&2
    exit 1
  fi
}
[ "$SWITCH_STARTED" = "1" ] || SWITCH_STARTED=1
mv "$SCRIPT_TMP" "$INSTALLED_SCRIPT"
mv "$SERVICE_TMP" "$INSTALLED_SERVICE"
mv "$SERVICE_SUPPORT_TMP" "$INSTALLED_SERVICE_SUPPORT"
mv "$STEWARD_TMP" "$INSTALLED_STEWARD"
mv "$SUPPORT_TMP" "$INSTALLED_SUPPORT"
mv "$CONFIG_TMP" "$CONFIG"
mv "$PLIST_TMP" "$PLIST"

launchctl bootstrap "gui/$(id -u)" "$PLIST"
rm -f "$HEALTH" "$STEWARD_HEALTH"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

PRINTED="$(launchctl print "gui/$(id -u)/$LABEL")"
grep -Fq "$HOME/.config/shipyard/queue-tick.env" <<<"$PRINTED" || {
  echo "LaunchAgent did not receive canonical config path" >&2
  exit 1
}
grep -Fq "$INSTALLED_SERVICE" <<<"$PRINTED" || {
  echo "LaunchAgent did not receive installed queue service supervisor" >&2
  exit 1
}
healthy=0
attempt=0
while [ "$attempt" -lt "$HEALTH_WAIT_SECS" ]; do
  if python3 - "$HEALTH" "$STEWARD_HEALTH" <<'PY' >/dev/null 2>&1
import json, sys
for path in sys.argv[1:]:
    with open(path) as source:
        value = json.load(source)
    if not isinstance(value, dict) or value.get("status") != "healthy":
        raise SystemExit(1)
PY
  then
    healthy=1
    break
  fi
  sleep 1
  attempt=$((attempt + 1))
done
[ "$healthy" = "1" ] || {
  echo "queue service did not publish fresh healthy verdicts: $HEALTH and $STEWARD_HEALTH" >&2
  exit 1
}
COMMITTED=1
echo "installed and started $LABEL in $MODE mode; queue and steward health verdicts are healthy"
