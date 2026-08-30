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
#     [--name pulp-linux-build] [--src-repo https://github.com/Generous-Corp/pulp] \
#     [--pulp-sha 40-hex] [--disk 80] [--memory 16384] [--cpu 8] \
#     [--skia-platform linux-arm64]
#     [--rosetta|--no-rosetta]
set -euo pipefail

export TART_HOME="${TART_HOME:-/Volumes/Workshop/VMs}"
VM_USER="${PULP_VM_USER:-admin}"
SSH_KEY="${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}"
BASE="ghcr.io/cirruslabs/ubuntu:24.04"   # pin to a @sha256 digest for a stable golden
NAME="pulp-linux-build"
SRC_REPO="https://github.com/Generous-Corp/pulp"
PULP_SHA="21fbc9da9214d4e6279fa2e8b4e70df9bed8662a"
DISK=80; MEMORY=16384; CPU=8; SKIA_PLATFORM="linux-arm64"; ENABLE_ROSETTA=1
RENDER_VERIFIER="$(cd "$(dirname "${BASH_SOURCE[0]}")/../common" && pwd)/pulp-render-generation.py"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
command -v tart >/dev/null 2>&1 || die "tart not installed"

while [ $# -gt 0 ]; do case "$1" in
  --base) BASE="$2"; shift 2;;
  --name) NAME="$2"; shift 2;;
  --src-repo) SRC_REPO="$2"; shift 2;;
  --pulp-sha) PULP_SHA="$2"; shift 2;;
  --disk) DISK="$2"; shift 2;;
  --memory) MEMORY="$2"; shift 2;;
  --cpu) CPU="$2"; shift 2;;
  --skia-platform|--skia-arch) SKIA_PLATFORM="$2"; shift 2;;
  --rosetta) ENABLE_ROSETTA=1; shift;;
  --no-rosetta) ENABLE_ROSETTA=0; shift;;
  -h|--help) sed -n '2,16p' "$0"; exit 0;;
  *) die "unknown arg: $1";;
esac; done

[[ "$PULP_SHA" =~ ^[0-9a-f]{40}$ ]] \
  || die "--pulp-sha must be an immutable lowercase 40-hex commit"
[ -f "$RENDER_VERIFIER" ] || die "render generation verifier missing: $RENDER_VERIFIER"

case "$BASE" in *@sha256:*) ;; *) note "WARNING: --base is not digest-pinned; :tag drifts glibc/sysroot under the golden (runbook §3.1)";; esac

note "§3.1 pull base + clone → $NAME"
tart pull "$BASE"
tart clone "$BASE" "$NAME"

note "§3.2 bump resources (VM must be stopped; cloud-init grows the FS on boot)"
tart set "$NAME" --disk-size "$DISK" --memory "$MEMORY" --cpu "$CPU"

note "§3.2 boot once so cloud-init grows the root FS"
if [ "$ENABLE_ROSETTA" = 1 ]; then
  note "§3.4 host Rosetta for Linux runtime (idempotent)"
  softwareupdate --install-rosetta --agree-to-license >/dev/null 2>&1 \
    || die "host Rosetta install/check failed; run: softwareupdate --install-rosetta --agree-to-license"
fi
TART_RUN_ARGS=(--no-graphics)
[ "$ENABLE_ROSETTA" = 1 ] && TART_RUN_ARGS+=(--rosetta=rosetta)
RPID=""; tart run "${TART_RUN_ARGS[@]}" "$NAME" >/dev/null 2>&1 & RPID=$!
trap '[ -n "$RPID" ] && kill "$RPID" 2>/dev/null || true' EXIT
IP=""; for _ in $(seq 1 60); do IP="$(tart ip "$NAME" 2>/dev/null || true)"; [ -n "$IP" ] && break; sleep 2; done
[ -n "$IP" ] || die "no IP for $NAME"
GUEST_TRANSPORT=""
for _ in $(seq 1 90); do
  if ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" "$VM_USER@$IP" true 2>/dev/null; then GUEST_TRANSPORT="ssh"; break; fi
  if tart exec "$NAME" true >/dev/null 2>&1; then GUEST_TRANSPORT="tart-exec"; break; fi
  sleep 2
done
[ -n "$GUEST_TRANSPORT" ] || die "guest did not become reachable for $NAME at $IP via SSH or Tart guest agent"
note "vm up at $IP via $GUEST_TRANSPORT — provisioning deps + Skia in-guest"

