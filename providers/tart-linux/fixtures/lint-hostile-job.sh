#!/usr/bin/env bash
# A hostile pull-request job for the lint lane (acceptance plan, section D).
# Runs inside the guest exactly as a job's steps would, as the runner user,
# and also as root after tearing down the guest's own firewall and routes.
# Every line it prints is a verdict the host-side harness asserts on.
#
# Lines:  PROBE <who> <target> REACH|BLOCK|ALLOWED(residual: DNS)
#         PERSIST <marker> PRESENT|ABSENT      (a previous job's leftovers)
#         CRED <what> FOUND|NONE
#         ESCAPE <target> REACH|BLOCK
#         ABUSE <what> <result>
set -u
GATEWAY="$(ip -4 route show default | awk '{print $3; exit}')"
SELF_SUBNET="$(ip -4 -o addr show scope global | awk '{print $4; exit}')"
printf 'INFO boot_id=%s gateway=%s self=%s\n' "$(cat /proc/sys/kernel/random/boot_id)" "$GATEWAY" "$SELF_SUBNET"

probe_tcp(){ # who host port
  if timeout 5 bash -c "</dev/tcp/$2/$3" 2>/dev/null; then echo "PROBE $1 $2:$3 REACH"; else echo "PROBE $1 $2:$3 BLOCK"; fi
}
probe_set(){ # who
  local who="$1"
  # Positive: what the lint jobs need.
  for t in api.github.com:443 github.com:443 objects.githubusercontent.com:443 codeload.github.com:443 \
           results-receiver.actions.githubusercontent.com:443 broker.actions.githubusercontent.com:443 \
           pipelines.actions.githubusercontent.com:443; do
    probe_tcp "$who" "${t%:*}" "${t#*:}"
  done
  # Negative: the host gateway's other ports, the LAN, the tailnet, the Mac Pro,
  # an arbitrary public address and name.
  for p in 22 80 443 8080; do probe_tcp "$who" "$GATEWAY" "$p"; done
  for t in 192.168.86.1:80 192.168.86.43:22 192.168.86.43:8006 192.168.86.21:22 \
           100.100.100.100:53 100.64.0.1:22 169.254.169.254:80 1.1.1.1:443 example.com:443; do
    probe_tcp "$who" "${t%:*}" "${t#*:}"
  done
  # UDP egress to a public resolver.
  if timeout 5 bash -c 'exec 3<>/dev/udp/8.8.8.8/53; printf "\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01" >&3; timeout 3 head -c 12 <&3 | wc -c' 2>/dev/null | grep -q 12; then
    echo "PROBE $who udp:8.8.8.8:53 REACH"; else echo "PROBE $who udp:8.8.8.8:53 BLOCK"; fi
  # DNS through the gateway resolver is the recorded residual; say so, never silently.
  if getent hosts example.com >/dev/null 2>&1; then echo "PROBE $who dns:example.com ALLOWED(residual: DNS)"; else echo "PROBE $who dns:example.com BLOCK"; fi
  # IPv6 egress.
  # A BLOCK here proves nothing on a host without an IPv6 route; the IPv6 claim
  # rests on the guest facts printed beside it (and asserted by the lane's probe).
  if timeout 5 bash -c '</dev/tcp/2606:4700:4700::1111/443' 2>/dev/null; then v6=REACH; else v6="BLOCK (uninformative on a host without IPv6)"; fi
  echo "PROBE $who ipv6:[2606:4700:4700::1111]:443 $v6 disable_ipv6=$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null) global_v6_addrs=$(ip -6 addr show scope global 2>/dev/null | grep -c inet6)"
}

# Probe-only mode: the network probes alone, for the NAT baseline control.
if [ "${TARTCI_FIXTURE_MODE:-full}" = probe-only ]; then
  probe_set "${TARTCI_FIXTURE_WHO:-user}"
  exit 0
fi

# Persistence: a previous job's markers must be gone.
for m in "$HOME/.tartci-d-marker" /tmp/.tartci-d-marker /var/tmp/.tartci-d-marker "$HOME/actions-runner/_work/.tartci-d-marker"; do
  [ -e "$m" ] && echo "PERSIST $m PRESENT" || echo "PERSIST $m ABSENT"
