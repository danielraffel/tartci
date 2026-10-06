#!/usr/bin/env bash
# tart-linux/provision-lint.sh — bake the `pulp-lint-linux` golden: a minimal
# arm64 Ubuntu 24.04 for jobs that compile nothing. Everything comes from
# manifests/pulp.lint-linux.toml (pinned base digest, runner sha256, Python
# tool-cache tarball, pip packages), so a re-bake reproduces the same image.
#
# The guest is reached through `tart exec` (the base image's guest agent), never
# with the image's default password, and the bake ends by locking that password
# and authorizing only the tartci-owned lint key. IPv6 is disabled: Softnet
# filters IPv4 only, so an IPv6 path would bypass the egress allowlist.
#
# Usage:
#   providers/tart-linux/provision-lint.sh [--name pulp-lint-linux:<date>] [--keep-builder]
#
# Prints a JSON receipt (name, base digest, disk sha256, pins) on success and
# writes it beside the VM store as $TART_HOME/goldens/<name>.json.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$ROOT/manifests/pulp.lint-linux.toml"
export TART_HOME="${TART_HOME:-$HOME/.tart}"
NAME="pulp-lint-linux:$(date -u +%Y-%m-%d)"
KEEP_BUILDER=0

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do case "$1" in
  --name) NAME="$2"; shift 2;;
  --keep-builder) KEEP_BUILDER=1; shift;;
  -h|--help) sed -n '2,17p' "$0"; exit 0;;
  *) die "unknown arg: $1";;
esac; done

command -v tart >/dev/null 2>&1 || die "tart not installed"
[ -f "$MANIFEST" ] || die "missing $MANIFEST"
# shellcheck source=providers/common/toml-python.lib.sh
. "$ROOT/providers/common/toml-python.lib.sh"

# Read the pins once; a missing pin is a refusal, not a default.
pins="$(tartci_toml_python - "$MANIFEST" <<'PY'
import sys, tomllib, shlex
m = tomllib.load(open(sys.argv[1], "rb"))
need = {
  "BASE": m["base"], "DISK_GB": m["disk_gb"],
  "RUNNER_VERSION": m["runner"]["version"], "RUNNER_URL": m["runner"]["url"],
  "RUNNER_SHA256": m["runner"]["sha256"],
  "PY_VERSION": m["python"]["version"], "PY_URL": m["python"]["url"],
  "PY_PACKAGES": " ".join(m["python"]["packages"]), "TOOL_CACHE": m["python"]["tool_cache"],
  "APT_PACKAGES": " ".join(m["apt"]["packages"]), "GUEST_USER": m["guest"]["user"],
  "SSH_KEY": m["guest"]["ssh_key"],
}
if "@sha256:" not in need["BASE"]:
    raise SystemExit("base must be pinned by digest")
for k, v in need.items():
    print(f"{k}={shlex.quote(str(v))}")
PY
)" || die "cannot read pins from $MANIFEST"
eval "$pins"
SSH_KEY="${SSH_KEY/#\~/$HOME}"

if [ ! -f "$SSH_KEY" ]; then
  note "creating the tartci-owned lint VM key $SSH_KEY"
  mkdir -p "$(dirname "$SSH_KEY")"; chmod 700 "$(dirname "$SSH_KEY")"
  ssh-keygen -q -t ed25519 -N '' -C "tartci-lint-vm@$(hostname -s)" -f "$SSH_KEY"
fi
PUBKEY="$(cat "$SSH_KEY.pub")"

