#!/usr/bin/env bash
# Install (or refresh) the per-host disk-reclaimer LaunchAgent.
#
# Unlike install_self_update_agent.sh, `tartci setup` DOES run this. The
# distinction is what the agent does to the host: an update takes the host down,
# which is an operator decision, while the reclaimer is what keeps the host
# alive. A tartci host admits work by lease and the lease has a disk axis, so a
# full data volume denies every lease and the host stops serving entirely —
# m5 reached 14 GiB free with 488 build dirs and refused 276 leases before
# anyone noticed. Leaving that to a manual step is how three hosts ended up
# without it.
#
# Idempotent, and safe to call on every setup: it renders the template, and only
# writes + (re)bootstraps when the rendered plist differs from what is installed
# or the label is not loaded. An unchanged, loaded agent is left strictly alone,
# because bootout/bootstrap of a running janitor mid-pass leaves state no later
# pass can classify.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.danielraffel.tartci.reclaim"
TEMPLATE="$HERE/launchd/$LABEL.plist.template"
AGENTS_DIR="${TARTCI_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
TARGET="$AGENTS_DIR/$LABEL.plist"
APPLY=0
case "${1:-}" in
  --install) APPLY=1 ;;
  ""|--plan) ;;
  -h|--help) echo "usage: install_reclaim_agent.sh [--plan|--install]"; exit 0 ;;
  *) echo "usage: install_reclaim_agent.sh [--plan|--install]" >&2; exit 2 ;;
esac

[ -f "$TEMPLATE" ] || { echo "install_reclaim_agent: missing $TEMPLATE" >&2; exit 3; }

rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
python3 "$HERE/scripts/render_launchd_template.py" "$TEMPLATE" --set "HOME=$HOME" >"$rendered"
plutil -lint "$rendered" >/dev/null 2>&1 ||
  python3 -c 'import plistlib,sys; plistlib.load(open(sys.argv[1],"rb"))' "$rendered"

loaded=0
launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 && loaded=1
same=0
[ -f "$TARGET" ] && cmp -s "$rendered" "$TARGET" && same=1

if [ "$same" = 1 ] && [ "$loaded" = 1 ]; then
  echo "reclaim agent: already installed and loaded ($LABEL)"
  exit 0
fi
if [ "$same" = 1 ]; then
  echo "plan: $TARGET is current but $LABEL is not loaded; bootstrap only"
else
  echo "plan: write $TARGET (hourly; runs: tartci reclaim --json --fix)"
  echo "plan: launchctl bootstrap gui/$(id -u) $TARGET"
fi
if [ "$APPLY" != 1 ]; then
  echo "(plan only; re-run with --install)"
  exit 0
fi

mkdir -p "$AGENTS_DIR" "$HOME/Library/Logs/tartci"
if [ "$same" != 1 ]; then
  install -m 0644 "$rendered" "$TARGET"
  # A launchd daemon respawns a CACHED spec and never re-reads the plist, so a
  # changed file is not a changed agent until bootout+bootstrap.
  [ "$loaded" = 1 ] && launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  loaded=0
fi
[ "$loaded" = 1 ] || launchctl bootstrap "gui/$(id -u)" "$TARGET"
launchctl print "gui/$(id -u)/$LABEL" >/dev/null
echo "reclaim agent: installed and loaded ($LABEL)"
