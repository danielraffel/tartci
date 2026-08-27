#!/usr/bin/env bash
# Install one rendered dynamic macOS fleet profile while host admission is off.
set -euo pipefail

APPLY=0
CONFIG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    -*) echo "usage: tartci fleet-macos install CONFIG [--apply]" >&2; exit 2 ;;
    *) [ -z "$CONFIG" ] || { echo "usage: tartci fleet-macos install CONFIG [--apply]" >&2; exit 2; }; CONFIG="$1" ;;
  esac
  shift
done
[ -n "$CONFIG" ] || { echo "usage: tartci fleet-macos install CONFIG [--apply]" >&2; exit 2; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/scripts/macos_fleet_lanes.py"
POOL_LIB="$ROOT/providers/common/pool.lib.sh"
AGENTS_DIR="$HOME/Library/LaunchAgents"
RECEIPT="$HOME/.config/tartci/macos-fleet-install.json"
PROFILE_SNAPSHOT="$HOME/.config/tartci/macos-fleet-profile.toml"
DOMAIN="gui/$(id -u)"
CONFIG="$(python3 - "$CONFIG" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).resolve())
PY
)"

plan_dir="$(mktemp -d "${TMPDIR:-/tmp}/tartci-fleet-plan.XXXXXX")"
cleanup_plan() { rm -rf "$plan_dir"; }
trap cleanup_plan EXIT
python3 "$PY" validate "$CONFIG"
python3 "$PY" render "$CONFIG" --output "$plan_dir" >/dev/null

replacements=()
while IFS= read -r label; do replacements+=("$label"); done < <(python3 - "$CONFIG" "$ROOT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]) / "scripts"))
import macos_fleet_lanes as fleet
for label in fleet.replacements(fleet.load(Path(sys.argv[1]))):
    print(label)
PY
)
candidates=()
while IFS= read -r candidate; do candidates+=("$candidate"); done < <(find "$plan_dir" -maxdepth 1 -type f -name '*.plist' -print | sort)
[ "${#candidates[@]}" -gt 0 ] || { echo "fleet profile rendered no LaunchAgents" >&2; exit 2; }
is_expected_name() {
  local wanted="$1" candidate
  for candidate in "${candidates[@]}"; do
    [ "$(basename "$candidate")" = "$wanted" ] && return 0
  done
  return 1
}
stale_targets=()
for target in "$AGENTS_DIR"/com.danielraffel.tartci.tart-runner-macos-fleet.*.plist; do
  [ -e "$target" ] || [ -L "$target" ] || continue
  is_expected_name "$(basename "$target")" || stale_targets+=("$target")
done

echo "macOS fleet profile install plan:"
echo "  config=$CONFIG"
echo "  install_count=${#candidates[@]}"
for candidate in "${candidates[@]}"; do echo "  install=$AGENTS_DIR/$(basename "$candidate")"; done
if [ "${#replacements[@]}" -gt 0 ]; then
  for label in "${replacements[@]}"; do echo "  retire=$AGENTS_DIR/$label.plist"; done
fi
if [ "${#stale_targets[@]}" -gt 0 ]; then
  for target in "${stale_targets[@]}"; do echo "  retire_stale_fleet=$target"; done
fi
echo "  receipt=$RECEIPT"
echo "  installed_profile=$PROFILE_SNAPSHOT"
echo "  activation=deferred to tartci pool on"
[ "$APPLY" = 1 ] || { echo "  action=dry-run (pass --apply only while the pool is terminally off)"; exit 0; }

# shellcheck source=providers/common/pool.lib.sh
# shellcheck disable=SC1091
. "$POOL_LIB"
tartci_pool_lock_acquire || { echo "pool transition busy; fleet install made no change" >&2; exit 4; }
lock_owned=1
stage_dir=""
backup_dir=""
receipt_backup=""
receipt_tmp=""
receipt_touched=0
profile_backup=""
profile_touched=0
changed_targets=()
target_backups=()
retired_paths=()
retired_backups=()

