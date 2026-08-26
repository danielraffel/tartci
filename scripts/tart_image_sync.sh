#!/usr/bin/env bash
# Export one stopped Tart golden, resume its transfer, verify it, and optionally
# import it under a collision-free staging name. Never changes pool state.
set -euo pipefail

usage() {
  cat <<'EOF'
usage: tartci tart-image-sync --name NAME --destination HOST \
         --source-tart-home PATH --destination-tart-home PATH \
         [--fallback HOST] [--staging PATH] [--apply] [--import]

Default is a read-only plan. --apply permits export/transfer. --import also
permits a verified import under NAME.incoming.TIMESTAMP; it never replaces NAME.
Run this command on the source host. Both source and destination pools must be
off with no running Tart VMs. HOST should be the LAN SSH alias; --fallback is
used only when that alias is unreachable.
EOF
}

name="" destination="" fallback="" source_home="" destination_home=""
staging="" apply=0 do_import=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2 ;;
    --destination) destination="$2"; shift 2 ;;
    --fallback) fallback="$2"; shift 2 ;;
    --source-tart-home) source_home="$2"; shift 2 ;;
    --destination-tart-home) destination_home="$2"; shift 2 ;;
    --staging) staging="$2"; shift 2 ;;
    --apply) apply=1; shift ;;
    --import) do_import=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "tart-image-sync: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$name" ] && [ -n "$destination" ] && [ -n "$source_home" ] && [ -n "$destination_home" ] \
  || { usage >&2; exit 2; }
