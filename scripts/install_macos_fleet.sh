#!/usr/bin/env bash
# Install one rendered dynamic macOS fleet profile while host admission is off.
set -euo pipefail

APPLY=0
CONFIG=""
SUPPORT_MANIFEST=""
SUPPORT_SOURCE=""
LAUNCH_HELPER_SOURCE=""
USAGE="usage: tartci fleet-macos install CONFIG [--support-source PATH] [--support-manifest PATH] [--launch-helper-source PATH] [--apply]"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --support-manifest)
      shift
      [ "$#" -gt 0 ] || { echo "--support-manifest requires a path" >&2; exit 2; }
      SUPPORT_MANIFEST="$1"
      ;;
    --support-source)
      shift
      [ "$#" -gt 0 ] || { echo "--support-source requires a path" >&2; exit 2; }
      SUPPORT_SOURCE="$1"
      ;;
    --launch-helper-source)
      shift
      [ "$#" -gt 0 ] || { echo "--launch-helper-source requires a path" >&2; exit 2; }
      LAUNCH_HELPER_SOURCE="$1"
      ;;
    -*) echo "$USAGE" >&2; exit 2 ;;
    *) [ -z "$CONFIG" ] || { echo "$USAGE" >&2; exit 2; }; CONFIG="$1" ;;
  esac
  shift
done
[ -n "$CONFIG" ] || { echo "$USAGE" >&2; exit 2; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -n "$SUPPORT_SOURCE" ] || SUPPORT_SOURCE="$ROOT"
SUPPORT_SOURCE="$(cd "$SUPPORT_SOURCE" && pwd)"
if [ -z "$SUPPORT_MANIFEST" ]; then
  SUPPORT_MANIFEST="$SUPPORT_SOURCE/.tartci-support-manifest.json"
fi
PY="$ROOT/scripts/macos_fleet_lanes.py"
POOL_LIB="$ROOT/providers/common/pool.lib.sh"
AGENTS_DIR="$HOME/Library/LaunchAgents"
RECEIPT="$HOME/.config/tartci/macos-fleet-install.json"
PROFILE_SNAPSHOT="$HOME/.config/tartci/macos-fleet-profile.toml"
LOADED_RECEIPT="$HOME/.config/tartci/macos-fleet-loaded.json"
SUPPORT_GENERATIONS="$HOME/.local/share/tartci-generations"
ENTRYPOINT="$HOME/.local/bin/tartci"
DOMAIN="gui/$(id -u)"
CONFIG="$(python3 - "$CONFIG" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).resolve())
PY
)"

plan_dir="$(mktemp -d "${TMPDIR:-/tmp}/tartci-fleet-plan.XXXXXX")"
wrapper_candidate=""
cleanup_plan() {
  rm -rf "$plan_dir"
  [ -z "$wrapper_candidate" ] || rm -f "$wrapper_candidate"
}
trap cleanup_plan EXIT
python3 "$PY" validate "$CONFIG"
python3 "$PY" render "$CONFIG" --output "$plan_dir" >/dev/null
python3 "$ROOT/scripts/tartci_support_manifest.py" verify "$SUPPORT_MANIFEST" \
  --root "$SUPPORT_SOURCE" >/dev/null

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
echo "  support_manifest=$SUPPORT_MANIFEST"
echo "  support_source=$SUPPORT_SOURCE"
echo "  entrypoint=$ENTRYPOINT"
[ -z "$LAUNCH_HELPER_SOURCE" ] || echo "  launch_helper_source=$LAUNCH_HELPER_SOURCE"
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
loaded_receipt_backup=""
loaded_receipt_touched=0
profile_backup=""
profile_touched=0
wrapper_backup=""
wrapper_touched=0
launcher_target=""
launcher_candidate=""
launcher_backup=""
launcher_touched=0
support_root=""
installed_support_manifest=""
launch_entrypoint=""
source_authority_commit=""
changed_targets=()
target_backups=()
retired_paths=()
retired_backups=()

