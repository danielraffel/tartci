#!/usr/bin/env bash
# tart-macos/run.sh — run one build+test inside an ephemeral Tart macOS VM,
# cloned from a macOS runner golden, with durable host caches mounted, then
# discard the clone. Project-specific defaults target Pulp, but every knob has
# a TARTCI_* env name plus PULP_* fallback for the existing first consumer.
set -euo pipefail

export TART_HOME="${TART_HOME:-$HOME/VMs}"
SSH_KEY_PRIV="${TARTCI_VM_SSH_KEY:-${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}}"
VM_USER="${TARTCI_VM_USER:-${PULP_VM_USER:-admin}}"
GOLDEN="${TARTCI_MACOS_GOLDEN:-${PULP_MACOS_GOLDEN:-${PULP_RUNNER_GOLDEN:-pulp-build-runner:latest}}}"
SRC="${TARTCI_SRC:-$PWD}"
VM=""
DISK=""
BUILD_TYPE="${TARTCI_BUILD_TYPE:-${PULP_BUILD_TYPE:-Release}}"
BUILD_TARGET="${TARTCI_BUILD_TARGET:-${PULP_BUILD_TARGET:-}}"
CACHE_ROOT="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/pulp-ci}}"
CTEST_ARGS="${TARTCI_CTEST_ARGS:-${PULP_CTEST_ARGS:---output-on-failure --exclude-regex AudioWorkgroup --label-exclude slow}}"
CMAKE_ARGS="${TARTCI_CMAKE_ARGS:-${PULP_CMAKE_ARGS:--DPULP_BUILD_TESTS=ON -DPULP_BUILD_EXAMPLES=ON}}"
KEEP=0
CLONED=0
RPID=""
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

usage(){
  cat <<'USAGE'
tart-macos/run.sh — run one build+test inside an ephemeral Tart macOS VM.

Usage:
  providers/tart-macos/run.sh --src /path/to/checkout [--golden pulp-build-runner:latest]
      [--vm macos-job-N] [--build-type Release] [--build-target target]
      [--cmake-args "..."] [--ctest-args "..."] [--keep]

Environment:
  TART_HOME defaults to ~/VMs. TARTCI_* env names have PULP_* fallbacks for Pulp.
USAGE
}

command -v tart >/dev/null 2>&1 || die "tart not installed (brew install openai/tools/tart)"
command -v ssh >/dev/null 2>&1 || die "ssh not installed"

while [ $# -gt 0 ]; do case "$1" in
  --golden) GOLDEN="$2"; shift 2;;
  --src) SRC="$2"; shift 2;;
  --vm) VM="$2"; shift 2;;
  --disk) DISK="$2"; shift 2;;
  --build-type) BUILD_TYPE="$2"; shift 2;;
  --build-target) BUILD_TARGET="$2"; shift 2;;
  --cache-root) CACHE_ROOT="$2"; shift 2;;
  --cmake-args) CMAKE_ARGS="$2"; shift 2;;
  --ctest-args) CTEST_ARGS="$2"; shift 2;;
  --keep) KEEP=1; shift;;
  -h|--help) usage; exit 0;;
  *) die "unknown arg: $1";;
esac; done

[ -d "$SRC" ] || die "--src <checkout-dir> must exist (got: $SRC)"
SRC="$(cd "$SRC" && pwd)"
VM="${VM:-macos-job-$$}"
FETCHCONTENT_SOURCE_ROOT="${PULP_SHARED_FETCHCONTENT_SOURCE_DIR:-$HOME/Library/Caches/Pulp/fetchcontent-src}"
mkdir -p "$CACHE_ROOT/ccache" "$FETCHCONTENT_SOURCE_ROOT"

cleanup(){
  if [ "$KEEP" = 1 ]; then note "--keep: leaving $VM (delete with: tart delete $VM)"; return; fi
  [ "$CLONED" = 1 ] || return
  tart stop "$VM" >/dev/null 2>&1 || true
  [ -n "$RPID" ] && kill "$RPID" 2>/dev/null || true
  sleep 2
  tart delete "$VM" >/dev/null 2>&1 || true
  note "discarded ephemeral VM $VM"
}
trap cleanup EXIT INT TERM

