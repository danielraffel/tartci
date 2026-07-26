#!/usr/bin/env bash
# Install the queue tick plus its trusted canonical authority configuration.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: install_shipyard_queue_tick.sh --repo-root PATH [--authority] [--install]

Validates and prints the install plan by default. --install writes the mode-600
canonical config, renders the LaunchAgent, bootstraps it, and verifies launchd
received the expected paths. Exactly one fleet host should use --authority.
EOF
}

REPO_ROOT=""
AUTHORITY=0
APPLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo-root) REPO_ROOT="${2:-}"; shift 2 ;;
    --authority) AUTHORITY=1; shift ;;
    --install) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -d "$REPO_ROOT/.git" ] || git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1 || {
  echo "repo root is not a Git checkout: $REPO_ROOT" >&2
  exit 2
}
REMOTE="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
case "$REMOTE" in
  *github.com:*/*|*github.com/*/*) ;;
  *) echo "repo root has no GitHub origin: $REPO_ROOT" >&2; exit 2 ;;
esac

HERE="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$HERE/launchd/com.danielraffel.shipyard.queue-tick.plist.template"
SCRIPT="$HERE/scripts/shipyard_queue_tick.sh"
CONFIG="$HOME/.config/shipyard/queue-tick.env"
PLIST="$HOME/Library/LaunchAgents/com.danielraffel.shipyard.queue-tick.plist"
LABEL="com.danielraffel.shipyard.queue-tick"

[ -f "$TEMPLATE" ] && [ -f "$SCRIPT" ] || {
  echo "installer must run from a complete tartci checkout" >&2
  exit 2
}

echo "queue tick install plan:"
echo "  repo_root=$REPO_ROOT"
echo "  authority=$AUTHORITY"
echo "  canonical_config=$CONFIG (mode 600)"
echo "  launch_agent=$PLIST"
if [ "$APPLY" != "1" ]; then
  echo "  mode=dry-run (pass --install to apply)"
  exit 0
fi

mkdir -p "$HOME/.config/shipyard" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
CONFIG_TMP="$(mktemp "$HOME/.config/shipyard/.queue-tick.env.XXXXXX")"
PLIST_TMP="$(mktemp "$HOME/Library/LaunchAgents/.queue-tick.plist.XXXXXX")"
trap 'rm -f "$CONFIG_TMP" "$PLIST_TMP"' EXIT
umask 077
{
  printf 'SHIPYARD_QUEUE_REPO_ROOT=%s\n' "$REPO_ROOT"
  printf 'SHIPYARD_QUEUE_AUTHORITY=%s\n' "$AUTHORITY"
} > "$CONFIG_TMP"
chmod 600 "$CONFIG_TMP"
mv "$CONFIG_TMP" "$CONFIG"

sed -e "s|\$HOME|$HOME|g" "$TEMPLATE" > "$PLIST_TMP"
plutil -lint "$PLIST_TMP" >/dev/null
mv "$PLIST_TMP" "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

PRINTED="$(launchctl print "gui/$(id -u)/$LABEL")"
grep -Fq "$HOME/.config/shipyard/queue-tick.env" <<<"$PRINTED" || {
  echo "LaunchAgent did not receive canonical config path" >&2
  exit 1
}
echo "installed and started $LABEL"
