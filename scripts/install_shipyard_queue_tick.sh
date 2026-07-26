#!/usr/bin/env bash
# Install the queue tick plus its trusted canonical authority configuration.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: install_shipyard_queue_tick.sh --repo-root PATH [--authority]
       [--mode dry-run|reap-only|live] [--install]

Validates and prints the install plan by default. --install writes the mode-600
canonical config, renders the LaunchAgent, bootstraps it, and verifies launchd
received the expected paths and the first tick reports healthy. Exactly one
fleet host should use --authority. The default mode is dry-run; live requires
--authority.
EOF
}

REPO_ROOT=""
AUTHORITY=0
APPLY=0
MODE="dry-run"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo-root) REPO_ROOT="${2:-}"; shift 2 ;;
    --authority) AUTHORITY=1; shift ;;
    --mode) MODE="${2:-}"; shift 2 ;;
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

[ -d "$REPO_ROOT/.git" ] || git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1 || {
  echo "repo root is not a Git checkout: $REPO_ROOT" >&2
  exit 2
}
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"
REMOTE="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
case "$REMOTE" in
  *github.com:*/*|*github.com/*/*) ;;
  *) echo "repo root has no GitHub origin: $REPO_ROOT" >&2; exit 2 ;;
esac

HERE="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$HERE/launchd/com.danielraffel.shipyard.queue-tick.plist.template"
SCRIPT="$HERE/scripts/shipyard_queue_tick.sh"
INSTALL_DIR="$HOME/.local/share/tartci/scripts"
INSTALLED_SCRIPT="$INSTALL_DIR/shipyard_queue_tick.sh"
CONFIG="$HOME/.config/shipyard/queue-tick.env"
PLIST="$HOME/Library/LaunchAgents/com.danielraffel.shipyard.queue-tick.plist"
HEALTH="$HOME/Library/Logs/shipyard-queue-tick.health.json"
LABEL="com.danielraffel.shipyard.queue-tick"

[ -f "$TEMPLATE" ] && [ -f "$SCRIPT" ] || {
  echo "installer must run from a complete tartci checkout" >&2
  exit 2
}

echo "queue tick install plan:"
echo "  repo_root=$REPO_ROOT"
echo "  authority=$AUTHORITY"
echo "  mode=$MODE"
echo "  executable=$INSTALLED_SCRIPT (mode 755)"
echo "  canonical_config=$CONFIG (mode 600)"
echo "  launch_agent=$PLIST"
if [ "$APPLY" != "1" ]; then
  echo "  action=dry-run (pass --install to apply)"
  exit 0
fi

mkdir -p "$INSTALL_DIR" "$HOME/.config/shipyard" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
SCRIPT_TMP=""
CONFIG_TMP=""
PLIST_TMP=""
trap 'rm -f "$SCRIPT_TMP" "$CONFIG_TMP" "$PLIST_TMP"' EXIT
SCRIPT_TMP="$(mktemp "$INSTALL_DIR/.shipyard_queue_tick.sh.XXXXXX")"
CONFIG_TMP="$(mktemp "$HOME/.config/shipyard/.queue-tick.env.XXXXXX")"
PLIST_TMP="$(mktemp "$HOME/Library/LaunchAgents/.queue-tick.plist.XXXXXX")"
umask 077
install -m 755 "$SCRIPT" "$SCRIPT_TMP"
mv "$SCRIPT_TMP" "$INSTALLED_SCRIPT"
[ -x "$INSTALLED_SCRIPT" ] && cmp -s "$SCRIPT" "$INSTALLED_SCRIPT" || {
  echo "installed queue tick executable failed verification: $INSTALLED_SCRIPT" >&2
  exit 1
}
{
  printf 'SHIPYARD_QUEUE_REPO_ROOT=%s\n' "$REPO_ROOT"
  printf 'SHIPYARD_QUEUE_AUTHORITY=%s\n' "$AUTHORITY"
} > "$CONFIG_TMP"
chmod 600 "$CONFIG_TMP"
mv "$CONFIG_TMP" "$CONFIG"

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
mv "$PLIST_TMP" "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
rm -f "$HEALTH"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

PRINTED="$(launchctl print "gui/$(id -u)/$LABEL")"
grep -Fq "$HOME/.config/shipyard/queue-tick.env" <<<"$PRINTED" || {
  echo "LaunchAgent did not receive canonical config path" >&2
  exit 1
}
grep -Fq "$INSTALLED_SCRIPT" <<<"$PRINTED" || {
  echo "LaunchAgent did not receive installed queue tick executable" >&2
  exit 1
}
healthy=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if python3 - "$HEALTH" <<'PY' >/dev/null 2>&1
import json, sys
with open(sys.argv[1]) as source:
    value = json.load(source)
if not isinstance(value, dict) or value.get("status") != "healthy":
    raise SystemExit(1)
PY
  then
    healthy=1
    break
  fi
  sleep 1
done
[ "$healthy" = "1" ] || {
  echo "queue tick did not publish a fresh healthy verdict: $HEALTH" >&2
  exit 1
}
echo "installed and started $LABEL in $MODE mode; fresh health verdict is healthy"
