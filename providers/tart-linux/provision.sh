#!/usr/bin/env bash
# tart-linux/provision.sh — bake the `pulp-linux-build` golden from a pinned
# Ubuntu 24.04 arm64 base: bump resources, install the full build dependency set,
# clone the project, and bake prebuilt Skia (linux-arm64) into the in-checkout
# external/skia-build so every CoW clone gets it for free. Codifies runbook §3.
#
# `run.sh` (the ephemeral lane) is host-validated end-to-end; this bake script
# codifies the proven runbook §3 recipe. Re-bake on a fresh host or to refresh
# the golden after a base / dependency / Skia pin bump.
#
# Usage:
#   providers/tart-linux/provision.sh \
#     [--base ghcr.io/cirruslabs/ubuntu:24.04@sha256:<pin>] \
#     [--name pulp-linux-build] [--src-repo https://github.com/danielraffel/pulp] \
#     [--disk 80] [--memory 16384] [--cpu 8] [--skia-arch linux-arm64]
set -euo pipefail

export TART_HOME="${TART_HOME:-/Volumes/Workshop/VMs}"
VM_USER="${PULP_VM_USER:-admin}"
SSH_KEY="${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}"
BASE="ghcr.io/cirruslabs/ubuntu:24.04"   # pin to a @sha256 digest for a stable golden
NAME="pulp-linux-build"
SRC_REPO="https://github.com/danielraffel/pulp"
DISK=80; MEMORY=16384; CPU=8; SKIA_ARCH="linux-arm64"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
command -v tart >/dev/null 2>&1 || die "tart not installed"

while [ $# -gt 0 ]; do case "$1" in
  --base) BASE="$2"; shift 2;;
  --name) NAME="$2"; shift 2;;
  --src-repo) SRC_REPO="$2"; shift 2;;
  --disk) DISK="$2"; shift 2;;
  --memory) MEMORY="$2"; shift 2;;
  --cpu) CPU="$2"; shift 2;;
  --skia-arch) SKIA_ARCH="$2"; shift 2;;
  -h|--help) sed -n '2,18p' "$0"; exit 0;;
  *) die "unknown arg: $1";;
esac; done

case "$BASE" in *@sha256:*) ;; *) note "WARNING: --base is not digest-pinned; :tag drifts glibc/sysroot under the golden (runbook §3.1)";; esac

note "§3.1 pull base + clone → $NAME"
tart pull "$BASE"
tart clone "$BASE" "$NAME"

note "§3.2 bump resources (VM must be stopped; cloud-init grows the FS on boot)"
tart set "$NAME" --disk-size "$DISK" --memory "$MEMORY" --cpu "$CPU"

note "§3.2 boot once so cloud-init grows the root FS"
RPID=""; tart run --no-graphics "$NAME" >/dev/null 2>&1 & RPID=$!
trap '[ -n "$RPID" ] && kill "$RPID" 2>/dev/null || true' EXIT
IP=""; for _ in $(seq 1 60); do IP="$(tart ip "$NAME" 2>/dev/null || true)"; [ -n "$IP" ] && break; sleep 2; done
[ -n "$IP" ] || die "no IP for $NAME"
for _ in $(seq 1 90); do ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" "$VM_USER@$IP" true 2>/dev/null && break; sleep 2; done
note "vm up at $IP — provisioning deps + Skia in-guest"

ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" "$VM_USER@$IP" \
  "SRC_REPO='$SRC_REPO' SKIA_ARCH='$SKIA_ARCH' bash -s" <<'GUEST'
set -euo pipefail

# §3.5 — the canonical Pulp Linux dependency set (mirror build.yml's "Install
# Linux dependencies"), plus CI extras. libicu-dev is required (Pulp calls ICU
# directly when Skia+ICU headers are present; libskia exports SkUnicode, not
# ICU's own symbols). libjack is deliberately omitted (only for a JACK lane).
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  libasound2-dev libdbus-1-dev libdrm-dev libegl1-mesa-dev \
  libfontconfig1-dev libgbm-dev libgl1-mesa-dev libx11-dev libxext-dev \
  libxfixes-dev libxi-dev libxinerama-dev libxkbcommon-dev libxrandr-dev \
  libxrender-dev libxss-dev libxtst-dev libwayland-dev wayland-protocols \
  libicu-dev \
  cmake ninja-build clang lld ccache git git-lfs python3 \
  qemu-user-static binfmt-support

# Clone the project (the golden carries a ~/pulp checkout that run.sh updates to
# the ref under test).
if [ ! -d "$HOME/pulp/.git" ]; then
  git clone "$SRC_REPO" "$HOME/pulp"
fi

# §3.6 — bake prebuilt Skia into the in-checkout external/skia-build so each CoW
# clone gets it free. FindSkia auto-discovers it (no SKIA_DIR needed).
cd "$HOME/pulp"
python3 tools/deps/fetch_skia_for_release.py --arch "$SKIA_ARCH"
ls external/skia-build/build/*-gpu/lib/Release/libskia.a >/dev/null 2>&1 \
  && echo "• Skia baked OK ($SKIA_ARCH)" || { echo "✗ Skia bake missing libskia.a"; exit 1; }
GUEST

note "§stop $NAME — golden ready. Tag it: tart stop $NAME (then keep as :latest, or"
note "  rename to a dated tag). run.sh clones it per job."
tart stop "$NAME" >/dev/null 2>&1 || true
trap - EXIT; [ -n "$RPID" ] && kill "$RPID" 2>/dev/null || true
note "done. Smoke it: providers/tart-linux/run.sh --golden $NAME"
