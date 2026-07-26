#!/usr/bin/env bash
# Retire the exact legacy macOS runner label and install the guarded replacement.
set -euo pipefail

APPLY=0
GUI_UPDATED=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --attest-external-gui-label-updated) GUI_UPDATED=1 ;;
    *) echo "usage: migrate_macos_gate_agent.sh [--apply --attest-external-gui-label-updated]" >&2; exit 2 ;;
  esac
  shift
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OLD_LABEL="com.danielraffel.pulp.tart-runner"
NEW_LABEL="com.danielraffel.pulp.tart-runner-macos-gate"
OLD_PLIST="$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
NEW_PLIST="$HOME/Library/LaunchAgents/$NEW_LABEL.plist"
TEMPLATE="$ROOT/launchd/com.danielraffel.pulp.tart-runner-macos.plist.template"
DOMAIN="gui/$(id -u)"

echo "macOS gate migration plan:"
echo "  retire=$DOMAIN/$OLD_LABEL ($OLD_PLIST)"
echo "  install=$DOMAIN/$NEW_LABEL ($NEW_PLIST)"
[ "$APPLY" = 1 ] || { echo "  action=dry-run (pass --apply to migrate)"; exit 0; }
[ "$GUI_UPDATED" = 1 ] || {
  echo "--apply requires --attest-external-gui-label-updated after the external shipyard-macos-gui deployment knows $NEW_LABEL" >&2
  exit 2
}
command -v ghapp >/dev/null 2>&1 || {
  echo "migration requires ghapp in PATH for unattended GitHub App authentication" >&2
  exit 2
}

mkdir -p "$HOME/Library/LaunchAgents"
tmp="$(mktemp "$HOME/Library/LaunchAgents/.macos-gate.plist.XXXXXX")"
readiness_marker=""
backup="$(mktemp -d "${TMPDIR:-/tmp}/tartci-macos-gate-migration.XXXXXX")"
old_loaded=0
new_loaded=0
launchctl print "$DOMAIN/$OLD_LABEL" >/dev/null 2>&1 && old_loaded=1
launchctl print "$DOMAIN/$NEW_LABEL" >/dev/null 2>&1 && new_loaded=1
[ "$old_loaded" != 1 ] || [ -f "$OLD_PLIST" ] || {
  echo "loaded legacy agent has no restorable canonical plist at $OLD_PLIST" >&2
  rm -f "$tmp"; rm -rf "$backup"
  exit 1
}
[ "$new_loaded" != 1 ] || [ -f "$NEW_PLIST" ] || {
  echo "loaded replacement agent has no restorable canonical plist at $NEW_PLIST" >&2
  rm -f "$tmp"; rm -rf "$backup"
  exit 1
}
[ ! -f "$OLD_PLIST" ] || cp -p "$OLD_PLIST" "$backup/old.plist"
[ ! -f "$NEW_PLIST" ] || cp -p "$NEW_PLIST" "$backup/new.plist"
committed=0
rollback() {
  rc=$?
  trap - EXIT
  if [ "$committed" != 1 ]; then
    set +e
    echo "migration failed; restoring prior LaunchAgent configuration" >&2
    launchctl bootout "$DOMAIN/$NEW_LABEL" >/dev/null 2>&1
    if [ -f "$backup/new.plist" ]; then cp -p "$backup/new.plist" "$NEW_PLIST"; else rm -f "$NEW_PLIST"; fi
    if [ -f "$backup/old.plist" ]; then cp -p "$backup/old.plist" "$OLD_PLIST"; else rm -f "$OLD_PLIST"; fi
    if [ "$new_loaded" = 1 ]; then
      launchctl bootstrap "$DOMAIN" "$NEW_PLIST" >/dev/null 2>&1 \
        || echo "ROLLBACK FAILED: could not bootstrap prior replacement $NEW_LABEL" >&2
    fi
    if [ "$old_loaded" = 1 ]; then
      if ! launchctl bootstrap "$DOMAIN" "$OLD_PLIST" >/dev/null 2>&1 \
        || ! launchctl print "$DOMAIN/$OLD_LABEL" >/dev/null 2>&1; then
        echo "ROLLBACK FAILED: legacy agent $OLD_LABEL was not restored" >&2
      fi
    fi
  fi
  rm -f "$tmp"
  [ -z "$readiness_marker" ] || rm -f "$readiness_marker"
  rm -rf "$backup"
  exit "$rc"
}
trap rollback EXIT
if [ -f "$OLD_PLIST" ]; then
  cp -p "$OLD_PLIST" "$tmp"
