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
#   providers/tart-linux/run.sh                          # build golden's ~/pulp as-is (native arm64)
#   providers/tart-linux/run.sh --ref origin/feature/x   # fetch + build a ref
#   providers/tart-linux/run.sh --no-gpu                 # fast no-Skia smoke
#   providers/tart-linux/run.sh --keep --ctest-args '-R Knob'
#   providers/tart-linux/run.sh --target-arch x86_64     # cross-build x64 + run tests under qemu-user (SMOKE)
#   providers/tart-linux/run.sh --target-arch x86_64 --self-test  # toolchain+emulator proof, golden-agnostic
#
# Cross / emulation (tartci#4): the guest is ARM64 (Apple Virtualization has no
# x86). `--target-arch x86_64` cross-compiles for x64 and runs the test subset
# under qemu-user-static (binfmt) — a SMOKE/debug signal, NOT a gate. GitHub-
# hosted x64 stays authoritative. The cross build defaults GPU OFF (the prebuilt
# Skia maps both Linux arches to the same libskia.a path — see docs/gotchas.md);
# `--gpu` requires an explicit x64 SKIA_DIR via --skia-dir, else it fails loud.
# Sanitizers, SIMD/Highway dispatch, and RT timing are unreliable emulated.
set -euo pipefail

export TART_HOME="${TART_HOME:-/Volumes/Workshop/VMs}"
GOLDEN="${PULP_LINUX_GOLDEN:-pulp-linux-build:latest}"
VM_USER="${PULP_VM_USER:-admin}"
SSH_KEY="${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}"
CACHE_ROOT="${PULP_CI_CACHE:-$HOME/.cache/pulp-ci}"
VM=""; REF=""; BUILD_TYPE="Release"; KEEP=0; NO_GPU=0
# Cross / emulation (tartci#4). Empty/arm64 TARGET_ARCH = native (unchanged path).
TARGET_ARCH="${PULP_TARGET_ARCH:-}"; WANT_GPU=0; X64_SKIA_DIR="${PULP_X64_SKIA_DIR:-}"; SELF_TEST=0
EMULATOR="${PULP_CROSS_EMULATOR:-qemu-user}"
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
  --target-arch) TARGET_ARCH="$2"; shift 2;;
  --gpu) WANT_GPU=1; shift;;
  --skia-dir) X64_SKIA_DIR="$2"; shift 2;;
  --self-test) SELF_TEST=1; shift;;
  --keep) KEEP=1; shift;;
  -h|--help) sed -n '2,29p' "$0"; exit 0;;
  *) die "unknown arg: $1";;
esac; done

# Normalize the cross intent. arm64 (or empty) == native; anything else crosses.
CROSS=0
case "$TARGET_ARCH" in
  ""|arm64|aarch64) TARGET_ARCH="arm64";;
  x86_64|amd64)     TARGET_ARCH="x86_64"; CROSS=1;;
  *) die "unsupported --target-arch '$TARGET_ARCH' (arm64 | x86_64)";;
esac
if [ "$CROSS" = 1 ]; then
  case "$EMULATOR" in qemu-user|qemu-user-static) ;; *) die "Linux cross only supports the qemu-user emulator (got '$EMULATOR')";; esac
  # GPU-on cross needs an x64 Skia tree; you can't reuse the baked arm64 one
  # (both arches collide on the same libskia.a path — docs/gotchas.md). Fail loud
  # rather than silently linking the wrong arch.
  if [ "$WANT_GPU" = 1 ] && [ -z "$X64_SKIA_DIR" ]; then
    die "--gpu with --target-arch x86_64 requires an explicit x64 Skia tree: pass --skia-dir <dir containing build/linux-gpu/lib/Release/libskia.a> (PULP_X64_SKIA_DIR). Without it the cross build would link the baked arm64 libskia.a. Default is GPU OFF."
  fi
  [ "$WANT_GPU" = 1 ] || NO_GPU=1   # cross defaults GPU off
fi

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
if [ "$SELF_TEST" = 1 ]; then
  note "vm $VM up at $IP — x86_64 toolchain+emulator self-test (cross-compile a trivial binary + run under $EMULATOR)"
elif [ "$CROSS" = 1 ]; then
  note "vm $VM up at $IP — CROSS-building x86_64 (gpu=$([ "$NO_GPU" = 1 ] && echo off || echo on), tests via $EMULATOR) — SMOKE, not a gate"