rollback() {
  local i target backup
  if [ "$launcher_touched" = 1 ]; then
    rm -rf -- "$launcher_target"
    if [ -n "$launcher_backup" ] && { [ -e "$launcher_backup" ] || [ -L "$launcher_backup" ]; }; then
      mv -f "$launcher_backup" "$launcher_target" || true
    fi
  fi
  if [ "$wrapper_touched" = 1 ]; then
    rm -f "$ENTRYPOINT"
    if [ -n "$wrapper_backup" ] && { [ -e "$wrapper_backup" ] || [ -L "$wrapper_backup" ]; }; then
      mv -f "$wrapper_backup" "$ENTRYPOINT" || true
    fi
  fi
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
  if [ "$loaded_receipt_touched" = 1 ]; then
    rm -f "$LOADED_RECEIPT"
    if [ -n "$loaded_receipt_backup" ] && { [ -e "$loaded_receipt_backup" ] || [ -L "$loaded_receipt_backup" ]; }; then
      mv -f "$loaded_receipt_backup" "$LOADED_RECEIPT" || true
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
  [ -z "$wrapper_candidate" ] || rm -f "$wrapper_candidate"
  [ -z "$launcher_candidate" ] || rm -rf -- "$launcher_candidate"
  if [ "$lock_owned" = 1 ]; then tartci_pool_lock_release || true; fi
  rm -rf "$plan_dir"
  exit "$rc"
}
trap cleanup EXIT

durable_file() {
  /usr/bin/python3 - "$1" <<'PY'
import os
import sys
from pathlib import Path
path = Path(sys.argv[1])
with path.open("rb") as handle:
    os.fsync(handle.fileno())
descriptor = os.open(path.parent, os.O_RDONLY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

durable_directory() {
  /usr/bin/python3 - "$1" <<'PY'
import os
import sys
descriptor = os.open(sys.argv[1], os.O_RDONLY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

durable_tree() {
  /usr/bin/python3 - "$1" <<'PY'
import os
import stat
import sys
from pathlib import Path
root = Path(sys.argv[1])
for path in sorted(root.rglob("*")):
    if path.is_symlink():
        raise SystemExit(f"durability tree contains a symlink: {path}")
    if path.is_file():
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
for path in sorted([root, *[p for p in root.rglob("*") if p.is_dir()]], reverse=True):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
}

maybe_test_crash() {
  [ "${TARTCI_TESTING:-0}" = 1 ] || return 0
  [ "${TARTCI_INSTALL_CRASH_AFTER:-}" = "$1" ] || return 0
  kill -KILL "$$"
}

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
profile_home="$(python3 - "$locked_config" "$ROOT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]) / "scripts"))
import macos_fleet_lanes as fleet
print(fleet.load(Path(sys.argv[1]))["host"]["home"])
PY
)"
launch_helper_json="$(python3 - "$locked_config" "$ROOT" <<'PY'
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]) / "scripts"))
import macos_fleet_lanes as fleet
print(json.dumps(fleet.load(Path(sys.argv[1])).get("launch_helper")))
PY
)"
[ "$(cd "$HOME" && pwd -P)" = "$(cd "$profile_home" && pwd -P)" ] || {
  echo "fleet profile home mismatch: profile=$profile_home runtime=$HOME" >&2
  exit 3
}
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
if [ "$launch_helper_json" != "null" ]; then
  launcher_target="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["path"])' <<<"$launch_helper_json")"
  launcher_identifier="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["identifier"])' <<<"$launch_helper_json")"
  launcher_team_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["team_id"])' <<<"$launch_helper_json")"
  launcher_profile_policy_sha256="$(python3 - "$locked_config" "$ROOT" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]) / "scripts"))