done
grep -q tartci-d-marker "$HOME/.bashrc" && echo "PERSIST bashrc PRESENT" || echo "PERSIST bashrc ABSENT"
crontab -l 2>/dev/null | grep -q tartci-d-marker && echo "PERSIST crontab PRESENT" || echo "PERSIST crontab ABSENT"
pgrep -f tartci-d-marker >/dev/null && echo "PERSIST process PRESENT" || echo "PERSIST process ABSENT"

# Credentials: nothing a host secret could look like.
for f in "$HOME"/.ssh/id_* "$HOME/.config/gh/hosts.yml" "$HOME/.git-credentials" "$HOME/.netrc" \
         "$HOME/actions-runner/.credentials" "$HOME/actions-runner/.runner" "$HOME/jit.cfg"; do
  [ -e "$f" ] && echo "CRED $f FOUND"
done
n=$(sudo grep -rIlE "gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}" /home /root /etc /var/lib /tmp 2>/dev/null | wc -l)
[ "$n" -eq 0 ] && echo "CRED token-strings NONE" || echo "CRED token-strings FOUND($n)"
grep -qE "danielraffel|id_ed25519" "$HOME/.ssh/authorized_keys" && echo "CRED personal-key-in-authorized_keys FOUND" || echo "CRED personal-key-in-authorized_keys NONE"

# Host escape: no share, no host state.
grep -qE " (virtiofs|9p) " /proc/mounts && echo "ESCAPE host-share REACH" || echo "ESCAPE host-share BLOCK"
for p in /Users /Volumes /mnt/host; do [ -d "$p" ] && ls "$p" >/dev/null 2>&1 && echo "ESCAPE $p REACH" || echo "ESCAPE $p BLOCK"; done
probe_tcp escape "$GATEWAY" 5000
probe_tcp escape "${GATEWAY%.*}.3" 22   # a sibling VM on the same vmnet

probe_set user
# As root, with the guest's own filtering and routing torn down.
sudo bash -c 'iptables -F 2>/dev/null; iptables -P OUTPUT ACCEPT 2>/dev/null; nft flush ruleset 2>/dev/null; ip route replace default via '"$GATEWAY"' 2>/dev/null; sysctl -qw net.ipv6.conf.all.disable_ipv6=0' 
probe_set root

# Plant markers for the next job to look for.
touch "$HOME/.tartci-d-marker" /tmp/.tartci-d-marker /var/tmp/.tartci-d-marker
mkdir -p "$HOME/actions-runner/_work" && touch "$HOME/actions-runner/_work/.tartci-d-marker"
echo '# tartci-d-marker' >> "$HOME/.bashrc"
(crontab -l 2>/dev/null; echo '* * * * * true # tartci-d-marker') | crontab - 2>/dev/null
nohup bash -c 'exec -a tartci-d-marker sleep 3600' >/dev/null 2>&1 &
echo "INFO markers planted"

# Resource abuse, bounded so the harness can observe the host meanwhile.
( timeout 20 bash -c 'for i in $(seq 1 4); do (while :; do :; done) & done; wait' ) >/dev/null 2>&1 &
# Ask for twice the guest's memory: the lease's guest size must bound it. The
# kernel OOM-kills the hog rather than raising MemoryError, so it runs as a child
# and the verdict is its exit status (137 = killed), printed either way.
guest_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
echo "INFO guest_mem_mb=$guest_mb"
python3 -c '
import sys
blocks = []
for _ in range(2 * int(sys.argv[1])):
    blocks.append(bytearray(1 << 20))
' "$guest_mb" >/dev/null 2>&1
hog_rc=$?
case "$hog_rc" in
  137) echo "ABUSE memory-hog killed rc=137 (guest memory bound held)" ;;
  0)   echo "ABUSE memory-hog allocated-$((2 * guest_mb))MiB rc=0 (NOT bounded)" ;;
  *)   echo "ABUSE memory-hog failed rc=$hog_rc" ;;
esac
wait
echo "ABUSE cpu-burn finished"
echo "DONE"
# Last, because it takes the guest down: an unbounded fork bomb. The verdict is
# host-side: the supervisor's wall cap ends the job and the host stays responsive.
if [ "${TARTCI_FIXTURE_FORK_BOMB:-1}" = 1 ]; then
  echo "ABUSE fork-bomb starting"
  # shellcheck disable=SC2264  # the self-invocation is the point
  bomb(){ bomb | bomb & }; bomb
fi
