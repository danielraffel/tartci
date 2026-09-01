#!/bin/bash
set -euo pipefail
usage(){ echo "usage: $0 --output PATH.app --approval-output PATH --identity STRING --support-root PATH --profile PATH" >&2; exit 64; }
output="" approval_output="" identity="" support_root="" profile=""
while [[ $# -gt 0 ]]; do case "$1" in
  --output) output="${2:-}"; shift 2;; --identity) identity="${2:-}"; shift 2;;
  --approval-output) approval_output="${2:-}"; shift 2;;
  --support-root) support_root="${2:-}"; shift 2;;
  --profile) profile="${2:-}"; shift 2;; *) usage;; esac; done
[[ -n "$output" && -n "$approval_output" && -n "$identity" && -n "$support_root" && -n "$profile" ]] || usage
[[ "$output" = /* && "$output" == *.app && "$approval_output" = /* ]] || { echo "output and approval-output must be absolute, with output ending in .app" >&2; exit 64; }
[[ ! -e "$output" && ! -L "$output" && ! -e "$approval_output" && ! -L "$approval_output" ]] || { echo "refusing to replace existing output" >&2; exit 73; }
root="$(cd "$(dirname "$0")/.." && pwd -P)"; support_root="$(cd "$support_root" && pwd -P)"; profile="$(cd "$(dirname "$profile")" && pwd -P)/$(basename "$profile")"
python3 "$support_root/scripts/tartci_support_manifest.py" verify "$support_root/.tartci-support-manifest.json" --root "$support_root" --immutable >/dev/null
parent="$(dirname "$output")"; mkdir -p "$parent"; stage="$(mktemp -d "$parent/.tartci-launcher-app.XXXXXX")"; trap 'rm -rf "$stage"' EXIT
rendered_dir="$stage/rendered"; mkdir -p "$rendered_dir"
python3 "$support_root/scripts/macos_fleet_lanes.py" render "$profile" --output "$rendered_dir" >/dev/null
app="$stage/TartCILauncher.app"; mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/support"
python3 - "$support_root" "$app/Contents/Resources/support" <<'PY'
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
python3 - "$rendered_dir" "$profile" "$support_root/.tartci-support-manifest.json" "$app/Contents/Resources/lanes.json" "$app/Contents/Resources/bundle.json" "$app/Contents/Resources/support/.tartci-launch" <<'PY'
import hashlib, json, plistlib, re, stat, sys, tomllib
from pathlib import Path
source, profile, manifest, output, metadata, launch = map(Path, sys.argv[1:]); lanes = {}
for path in sorted(source.glob("*.plist")):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode): raise SystemExit(f"invalid lane plist: {path}")
    value = plistlib.loads(path.read_bytes()); env = value.get("EnvironmentVariables")
    if not isinstance(env, dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in env.items()): raise SystemExit(f"invalid lane environment: {path}")
    lane = env.get("TARTCI_QUEUE_LANE_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", lane) or lane in lanes: raise SystemExit(f"invalid or duplicate lane enum: {path}")
    if env.get("TART_HOME") != "/Volumes/Workshop/VMs": raise SystemExit(f"wrong M3 Tart store: {path}")
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
    "tart_home":"/Volumes/Workshop/VMs",
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
<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd"><plist version="1.0"><dict><key>CFBundleExecutable</key><string>tartci-launcher</string><key>CFBundleIdentifier</key><string>com.danielraffel.tartci.launcher</string><key>CFBundleName</key><string>TartCI Launcher</string><key>CFBundlePackageType</key><string>APPL</string><key>CFBundleVersion</key><string>1</string><key>LSBackgroundOnly</key><true/><key>NSRemovableVolumesUsageDescription</key><string>TartCI uses the Workshop volume for isolated local CI virtual machines.</string></dict></plist>
PLIST
xcrun swiftc -O -whole-module-optimization -target arm64-apple-macos13 -framework Security "$support_root/native/macos/tartci-launcher/main.swift" -o "$app/Contents/MacOS/tartci-launcher"
/usr/bin/codesign --force --timestamp --options runtime --sign "$identity" --identifier com.danielraffel.tartci.launcher "$app"
/usr/bin/codesign --verify --strict --deep --verbose=2 "$app"; mv "$app" "$output"
python3 - "$root" "$output" "$approval_output" <<'PY'
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