else
  note "vm $VM up at $IP — building $BUILD_TYPE (ref=${REF:-<golden as-is>}, gpu=$([ "$NO_GPU" = 1 ] && echo off || echo on))"
fi

set +e
ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" "$VM_USER@$IP" \
  "REF='$REF' BUILD_TYPE='$BUILD_TYPE' NO_GPU='$NO_GPU' CTEST_ARGS='$CTEST_ARGS' TARGET_ARCH='$TARGET_ARCH' CROSS='$CROSS' SELF_TEST='$SELF_TEST' X64_SKIA_DIR='$X64_SKIA_DIR' EMULATOR='$EMULATOR' bash -s" <<'GUEST'
set -euo pipefail

# --- x86_64 cross toolchain + qemu-user helpers (tartci#4) -------------------
# Resolve an x86_64 C/C++ cross compiler. Prefer the gnu cross packages
# (gcc/g++-x86-64-linux-gnu); fall back to clang --target. install-if-missing so
# a golden that didn't pre-bake them still works (best-effort; needs apt).
ensure_x64_toolchain(){
  if command -v x86_64-linux-gnu-gcc >/dev/null 2>&1 && command -v x86_64-linux-gnu-g++ >/dev/null 2>&1; then
    X64_CC="$(command -v x86_64-linux-gnu-gcc)"; X64_CXX="$(command -v x86_64-linux-gnu-g++)"; return 0
  fi
  echo "• installing x86_64 cross toolchain (gcc/g++-x86-64-linux-gnu)"
  sudo apt-get update -qq >/dev/null 2>&1 || true
  sudo apt-get install -y -qq gcc-x86-64-linux-gnu g++-x86-64-linux-gnu >/dev/null 2>&1 || true
  if command -v x86_64-linux-gnu-gcc >/dev/null 2>&1 && command -v x86_64-linux-gnu-g++ >/dev/null 2>&1; then
    X64_CC="$(command -v x86_64-linux-gnu-gcc)"; X64_CXX="$(command -v x86_64-linux-gnu-g++)"; return 0
  fi
  return 1
}
# Resolve a qemu-user x86_64 runner (binfmt makes ./x64binary transparent, but
# we also keep an explicit runner for the self-test + clarity).
ensure_qemu_user(){
  if command -v qemu-x86_64-static >/dev/null 2>&1; then QEMU_X64="$(command -v qemu-x86_64-static)"; return 0; fi
  if command -v qemu-x86_64 >/dev/null 2>&1; then QEMU_X64="$(command -v qemu-x86_64)"; return 0; fi
  echo "• installing qemu-user-static + binfmt-support"
  sudo apt-get update -qq >/dev/null 2>&1 || true
  sudo apt-get install -y -qq qemu-user-static binfmt-support >/dev/null 2>&1 || true
  command -v qemu-x86_64-static >/dev/null 2>&1 && { QEMU_X64="$(command -v qemu-x86_64-static)"; return 0; }
  command -v qemu-x86_64 >/dev/null 2>&1 && { QEMU_X64="$(command -v qemu-x86_64)"; return 0; }
  return 1
}

# --self-test: prove the cross toolchain + emulator end-to-end on a trivial
# program — golden-agnostic, no Pulp checkout / Skia / multiarch sysroot needed.
# This is exactly the issue's "How to test" acceptance check.
if [ "${SELF_TEST:-0}" = 1 ]; then
  echo "• host arch: $(uname -m)"
  ensure_x64_toolchain || { echo "✗ no x86_64 cross compiler (install gcc-x86-64-linux-gnu)"; exit 1; }
  ensure_qemu_user     || { echo "✗ no qemu-user x86_64 (install qemu-user-static)"; exit 1; }
  td="$(mktemp -d)"; trap 'rm -rf "$td"' EXIT
  cat > "$td/probe.c" <<'EOF'
#include <stdio.h>
int main(void){ printf("tartci-x64-selftest-ok\n"); return 0; }
EOF
  "$X64_CXX" -x c "$td/probe.c" -o "$td/probe.x64" -static 2>/dev/null \
    || "$X64_CC" "$td/probe.c" -o "$td/probe.x64" -static
  file "$td/probe.x64" 2>/dev/null | grep -qi 'x86-64' || { echo "✗ produced binary is not x86-64"; exit 1; }
  out="$("$QEMU_X64" "$td/probe.x64")" || { echo "✗ qemu-user failed to run the x64 binary"; exit 1; }
  echo "• qemu-user output: $out"
  [ "$out" = "tartci-x64-selftest-ok" ] || { echo "✗ unexpected output from emulated x64 binary"; exit 1; }
  echo "✓ x86_64 cross-compile + qemu-user execution verified"
  exit 0
