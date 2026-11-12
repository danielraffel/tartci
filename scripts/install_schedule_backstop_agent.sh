#!/usr/bin/env bash
# Install (or refresh) the schedule-backstop LaunchAgent as this host's fleet
# profile asks: `schedule_backstop = "live" | "dry-run" | "off"`
# (scripts/schedule_backstop_mode.py). `tartci setup` runs this.
#
# The backstop must dispatch from exactly ONE host, so the switch lives in the
# reviewed fleet profile rather than in a hand-edited plist: on 2026-10-03 m3's
# live agent was a copied script plus a PlistBuddy edit that no self-update
# would ever refresh. The rendered agent runs `~/.local/bin/tartci
# schedule-backstop`, i.e. the installed generation's script, so an update
# carries the backstop with it.
#
#   live     render the agent with APPLY=1 and AUTHORITY=1
#   dry-run  render it with both 0 (logs what it would dispatch)
#   off      install nothing. An agent already present is reported, never
#            removed: an installed profile snapshot that predates the key reads
#            as off, and removing the one live dispatcher on that misreading
#            would be silent. Remove it deliberately with --uninstall.
#
# A mode it cannot determine (no tomllib, an unreadable profile, an invalid
# value) changes nothing and exits 5.
#
# Same shape and guards as install_reclaim_agent.sh: idempotent, writes and
# (re)bootstraps only when the rendered agent differs or the label is not
# loaded, boots out a registration held from any other plist, and never touches
# the real gui/<uid> domain from a temporary HOME. The backstop's own state
# (~/.local/state/tartci/schedule-backstop.json, its own-dispatch pacing) is
# never touched, so a reinstall keeps its pacing.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.danielraffel.pulp.schedule-backstop"
TEMPLATE="$HERE/launchd/$LABEL.plist.template"
AGENTS_DIR="${TARTCI_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
TARGET="$AGENTS_DIR/$LABEL.plist"
LAUNCHCTL="${TARTCI_LAUNCHCTL_BIN:-/bin/launchctl}"
PROFILE="${TARTCI_FLEET_PROFILE:-$HOME/.config/tartci/macos-fleet-profile.toml}"
# shellcheck source=scripts/launchd_domain_guard.sh
. "$HERE/scripts/launchd_domain_guard.sh"
usage="usage: install_schedule_backstop_agent.sh [--plan|--install|--uninstall]"
APPLY=0 UNINSTALL=0
case "${1:-}" in
  --install) APPLY=1 ;;
  --uninstall) APPLY=1 UNINSTALL=1 ;;
  ""|--plan) ;;
  -h|--help) echo "$usage"; exit 0 ;;
  *) echo "$usage" >&2; exit 2 ;;
esac

[ -f "$TEMPLATE" ] || { echo "install_schedule_backstop_agent: missing $TEMPLATE" >&2; exit 3; }
# Before ANY launchctl call, print included: a temp HOME must never reach the
# real gui/<uid> domain, where this label would replace the host's dispatcher.
tartci_launchd_domain_guard "$TARGET" "$LAUNCHCTL" || exit 4

# The profile needs tomllib; /usr/bin/python3 (3.9) has none.
PY=""
for candidate in ${TARTCI_PYTHON:-} python3.13 python3.12 python3.11 python3; do
  candidate="$(command -v "$candidate" 2>/dev/null || true)"
  if [ -n "$candidate" ] && "$candidate" -c 'import tomllib' >/dev/null 2>&1; then
    PY="$candidate"; break
  fi
done
[ -n "$PY" ] || PY=python3

# loaded=1: launchd holds this label FROM $TARGET. loaded=2: it holds the label
# from some other plist, a leaked registration shadowing this agent.
loaded=0 held_path=""
if held="$("$LAUNCHCTL" print "gui/$(id -u)/$LABEL" 2>/dev/null)"; then
  held_path="$(printf '%s\n' "$held" | sed -n 's/^[[:space:]]*path = //p' | head -1)"
  if [ "$held_path" = "$TARGET" ]; then loaded=1; else loaded=2; fi
