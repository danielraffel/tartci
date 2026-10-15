#!/bin/bash
set -euo pipefail
usage(){ echo "usage: $0 --output PATH.app --approval-output PATH --identity STRING --support-root PATH --profile PATH [--python PATH] [--keychain PATH]" >&2; exit 64; }
output="" approval_output="" identity="" support_root="" profile="" keychain="" render_python="" print_render_python=0
while [[ $# -gt 0 ]]; do case "$1" in
  --output) output="${2:-}"; shift 2;; --identity) identity="${2:-}"; shift 2;;
  --approval-output) approval_output="${2:-}"; shift 2;;
  --support-root) support_root="${2:-}"; shift 2;;
  --profile) profile="${2:-}"; shift 2;;
  --python) render_python="${2:-}"; shift 2;;
  --print-render-python) print_render_python=1; shift;;
  --keychain) keychain="${2:-}"; shift 2;; *) usage;; esac; done
# Name the keychain when given: a bare --sign walks the search list, and a
# locked keychain there raises a password dialog in the GUI session.
keychain_args=(); [[ -n "$keychain" ]] && keychain_args=(--keychain "$keychain")
if [[ "$print_render_python" = 0 ]]; then
  [[ -n "$output" && -n "$approval_output" && -n "$identity" && -n "$support_root" && -n "$profile" ]] || usage
