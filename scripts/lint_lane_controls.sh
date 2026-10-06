#!/usr/bin/env bash
# Negative controls for the lint lane's own instruments (acceptance plan, I.6.3):
# each must make its instrument report the failure it exists to catch, so a later
# PASS from the same instrument means something.
#
#   1. probe-credentials: a clone with a planted fake GitHub token file and an
#      AKIA-shaped string must FAIL the guest isolation probe.
#   2. probe-share:       a clone booted with a --dir share must FAIL the probe's
#                         no-host-share check (device present even if unmounted).
#   3. egress-baseline:   the hostile fixture's network probes under tart's default
#                         NAT (no Softnet) must read REACH for the LAN and public
#                         targets, so a BLOCK under Softnet proves Softnet, not a
#                         dead network.
#
# These VMs run only this repository's code, under a lease, and are discarded.
# Nothing here is an egress result; control 3 is the opposite of one.
#
#   scripts/lint_lane_controls.sh <golden>
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GOLDEN="$1"
export TART_HOME="${TART_HOME:-$HOME/.tart}"
export TARTCI_ROOT="$ROOT"
# shellcheck source=providers/common/vm-lease.lib.sh
source "$ROOT/providers/common/vm-lease.lib.sh"
SSH_KEY_PRIV="${TARTCI_VM_SSH_KEY:-$HOME/.config/tartci/keys/lint-vm_ed25519}"
VM_USER="admin"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o BatchMode=yes)
note(){ printf '• %s\n' "$*" >&2; }
die(){ printf 'DIE %s\n' "$*"; exit 3; }
# shellcheck source=providers/tart-linux/lint-lane.lib.sh
source "$ROOT/providers/tart-linux/lint-lane.lib.sh"
cores="$(tartci_vm_lease_cores tart-linux-lint)"; mem="$(tartci_vm_lease_mem_mb tart-linux-lint)"
share_dir="$(mktemp -d)"

boot(){ # name [tart run args...] ; sets IP. Waits for the lease like the lane does.
  local vm="$1" lease; shift
  IP=""
  if ! lease="$(tartci leases acquire --id "$vm" --cores "$cores" --mem-mb "$mem" --priority vm \
      --kind tart-linux-lint-control --pid $$ --wait-secs "${TARTCI_CONTROL_LEASE_WAIT_SECS:-3600}" --json)"; then
    echo "LEASE refused for $vm: $(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("reason"), d.get("exceeded_axis"))' <<<"$lease")"
    return 1
  fi
  { tart clone "$GOLDEN" "$vm" && tart set "$vm" --cpu "$cores" --memory "$mem"; } >&2 || return 1
  tart run --no-graphics "$@" "$vm" >/dev/null 2>&1 &
  for _ in $(seq 1 300); do IP="$(tart ip "$vm" 2>/dev/null || true)"; [ -n "$IP" ] && break; sleep 0.2; done
  for _ in $(seq 1 300); do ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$IP" true 2>/dev/null && return 0; sleep 0.2; done
  return 1
}
discard(){ tart stop "$1" >/dev/null 2>&1; tart delete "$1" >/dev/null 2>&1; tartci leases release --id "$1" >/dev/null 2>&1; }
# An empty probe is an unreadable guest, not a FAIL of the property under test.
verdict(){ [ -n "$1" ] || { echo NO-PROBE; return; }; if tartci_lint_probe_ok "$1"; then echo PASS; else echo FAIL; fi; }

# 0. Baseline: an untouched clone of the golden must PASS, or every later FAIL
#    could be the golden itself rather than the planted defect.
vm="lint-ctl-base-$$"; boot "$vm" || { discard "$vm"; exit 1; }
probe="$(tartci_lint_guest_probe "$IP")"
# Tie any token paths to the pinned inputs: the runner version inside the guest
# and the golden's recorded disk digest.
runner_version="$(ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$IP" \
  'cat ~/actions-runner/bin/Runner.Listener.deps.json 2>/dev/null | grep -o "\"Runner.Listener/[0-9.]*\"" | head -1' 2>/dev/null)"
