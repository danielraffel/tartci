# shellcheck shell=bash
# The "lint" lane of providers/tart-linux/runner.sh: one small VM per job for
# jobs that compile nothing (the merge-queue preamble, "Enforce version & skill
# sync", "Vellum freeze"). Those jobs run pull-request or merge-queue content,
# so the VM is untrusted by construction. Compared with the build lane:
#
#   * egress is default-deny with GitHub's published self-hosted-runner egress
#     set allowed (Softnet, enforced on the host, IPv4 only, guest IPv6 off);
#   * nothing on the host is shared into the guest (no ccache mount);
#   * the guest is reached with a tartci-owned key, never a personal one;
#   * the VM is sized from the host profile's lint_vm_* keys, never a typed number;
#   * every job writes a receipt proving those properties held for it.
#
# Selected with TARTCI_LINUX_LANE=lint. Sourced by runner.sh after its defaults.

TARTCI_LINT_DEFAULT_LABELS="self-hosted,Linux,ARM64,pulp-lint-linux-arm64"
TARTCI_LINT_NAME_PREFIX_DEFAULT="pulp-lint-ephemeral-"

tartci_lint_lane_enabled(){ [ "${TARTCI_LINUX_LANE:-build}" = lint ]; }

# Apply lane defaults. An explicit TARTCI_* value still wins, except the build
# labels, which a lint VM must never advertise.
# Under launchd the lane must come from the reviewed renderer (fleet-macos
# render), never from the hand-sed template, whose values drift per host. A
# launchd job is recognised by XPC_SERVICE_NAME (launchd sets it to the job's
# Label; ssh and Terminal sessions leave it unset or "0"). The renderer writes a
# render receipt and points TARTCI_LANE_RENDER_RECEIPT at it; until that renderer
# learns the tart-linux kind, no launchd job can start this lane.
TARTCI_LINUX_HAND_TEMPLATE_LABEL="com.danielraffel.pulp.tart-runner-linux"
tartci_lint_launch_source_ok(){
  local job="${XPC_SERVICE_NAME:-}" receipt="${TARTCI_LANE_RENDER_RECEIPT:-}"
  case "$job" in ''|0) return 0 ;; esac
  if [ "$job" = "$TARTCI_LINUX_HAND_TEMPLATE_LABEL" ]; then
    die "lint lane refuses the hand-rendered tart-runner-linux template ($job); render it with fleet-macos render"
  fi
  [ -n "$receipt" ] && [ -r "$receipt" ] \
    && python3 - "$receipt" <<'PY' || die "lint lane under launchd ($job) needs a fleet-macos render receipt for a tart-linux lint lane"
import json, sys
r = json.load(open(sys.argv[1]))
ok = r.get("renderer") == "fleet-macos" and r.get("lane_kind") == "tart-linux" and r.get("lane_profile") == "lint"
raise SystemExit(0 if ok else 1)
PY
}

tartci_lint_lane_configure(){
  tartci_lint_launch_source_ok
  LABELS="${TARTCI_RUNNER_LABELS:-$TARTCI_LINT_DEFAULT_LABELS}"
  case ",$LABELS," in
    *,pulp-build-linux,*|*,pulp-trusted-build,*|*,pulp-build,*)
      die "lint lane refuses build labels: $LABELS" ;;
  esac
  GOLDEN="${TARTCI_LINUX_GOLDEN:-}"
  [ -n "$GOLDEN" ] || die "lint lane needs TARTCI_LINUX_GOLDEN (a baked pulp-lint-linux:<date>)"
  SSH_KEY_PRIV="${TARTCI_VM_SSH_KEY:-$HOME/.config/tartci/keys/lint-vm_ed25519}"
  case "$SSH_KEY_PRIV" in
    "$HOME/.ssh/"*) die "lint lane refuses a personal SSH key ($SSH_KEY_PRIV); use the tartci-owned lint key" ;;
  esac
  [ -r "$SSH_KEY_PRIV" ] || die "lint lane key $SSH_KEY_PRIV is missing (provision-lint.sh creates it)"
  TARTCI_LINT_NAME_PREFIX="${TARTCI_RUNNER_NAME_PREFIX:-$TARTCI_LINT_NAME_PREFIX_DEFAULT}"
  case "$TARTCI_LINT_NAME_PREFIX" in
    *-|*_) ;;
    *) die "runner name prefix must end in - or _ so a lease can namespace it: $TARTCI_LINT_NAME_PREFIX" ;;
  esac
  # shellcheck disable=SC2034  # read by runner.sh, which sources this file
  LOGROOT="${TARTCI_LINUX_LOGS:-$HOME/VMs/logs/tartci-linux-lint}"
}

tartci_lint_vm_name(){ # $1 = iteration
  local host; host="$(hostname -s | tr -c 'A-Za-z0-9\n' '-' | tr '[:upper:]' '[:lower:]')"
  printf '%s%s-%s-%s' "$TARTCI_LINT_NAME_PREFIX" "$host" "$$" "$1"
}