run_guest_script(){
  if [ "$GUEST_TRANSPORT" = "ssh" ]; then
    ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" "$VM_USER@$IP" \
      "SRC_REPO='$SRC_REPO' PULP_SHA='$PULP_SHA' SKIA_PLATFORM='$SKIA_PLATFORM' ENABLE_ROSETTA='$ENABLE_ROSETTA' PARENT_IDENTITY='$BASE' bash -s"
  else
    tart exec -i "$NAME" env \
      "SRC_REPO=$SRC_REPO" "PULP_SHA=$PULP_SHA" "SKIA_PLATFORM=$SKIA_PLATFORM" \
      "ENABLE_ROSETTA=$ENABLE_ROSETTA" "PARENT_IDENTITY=$BASE" \
      bash -s
  fi
}

install_render_verifier(){
  if [ "$GUEST_TRANSPORT" = "ssh" ]; then
    ssh "${SSH_OPTS[@]}" -i "$SSH_KEY" "$VM_USER@$IP" \
      'mkdir -p "$HOME/.local/lib/tartci" && cat >"$HOME/.local/lib/tartci/pulp-render-generation.py"' \
      <"$RENDER_VERIFIER"
  else
    tart exec -i "$NAME" bash -c \
      'mkdir -p "$HOME/.local/lib/tartci" && cat >"$HOME/.local/lib/tartci/pulp-render-generation.py"' \
      <"$RENDER_VERIFIER"
  fi
}

install_render_verifier

run_guest_script <<'GUEST'
set -euo pipefail

