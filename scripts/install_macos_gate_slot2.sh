#!/usr/bin/env bash
# Render and install the managed second Pulp macOS gate supervisor.
set -euo pipefail

APPLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    *) echo "usage: install_macos_gate_slot2.sh [--apply]" >&2; exit 2 ;;
  esac
  shift
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.danielraffel.pulp.tart-runner-macos-gate-slot2"
PRIMARY_LABEL="com.danielraffel.pulp.tart-runner-macos-gate"
DOMAIN="gui/$(id -u)"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PRIMARY_PLIST="$AGENTS_DIR/$PRIMARY_LABEL.plist"
TARGET="$AGENTS_DIR/$LABEL.plist"
POOL_LIB="$ROOT/providers/common/pool.lib.sh"

[ -f "$PRIMARY_PLIST" ] || {
  echo "gate-slot2 install requires the managed primary plist at $PRIMARY_PLIST" >&2
  exit 2
}

TART_HOME="${TART_HOME:-$(python3 - "$PRIMARY_PLIST" <<'PY'
import plistlib, sys
with open(sys.argv[1], "rb") as source:
    value = plistlib.load(source)
print(value.get("EnvironmentVariables", {}).get("TART_HOME", ""))
PY
)}"
[ -n "$TART_HOME" ] || {
  echo "gate-slot2 install could not resolve TART_HOME from the primary plist" >&2
  exit 2
}

mkdir -p "$AGENTS_DIR" "$HOME/Library/Logs/tartci"
candidate="$(mktemp "$AGENTS_DIR/.macos-gate-slot2.plist.XXXXXX")"
lock_owned=0
cleanup() {
  rc=$?
  trap - EXIT
  if [ "$lock_owned" = 1 ]; then
    tartci_pool_lock_release || true
  fi
  rm -f "$candidate"
  exit "$rc"
}
trap cleanup EXIT

python3 "$ROOT/scripts/macos_gate_slot2.py" render \
  --home "$HOME" --tart-home "$TART_HOME" --output "$candidate"
# The canonical validator parses through Python plistlib before enforcing the
# profile contract. Keep this portable so hosted Linux can prove the installer;
# launchd receives the same plistlib-serialized bytes on macOS.
python3 "$ROOT/scripts/macos_gate_slot2.py" validate \
  "$candidate" --sibling "$PRIMARY_PLIST" >/dev/null

echo "macOS gate slot-2 install plan:"
echo "  primary=$DOMAIN/$PRIMARY_LABEL (unchanged)"
echo "  install=$DOMAIN/$LABEL ($TARGET)"
echo "  tart_home=$TART_HOME (shared)"
echo "  resources=6 cores, 8192 MiB; host cap=2"
echo "  routing=merge-group first, then PR-head; event-class-v2"
echo "  activation=deferred to tartci pool on"
[ "$APPLY" = 1 ] || {
  echo "  action=dry-run (pass --apply only after tartci pool drain/off is terminal)"
  exit 0
}

# shellcheck source=providers/common/pool.lib.sh
# ROOT is resolved from this installed script.
# shellcheck disable=SC1091
. "$POOL_LIB"
# Serialize the admission recheck and atomic plist publication with pool on/off.
tartci_pool_lock_acquire || {
  echo "pool transition busy; slot-2 install made no change" >&2
  exit 4
}
lock_owned=1
if tartci_pool_admission_open; then
  echo "refusing slot-2 install while pool admission is open; drain or turn the pool off first" >&2
  exit 3
fi
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  echo "refusing slot-2 install while its LaunchAgent is loaded" >&2
  exit 3
fi
launch_path="$(python3 - "$candidate" <<'PY'
import plistlib, sys
with open(sys.argv[1], "rb") as source:
    value = plistlib.load(source)
print(value.get("EnvironmentVariables", {}).get("PATH", ""))
PY
)"
for tool in ghapp tart python3; do
  resolved="$(PATH="$launch_path" command -v "$tool" 2>/dev/null || true)"
  [ -n "$resolved" ] || {
    echo "gate-slot2 install requires $tool in the rendered launchd PATH" >&2
    exit 2
  }
done
program="$(python3 - "$candidate" <<'PY'
import plistlib, sys
with open(sys.argv[1], "rb") as source:
    value = plistlib.load(source)
arguments = value.get("ProgramArguments", [])
print(arguments[1] if isinstance(arguments, list) and len(arguments) > 1 else "")
PY
)"
[ -n "$program" ] && [ -r "$program" ] && [ -x "$program" ] || {
  echo "gate-slot2 install requires the rendered tartci program to be readable and executable: $program" >&2
  exit 2
}

if [ -f "$TARGET" ]; then
  backup="$(mktemp "$TARGET.pre-slot2.XXXXXX")"
  cp -p "$TARGET" "$backup"
  echo "  rollback=$backup"
fi
mv "$candidate" "$TARGET"
tartci_pool_lock_release
lock_owned=0
trap - EXIT
chmod 644 "$TARGET"
echo "installed $TARGET; pool admission remains closed"
echo "run 'tartci pool on' at an approved idle boundary to load all managed lanes"