fi

if [ "$UNINSTALL" = 1 ]; then
  [ "$loaded" = 0 ] || "$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$TARGET"
  echo "schedule backstop: uninstalled ($LABEL); the crons carry on alone"
  exit 0
fi

if ! mode="$("$PY" "$HERE/scripts/schedule_backstop_mode.py" --profile-file "$PROFILE")"; then
  echo "schedule backstop: mode unknown; nothing changed" >&2
  exit 5
fi

if [ "$mode" = off ]; then
  if [ "$loaded" != 0 ] || [ -f "$TARGET" ]; then
    echo "schedule backstop: profile says off, but $LABEL is present; left alone" \
         "(remove it with install_schedule_backstop_agent.sh --uninstall)"
  else
    echo "schedule backstop: off on this host; nothing to install"
  fi
  exit 0
fi

case "$mode" in
  live) apply_flag=1 ;;
  dry-run) apply_flag=0 ;;
  *) echo "schedule backstop: unexpected mode '$mode'; nothing changed" >&2; exit 5 ;;
esac

rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
python3 "$HERE/scripts/render_launchd_template.py" "$TEMPLATE" --set "HOME=$HOME" \
  --environment "TARTCI_BACKSTOP_APPLY=$apply_flag" \
  --environment "TARTCI_BACKSTOP_AUTHORITY=$apply_flag" >"$rendered"
plutil -lint "$rendered" >/dev/null 2>&1 ||
  python3 -c 'import plistlib,sys; plistlib.load(open(sys.argv[1],"rb"))' "$rendered"

# Compare as plists, not bytes: the launchd watchdog rewrites this agent with
# sorted keys, and a key-order difference is not a different agent.
same=0
if [ -f "$TARGET" ] && python3 -c '
import plistlib, sys
a, b = (plistlib.load(open(p, "rb")) for p in sys.argv[1:3])
sys.exit(0 if a == b else 1)' "$rendered" "$TARGET" 2>/dev/null; then
  same=1
fi

if [ "$same" = 1 ] && [ "$loaded" = 1 ]; then
  echo "schedule backstop: already installed and loaded ($LABEL, $mode)"
  exit 0
fi
if [ "$loaded" = 2 ]; then
  echo "plan: $LABEL is loaded from ${held_path:-an unknown plist}, not $TARGET (leaked registration); boot it out"
fi
if [ "$same" = 1 ]; then
  echo "plan: $TARGET is current but $LABEL is not loaded from it; bootstrap"
else
  echo "plan: write $TARGET ($mode; every 300s runs: tartci schedule-backstop)"
  echo "plan: launchctl bootstrap gui/$(id -u) $TARGET"
fi
if [ "$APPLY" != 1 ]; then
  echo "(plan only; re-run with --install)"
  exit 0
fi

mkdir -p "$AGENTS_DIR" "$HOME/Library/Logs"
if [ "$loaded" = 2 ]; then
  "$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  loaded=0
fi
if [ "$same" != 1 ]; then
  install -m 0644 "$rendered" "$TARGET"
  # A launchd daemon respawns a CACHED spec and never re-reads the plist, so a
  # changed file is not a changed agent until bootout+bootstrap.
  [ "$loaded" = 1 ] && "$LAUNCHCTL" bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  loaded=0
fi
# RunAtLoad is speculative and launchd can defer it indefinitely on a busy
# host; kickstart makes the first tick an on-demand spawn now.
if [ "$loaded" != 1 ]; then
  "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$TARGET"
  "$LAUNCHCTL" kickstart "gui/$(id -u)/$LABEL"
fi
"$LAUNCHCTL" print "gui/$(id -u)/$LABEL" >/dev/null
echo "schedule backstop: installed and loaded ($LABEL, $mode)"