# Softnet needs root to create the vmnet interface, then drops privileges. A
# missing grant must stop the lane: booting on tart's default NAT would give an
# untrusted guest the LAN and the tailnet.
tartci_lint_softnet_ready(){
  local bin real owner mode
  bin="$(command -v softnet 2>/dev/null)" || return 1
  real="$(python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$bin")"
  owner="$(stat -f %Su "$real" 2>/dev/null || stat -c %U "$real")"
  mode="$(stat -f %Lp "$real" 2>/dev/null || stat -c %a "$real")"
  if [ "$owner" = root ] && [ $((8#$mode & 8#4000)) -ne 0 ]; then
    return 0
  fi
  sudo -n -l "$bin" >/dev/null 2>&1
}

# Echo the tart run network arguments, refreshing the allowlist if stale. The
# summary JSON (name, fetched_at, cidr_count, sha256) goes to $1 for the receipt.
tartci_lint_softnet_args(){
  local summary_out="$1" rules
  python3 "$TARTCI_ROOT/scripts/egress_allowlist.py" fetch >"$summary_out" || return 1
  rules="$(python3 "$TARTCI_ROOT/scripts/egress_allowlist.py" rules)" || return 1
  [ -n "$rules" ] || return 1
  printf '%s\n' "--net-softnet-block=0.0.0.0/0" "--net-softnet-allow=$rules"
}

# Prove, from inside the guest, the properties the job relies on, before any
# runner is registered. Output is key=value lines for the receipt.
TARTCI_LINT_GUEST_PROBE='
set -u
printf "boot_id=%s\n" "$(cat /proc/sys/kernel/random/boot_id)"
# No IPv6 stack at all (kernel cmdline ipv6.disable=1). A sysctl-only disable
# raced a router advertisement: an address appeared after the probe passed.
printf "ipv6_stack=%s\n" "$([ -e /proc/sys/net/ipv6 ] && echo present || echo absent)"
printf "ipv6_global_addrs=%s\n" "$(ip -6 addr show scope global 2>/dev/null | grep -c inet6)"
printf "host_shares=%s\n" "$(grep -cE " (virtiofs|9p|fuse\.vmhgfs) " /proc/mounts)"
# A share the job could mount itself: any virtio-fs device (virtio id 0x001a).
printf "host_share_devices=%s\n" "$(cat /sys/bus/virtio/devices/*/device 2>/dev/null | grep -cx 0x001a)"
# Redaction proof. Bytes older than the bake stamp belong to the golden, fixed by
# its disk digest (and they include npm docs with example private keys), so the
# token scan covers what boot and the supervisor added: files under $HOME and
# /etc newer than /etc/tartci/bake-stamp, with every pattern kept. A missing
# stamp, or one newer than this boot, is a FAIL: that is not a lint golden.
stamp=/etc/tartci/bake-stamp
btime=$(awk "/^btime/ {print \$2}" /proc/stat)
if [ -f "$stamp" ] && [ "$(stat -c %Y "$stamp")" -lt "$btime" ]; then
  printf "bake_stamp=ok\n"
else
  printf "bake_stamp=bad\n"
fi
hits=0
for f in "$HOME/.ssh/id_"* "$HOME/.config/gh/hosts.yml" "$HOME/.git-credentials" \
         "$HOME/.netrc" "$HOME/actions-runner/.credentials" "$HOME/actions-runner/.runner" \
         "$HOME/jit.cfg"; do [ -e "$f" ] && hits=$((hits + 1)); done
tok_paths=$(find "$HOME" /etc -type f -newer "$stamp" -readable -print0 2>/dev/null \
  | xargs -0 -r grep -IlE "gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----" 2>/dev/null)
tok=$(printf "%s" "$tok_paths" | grep -c .)
printf "credential_files=%s\n" "$hits"
printf "token_strings=%s\n" "$tok"
# Paths only, never contents, so a FAIL names what tripped it.
printf "token_paths=%s\n" "$(printf "%s" "$tok_paths" | head -20 | paste -sd, -)"
'

tartci_lint_guest_probe(){ # $1 = ip ; prints key=value lines
  ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$1" "bash -c '$TARTCI_LINT_GUEST_PROBE'"
}

# Refuse to register a runner on a guest whose probe does not show the lane's
# properties.
tartci_lint_probe_ok(){ # $1 = probe output
  local p="$1"
  grep -qx 'ipv6_stack=absent' <<<"$p" \
    && grep -qx 'bake_stamp=ok' <<<"$p" \
    && grep -qx 'ipv6_global_addrs=0' <<<"$p" \
    && grep -qx 'host_shares=0' <<<"$p" \
    && grep -qx 'host_share_devices=0' <<<"$p" \
    && grep -qx 'credential_files=0' <<<"$p" \
    && grep -qx 'token_strings=0' <<<"$p"
}

tartci_lint_write_receipt(){ # $1 out, $2 vm, $3 probe, $4 egress summary file, $5 cores, $6 mem, $7 status
  python3 - "$@" "$GOLDEN" "${TART_HOME:-$HOME/.tart}" "$LABELS" <<'PY'
import hashlib, json, os, socket, sys, time
out, vm, probe, egress_file, cores, mem, status, golden, tart_home, labels = sys.argv[1:11]
kv = dict(line.split("=", 1) for line in probe.splitlines() if "=" in line)
try:
    egress = json.load(open(egress_file))
except (OSError, ValueError):
    egress = None
receipt_path = os.path.join(tart_home, "goldens", golden.replace(":", "_").replace("/", "_") + ".json")
try:
    golden_receipt = json.load(open(receipt_path))
except (OSError, ValueError):
    golden_receipt = {}
json.dump({
    "schema": 1,
    "lane": "tart-linux/lint",
    "backend": "tart",
    "vm": vm,
    "host": socket.gethostname(),
    "golden": golden,
    "golden_disk_sha256": golden_receipt.get("disk_sha256"),
    "labels": labels.split(","),
    "lease": {"cores": int(cores), "mem_mb": int(mem) if mem else None},
    "egress": egress,
    "guest": kv,
    "isolation_proved": status == "ok",
    "written_at": int(time.time()),
}, open(out, "w"), indent=1)
PY
}