note "cloning $GOLDEN → $VM (CoW)"
tart clone "$GOLDEN" "$VM"
CLONED=1
if [ -n "$DISK" ]; then tart set "$VM" --disk-size "$DISK"; note "disk → ${DISK}G"; fi

note "booting with host mounts (src ro, ccache/fetchcontent rw); baked Skia expected in the golden"
boot_log="$(mktemp -t "tart-macos-run-$VM")"
tart run --no-graphics \
  --dir="src:$SRC:ro" \
  --dir="ccache:$CACHE_ROOT/ccache" \
  --dir="fetchcontent:$FETCHCONTENT_SOURCE_ROOT:ro" \
  "$VM" >"$boot_log" 2>&1 & RPID=$!

IP=""
for _ in $(seq 1 60); do IP="$(tart ip "$VM" 2>/dev/null || true)"; [ -n "$IP" ] && break; sleep 2; done
if [ -z "$IP" ]; then
  note "no IP after 120s — last tart run lines:"; tail -10 "$boot_log" >&2 2>/dev/null || true
  rm -f "$boot_log"
  die "no IP for $VM"
fi
rm -f "$boot_log"

sshok=0
for _ in $(seq 1 90); do
  if ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$IP" true 2>/dev/null; then sshok=1; break; fi
  sleep 2
done
[ "$sshok" = 1 ] || die "ssh did not become ready for $VM at $IP"
note "vm $VM up at $IP — running build ($BUILD_TYPE) + ctest in guest"

set +e
ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$IP" \
  "BUILD_TYPE='$BUILD_TYPE' BUILD_TARGET='$BUILD_TARGET' CMAKE_ARGS='$CMAKE_ARGS' CTEST_ARGS='$CTEST_ARGS' bash -s" <<'GUEST'
set -euo pipefail
eval "$(/opt/homebrew/bin/brew shellenv)"
SHARED="/Volumes/My Shared Files"
ln -sfn "$SHARED/src" "$HOME/src"
ln -sfn "$SHARED/ccache" "$HOME/ccache"
mkdir -p "$HOME/Library/Caches/Pulp/fetchcontent-src"
fetchcontent_hydrated=false
for attempt in 1 2 3; do
  if rsync -a "$SHARED/fetchcontent/" "$HOME/Library/Caches/Pulp/fetchcontent-src/"; then
    fetchcontent_hydrated=true
    break
  fi
  [ "$attempt" -eq 3 ] || sleep 1
done
[ "$fetchcontent_hydrated" = true ] || {
  echo "tartci: FetchContent seed changed during three hydration attempts" >&2
  exit 1
}

export CCACHE_DIR="$HOME/ccache"
export CCACHE_TEMPDIR="$HOME/.ccache-tmp"
mkdir -p "$CCACHE_TEMPDIR"
export CCACHE_BASEDIR="$HOME/src"
export CCACHE_NOHASHDIR=true
export CCACHE_COMPILERCHECK=content
export CCACHE_NODEPEND=true
unset CCACHE_DEPEND
export CCACHE_SLOPPINESS=time_macros
export SKIA_DIR="$HOME/pulp-skia-build"
export PULP_SHARED_FETCHCONTENT_SOURCE_DIR="$HOME/Library/Caches/Pulp/fetchcontent-src"

ccache --zero-stats >/dev/null 2>&1 || true
BUILD="$HOME/build"
rm -rf "$BUILD"
# shellcheck disable=SC2086
cmake -S "$HOME/src" -B "$BUILD" \
  -G Ninja \
  -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
  $CMAKE_ARGS
if [ -n "${BUILD_TARGET:-}" ]; then
    cmake --build "$BUILD" --target "$BUILD_TARGET" --parallel "$(sysctl -n hw.ncpu)"
else
    cmake --build "$BUILD" --parallel "$(sysctl -n hw.ncpu)"
fi
echo "=== ccache stats (warmth) ==="
ccache --show-stats | grep -iE 'cacheable|hit|miss|cache size' || ccache -s
ctest --test-dir "$BUILD" $CTEST_ARGS
GUEST
RC=$?
set -e

note "in-guest exit=$RC"
echo "=== host ccache after job (durable across clones) ==="
CCACHE_DIR="$CACHE_ROOT/ccache" ccache --show-stats 2>/dev/null | grep -iE 'cache size|hits|misses|hit rate' || true
exit "$RC"