golden_digest="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("disk_sha256"))' \
  "$TART_HOME/goldens/${GOLDEN//[:\/]/_}.json" 2>/dev/null || echo unknown)"
discard "$vm"
echo "CONTROL probe-baseline golden=$GOLDEN golden_disk_sha256=$golden_digest runner=${runner_version:-unknown}"
echo "CONTROL probe-baseline expect=PASS got=$(verdict "$probe") $(grep -E '^(credential_files|token_strings|token_paths|host_share|ipv6|bake_stamp)' <<<"$probe" | tr '\n' ' ')"

vm="lint-ctl-cred-$$"; boot "$vm" || { discard "$vm"; exit 1; }; ip="$IP"
ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
  'mkdir -p ~/.config/gh && printf "github.com:\n  oauth_token: ghp_%s\n" 0123456789abcdefghijABCDEFGHIJ01234567 > ~/.config/gh/hosts.yml
   printf "aws_access_key_id = AKIA%s\n" ABCDEFGHIJKLMNOP > ~/notes.txt'
probe="$(tartci_lint_guest_probe "$ip")"; discard "$vm"
echo "CONTROL probe-credentials expect=FAIL got=$(verdict "$probe") $(grep -E '^(credential_files|token_strings|token_paths)=' <<<"$probe" | tr '\n' ' ')"

# 1b. Scan boundary: a key file dated before the bake stamp is the golden's
#     territory (covered by its digest), so the scan must NOT report it, while
#     the same content dated now must be reported.
vm="lint-ctl-stamp-$$"; boot "$vm" || { discard "$vm"; exit 1; }; ip="$IP"
ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
  'k="-----BEGIN OPENSSH PRIVATE KEY-----"; printf "%s\n" "$k" > ~/old-key.txt; touch -d 2000-01-01 ~/old-key.txt'
probe_old="$(tartci_lint_guest_probe "$ip")"
ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" 'cp -p ~/old-key.txt ~/new-key.txt; touch ~/new-key.txt'
probe_new="$(tartci_lint_guest_probe "$ip")"; discard "$vm"
echo "CONTROL scan-boundary-prestamp expect=PASS got=$(verdict "$probe_old") $(grep -E '^(token_strings|token_paths|bake_stamp)=' <<<"$probe_old" | tr '\n' ' ')"
echo "CONTROL scan-boundary-poststamp expect=FAIL got=$(verdict "$probe_new") $(grep -E '^(token_strings|token_paths)=' <<<"$probe_new" | tr '\n' ' ')"

# 1c. IPv6: the same golden with the kernel command-line flag removed and the
#     guest rebooted must FAIL the probe's ipv6_stack check.
vm="lint-ctl-ipv6-$$"; boot "$vm" || { discard "$vm"; exit 1; }; ip="$IP"
ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
  'sudo rm -f /etc/default/grub.d/99-tartci-no-ipv6.cfg && sudo update-grub >/dev/null 2>&1 && (sudo systemctl reboot >/dev/null 2>&1 &)' || true
sleep 15
for _ in $(seq 1 300); do ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" true 2>/dev/null && break; sleep 0.5; done
sleep 20  # give a router advertisement time to land, the race the flag closes
probe="$(tartci_lint_guest_probe "$ip")"; discard "$vm"
echo "CONTROL ipv6-without-cmdline expect=FAIL got=$(verdict "$probe") $(grep -E '^ipv6' <<<"$probe" | tr '\n' ' ')"

vm="lint-ctl-share-$$"; boot "$vm" --dir="ctl:$share_dir" || { discard "$vm"; exit 1; }; ip="$IP"
probe="$(tartci_lint_guest_probe "$ip")"; discard "$vm"
echo "CONTROL probe-share expect=FAIL got=$(verdict "$probe") $(grep -E '^host_share' <<<"$probe" | tr '\n' ' ')"

vm="lint-ctl-nat-$$"; boot "$vm" || { discard "$vm"; exit 1; }; ip="$IP"
out="$({ echo 'TARTCI_FIXTURE_MODE=probe-only TARTCI_FIXTURE_WHO=nat'; echo 'export TARTCI_FIXTURE_MODE TARTCI_FIXTURE_WHO'; \
        cat "$ROOT/providers/tart-linux/fixtures/lint-hostile-job.sh"; } \
      | ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" 'bash -s' 2>&1)"
discard "$vm"
printf '%s\n' "$out" | sed 's/^/CONTROL egress-baseline /'
reach=$(grep -cE 'PROBE nat (192\.168\.86\.|1\.1\.1\.1|example\.com).* REACH' <<<"$out")
echo "CONTROL egress-baseline expect=REACH-on-LAN-and-public got=$([ "$reach" -ge 2 ] && echo REACH || echo NO-REACH) reach_count=$reach"
rm -rf "$share_dir"
