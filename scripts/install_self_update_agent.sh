#!/usr/bin/env bash
# Install the periodic tartci self-update LaunchAgent. Prints the plan by
# default; --install renders, bootstraps and verifies it. Never run by tartci
# itself: enabling automatic updates on a host is an operator decision.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.danielraffel.tartci.self-update"
TEMPLATE="$HERE/launchd/$LABEL.plist.template"
AGENTS_DIR="${TARTCI_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
TARGET="$AGENTS_DIR/$LABEL.plist"
APPLY=0
case "${1:-}" in
  --install) APPLY=1 ;;
  ""|--plan) ;;
  -h|--help) echo "usage: install_self_update_agent.sh [--plan|--install]"; exit 0 ;;
  *) echo "usage: install_self_update_agent.sh [--plan|--install]" >&2; exit 2 ;;
esac
# Peers resolve from the published supply (each profile's host.ssh, else the
# alias tartci-<host_id>); ~/.config/tartci/self-update.toml [peers] only
# overrides. Show the resolution so a missing alias is visible before the
# agent's first run refuses on it.
if [ "${TARTCI_SELF_UPDATE_SKIP_PEERS:-0}" != 1 ]; then
  echo "peers (host_id -> ssh target):"
  "$HERE/tartci" fleet-macos self-update --peers | sed 's/^/  /' || {
    echo "refusing: peers could not be resolved; see above" >&2
    exit 3
  }
fi
rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
python3 "$HERE/scripts/render_launchd_template.py" "$TEMPLATE" --set "HOME=$HOME" >"$rendered"
plutil -lint "$rendered" >/dev/null 2>&1 || python3 -c 'import plistlib,sys; plistlib.load(open(sys.argv[1],"rb"))' "$rendered"
echo "plan: write $TARGET (StartInterval 1800, runs: tartci fleet-macos self-update --apply --scheduled)"
echo "plan: launchctl bootstrap gui/$(id -u) $TARGET"
if [ "$APPLY" != 1 ]; then
  echo "(plan only; re-run with --install)"
  exit 0
fi
mkdir -p "$AGENTS_DIR" "$HOME/Library/Logs/tartci"
install -m 0644 "$rendered" "$TARGET"
launchctl bootstrap "gui/$(id -u)" "$TARGET"
launchctl print "gui/$(id -u)/$LABEL" >/dev/null
echo "installed and loaded $LABEL"