import macos_launcher_identity
print(macos_launcher_identity.profile_policy_digest(Path(sys.argv[1])))
PY
)"
  [ -n "$LAUNCH_HELPER_SOURCE" ] || LAUNCH_HELPER_SOURCE="$launcher_target"
  launcher_identity_json="$(python3 "$ROOT/scripts/macos_launcher_identity.py" verify "$LAUNCH_HELPER_SOURCE" \
    --identifier "$launcher_identifier" --team-id "$launcher_team_id" \
    --profile-policy-sha256 "$launcher_profile_policy_sha256")"
  launcher_source_commit="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["source_commit"])' <<<"$launcher_identity_json")"
  mkdir -p "$(dirname "$launcher_target")"
  python3 - "$profile_home" "$launcher_target" <<'PY' || {
import os
import sys
from pathlib import Path
home = Path(sys.argv[1])
target = Path(sys.argv[2])
try:
    relative = target.relative_to(home)
except ValueError:
    raise SystemExit(1)
current = home
for component in relative.parts[:-1]:
    current /= component
    if current.is_symlink() or not current.is_dir() or current.stat().st_uid != os.getuid():
        raise SystemExit(1)
PY
    echo "launch helper parent chain must be owned, home-relative, and non-symlinked" >&2
    exit 3
  }
  launcher_candidate="$(mktemp -d "$(dirname "$launcher_target")/.TartCILauncher.app.XXXXXX")"
  /usr/bin/ditto --noqtn "$LAUNCH_HELPER_SOURCE/" "$launcher_candidate/"
  python3 "$ROOT/scripts/macos_launcher_identity.py" verify "$launcher_candidate" \
    --identifier "$launcher_identifier" --team-id "$launcher_team_id" \
    --profile-policy-sha256 "$launcher_profile_policy_sha256" \
    --source-commit "$launcher_source_commit" >/dev/null
elif [ -n "$LAUNCH_HELPER_SOURCE" ]; then
  echo "--launch-helper-source is invalid for a profile without launch_helper" >&2
  exit 2
fi
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

backup_dir="$AGENTS_DIR/.tartci-retired/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -p "$backup_dir"
durable_directory "$(dirname "$backup_dir")"
stage_result="$(python3 "$ROOT/scripts/tartci_support_manifest.py" stage-install \
  "$SUPPORT_MANIFEST" --source-root "$SUPPORT_SOURCE" \
  --generations-root "$SUPPORT_GENERATIONS")"
support_root="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["root"])' <<<"$stage_result")"
installed_support_manifest="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["manifest"])' <<<"$stage_result")"
launch_entrypoint="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["launch_entrypoint"])' <<<"$stage_result")"

mkdir -p "$(dirname "$ENTRYPOINT")"
python3 - "$(dirname "$ENTRYPOINT")" <<'PY' || {
import os
import sys
from pathlib import Path
path = Path(sys.argv[1])
raise SystemExit(0 if path.is_dir() and not path.is_symlink()
                 and path.stat().st_uid == os.getuid() else 1)
PY
    echo "fleet install requires an owned non-symlink entrypoint directory: $(dirname "$ENTRYPOINT")" >&2
    exit 3
}
wrapper_candidate="$(mktemp "$(dirname "$ENTRYPOINT")/.tartci-wrapper.XXXXXX")"
python3 "$ROOT/scripts/tartci_support_manifest.py" wrapper-write "$wrapper_candidate" \
  --support-root "$support_root" >/dev/null

if [ -z "$launcher_target" ]; then
  python3 - "$render_dir" "$launch_entrypoint" "$ENTRYPOINT" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

root = Path(sys.argv[1])
launch = Path(sys.argv[2])
old_launch = Path(sys.argv[3])
if launch.is_symlink() or not launch.is_file() or not os.access(launch, os.R_OK | os.X_OK):
    raise SystemExit(f"immutable TartCI launch entrypoint is unavailable: {launch}")
for path in root.glob("*.plist"):
    value = plistlib.loads(path.read_bytes())
    arguments = value["ProgramArguments"]
    matches = [index for index, argument in enumerate(arguments)
               if argument == str(old_launch)]
    if len(matches) != 1:
        raise SystemExit(f"rendered fleet service has no unique launch entrypoint: {path}")
    arguments[matches[0]] = str(launch)
    path.write_bytes(plistlib.dumps(value, sort_keys=False))
