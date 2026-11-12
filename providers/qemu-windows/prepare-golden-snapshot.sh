#!/usr/bin/env bash
# Make a private APFS CoW backing file and prove its exact bytes before boot.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=providers/common/toml-python.lib.sh
source "$ROOT/providers/common/toml-python.lib.sh"
SOURCE="${1:?source qcow2 required}"
SNAPSHOT="${2:?snapshot qcow2 required}"
# Callers normally pass the already-resolved host interpreter through the
# environment. The optional third argument keeps direct operator use explicit
# while still refusing an unpinned launchd interpreter.
if [ -n "${3:-}" ]; then
  export TARTCI_PYTHON="$3"
fi
TARTCI_PYTHON="$(tartci_toml_python_path)" || {
  echo "Windows snapshot admission requires a pinned Python 3.11+ interpreter" >&2
  exit 1
}
export TARTCI_PYTHON
[[ ! -e "$SNAPSHOT" && ! -L "$SNAPSHOT" ]] || {
  echo "Windows snapshot already exists: $SNAPSHOT" >&2
  exit 1
}
[[ -f "$SOURCE" && ! -L "$SOURCE" ]] || {
  echo "Windows golden must be a regular file: $SOURCE" >&2
  exit 1
}
[[ -f "$SOURCE.sha256" && ! -L "$SOURCE.sha256" ]] || {
  echo "Windows golden SHA-256 sidecar missing or linked: $SOURCE.sha256" >&2
  exit 1
}
snapshot_ready=0
cleanup_partial_snapshot(){
  [ "$snapshot_ready" = 0 ] || return 0
  chflags nouchg "$SNAPSHOT" "$SNAPSHOT.sha256" 2>/dev/null || true
  [ ! -e "$SNAPSHOT" ] || rm "$SNAPSHOT"
  [ ! -e "$SNAPSHOT.sha256" ] || rm "$SNAPSHOT.sha256"
}
trap cleanup_partial_snapshot EXIT

# cp -c fails closed off APFS or across volumes. A regular copy would consume
# another full Windows disk for every job and must be admitted separately.
cp -c "$SOURCE" "$SNAPSHOT"
cp "$SOURCE.sha256" "$SNAPSHOT.sha256"
cmp -s "$SOURCE.sha256" "$SNAPSHOT.sha256" || {
  echo "Windows golden sidecar changed while cloning" >&2
  exit 1
}
[[ -f "$SNAPSHOT" && ! -L "$SNAPSHOT" && -f "$SNAPSHOT.sha256" && ! -L "$SNAPSHOT.sha256" ]] || {
  echo "Windows snapshot is not a regular file" >&2
  exit 1
}
chflags uchg "$SNAPSHOT" "$SNAPSHOT.sha256"
hash_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
hash_started_epoch="$(date +%s)"
if nice -n 20 "$TARTCI_PYTHON" "$ROOT/scripts/verify_windows_golden.py" --no-cache "$SNAPSHOT"; then
  hash_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  hash_finished_epoch="$(date +%s)"
  printf 'windows golden hash start=%s end=%s seconds=%s\n' \
    "$hash_started_at" "$hash_finished_at" "$((hash_finished_epoch - hash_started_epoch))" >&2
else
  rc=$?
  printf 'windows golden hash start=%s end=%s status=failed\n' \
    "$hash_started_at" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
  exit "$rc"
fi
snapshot_ready=1