BUILDER="pulp-lint-builder-$$"
cleanup(){ tart stop "$BUILDER" >/dev/null 2>&1 || true
  [ "$KEEP_BUILDER" = 1 ] || tart delete "$BUILDER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

note "clone $BASE → $BUILDER"
tart clone "$BASE" "$BUILDER"
tart set "$BUILDER" --cpu 2 --memory 4096 --disk-size "$DISK_GB"
tart run --no-graphics "$BUILDER" >/dev/null 2>&1 &
for _ in $(seq 1 150); do tart exec "$BUILDER" true >/dev/null 2>&1 && break; sleep 1; done
tart exec "$BUILDER" true >/dev/null 2>&1 || die "builder never answered tart exec"

note "provision guest (runner $RUNNER_VERSION, Python $PY_VERSION, apt: $APT_PACKAGES)"
tart exec -i "$BUILDER" sudo env \
  RUNNER_URL="$RUNNER_URL" RUNNER_SHA256="$RUNNER_SHA256" \
  PY_VERSION="$PY_VERSION" PY_URL="$PY_URL" PY_PACKAGES="$PY_PACKAGES" \
  TOOL_CACHE="$TOOL_CACHE" APT_PACKAGES="$APT_PACKAGES" GUEST_USER="$GUEST_USER" \
  PUBKEY="$PUBKEY" bash -s <<'GUEST'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
home="/home/$GUEST_USER"

# No background apt in a clone: it holds the dpkg lock while a job runs apt,
# and it would make every job start from a different package set.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer motd-news.timer 2>/dev/null || true
systemctl mask apt-daily.service apt-daily-upgrade.service 2>/dev/null || true
apt-get update -qq
apt-get purge -y -qq unattended-upgrades >/dev/null 2>&1 || true
apt-get install -y -qq --no-install-recommends $APT_PACKAGES >/dev/null

# GitHub Actions runner, verified against the pinned digest.
curl -fsSL --retry 5 -o /tmp/runner.tgz "$RUNNER_URL"
echo "$RUNNER_SHA256  /tmp/runner.tgz" | sha256sum -c - >/dev/null
install -d -o "$GUEST_USER" -g "$GUEST_USER" "$home/actions-runner"
tar -xzf /tmp/runner.tgz -C "$home/actions-runner"
chown -R "$GUEST_USER:$GUEST_USER" "$home/actions-runner"
"$home/actions-runner/bin/installdependencies.sh" >/dev/null
rm -f /tmp/runner.tgz

# Python in the runner's tool cache, laid out the way actions/setup-python reads it.
install -d -o "$GUEST_USER" -g "$GUEST_USER" "$TOOL_CACHE"
work="$(mktemp -d)"
curl -fsSL --retry 5 -o "$work/python.tgz" "$PY_URL"
tar -xzf "$work/python.tgz" -C "$work"
(cd "$work" && sudo -u "$GUEST_USER" env RUNNER_TOOL_CACHE="$TOOL_CACHE" AGENT_TOOLSDIRECTORY="$TOOL_CACHE" bash ./setup.sh >/dev/null)
py="$TOOL_CACHE/Python/$PY_VERSION/arm64/bin/python3"
[ -x "$py" ] && [ -f "$TOOL_CACHE/Python/$PY_VERSION/arm64.complete" ]
sudo -u "$GUEST_USER" "$py" -m pip install --quiet --disable-pip-version-check $PY_PACKAGES
rm -rf "$work"
printf 'RUNNER_TOOL_CACHE=%s\nAGENT_TOOLSDIRECTORY=%s\n' "$TOOL_CACHE" "$TOOL_CACHE" \
  > "$home/actions-runner/.env"
chown "$GUEST_USER:$GUEST_USER" "$home/actions-runner/.env"

# IPv4 only: Softnet does not filter IPv6. The kernel command line removes the
# IPv6 stack outright. The sysctl alone raced a router advertisement on the
# vmnet: the interface took a global address after a boot probe had passed.
# The sysctl stays as a second layer.
printf 'GRUB_CMDLINE_LINUX="$GRUB_CMDLINE_LINUX ipv6.disable=1"\n' > /etc/default/grub.d/99-tartci-no-ipv6.cfg
update-grub >/dev/null 2>&1
printf 'net.ipv6.conf.all.disable_ipv6 = 1\nnet.ipv6.conf.default.disable_ipv6 = 1\nnet.ipv6.conf.lo.disable_ipv6 = 1\n' \
  > /etc/sysctl.d/99-tartci-no-ipv6.conf

# Access: the tartci lint key only; no password logins; the default password locked.
install -d -m 700 -o "$GUEST_USER" -g "$GUEST_USER" "$home/.ssh"
printf '%s\n' "$PUBKEY" > "$home/.ssh/authorized_keys"
chown "$GUEST_USER:$GUEST_USER" "$home/.ssh/authorized_keys"; chmod 600 "$home/.ssh/authorized_keys"
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' > /etc/ssh/sshd_config.d/10-tartci.conf
passwd -l "$GUEST_USER" >/dev/null

apt-get clean; rm -rf /var/lib/apt/lists/*
sync
GUEST

# Reboot once to prove the IPv6 stack is gone, then finish the image. The bake
# stamp is the very last write: the lane's token scan covers only files newer
# than it, because the bytes before it are fixed by the golden's disk digest.
note "reboot to verify the kernel command line"
tart stop "$BUILDER" >/dev/null
tart run --no-graphics "$BUILDER" >/dev/null 2>&1 &
for _ in $(seq 1 150); do tart exec "$BUILDER" true >/dev/null 2>&1 && break; sleep 1; done
tart exec "$BUILDER" sh -c 'grep -qw ipv6.disable=1 /proc/cmdline && [ ! -e /proc/sys/net/ipv6 ]' \
  || die "the IPv6 stack is still present after reboot; refusing to template"
tart exec -i "$BUILDER" sudo env GUEST_USER="$GUEST_USER" bash -s <<'FINAL'
set -euo pipefail
truncate -s 0 /etc/machine-id
rm -f "/home/$GUEST_USER/.bash_history" /root/.bash_history
install -d -m 755 /etc/tartci
date -u +%Y-%m-%dT%H:%M:%SZ > /etc/tartci/bake-stamp
sync
FINAL
stamp_mtime="$(tart exec "$BUILDER" stat -c %Y /etc/tartci/bake-stamp)"

note "shut down and template"
tart stop "$BUILDER" >/dev/null
tart clone "$BUILDER" "$NAME"
disk="$TART_HOME/vms/$NAME/disk.img"
[ -f "$disk" ] || die "no disk image at $disk"
digest="$(shasum -a 256 "$disk" | awk '{print $1}')"
mkdir -p "$TART_HOME/goldens"
receipt="$TART_HOME/goldens/${NAME//[:\/]/_}.json"
python3 - "$receipt" <<PY
import json, sys
json.dump({
  "name": "$NAME", "manifest": "manifests/pulp.lint-linux.toml",
  "base": "$BASE", "disk_sha256": "$digest",
  "runner_version": "$RUNNER_VERSION", "runner_sha256": "$RUNNER_SHA256",
  "python": "$PY_VERSION", "python_packages": "$PY_PACKAGES".split(),
  "ssh_key_fingerprint": "$(ssh-keygen -lf "$SSH_KEY.pub" | awk '{print $2}')",
  "bake_stamp": "/etc/tartci/bake-stamp", "bake_stamp_mtime": int("$stamp_mtime"),
  "ipv6": "absent (kernel cmdline ipv6.disable=1)",
}, open(sys.argv[1], "w"), indent=1)
PY
cat "$receipt"