rollback() {
  local i target backup
  for ((i=${#retired_paths[@]}-1; i>=0; i--)); do
    target="${retired_paths[$i]}"; backup="${retired_backups[$i]}"
    if [ -e "$backup" ] || [ -L "$backup" ]; then mv -f "$backup" "$target" || true; fi
  done
  for ((i=${#changed_targets[@]}-1; i>=0; i--)); do
    target="${changed_targets[$i]}"; backup="${target_backups[$i]}"
    rm -f "$target"
    if [ -n "$backup" ] && { [ -e "$backup" ] || [ -L "$backup" ]; }; then
      mv -f "$backup" "$target" || true
    fi
  done
  if [ "$receipt_touched" = 1 ]; then
    rm -f "$RECEIPT"
    if [ -n "$receipt_backup" ] && { [ -e "$receipt_backup" ] || [ -L "$receipt_backup" ]; }; then
      mv -f "$receipt_backup" "$RECEIPT" || true
    fi
  fi
  if [ "$profile_touched" = 1 ]; then
    rm -f "$PROFILE_SNAPSHOT"
    if [ -n "$profile_backup" ] && { [ -e "$profile_backup" ] || [ -L "$profile_backup" ]; }; then
      mv -f "$profile_backup" "$PROFILE_SNAPSHOT" || true
    fi
  fi
}
cleanup() {
  local rc=$?
  trap - EXIT
  if [ "$rc" -ne 0 ]; then rollback; fi
  [ -n "$stage_dir" ] && rm -rf "$stage_dir"
  [ -n "$receipt_tmp" ] && rm -f "$receipt_tmp"
  if [ "$lock_owned" = 1 ]; then tartci_pool_lock_release || true; fi
  rm -rf "$plan_dir"
  exit "$rc"
}
trap cleanup EXIT

[ "$(tartci_pool_read_participation)" = 0 ] && [ "$(tartci_pool_read_state)" = off ] || {
  echo "refusing fleet install unless pool state is off and participation is 0" >&2
  exit 3
}

assert_agent_unloaded() {
  local label="$1" kind="$2" output rc
  if output="$(launchctl print "$DOMAIN/$label" 2>&1)"; then
    echo "refusing fleet install while $kind LaunchAgent is loaded: $label" >&2
    return 1
  else
    rc=$?
  fi
  case "$output" in
    *"Could not find service"*|*"service not found"*) return 0 ;;
    *)
      echo "refusing fleet install because launchctl could not prove $kind LaunchAgent absent: $label (rc=$rc: $output)" >&2
      return 1
      ;;
  esac
}

mkdir -p "$AGENTS_DIR" "$HOME/Library/Logs/tartci" "$(dirname "$RECEIPT")"
stage_dir="$(mktemp -d "$AGENTS_DIR/.macos-fleet-install.XXXXXX")"
locked_config="$stage_dir/profile.toml"
render_dir="$stage_dir/rendered"
cp "$CONFIG" "$locked_config"
python3 "$PY" validate "$locked_config" >/dev/null
profile_host_id="$(python3 - "$locked_config" "$ROOT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]) / "scripts"))
import macos_fleet_lanes as fleet
print(fleet.load(Path(sys.argv[1]))["host"]["id"])
PY
)"
shipyard_bin="$(command -v shipyard 2>/dev/null || true)"
[ -n "$shipyard_bin" ] || { echo "fleet install requires Shipyard's durable runner tag" >&2; exit 2; }
actual_host_id="$("$shipyard_bin" runner tag 2>/dev/null)" || {
  echo "fleet install could not read Shipyard's durable runner tag" >&2
  exit 2
}
[ "$actual_host_id" = "$profile_host_id" ] || {
  echo "fleet profile host mismatch: profile=$profile_host_id shipyard=$actual_host_id" >&2
  exit 3
}
python3 "$PY" render "$locked_config" --output "$render_dir" >/dev/null
replacements=()
while IFS= read -r label; do replacements+=("$label"); done < <(python3 - "$locked_config" "$ROOT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]) / "scripts"))
import macos_fleet_lanes as fleet
for label in fleet.replacements(fleet.load(Path(sys.argv[1]))):
    print(label)
PY
)
candidates=()
while IFS= read -r candidate; do candidates+=("$candidate"); done < <(find "$render_dir" -maxdepth 1 -type f -name '*.plist' -print | sort)
stale_targets=()
for target in "$AGENTS_DIR"/com.danielraffel.tartci.tart-runner-macos-fleet.*.plist; do
  [ -e "$target" ] || [ -L "$target" ] || continue
  is_expected_name "$(basename "$target")" || stale_targets+=("$target")
