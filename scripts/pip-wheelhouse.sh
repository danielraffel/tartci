#!/usr/bin/env bash
# Populate the host pip wheelhouse that macOS guests mount read-only.
#
#   scripts/pip-wheelhouse.sh sync --lock <requirements.lock> \
#     --python-version 3.14 --platform macosx_14_0_arm64 [--dir DIR] [--python PY]
#
# The lock must be hash-pinned (pip-compile --generate-hashes): every wheel is
# downloaded under --require-hashes, so the wheelhouse can only ever hold the
# exact bytes the consuming repository already trusts. --python-version and
# --platform describe the GUEST interpreter, not this host's; run once per
# guest interpreter shape.
#
# The sync is additive. A running guest may have the directory mounted, so no
# wheel is ever removed or rewritten in place: new wheels are downloaded into a
# private staging directory on the same filesystem and each is renamed into
# place, which is atomic. Superseded wheels are harmless because the consuming
# install is hash-pinned to one version.
#
# Runners pick the wheelhouse up at the next VM boot; no service restart is
# needed and an empty or absent directory leaves boots unchanged.
set -euo pipefail

usage(){
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

die(){ printf 'pip-wheelhouse: %s\n' "$*" >&2; exit 1; }

[ "${1:-}" = "sync" ] || usage 2
shift

cache_root="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/pulp-ci}}"
dir="${TARTCI_PIP_WHEELHOUSE_DIR:-$cache_root/pip-wheelhouse}"
lock="" python_version="" platform="" python="${PYTHON:-python3}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --lock) lock="${2:-}"; shift 2;;
    --python-version) python_version="${2:-}"; shift 2;;
    --platform) platform="${2:-}"; shift 2;;
    --dir) dir="${2:-}"; shift 2;;
    --python) python="${2:-}"; shift 2;;
    -h|--help) usage 0;;
    *) die "unknown argument: $1";;
  esac
done

[ -n "$lock" ] || die "--lock is required"
[ -f "$lock" ] || die "lock file not found: $lock"
grep -q -- '--hash=sha256:' "$lock" \
  || die "$lock carries no --hash entries; generate it with pip-compile --generate-hashes"
[[ "$python_version" =~ ^3\.[0-9]+$ ]] || die "--python-version must look like 3.14"
[[ "$platform" =~ ^[A-Za-z0-9_.]+$ ]] || die "--platform must be a wheel platform tag such as macosx_14_0_arm64"
case "$dir" in
  /*) ;;
  *) die "--dir must be an absolute path: $dir";;
esac
case "$dir" in
  *:*|*$'\n'*|*$'\r'*) die "--dir contains a character Tart cannot share: $dir";;
esac

mkdir -p "$dir"
staging="$(mktemp -d "$dir/.staging.XXXXXX")"
trap 'rm -rf "$staging"' EXIT

# --no-deps: the lock already lists the full closure, and resolving again for a
# foreign interpreter could only disagree with it. The wheel's own platform tag
# may be older than the one requested (macosx_11_0 for a macosx_14_0 guest), so
# pure-Python wheels are admitted through the extra `any` platform.
"$python" -m pip download \
  --quiet --disable-pip-version-check \
  --require-hashes --only-binary=:all: --no-deps \
  --python-version "$python_version" \
  --platform "$platform" --platform any \
  --dest "$staging" -r "$lock"

added=0 kept=0
for wheel in "$staging"/*.whl; do
  [ -f "$wheel" ] || continue
  name="${wheel##*/}"
  if [ -f "$dir/$name" ] && cmp -s "$wheel" "$dir/$name"; then
    kept=$((kept + 1))
    continue
  fi
  [ -e "$dir/$name" ] && die "refusing to replace $dir/$name with different bytes"
  mv "$wheel" "$dir/$name"
  added=$((added + 1))
done
[ $((added + kept)) -gt 0 ] || die "pip downloaded nothing for $lock"
printf 'pip-wheelhouse: %s: %d added, %d already present (python %s, %s)\n' \
  "$dir" "$added" "$kept" "$python_version" "$platform"