case "$name" in
  ''|*/*|*..*|*[!A-Za-z0-9_.:-]*) echo "tart-image-sync: image name contains unsupported characters" >&2; exit 2 ;;
esac
case "$destination:$fallback" in
  *[!A-Za-z0-9_.:-]*) echo "tart-image-sync: hosts contain unsupported characters" >&2; exit 2 ;;
esac
case "$source_home:$destination_home" in
  /*:/*) ;;
  *) echo "tart-image-sync: source and destination paths must be absolute" >&2; exit 2 ;;
esac
case "$source_home:$destination_home" in
  *[!A-Za-z0-9_.:/-]*) echo "tart-image-sync: paths contain unsupported characters" >&2; exit 2 ;;
esac
[ "$apply" = 0 ] || [ -n "$staging" ] \
  || { echo "tart-image-sync: --apply requires an explicit dedicated --staging path" >&2; exit 2; }
if [ -n "$staging" ]; then
  case "$staging" in /*) ;; *) echo "tart-image-sync: staging path must be absolute" >&2; exit 2;; esac
  case "$staging" in *[!A-Za-z0-9_.:/-]*) echo "tart-image-sync: staging path contains unsupported characters" >&2; exit 2;; esac
fi
[ "$do_import" = 0 ] || [ "$apply" = 1 ] \
  || { echo "tart-image-sync: --import requires --apply" >&2; exit 2; }

pick_host() {
  if ssh -o BatchMode=yes -o ConnectTimeout=3 "$destination" true >/dev/null 2>&1; then
    printf '%s\n' "$destination"
  elif [ -n "$fallback" ] && ssh -o BatchMode=yes -o ConnectTimeout=5 "$fallback" true >/dev/null 2>&1; then
    printf '%s\n' "$fallback"
  else
    return 1
  fi
}

remote="$(pick_host)" || {
  echo "tart-image-sync: neither preferred LAN destination nor fallback is reachable" >&2
  exit 3
}
archive="${staging:-<required-for-apply>}/${name//[:\/]/_}.tvm"
remote_stage="$destination_home/.tartci/imports"
remote_archive="$remote_stage/$(basename "$archive")"
remote_path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
manifest="$archive.source.json"
fingerprint_tool="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tart_source_fingerprint.py"

assert_all_stopped() {
  local side="$1" rows="$2"
  python3 - "$side" "$rows" <<'PY'
import json, sys
side, raw = sys.argv[1:]
try: rows = json.loads(raw)
except Exception as exc: raise SystemExit(f"{side} Tart inventory unavailable: {exc}")
running = [row.get("Name", "?") for row in rows if row.get("State") != "stopped"]
if running: raise SystemExit(f"{side} has non-stopped Tart VMs; refusing fail-closed: {running}")
PY
}

read_remote_inventory() {
  ssh "$remote" "PATH='$remote_path' TART_HOME='$destination_home' tart list --format json"
}

check_pools_off() {
  local local_pool remote_pool
  local_pool="$(TART_HOME="$source_home" tartci pool status --json 2>/dev/null || true)"
  remote_pool="$(ssh "$remote" "PATH=/opt/homebrew/bin:/usr/local/bin:\$HOME/.local/bin:/usr/bin:/bin /bin/bash -lc 'TART_HOME=\"$destination_home\" tartci pool status --json'" 2>/dev/null || true)"
  python3 - "$local_pool" "$remote_pool" <<'PY'
import json, sys
for side, raw in (("source", sys.argv[1]), ("destination", sys.argv[2])):
    try: value = json.loads(raw)
    except Exception: raise SystemExit(f"{side} pool status unavailable; refusing fail-closed")
    if value.get("state") != "off" or value.get("participating") is not False:
        raise SystemExit(f"{side} pool must be off and nonparticipating")
PY
}

printf 'source_image=%s\nsource_tart_home=%s\ndestination=%s\ndestination_tart_home=%s\narchive=%s\n' \
  "$name" "$source_home" "$remote" "$destination_home" "$archive"
if [ "$apply" = 0 ]; then
  echo "plan_only=true (no export, transfer, import, pool, or LaunchAgent mutation)"
  exit 0
fi

command -v tart >/dev/null || { echo "tart-image-sync: tart not found" >&2; exit 4; }
command -v rsync >/dev/null || { echo "tart-image-sync: rsync not found" >&2; exit 4; }
source_json="$(TART_HOME="$source_home" tart list --format json)"
destination_json="$(read_remote_inventory)"
assert_all_stopped source "$source_json"
assert_all_stopped destination "$destination_json"
python3 - "$name" "$source_json" <<'PY'
import json, sys
name = sys.argv[1]
rows = json.loads(sys.argv[2])
row = next((x for x in rows if x.get("Name") == name), None)
if row is None:
    raise SystemExit(f"source golden not found: {name}")
if row.get("State") != "stopped":
    raise SystemExit(f"source golden must be stopped, observed {row.get('State')}")
PY

check_pools_off

mkdir -p "$staging"
[ ! -L "$staging" ] && [ "$(stat -f %u "$staging")" = "$(id -u)" ] || {
  echo "tart-image-sync: staging directory must be a real directory owned by the current user" >&2
  exit 5
}
chmod 700 "$staging"
[ ! -e "$archive" ] || { [ -f "$archive" ] && [ ! -L "$archive" ] && [ "$(stat -f %u "$archive")" = "$(id -u)" ]; } || {
  echo "tart-image-sync: archive path is unsafe" >&2; exit 5;
}
[ ! -e "$manifest" ] || { [ -f "$manifest" ] && [ ! -L "$manifest" ] && [ "$(stat -f %u "$manifest")" = "$(id -u)" ]; } || {
  echo "tart-image-sync: manifest path is unsafe" >&2; exit 5;
}
fingerprint="$(python3 "$fingerprint_tool" --tart-home "$source_home" --name "$name")"
if [ ! -f "$archive" ]; then
  echo "exporting stopped golden to $archive"
  TART_HOME="$source_home" tart export "$name" "$archive"
  after="$(python3 "$fingerprint_tool" --tart-home "$source_home" --name "$name")"
  [ "$fingerprint" = "$after" ] || {
    echo "tart-image-sync: source image changed during export; refusing archive" >&2
    exit 5
  }
  checksum="$(shasum -a 256 "$archive" | awk '{print $1}')"
  python3 - "$manifest" "$fingerprint" "$checksum" "$(stat -f %z "$archive")" <<'PY'
import json, pathlib, sys
target, fingerprint, checksum, size = sys.argv[1:]
value = {"schema": 1, "source": json.loads(fingerprint), "archive_sha256": checksum, "archive_size": int(size)}
pathlib.Path(target).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
PY
else
  [ -f "$manifest" ] || {
    echo "tart-image-sync: existing archive has no source manifest; refusing stale reuse" >&2
    exit 5
  }
fi
checksum="$(shasum -a 256 "$archive" | awk '{print $1}')"
python3 - "$manifest" "$fingerprint" "$checksum" "$(stat -f %z "$archive")" <<'PY'
import json, pathlib, sys
manifest, fingerprint, checksum, size = sys.argv[1:]
value = json.loads(pathlib.Path(manifest).read_text())
if value.get("schema") != 1 or value.get("source") != json.loads(fingerprint):
    raise SystemExit("existing archive source fingerprint does not match current golden")
if value.get("archive_sha256") != checksum or value.get("archive_size") != int(size):
    raise SystemExit("existing archive bytes do not match their provenance manifest")
PY
# Recheck immediately before the first remote mutation. Pool-off plus an empty
# running inventory is the required transfer boundary.
check_pools_off
assert_all_stopped source "$(TART_HOME="$source_home" tart list --format json)"
assert_all_stopped destination "$(read_remote_inventory)"
ssh "$remote" "mkdir -p '$remote_stage'"
rsync -a --partial --partial-dir=.rsync-partial --progress "$archive" "$remote:$remote_archive"
remote_checksum="$(ssh "$remote" "shasum -a 256 '$remote_archive' | awk '{print \$1}'")"
[ "$checksum" = "$remote_checksum" ] || {
  echo "tart-image-sync: checksum mismatch; leaving resumable staging artifacts in place" >&2
  exit 5
}
printf 'sha256=%s\nverified=true\n' "$checksum"
[ "$do_import" = 1 ] || { echo "staged_only=true"; exit 0; }

# A transfer can be long. Re-establish the destination idle boundary before
# importing into its Tart store; never infer idleness from the earlier check.
check_pools_off
assert_all_stopped destination "$(read_remote_inventory)"
assert_all_stopped source "$(TART_HOME="$source_home" tart list --format json)"
fresh_fingerprint="$(python3 "$fingerprint_tool" --tart-home "$source_home" --name "$name")"
[ "$fingerprint" = "$fresh_fingerprint" ] || {
  echo "tart-image-sync: source golden changed during transfer; refusing import" >&2
  exit 5
}
incoming="${name}.incoming.$(date -u +%Y%m%dT%H%M%SZ)"
ssh "$remote" "PATH='$remote_path' TART_HOME='$destination_home' tart import '$remote_archive' '$incoming'"
ssh "$remote" "PATH='$remote_path' TART_HOME='$destination_home' tart get '$incoming' --format json"
echo "imported=$incoming"
echo "activation_unchanged=true"
echo "Next idle-boundary action is an explicit operator-reviewed rename/canary; this command never replaces $name."
