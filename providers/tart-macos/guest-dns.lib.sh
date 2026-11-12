#!/usr/bin/env bash
# Optional per-host guest resolvers for disposable macOS guests.
#
# A guest takes its resolver from the host's vmnet DHCP (192.168.64.1), and the
# host answers it by forwarding to its own resolver. When that is Tailscale
# MagicDNS, every guest on every such host shares the tailnet's single global
# nameserver, so one stall there fails npm/cargo/curl lookups in gate VMs on
# several hosts in the same minute while the jobs themselves are fine.
#
# With `[guest_network] dns_servers` in the fleet profile (read through
# host_profile.py as guest_dns_servers), the runner points the guest's primary
# network service at those resolvers before it registers, proves one lookup
# through them, and otherwise puts the DHCP resolver back. Every failure is
# fail-open: the guest keeps the resolver it booted with and the job runs
# exactly as it would have without the knob. Absent (the default) this is a
# no-op.
#
# Callers provide: tartci_profile_value, event, note, ssh (with SSH_OPTS,
# SSH_KEY_PRIV, VM_USER).

TARTCI_GUEST_DNS_PROBE_NAME="${TARTCI_GUEST_DNS_PROBE_NAME:-github.com}"
_tartci_guest_dns_servers=""
_tartci_guest_dns_loaded=0

# Succeeds when every word of $1 has the shape of an IP literal. The profile
# reader already validated the addresses; this only guarantees that nothing
# but address characters ever reaches the guest command line.
guest_dns_servers_shape_ok(){
  local servers="${1:-}" word count=0
  [ -n "$servers" ] || return 1
  for word in $servers; do
    case "$word" in
      *[!0-9A-Fa-f:.]*) return 1 ;;
    esac
    count=$((count + 1))
  done
  [ "$count" -ge 1 ] && [ "$count" -le 4 ]
}

# The configured resolvers, space-separated, or empty when the knob is off. Read
# once per supervisor process: a profile change reaches it on its next restart,
# as for the other profile knobs.
tartci_guest_dns_servers(){
  if [ "$_tartci_guest_dns_loaded" != 1 ]; then
    _tartci_guest_dns_servers="$(tartci_profile_value guest_dns_servers 2>/dev/null)" \
      || _tartci_guest_dns_servers=""
    _tartci_guest_dns_loaded=1
  fi
  printf '%s' "$_tartci_guest_dns_servers"
}

# The script run inside the guest. Arguments: probe name, then the resolvers.
# Exit 0 applied and proven; 2 no service to configure; 3 the set was refused;
# 4 the read-back disagreed; 5 no lookup succeeded through the new resolvers.
# Every exit after a successful set restores the DHCP resolver ("Empty").
guest_dns_guest_script(){
  cat <<'GUEST'
set -uo pipefail
probe="$1"; shift
iface="$(route -n get default 2>/dev/null | awk '/interface:/ { print $2; exit }')"
[ -n "$iface" ] || { echo "guest-dns: no default-route interface" >&2; exit 2; }
service="$(networksetup -listnetworkserviceorder 2>/dev/null | awk -v dev="$iface" '
  /^\([0-9*]+\) / { sub(/^\([0-9*]+\) /, ""); name = $0; next }
  index($0, "Device: " dev ")") { print name; exit }')"
[ -n "$service" ] || { echo "guest-dns: no network service for $iface" >&2; exit 2; }
restore(){ sudo -n networksetup -setdnsservers "$service" Empty >/dev/null 2>&1 || true; }
sudo -n networksetup -setdnsservers "$service" "$@" || { restore; exit 3; }
applied="$(networksetup -getdnsservers "$service" 2>/dev/null | tr '\n' ' ')"
[ "$applied" = "$* " ] || { echo "guest-dns: read back '$applied'" >&2; restore; exit 4; }
sudo -n killall -HUP mDNSResponder >/dev/null 2>&1 || true
for _ in 1 2 3 4 5; do
  if dscacheutil -q host -a name "$probe" 2>/dev/null | grep -q '^ip_address'; then
    echo "guest-dns: $service -> $*"
    exit 0
  fi
  sleep 1
done
echo "guest-dns: $probe did not resolve through $*" >&2
restore
exit 5
GUEST
}

# $1 guest IP. Always returns 0; the outcome is an event, never a job failure.
tartci_apply_guest_dns(){
  local ip="$1" servers rc=0
  servers="$(tartci_guest_dns_servers)"
  [ -n "$servers" ] || return 0
  if ! guest_dns_servers_shape_ok "$servers"; then
    event guest_dns "result=skipped reason=bad_servers"
    return 0
  fi
  # shellcheck disable=SC2086 # servers is a validated, space-separated list
  guest_dns_guest_script | ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    bash -s -- "$TARTCI_GUEST_DNS_PROBE_NAME" $servers >/dev/null 2>&1 || rc=$?
  if [ "$rc" -eq 0 ]; then
    event guest_dns "result=applied servers=${servers// /,}"
  else
    note "guest resolvers ${servers} not applied (rc=$rc); keeping the DHCP resolver"
    event guest_dns "result=kept_dhcp rc=$rc servers=${servers// /,}"
  fi
  return 0
}
