#!/usr/bin/env bash
# tart-linux/run.sh — run ONE Pulp build+test inside an ephemeral Tart Linux VM
# cloned from the `pulp-linux-build` golden, with the host ccache mounted, then
# discard the clone. Native arm64 Ubuntu 24.04; Skia is baked into the golden's
# in-checkout external/skia-build (FindSkia auto-discovers it, no SKIA_DIR).
#
# This codifies the proven runbook §3 ("the easy, fully-proven win": golden
# builds 1003/1003, ctest 99%, warm build ~20s @ 99.9% ccache) as a one-command,
# repeatable, ephemeral lane — the structural mirror of the macOS tart-run-job.sh.
#
# Flow:
#   tart clone <golden> <job-vm>             # CoW, seconds
#   tart run --dir=ccache:<host>             # virtio-fs host ccache mount
#   ssh admin@vm → build + ctest in ~/pulp   # at --ref, ccache warm if mountable
#   tart stop && tart delete <job-vm>         # discard (unless --keep)
#
# Usage:
#   providers/tart-linux/run.sh                          # build golden's ~/pulp as-is
#   providers/tart-linux/run.sh --ref origin/feature/x   # fetch + build a ref
#   providers/tart-linux/run.sh --no-gpu                 # fast no-Skia smoke
#   providers/tart-linux/run.sh --keep --ctest-args '-R Knob'
set -euo pipefail

export TART_HOME="${TART_HOME:-/Volumes/Workshop/VMs}"
GOLDEN="${PULP_LINUX_GOLDEN:-pulp-linux-build:latest}"
VM_USER="${PULP_VM_USER:-admin}"
SSH_KEY="${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}"
CACHE_ROOT="${PULP_CI_CACHE:-$HOME/.cache/pulp-ci}"
VM=""; REF=""; BUILD_TYPE="Release"; KEEP=0; NO_GPU=0
CTEST_ARGS="${PULP_CTEST_ARGS:---output-on-failure --label-exclude slow}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
command -v tart >/dev/null 2>&1 || die "tart not installed (brew install cirruslabs/cli/tart)"

while [ $# -gt 0 ]; do case "$1" in
  --golden) GOLDEN="$2"; shift 2;;
  --ref) REF="$2"; shift 2;;
  --vm) VM="$2"; shift 2;;
  --build-type) BUILD_TYPE="$2"; shift 2;;
  --ctest-args) CTEST_ARGS="$2"; shift 2;;
  --cache-root) CACHE_ROOT="$2"; shift 2;;
  --no-gpu) NO_GPU=1; shift;;
  --keep) KEEP=1; shift;;
  -h|--help) sed -n '2,21p' "$0"; exit 0;;
  *) die "unknown arg: $1";;
esac; done

VM="${VM:-linux-job-$$}"
mkdir -p "$CACHE_ROOT/ccache-linux"

RPID=""
CLONED=0
cleanup(){
  if [ "$KEEP" = 1 ]; then note "--keep: leaving $VM (tart delete $VM to remove)"; return; fi
  # Only ever touch a VM THIS run created. Otherwise a name collision (a stale
  # $VM, or a --vm a caller also uses elsewhere) makes `tart clone` fail and the
  # EXIT trap would blindly stop+delete the pre-existing VM out from under it.
  [ "$CLONED" = 1 ] || return
  tart stop "$VM" >/dev/null 2>&1 || true
  [ -n "$RPID" ] && kill "$RPID" 2>/dev/null || true
  sleep 2
  tart delete "$VM" >/dev/null 2>&1 || true
  note "discarded ephemeral VM $VM"
}
trap cleanup EXIT

note "cloning $GOLDEN → $VM (CoW)"
tart clone "$GOLDEN" "$VM"
CLONED=1

note "booting with host ccache mount (Skia is baked into the golden)"
tart run --no-graphics --dir="ccache:$CACHE_ROOT/ccache-linux" "$VM" >/dev/null 2>&1 & RPID=$!

IP=""; for _ in $(seq 1 60); do IP="$(tart ip "$VM" 2>/dev/null || true)"; [ -n "$IP" ] && break; sleep 2; done
[ -n "$IP" ] || die "no IP for $VM after 120s"
for _ in $(seq 1 90); do ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" "$VM_USER@$IP" true 2>/dev/null && break; sleep 2; done
note "vm $VM up at $IP — building $BUILD_TYPE (ref=${REF:-<golden as-is>}, gpu=$([ "$NO_GPU" = 1 ] && echo off || echo on))"

set +e
ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" "$VM_USER@$IP" \
  "REF='$REF' BUILD_TYPE='$BUILD_TYPE' NO_GPU='$NO_GPU' CTEST_ARGS='$CTEST_ARGS' bash -s" <<'GUEST'
set -euo pipefail

# Best-effort host-ccache via virtio-fs. The share root is perm-restricted; the
# named "ccache" subdir is the rw one. If the mount isn't usable (perm/uid),
# fall back to an in-guest ccache — the build still completes, just colder.
CCACHE_DIR="$HOME/.ccache"
if sudo mkdir -p /mnt/host 2>/dev/null && \
   sudo mount -t virtiofs com.apple.virtio-fs.automount /mnt/host 2>/dev/null && \
   [ -d /mnt/host/ccache ] && [ -w /mnt/host/ccache ]; then
  CCACHE_DIR="/mnt/host/ccache"
  echo "• ccache: host virtio-fs mount (warm)"
else
  echo "• ccache: in-guest fallback (cold; host mount not usable)"
fi
export CCACHE_DIR
mkdir -p "$CCACHE_DIR"
# Matched hashing config is mandatory for cross-clone hits (runbook §3.4).
export CCACHE_BASEDIR="$HOME/pulp"
export CCACHE_NOHASHDIR=true
export CCACHE_SLOPPINESS=time_macros,pch_defines
export CCACHE_DEPEND=true
export CCACHE_TEMPDIR="$HOME/.ccache-tmp"; mkdir -p "$CCACHE_TEMPDIR"

cd "$HOME/pulp"
if [ -n "$REF" ]; then
  # Fetch ALL remote-tracking refs first, then check out the ref detached. The
  # documented form is `origin/<branch>` (or a SHA/tag); `git fetch origin "$REF"`
  # with REF="origin/main" asks the remote for a ref literally named "origin/main"
  # (fails) and the old fallback then built a STALE locally-cached ref. Fetching
  # the whole remote and detaching at the ref builds exactly what's on origin now.
  git fetch --quiet origin
  git checkout --quiet --detach "$REF"
fi
echo "• building pulp @ $(git rev-parse --short HEAD) ($(git rev-parse --abbrev-ref HEAD 2>/dev/null))"

GPU_FLAG=""; [ "$NO_GPU" = 1 ] && GPU_FLAG="-DPULP_ENABLE_GPU=OFF"
ccache --zero-stats >/dev/null 2>&1 || true
cmake -S . -B build -G Ninja \
  -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
  -DPULP_BUILD_TESTS=ON -DPULP_BUILD_EXAMPLES=ON $GPU_FLAG \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
cmake --build build --parallel "$(nproc)"
echo "=== ccache stats ==="; ccache -s 2>/dev/null | grep -iE 'hits|misses|cache size' || true
ctest --test-dir build $CTEST_ARGS
GUEST
RC=$?
set -e
note "in-guest build+ctest exit=$RC"
exit $RC