fi
if [[ "$print_render_python" = 0 ]]; then
  [[ "$output" = /* && "$output" == *.app && "$approval_output" = /* ]] || { echo "output and approval-output must be absolute, with output ending in .app" >&2; exit 64; }
  [[ ! -e "$output" && ! -L "$output" && ! -e "$approval_output" && ! -L "$approval_output" ]] || { echo "refusing to replace existing output" >&2; exit 73; }
fi
root="$(cd "$(dirname "$0")/.." && pwd -P)"
[[ -n "$support_root" ]] || usage
support_root="$(cd "$support_root" && pwd -P)"
# Keep the fallback identical to the old installed orchestrator: resolve a
# TOML-capable interpreter by its ordered PATH/absolute candidates, then
# canonicalise symlinks so the installer and sealed lanes record one identity.
resolve_render_python() {
  local candidate="${1:-${TARTCI_RENDER_PYTHON:-${TARTCI_PYTHON:-}}}"
  # shellcheck disable=SC1091 # support_root is an explicit checkout argument
  . "$support_root/providers/common/toml-python.lib.sh"
  if [[ -n "$candidate" ]]; then
    TARTCI_PYTHON="$candidate" tartci_toml_python_path
  else
    tartci_toml_python_path
  fi
}
canonical_python() {
  local value="$1" hops=0 target
  while [[ -L "$value" ]]; do
    ((hops += 1))
    [[ "$hops" -le 16 ]] || { echo "launcher render interpreter symlink cycle: $value" >&2; return 78; }
    target="$(readlink "$value")"
    [[ "$target" = /* ]] || target="$(dirname "$value")/$target"
    value="$target"
  done
  [[ -x "$value" ]] || { echo "launcher render interpreter is not executable: $value" >&2; return 78; }
  (cd "$(dirname "$value")" && printf '%s/%s\n' "$PWD" "$(basename "$value")")
}
resolved_render_python="$(resolve_render_python "$render_python")" || {
  echo "launcher build requires a Python 3.11+ interpreter with tomllib; set TARTCI_PYTHON or pass --python" >&2
  exit 127
}
[[ -n "$resolved_render_python" ]] || {
  echo "launcher build could not find a Python 3.11+ interpreter with tomllib; set TARTCI_PYTHON or pass --python" >&2
  exit 127
}
render_python="$(canonical_python "$resolved_render_python")" || exit $?
if [[ "$print_render_python" = 1 ]]; then
  [[ -z "$output" && -z "$approval_output" && -z "$identity" && -z "$profile" ]] || usage
  printf '%s\n' "$render_python"
  exit 0
fi
profile="$(cd "$(dirname "$profile")" && pwd -P)/$(basename "$profile")"
export TARTCI_RENDER_PYTHON="$render_python" TARTCI_PYTHON="$render_python"
"$render_python" "$support_root/scripts/tartci_support_manifest.py" verify "$support_root/.tartci-support-manifest.json" --root "$support_root" --immutable >/dev/null
parent="$(dirname "$output")"; mkdir -p "$parent"; stage="$(mktemp -d "$parent/.tartci-launcher-app.XXXXXX")"; trap 'rm -rf "$stage"' EXIT
rendered_dir="$stage/rendered"; mkdir -p "$rendered_dir"
"$render_python" "$support_root/scripts/macos_fleet_lanes.py" render "$profile" --output "$rendered_dir" >/dev/null
app="$stage/TartCILauncher.app"; mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/support"
"$render_python" - "$support_root" "$app/Contents/Resources/support" <<'PY'
import json, shutil, sys
from pathlib import Path
source, destination = map(Path, sys.argv[1:])
manifest = json.loads((source / ".tartci-support-manifest.json").read_text())
for member in manifest["members"]:
    relative = Path(member["path"])
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / relative, target)
shutil.copy2(
    source / ".tartci-support-manifest.json",
    destination / ".tartci-support-manifest.json",
)
PY
chmod -R u+w "$app/Contents/Resources/support"
"$render_python" - "$rendered_dir" "$profile" "$support_root/.tartci-support-manifest.json" "$app/Contents/Resources/lanes.json" "$app/Contents/Resources/bundle.json" "$app/Contents/Resources/support/.tartci-launch" "$render_python" <<'PY'
import hashlib, json, plistlib, re, stat, sys, tomllib
from pathlib import Path
source, profile, manifest, output, metadata, launch = map(Path, sys.argv[1:7]); sys_python = sys.argv[7]; lanes = {}
# The sealed store is the profile's own external-volume Tart home (m3:
# /Volumes/<data-volume>/VMs, m5studio: /Volumes/<data-volume>/VMs); every lane must use it.
tart_home = tomllib.loads(profile.read_text())["host"]["tart_home"]
store_parts = tart_home.split("/")[1:]
if not tart_home.startswith("/Volumes/") or len(store_parts) < 3 or any(p in ("", ".", "..") for p in store_parts):
    raise SystemExit(f"launch helper requires an external-volume Tart store, got {tart_home!r}")
for path in sorted(source.glob("*.plist")):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode): raise SystemExit(f"invalid lane plist: {path}")
    value = plistlib.loads(path.read_bytes()); env = value.get("EnvironmentVariables")
    if not isinstance(env, dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in env.items()): raise SystemExit(f"invalid lane environment: {path}")
    lane = env.get("TARTCI_QUEUE_LANE_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", lane) or lane in lanes: raise SystemExit(f"invalid or duplicate lane enum: {path}")
    if env.get("TART_HOME") != tart_home: raise SystemExit(f"wrong Tart store (expected {tart_home}): {path}")
    if env.get("TARTCI_PYTHON") != sys_python or env.get("SHIPYARD_GHAPP_PYTHON_BINARY") != sys_python:
        raise SystemExit(f"lane helper interpreter is not pinned to {sys_python}: {path}")
    lanes[lane] = {"environment": dict(sorted(env.items()))}
if not lanes: raise SystemExit("rendered config directory contains no fleet plists")
output.write_text(json.dumps({"schema":1,"lanes":lanes},sort_keys=True,separators=(",",":"))+"\n")
manifest_value=json.loads(manifest.read_text())
profile_value=tomllib.loads(profile.read_text())
profile_value["launch_helper"].pop("sha256",None)
profile_policy=hashlib.sha256(json.dumps(profile_value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()
metadata.write_text(json.dumps({
    "schema":1,
    "source_commit":manifest_value["source_commit"],
    "support_manifest_sha256":hashlib.sha256(manifest.read_bytes()).hexdigest(),
    "profile_policy_sha256":profile_policy,
    "tart_home":tart_home,
},sort_keys=True,separators=(",",":"))+"\n")
launch.write_text(
    '#!/bin/bash\n'
    'set -euo pipefail\n'
    'support_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"\n'
    'exec /bin/bash "$support_root/tartci" "$@"\n'
)
launch.chmod(0o555)
PY
chmod -R a-w "$app/Contents/Resources/support"
cat >"$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>CFBundleExecutable</key><string>tartci-launcher</string><key>CFBundleIdentifier</key><string>com.danielraffel.tartci.launcher</string><key>CFBundleName</key><string>TartCI Launcher</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleVersion</key><string>1</string><key>LSBackgroundOnly</key><true/><key>NSRemovableVolumesUsageDescription</key><string>TartCI uses an external volume for isolated local CI virtual machines.</string></dict></plist>
PLIST
xcrun swiftc -O -whole-module-optimization -target arm64-apple-macos13 -framework Security "$support_root/native/macos/tartci-launcher/main.swift" -o "$app/Contents/MacOS/tartci-launcher"
/usr/bin/codesign --force --timestamp --options runtime ${keychain_args[@]+"${keychain_args[@]}"} --sign "$identity" --identifier com.danielraffel.tartci.launcher "$app"
/usr/bin/codesign --verify --strict --deep --verbose=2 "$app"; mv "$app" "$output"
"$render_python" - "$root" "$output" "$approval_output" <<'PY'
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "scripts"))
import macos_launcher_identity
digest = macos_launcher_identity.bundle_digest(Path(sys.argv[2]))
path = Path(sys.argv[3])
path.parent.mkdir(parents=True, exist_ok=True)
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w") as handle:
    handle.write(digest + "\n")
    handle.flush()
    os.fsync(handle.fileno())
PY