done

for candidate in "${candidates[@]}"; do
  label="$(basename "$candidate" .plist)"
  assert_agent_unloaded "$label" target || exit 3
done
if [ "${#replacements[@]}" -gt 0 ]; then
  for label in "${replacements[@]}"; do
    assert_agent_unloaded "$label" replaced || exit 3
  done
fi
if [ "${#stale_targets[@]}" -gt 0 ]; then
  for target in "${stale_targets[@]}"; do
    label="$(basename "$target" .plist)"
    assert_agent_unloaded "$label" "stale managed" || exit 3
  done
fi

python3 - "$render_dir" <<'PY'
import os, plistlib, shutil, sys
from pathlib import Path
for path in Path(sys.argv[1]).glob("*.plist"):
    value = plistlib.loads(path.read_bytes())
    env = value["EnvironmentVariables"]
    launch_path = env["PATH"]
    for tool in ("ghapp", "tart", "python3"):
        if not shutil.which(tool, path=launch_path):
            raise SystemExit(f"rendered launchd PATH cannot resolve {tool}: {path}")
    program = value["ProgramArguments"][1]
    if not os.access(program, os.R_OK | os.X_OK):
        raise SystemExit(f"rendered tartci program is not readable and executable: {program}")
PY

backup_dir="$AGENTS_DIR/.tartci-retired/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$backup_dir"
for candidate in "${candidates[@]}"; do
  target="$AGENTS_DIR/$(basename "$candidate")"
  backup=""
  if [ -e "$target" ] || [ -L "$target" ]; then backup="$backup_dir/$(basename "$target").previous"; mv "$target" "$backup"; fi
  changed_targets+=("$target")
  target_backups+=("$backup")
  chmod 644 "$candidate"
  mv "$candidate" "$target"
done
if [ "${#replacements[@]}" -gt 0 ]; then
  for label in "${replacements[@]}"; do
    target="$AGENTS_DIR/$label.plist"
    [ -e "$target" ] || [ -L "$target" ] || continue
    backup="$backup_dir/$(basename "$target").retired"
    mv "$target" "$backup"
    retired_paths+=("$target")
    retired_backups+=("$backup")
  done
fi
if [ "${#stale_targets[@]}" -gt 0 ]; then
  for target in "${stale_targets[@]}"; do
    [ -e "$target" ] || [ -L "$target" ] || continue
    backup="$backup_dir/$(basename "$target").stale"
    mv "$target" "$backup"
    retired_paths+=("$target")
    retired_backups+=("$backup")
  done
fi

if [ -e "$PROFILE_SNAPSHOT" ] || [ -L "$PROFILE_SNAPSHOT" ]; then
  profile_backup="$backup_dir/$(basename "$PROFILE_SNAPSHOT").previous"
  mv "$PROFILE_SNAPSHOT" "$profile_backup"
fi
profile_touched=1
chmod 644 "$locked_config"
mv "$locked_config" "$PROFILE_SNAPSHOT"

receipt_tmp="$(mktemp "$(dirname "$RECEIPT")/.macos-fleet-install.json.XXXXXX")"
python3 "$PY" write-receipt "$PROFILE_SNAPSHOT" --agents-dir "$AGENTS_DIR" --output "$receipt_tmp" >/dev/null
python3 "$PY" verify-installed "$receipt_tmp" \
  --config "$PROFILE_SNAPSHOT" --agents-dir "$AGENTS_DIR" >/dev/null
if [ -e "$RECEIPT" ] || [ -L "$RECEIPT" ]; then receipt_backup="$backup_dir/$(basename "$RECEIPT").previous"; mv "$RECEIPT" "$receipt_backup"; fi
receipt_touched=1
chmod 644 "$receipt_tmp"
mv "$receipt_tmp" "$RECEIPT"
receipt_tmp=""

tartci_pool_lock_release
lock_owned=0
trap - EXIT
rm -rf "$stage_dir" "$plan_dir"
echo "installed ${#changed_targets[@]} profile-rendered LaunchAgents; retired ${#retired_paths[@]} declared legacy LaunchAgents"
echo "verified receipt $RECEIPT; pool admission remains closed"
echo "run 'tartci pool on' at an approved idle boundary"