fi
# ----------------------------------------------------------------------------

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
  # Build exactly what's on origin now, for ANY ref form. Fetch all remote refs,
  # then prefer the freshly-fetched origin/<REF> when it exists — so an unqualified
  # `--ref main` detaches at fresh origin/main, NOT the golden's stale local `main`.
  # Already-qualified (origin/feature/x), SHA, and tag refs have no origin/<REF>
  # and fall through to the ref as given. (The old `git fetch origin "$REF"` failed
  # for the origin/* form and built a stale cached ref.)
  git fetch --quiet --prune origin
  if git rev-parse --verify --quiet "origin/$REF^{commit}" >/dev/null 2>&1; then
    git checkout --quiet --detach "origin/$REF"
  else
    git checkout --quiet --detach "$REF"
  fi
fi
echo "• building pulp @ $(git rev-parse --short HEAD) ($(git rev-parse --abbrev-ref HEAD 2>/dev/null))"

GPU_FLAG=""; [ "$NO_GPU" = 1 ] && GPU_FLAG="-DPULP_ENABLE_GPU=OFF"
ccache --zero-stats >/dev/null 2>&1 || true

CROSS_FLAGS=()
if [ "${CROSS:-0}" = 1 ]; then
  # x86_64 cross: distinct compilers + SYSTEM_PROCESSOR so CMake knows it's a
  # cross. The build also needs x64 versions of the linked system libs (ALSA /
  # X11 / wayland …) — i.e. `dpkg --add-architecture amd64` + the `:amd64` dev
  # packages, or an x64 sysroot baked into the golden. If they're absent the
  # configure/link fails with a clear missing-lib error rather than silently
  # producing an arm64 artifact; GitHub-hosted x64 is the authoritative gate.
  ensure_x64_toolchain || { echo "✗ no x86_64 cross compiler"; exit 1; }
  ensure_qemu_user     || { echo "✗ no qemu-user x86_64 runtime"; exit 1; }
  CROSS_FLAGS=(-DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=x86_64
               -DCMAKE_C_COMPILER="$X64_CC" -DCMAKE_CXX_COMPILER="$X64_CXX")
  if [ "$NO_GPU" != 1 ] && [ -n "${X64_SKIA_DIR:-}" ]; then
    echo "• cross GPU-on: SKIA_DIR=$X64_SKIA_DIR (x64 Skia; avoids the arm64/x64 libskia.a collision)"
    CROSS_FLAGS+=(-DSKIA_DIR="$X64_SKIA_DIR")
  fi
  echo "• cross toolchain: CC=$X64_CC CXX=$X64_CXX  emulator=$QEMU_X64"
fi

cmake -S . -B build -G Ninja \
  -DCMAKE_BUILD_TYPE="$BUILD_TYPE" \
  -DPULP_BUILD_TESTS=ON -DPULP_BUILD_EXAMPLES=ON $GPU_FLAG \
  "${CROSS_FLAGS[@]}" \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
cmake --build build --parallel "$(nproc)"
echo "=== ccache stats ==="; ccache -s 2>/dev/null | grep -iE 'hits|misses|cache size' || true

if [ "${CROSS:-0}" = 1 ]; then
  # Run the test subset under qemu-user. binfmt routes x64 test binaries to
  # qemu-x86_64 transparently, so plain ctest works once binfmt is registered;
  # we point CMAKE/ctest at it via the crosscompiling emulator. Keep the subset
  # small + exclude sanitizer/SIMD/timing-sensitive labels (unreliable emulated).
  echo "• running ctest subset under qemu-user (emulated x64 — SMOKE, not a gate)"
  export QEMU_LD_PREFIX="${QEMU_LD_PREFIX:-/usr/x86_64-linux-gnu}"
  ctest --test-dir build $CTEST_ARGS --label-exclude 'sanitizer|simd|gpu|timing' || {
    rc=$?; echo "• emulated-x64 ctest exit=$rc (treat as smoke signal; GitHub-hosted x64 is the gate)"; exit $rc; }
else
  ctest --test-dir build $CTEST_ARGS
fi
GUEST
RC=$?
set -e
note "in-guest build+ctest exit=$RC"
exit $RC