elif [ -f "$NEW_PLIST" ]; then
  cp -p "$NEW_PLIST" "$tmp"
else
  sed -e "s|\$HOME|$HOME|g" "$TEMPLATE" > "$tmp"
fi
if [ -f "$OLD_PLIST" ] || [ -f "$NEW_PLIST" ]; then
  python3 - "$tmp" "$NEW_LABEL" <<'PY'
import plistlib, sys
path, label = sys.argv[1:]
with open(path, "rb") as source:
    value = plistlib.load(source)
value["Label"] = label
environment = value.setdefault("EnvironmentVariables", {})
environment["TARTCI_LAUNCHD_LABEL"] = label
environment["TARTCI_GH_CLI"] = "ghapp"
arguments = [str(item) for item in value.get("ProgramArguments", [])]
for index, argument in enumerate(arguments):
    if argument.endswith(("tools/ci/tart-runner.sh", "providers/tart-macos/runner.sh")):
        home = environment.get("HOME", "")
        dispatcher = f"{home}/.local/bin/tartci" if home else "tartci"
        value["ProgramArguments"] = ["/bin/bash", dispatcher, "serve", "macos", *arguments[index + 1:]]
        break
with open(path, "wb") as destination:
    plistlib.dump(value, destination, sort_keys=False)
PY
fi
plutil -lint "$tmp" >/dev/null
launchctl bootout "$DOMAIN/$OLD_LABEL" >/dev/null 2>&1 || true
if launchctl print "$DOMAIN/$OLD_LABEL" >/dev/null 2>&1; then
  echo "legacy label remains loaded; refusing replacement startup" >&2
  exit 1
fi
identity="$(python3 - "$tmp" "$ROOT/scripts" <<'PY'
import os, plistlib, sys
sys.path.insert(0, sys.argv[2])
from macos_runner_identity import resolve_plist_identity
with open(sys.argv[1], "rb") as source:
    plist = plistlib.load(source)
identity = resolve_plist_identity(plist, hostname=os.uname().nodename.split(".")[0])
print(f"{identity.runner_name}|{identity.state_dir}")
PY
)"
runner_name="${identity%%|*}"
state_dir="${identity#*|}"
state_file="$state_dir/$runner_name.state.json"
python3 "$ROOT/scripts/macos_runner_identity_guard.py" \
  --current-label "$NEW_LABEL" \
  --runner-name "$runner_name" \
  --state-dir "$state_dir"
rm -f "$OLD_PLIST"
launchctl bootout "$DOMAIN/$NEW_LABEL" >/dev/null 2>&1 || true
mv "$tmp" "$NEW_PLIST"
readiness_marker="$(mktemp "${TMPDIR:-/tmp}/tartci-macos-gate-readiness.XXXXXX")"
launchctl bootstrap "$DOMAIN" "$NEW_PLIST"
launchctl kickstart -k "$DOMAIN/$NEW_LABEL"
healthy=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30; do
  printed="$(launchctl print "$DOMAIN/$NEW_LABEL" 2>/dev/null || true)"
  if grep -Eq '^[[:space:]]*state = running[[:space:]]*$' <<<"$printed" \
    && [ -f "$state_file" ] && [ "$state_file" -nt "$readiness_marker" ] \
    && python3 - "$state_file" "$runner_name" <<'PY' >/dev/null 2>&1
import json, sys
with open(sys.argv[1]) as source:
    value = json.load(source)
if not isinstance(value, dict) or value.get("runner") != sys.argv[2]:
    raise SystemExit(1)
PY
  then
    healthy=1
    break
  fi
  sleep 1
done
rm -f "$readiness_marker"
[ "$healthy" = 1 ] || {
  echo "replacement did not publish a fresh post-guard runner heartbeat" >&2
  exit 1
}
committed=1
echo "retired $OLD_LABEL and started $NEW_LABEL"