pin_existing_apt_sources_to_arm64(){
  local file tmp
  for file in /etc/apt/sources.list.d/*.sources; do
    [ -e "$file" ] || continue
    tmp="$(mktemp)"
    awk '
      function flush(  i) {
        if (n == 0) return;
        if (has_types && !has_arch) {
          inserted = 0;
          for (i = 1; i <= n; i++) {
            print lines[i];
            if (!inserted && lines[i] ~ /^URIs:/) {
              print "Architectures: arm64";
              inserted = 1;
            }
          }
          if (!inserted) print "Architectures: arm64";
        } else {
          for (i = 1; i <= n; i++) print lines[i];
        }
        n = 0; has_types = 0; has_arch = 0;
      }
      BEGIN { n = 0; has_types = 0; has_arch = 0 }
      NF == 0 {
        flush(); print ""; next
      }
      {
        lines[++n] = $0;
        if ($0 ~ /^Types:/) has_types = 1;
        if ($0 ~ /^Architectures:/) has_arch = 1;
      }
      END { flush() }
    ' "$file" >"$tmp"
    sudo install -m 0644 "$tmp" "$file"
    rm -f "$tmp"
  done
}

install_amd64_userland(){
  local codename
  codename="$(
    . /etc/os-release
    printf '%s' "${VERSION_CODENAME:-noble}"
  )"
  sudo dpkg --add-architecture amd64
  pin_existing_apt_sources_to_arm64
  sudo tee /etc/apt/sources.list.d/tartci-amd64.sources >/dev/null <<EOF
Types: deb
URIs: http://archive.ubuntu.com/ubuntu
Suites: ${codename} ${codename}-updates ${codename}-backports
Components: main restricted universe multiverse
Architectures: amd64
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu
Suites: ${codename}-security
Components: main restricted universe multiverse
Architectures: amd64
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
  sudo apt-get update
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
    libc6:amd64 libstdc++6:amd64 libgcc-s1:amd64 zlib1g:amd64 \
    libtinfo6:amd64 libxml2:amd64
}

install_rosetta_units(){
  sudo mkdir -p /mnt/rosetta /usr/local/sbin
  sudo tee /usr/local/sbin/tartci-register-rosetta-binfmt >/dev/null <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc
if [ ! -x /mnt/rosetta/rosetta ]; then
  echo "Rosetta runtime not mounted at /mnt/rosetta/rosetta" >&2
  exit 1
fi
if [ -e /proc/sys/fs/binfmt_misc/rosetta ]; then
  echo -1 > /proc/sys/fs/binfmt_misc/rosetta
fi
# binfmt_misc decodes \xHH escapes itself. Do not use printf %b here; decoded
# NUL bytes truncate the match and make Rosetta catch arm64 binaries too.
printf '%s' ':rosetta:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x3e\x00:\xff\xff\xff\xff\xff\xfe\xfe\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/mnt/rosetta/rosetta:F' > /proc/sys/fs/binfmt_misc/register
SCRIPT
  sudo chmod 0755 /usr/local/sbin/tartci-register-rosetta-binfmt
  sudo tee /etc/systemd/system/mnt-rosetta.mount >/dev/null <<'UNIT'
[Unit]
Description=Apple Rosetta for Linux virtiofs mount
After=local-fs-pre.target
Before=tartci-rosetta-binfmt.service

[Mount]
What=rosetta
Where=/mnt/rosetta
Type=virtiofs
Options=rw,nofail

[Install]
WantedBy=multi-user.target
UNIT
  sudo tee /etc/systemd/system/tartci-rosetta-binfmt.service >/dev/null <<'UNIT'
[Unit]
Description=Register Apple Rosetta for Linux x86_64 binfmt
Requires=mnt-rosetta.mount
After=mnt-rosetta.mount proc-sys-fs-binfmt_misc.mount systemd-binfmt.service

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/tartci-register-rosetta-binfmt
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable mnt-rosetta.mount tartci-rosetta-binfmt.service >/dev/null
  sudo mount -t virtiofs rosetta /mnt/rosetta 2>/dev/null || true
  sudo /usr/local/sbin/tartci-register-rosetta-binfmt
}

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
  gcc-x86-64-linux-gnu g++-x86-64-linux-gnu \
  qemu-user-static binfmt-support

if [ "${ENABLE_ROSETTA:-1}" = 1 ]; then
  echo "• configuring Rosetta-for-Linux x86_64 runtime + amd64 userland"
  install_amd64_userland
  install_rosetta_units
  /lib64/ld-linux-x86-64.so.2 --version >/dev/null
  echo "• Rosetta x86_64 dynamic runtime verified"
fi

# Clone the project, then detach at the immutable render-toolchain source. A
# branch tip is never an acceptable golden input.
if [ ! -d "$HOME/pulp/.git" ]; then
  git clone "$SRC_REPO" "$HOME/pulp"
fi
git -C "$HOME/pulp" remote set-url origin "$SRC_REPO"
git -C "$HOME/pulp" fetch --no-tags origin "$PULP_SHA"
git -C "$HOME/pulp" cat-file -e "$PULP_SHA^{commit}"
git -C "$HOME/pulp" checkout --detach "$PULP_SHA"
git -C "$HOME/pulp" reset --hard "$PULP_SHA"
git -C "$HOME/pulp" clean -ffd
[ "$(git -C "$HOME/pulp" rev-parse HEAD)" = "$PULP_SHA" ] || {
  echo "Pulp checkout did not land on immutable SHA $PULP_SHA" >&2
  exit 1
}

# §3.6 — bake prebuilt Skia into the in-checkout external/skia-build so each CoW
# clone gets it free. FindSkia auto-discovers it (no SKIA_DIR needed).
cd "$HOME/pulp"
python3 tools/scripts/fetch_skia_for_release.py "$SKIA_PLATFORM"
ls external/skia-build/build/*-gpu/lib/Release/libskia.a >/dev/null 2>&1 \
  && echo "• Skia baked OK ($SKIA_PLATFORM)" || { echo "✗ Skia bake missing libskia.a"; exit 1; }

# Prove the receipt-bound provider exports executable SkLogHandler and Graphite
# executor support, then retain the matched V8 provider bytes without changing
# Pulp's QuickJS-default runtime policy.
mkdir -p "$HOME/.config/tartci"
python3 tools/scripts/verify_skia_m153_capabilities.py \
  --platform "$SKIA_PLATFORM" \
  --skia-dir external/skia-build \
  --result "$HOME/.config/tartci/pulp-skia-capabilities.json"
python3 tools/scripts/fetch_v8_for_release.py "$SKIA_PLATFORM"

parent_digest="$(printf '%s' "$PARENT_IDENTITY" | sha256sum | awk '{print $1}')"
python3 "$HOME/.local/lib/tartci/pulp-render-generation.py" \
  --repo "$HOME/pulp" \
  --pulp-sha "$PULP_SHA" \
  --pulp-repository "$SRC_REPO" \
  --platform "$SKIA_PLATFORM" \
  --capability-result "$HOME/.config/tartci/pulp-skia-capabilities.json" \
  --v8-disposition baked-provider-only \
  --parent-kind tart-base-reference \
  --parent-identity "$PARENT_IDENTITY" \
  --parent-digest "$parent_digest" \
  --output "$HOME/.config/tartci/pulp-render-generation.json"
test -s "$HOME/.config/tartci/pulp-render-generation.json"
GUEST

note "§stop $NAME — golden ready. Tag it: tart stop $NAME (then keep as :latest, or"
note "  rename to a dated tag). run.sh clones it per job."
tart stop "$NAME" >/dev/null 2>&1 || true
trap - EXIT; [ -n "$RPID" ] && kill "$RPID" 2>/dev/null || true
note "done. Smoke it: providers/tart-linux/run.sh --golden $NAME"
