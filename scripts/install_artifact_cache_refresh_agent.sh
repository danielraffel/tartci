#!/usr/bin/env bash
# Install (or refresh) the artifact-cache mirror refresher LaunchAgent.
#
# `tartci setup` runs this. The agent is a no-op on a host whose artifact cache
# holds no git mirror (refresh never creates one), so installing it everywhere
# changes nothing until a host opts in with `tartci artifact-cache git-sync`,
# and from then on keeps that mirror current without anyone remembering to.
#
# Same shape and guards as install_reclaim_agent.sh: idempotent, writes and
# (re)bootstraps only when the rendered plist differs or the label is not
# loaded, boots out a registration held from any other plist, and never touches
# the real gui/<uid> domain from a temporary HOME.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.danielraffel.tartci.artifact-cache-refresh"
TEMPLATE="$HERE/launchd/$LABEL.plist.template"
AGENTS_DIR="${TARTCI_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
TARGET="$AGENTS_DIR/$LABEL.plist"
LAUNCHCTL="${TARTCI_LAUNCHCTL_BIN:-/bin/launchctl}"
PROFILE="${TARTCI_FLEET_PROFILE:-$HOME/.config/tartci/macos-fleet-profile.toml}"
# shellcheck source=scripts/launchd_domain_guard.sh
. "$HERE/scripts/launchd_domain_guard.sh"
APPLY=0
case "${1:-}" in
  --install) APPLY=1 ;;
  ""|--plan) ;;
  -h|--help) echo "usage: install_artifact_cache_refresh_agent.sh [--plan|--install]"; exit 0 ;;
  *) echo "usage: install_artifact_cache_refresh_agent.sh [--plan|--install]" >&2; exit 2 ;;
esac

[ -f "$TEMPLATE" ] || { echo "install_artifact_cache_refresh_agent: missing $TEMPLATE" >&2; exit 3; }
# Before ANY launchctl call, print included: a temp HOME must never reach the
# real gui/<uid> domain, where this label would replace the host's agent.
tartci_launchd_domain_guard "$TARGET" "$LAUNCHCTL" || exit 4

# The runners mount the installed profile's [host].cache_root, rendered into
# their plists as TARTCI_CI_CACHE. A host that moves its CI cache off the home
# default declares it there, and this agent must refresh that same cache. The
# profile needs tomllib; /usr/bin/python3 (3.9) has none.
cache_root=""
if [ -f "$PROFILE" ]; then
  PY=""
  for candidate in ${TARTCI_PYTHON:-} python3.13 python3.12 python3.11 python3; do
    candidate="$(command -v "$candidate" 2>/dev/null || true)"
    if [ -n "$candidate" ] && "$candidate" -c 'import tomllib' >/dev/null 2>&1; then
      PY="$candidate"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "install_artifact_cache_refresh_agent: no Python with tomllib to read $PROFILE;" \
      "refusing to guess the CI cache" >&2
    exit 5
  fi
  cache_root="$("$PY" - "$PROFILE" <<'PYEOF'
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    value = (tomllib.load(handle).get("host") or {}).get("cache_root", "")
print(value if isinstance(value, str) and value.startswith("/") else "")
PYEOF
)"
fi
render_args=(--set "HOME=$HOME")
[ -n "$cache_root" ] && render_args+=(--environment "TARTCI_CI_CACHE=$cache_root")

rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
python3 "$HERE/scripts/render_launchd_template.py" "$TEMPLATE" ${render_args[@]+"${render_args[@]}"} >"$rendered"
plutil -lint "$rendered" >/dev/null 2>&1 ||
  python3 -c 'import plistlib,sys; plistlib.load(open(sys.argv[1],"rb"))' "$rendered"

# loaded=1: launchd holds this label FROM $TARGET. loaded=2: it holds the label
# from some other plist, which is a leaked registration shadowing this agent
# (a temp-HOME install); it has to be booted out, never reported as installed.
loaded=0 held_path=""
if held="$("$LAUNCHCTL" print "gui/$(id -u)/$LABEL" 2>/dev/null)"; then
  held_path="$(printf '%s\n' "$held" | sed -n 's/^[[:space:]]*path = //p' | head -1)"
  if [ "$held_path" = "$TARGET" ]; then loaded=1; else loaded=2; fi
fi
same=0
[ -f "$TARGET" ] && cmp -s "$rendered" "$TARGET" && same=1

if [ "$same" = 1 ] && [ "$loaded" = 1 ]; then
  echo "artifact-cache refresh agent: already installed and loaded ($LABEL)"
  exit 0
fi
if [ "$loaded" = 2 ]; then
  echo "plan: $LABEL is loaded from ${held_path:-an unknown plist}, not $TARGET (leaked registration); boot it out"
fi
if [ "$same" = 1 ]; then
  echo "plan: $TARGET is current but $LABEL is not loaded from it; bootstrap"
else
  echo "plan: write $TARGET (every 6h; runs: tartci artifact-cache refresh --compact-above 16)"
  echo "plan: launchctl bootstrap gui/$(id -u) $TARGET"
fi
if [ "$APPLY" != 1 ]; then
  echo "(plan only; re-run with --install)"
  exit 0
fi

mkdir -p "$AGENTS_DIR" "$HOME/Library/Logs/tartci"
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
[ "$loaded" = 1 ] || "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$TARGET"
"$LAUNCHCTL" print "gui/$(id -u)/$LABEL" >/dev/null
echo "artifact-cache refresh agent: installed and loaded ($LABEL)"