PY
fi

ghapp_path="$(python3 - "$render_dir" <<'PY'
import os, plistlib, shutil, stat, sys
from pathlib import Path
ghapp_paths = set()
for path in Path(sys.argv[1]).glob("*.plist"):
    value = plistlib.loads(path.read_bytes())
    env = value["EnvironmentVariables"]
    launch_path = env["PATH"]
    for tool in ("ghapp", "tart", "python3"):
        resolved = shutil.which(tool, path=launch_path)
        if not resolved:
            raise SystemExit(f"rendered launchd PATH cannot resolve {tool}: {path}")
        if tool == "ghapp":
            ghapp_paths.add(str(Path(resolved).resolve()))
    app_id = env.get("SHIPYARD_GITHUB_APP_ID")
    private_key = env.get("SHIPYARD_GITHUB_APP_PRIVATE_KEY_PATH")
    cache_dir = env.get("SHIPYARD_GITHUB_APP_CACHE_DIR")
    github_app_refs = (app_id, private_key, cache_dir)
    if any(github_app_refs) and not all(github_app_refs):
        raise SystemExit(f"rendered GitHub App references are incomplete: {path}")
    if all(github_app_refs):
        key_path = Path(private_key)
        cache_path = Path(cache_dir)
        if (key_path.is_symlink() or not key_path.is_file()
                or stat.S_IMODE(key_path.stat().st_mode) != 0o600
                or key_path.stat().st_uid != os.getuid()):
            raise SystemExit(
                f"rendered GitHub App private key must be an owned mode-0600 regular file: {key_path}"
            )
        if (cache_path.is_symlink() or not cache_path.is_dir()
                or stat.S_IMODE(cache_path.stat().st_mode) != 0o700
                or cache_path.stat().st_uid != os.getuid()):
            raise SystemExit(
                f"rendered GitHub App cache must be an owned mode-0700 directory: {cache_path}"
            )
if len(ghapp_paths) != 1:
    raise SystemExit(
        "rendered fleet lanes must resolve one exact shared ghapp executable"
    )
print(next(iter(ghapp_paths)))
PY
)"

support_repository="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["repository"])' "$installed_support_manifest")"
support_commit="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_commit"])' "$installed_support_manifest")"
authority_env_json="$(python3 - "$locked_config" "$ROOT" <<'PY'
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]) / "scripts"))
import macos_fleet_lanes as fleet
app = fleet.load(Path(sys.argv[1])).get("github_app")
if app is None:
    print("{}")
    raise SystemExit(0)
print(json.dumps(app))
PY
)"
[ "$support_repository" = "https://github.com/danielraffel/tartci.git" ] || {
  echo "fleet install refuses an untrusted support repository: $support_repository" >&2
  exit 3
}
if [ "$authority_env_json" = "{}" ]; then
  source_authority_commit="$(
    SHIPYARD_GH_APP_REPO="danielraffel/tartci" \
      "$ghapp_path" api \
        "repos/danielraffel/tartci/commits/$support_commit" --jq .sha
  )" || source_authority_commit=""
else
  app_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$authority_env_json")"
  app_key="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["private_key_path"])' <<<"$authority_env_json")"
  app_cache="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["cache_dir"])' <<<"$authority_env_json")"
  source_authority_commit="$(
    SHIPYARD_GITHUB_APP_ID="$app_id" \
    SHIPYARD_GITHUB_APP_PRIVATE_KEY_PATH="$app_key" \
    SHIPYARD_GITHUB_APP_CACHE_DIR="$app_cache" \
    SHIPYARD_GH_APP_REPO="danielraffel/tartci" \
      "$ghapp_path" api \
        "repos/danielraffel/tartci/commits/$support_commit" --jq .sha
  )" || source_authority_commit=""
fi
[ -n "$source_authority_commit" ] || {
  echo "fleet install could not authenticate the exact TartCI source commit" >&2
  exit 3
}
[ "$source_authority_commit" = "$support_commit" ] || {
  echo "fleet install source authority returned a mismatched commit" >&2
  exit 3
}
if [ -n "$launcher_target" ]; then
  [ "$launcher_source_commit" = "$support_commit" ] || {
    echo "fleet install requires launcher and support cohorts from the same exact commit" >&2
    exit 3
  }
  if [ "$authority_env_json" = "{}" ]; then
    launcher_authority_commit="$(
      SHIPYARD_GH_APP_REPO="danielraffel/tartci" \
        "$ghapp_path" api \
          "repos/danielraffel/tartci/commits/$launcher_source_commit" --jq .sha
    )" || launcher_authority_commit=""
  else
    launcher_authority_commit="$(
      SHIPYARD_GITHUB_APP_ID="$app_id" \
      SHIPYARD_GITHUB_APP_PRIVATE_KEY_PATH="$app_key" \
      SHIPYARD_GITHUB_APP_CACHE_DIR="$app_cache" \
      SHIPYARD_GH_APP_REPO="danielraffel/tartci" \
        "$ghapp_path" api \
          "repos/danielraffel/tartci/commits/$launcher_source_commit" --jq .sha
    )" || launcher_authority_commit=""
  fi
  [ "$launcher_authority_commit" = "$launcher_source_commit" ] || {
    echo "fleet install could not authenticate the launcher's exact TartCI source commit" >&2
    exit 3
  }
fi

if [ -n "$launcher_target" ]; then
  if { [ -e "$launcher_target" ] || [ -L "$launcher_target" ]; } \
      && { [ -L "$launcher_target" ] || [ ! -d "$launcher_target" ]; }; then
    echo "fleet install requires a regular app directory at the launch helper path: $launcher_target" >&2
    exit 3
  fi
  if [ -e "$launcher_target" ] || [ -L "$launcher_target" ]; then
    launcher_backup="$backup_dir/TartCILauncher.previous.app"
    mv "$launcher_target" "$launcher_backup"
    durable_directory "$(dirname "$launcher_target")"
    durable_directory "$backup_dir"
  fi
  launcher_touched=1
  durable_tree "$launcher_candidate"
  mv "$launcher_candidate" "$launcher_target"
  launcher_candidate=""
  durable_tree "$launcher_target"
  python3 "$ROOT/scripts/macos_launcher_identity.py" verify "$launcher_target" \
    --identifier "$launcher_identifier" --team-id "$launcher_team_id" \
    --profile-policy-sha256 "$launcher_profile_policy_sha256" \
    --source-commit "$launcher_source_commit" >/dev/null
  maybe_test_crash launcher
fi

for candidate in "${candidates[@]}"; do
  target="$AGENTS_DIR/$(basename "$candidate")"
  backup=""
  if [ -e "$target" ] || [ -L "$target" ]; then backup="$backup_dir/$(basename "$target").previous"; mv "$target" "$backup"; durable_directory "$AGENTS_DIR"; durable_directory "$backup_dir"; fi
  changed_targets+=("$target")
  target_backups+=("$backup")
  chmod 644 "$candidate"
  durable_file "$candidate"
  mv "$candidate" "$target"
  durable_file "$target"
done
if [ "${#replacements[@]}" -gt 0 ]; then
  for label in "${replacements[@]}"; do
    target="$AGENTS_DIR/$label.plist"
    [ -e "$target" ] || [ -L "$target" ] || continue
    backup="$backup_dir/$(basename "$target").retired"
    mv "$target" "$backup"
    durable_directory "$AGENTS_DIR"
    durable_directory "$backup_dir"
    retired_paths+=("$target")
    retired_backups+=("$backup")
  done
fi
if [ "${#stale_targets[@]}" -gt 0 ]; then
  for target in "${stale_targets[@]}"; do
    [ -e "$target" ] || [ -L "$target" ] || continue
    backup="$backup_dir/$(basename "$target").stale"
    mv "$target" "$backup"
    durable_directory "$AGENTS_DIR"
    durable_directory "$backup_dir"
    retired_paths+=("$target")
    retired_backups+=("$backup")
  done
fi

if [ -e "$PROFILE_SNAPSHOT" ] || [ -L "$PROFILE_SNAPSHOT" ]; then
  profile_backup="$backup_dir/$(basename "$PROFILE_SNAPSHOT").previous"
  mv "$PROFILE_SNAPSHOT" "$profile_backup"
  durable_directory "$(dirname "$PROFILE_SNAPSHOT")"
  durable_directory "$backup_dir"
fi
profile_touched=1
chmod 644 "$locked_config"
durable_file "$locked_config"
mv "$locked_config" "$PROFILE_SNAPSHOT"
durable_file "$PROFILE_SNAPSHOT"

receipt_tmp="$(mktemp "$(dirname "$RECEIPT")/.macos-fleet-install.json.XXXXXX")"
python3 "$PY" write-receipt "$PROFILE_SNAPSHOT" --agents-dir "$AGENTS_DIR" \
  --support-root "$support_root" --support-manifest "$installed_support_manifest" \
  --entrypoint "$ENTRYPOINT" --entrypoint-source "$wrapper_candidate" \
  --launch-entrypoint "$launch_entrypoint" \
  --source-authority-commit "$source_authority_commit" \
  --output "$receipt_tmp" >/dev/null
if [ -e "$RECEIPT" ] || [ -L "$RECEIPT" ]; then receipt_backup="$backup_dir/$(basename "$RECEIPT").previous"; mv "$RECEIPT" "$receipt_backup"; durable_directory "$(dirname "$RECEIPT")"; durable_directory "$backup_dir"; fi
receipt_touched=1
chmod 644 "$receipt_tmp"
durable_file "$receipt_tmp"
mv "$receipt_tmp" "$RECEIPT"
durable_file "$RECEIPT"
receipt_tmp=""
if [ -e "$LOADED_RECEIPT" ] || [ -L "$LOADED_RECEIPT" ]; then
  loaded_receipt_backup="$backup_dir/$(basename "$LOADED_RECEIPT").previous"
  mv "$LOADED_RECEIPT" "$loaded_receipt_backup"
  durable_directory "$(dirname "$LOADED_RECEIPT")"
  durable_directory "$backup_dir"
fi
loaded_receipt_touched=1
maybe_test_crash receipt

if [ -d "$ENTRYPOINT" ] && [ ! -L "$ENTRYPOINT" ]; then
  echo "fleet install refuses a directory at the canonical TartCI entrypoint: $ENTRYPOINT" >&2
  exit 3
fi
if [ -e "$ENTRYPOINT" ] || [ -L "$ENTRYPOINT" ]; then
  wrapper_backup="$backup_dir/tartci-wrapper.previous"
  cp -pP "$ENTRYPOINT" "$wrapper_backup"
  if [ -L "$wrapper_backup" ]; then
    durable_directory "$backup_dir"
  else
    durable_file "$wrapper_backup"
  fi
fi
wrapper_touched=1
durable_file "$wrapper_candidate"
mv -f "$wrapper_candidate" "$ENTRYPOINT"
wrapper_candidate=""
durable_file "$ENTRYPOINT"
maybe_test_crash wrapper
python3 "$PY" verify-installed "$RECEIPT" \
  --config "$PROFILE_SNAPSHOT" --agents-dir "$AGENTS_DIR" \
  --support-root "$support_root" >/dev/null

tartci_pool_lock_release
lock_owned=0
trap - EXIT
rm -rf "$stage_dir" "$plan_dir"
echo "installed ${#changed_targets[@]} profile-rendered LaunchAgents; retired ${#retired_paths[@]} declared legacy LaunchAgents"
echo "verified receipt $RECEIPT; pool admission remains closed"
echo "run 'tartci pool on' at an approved idle boundary"
