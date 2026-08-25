#!/usr/bin/env bash
# Prove proxy-only Softnet behavior in one governed disposable Tart guest.
set -euo pipefail

TARTCI_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export TART_HOME="${TART_HOME:-$HOME/VMs}"
TART_BIN="${TARTCI_TART_BIN:-$(command -v tart)}"
SOFTNET_BIN="${TARTCI_SOFTNET_BIN:-/usr/local/libexec/tartci/softnet}"
GOLDEN="${TARTCI_MACOS_GOLDEN:-pulp-build-runner:latest}"
SSH_KEY="${TARTCI_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}"
VM_USER="${TARTCI_VM_USER:-admin}"
GUEST_PROXY="${TARTCI_GUEST_HTTP_PROXY:-http://192.168.64.1:49125}"
[[ "$GUEST_PROXY" =~ ^http://192\.168\.64\.1:([0-9]{1,5})$ ]] \
  || { echo "invalid TARTCI_GUEST_HTTP_PROXY" >&2; exit 1; }
PROXY_PORT="${BASH_REMATCH[1]}"
[ "$PROXY_PORT" -ge 1 ] && [ "$PROXY_PORT" -le 65535 ] \
  || { echo "invalid TARTCI_GUEST_HTTP_PROXY port" >&2; exit 1; }
CANARY_UUID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
VM_NAME="forge-softnet-egress-canary-$CANARY_UUID"
BOOT_LOG="$(mktemp -t forge-softnet-egress)"
RUN_PID=""
TUNNEL_PID=""
LEASE_ACQUIRED=0
VM_OWNED=0

# shellcheck source=providers/common/vm-lease.lib.sh
source "$TARTCI_ROOT/providers/common/vm-lease.lib.sh"

python3 "$TARTCI_ROOT/scripts/verify_macos_softnet_install.py" --path "$SOFTNET_BIN"

terminate_process_tree() {
  local pid="$1" signal="$2" child
  while IFS= read -r child; do
    [ -z "$child" ] || terminate_process_tree "$child" "$signal"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  kill -s "$signal" "$pid" 2>/dev/null || true
}

terminate_process_bounded() {
  local pid="$1" seconds="${2:-5}"
  terminate_process_tree "$pid" TERM
  for _ in $(seq 1 "$seconds"); do
    kill -0 "$pid" 2>/dev/null || { wait "$pid" 2>/dev/null || true; return 0; }
    sleep 1
  done
  terminate_process_tree "$pid" KILL
  wait "$pid" 2>/dev/null || true
}

wait_process_exit_bounded() {
  local pid="$1" seconds="${2:-10}"
  for _ in $(seq 1 "$seconds"); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null || true
      return 0
    fi
    sleep 1
  done
  return 124
}

run_cleanup_command_bounded() {
  local cleanup_pid
  "$@" >/dev/null 2>&1 & cleanup_pid=$!
  for _ in $(seq 1 10); do
    if ! kill -0 "$cleanup_pid" 2>/dev/null; then
      wait "$cleanup_pid"
      return $?
    fi
    sleep 1
  done
  terminate_process_tree "$cleanup_pid" TERM
  sleep 1
  terminate_process_tree "$cleanup_pid" KILL
  wait "$cleanup_pid" 2>/dev/null || true
  return 124
}

capture_command_bounded() {
  local seconds="$1" output_file command_pid rc=0
  shift
  output_file="$(mktemp -t tartci-canary-capture)" || return 1
  "$@" >"$output_file" 2>/dev/null & command_pid=$!
  for _ in $(seq 1 "$seconds"); do
    if ! kill -0 "$command_pid" 2>/dev/null; then
      wait "$command_pid" || rc=$?
      [ "$rc" -eq 0 ] && cat "$output_file"
      /usr/bin/unlink "$output_file" >/dev/null 2>&1 || true
      return "$rc"
    fi
    sleep 1
  done
  terminate_process_tree "$command_pid" TERM
  sleep 1
  terminate_process_tree "$command_pid" KILL
  wait "$command_pid" 2>/dev/null || true
  /usr/bin/unlink "$output_file" >/dev/null 2>&1 || true
  return 124
}

capture_command_bounded_allow_warning() {
  local seconds="$1" output_file command_pid rc=0
  shift
  output_file="$(mktemp -t tartci-canary-capture-warning)" || return 1
  "$@" >"$output_file" 2>/dev/null & command_pid=$!
  for _ in $(seq 1 "$seconds"); do
    if ! kill -0 "$command_pid" 2>/dev/null; then
      wait "$command_pid" || rc=$?
      if [ "$rc" -eq 0 ] || [ "$rc" -eq 1 ]; then
        cat "$output_file"
        /usr/bin/unlink "$output_file" >/dev/null 2>&1 || true
        return 0
      fi
      /usr/bin/unlink "$output_file" >/dev/null 2>&1 || true
      return "$rc"
    fi
    sleep 1
  done
  terminate_process_tree "$command_pid" TERM
  sleep 1
  terminate_process_tree "$command_pid" KILL
  wait "$command_pid" 2>/dev/null || true
  /usr/bin/unlink "$output_file" >/dev/null 2>&1 || true
  return 124
}

canary_vm_exists_or_unknown() {
  local inventory
  inventory="$(capture_command_bounded 10 "$TART_BIN" list --format json)" || return 0
  TARTCI_VM_INVENTORY="$inventory" python3 - "$VM_NAME" <<'PY'
import json
import os
import sys

name = sys.argv[1]
try:
    entries = json.loads(os.environ["TARTCI_VM_INVENTORY"])
except Exception:
    raise SystemExit(0)
if not isinstance(entries, list):
    raise SystemExit(0)
for entry in entries:
    if isinstance(entry, dict) and (entry.get("Name") == name or entry.get("name") == name):
        raise SystemExit(0)
raise SystemExit(1)
PY
}

canary_lease_absence_proved() {
  local lease_id="$1" inventory
  inventory="$(capture_command_bounded_allow_warning 10 python3 "$TARTCI_ROOT/scripts/leases.py" status --json)" \
    || return 1
  TARTCI_LEASE_INVENTORY="$inventory" python3 - "$lease_id" <<'PY'
import json
import os
import sys

lease_id = sys.argv[1]
try:
    payload = json.loads(os.environ["TARTCI_LEASE_INVENTORY"])
except Exception:
    raise SystemExit(1)
leases = payload.get("leases")
problems = payload.get("problems")
if not isinstance(leases, list) or not isinstance(problems, list):
    raise SystemExit(1)
for lease in leases:
    if isinstance(lease, dict) and lease.get("id") == lease_id:
        raise SystemExit(1)
for problem in problems:
    if isinstance(problem, str) and problem.rsplit(":", 1)[-1] == lease_id:
        raise SystemExit(1)
raise SystemExit(0)
PY
}

cleanup() {
  local lease_id="" lease_cores="" delete_rc=0
  if [ "$VM_OWNED" = 1 ]; then
    if [ -n "$TUNNEL_PID" ]; then
      terminate_process_bounded "$TUNNEL_PID" 5
      TUNNEL_PID=""
    fi
    run_cleanup_command_bounded "$TART_BIN" stop "$VM_NAME" || true
    if [ -n "$RUN_PID" ] && ! wait_process_exit_bounded "$RUN_PID" 10; then
      echo "canary VM guardian remains live after stop; lease preserved for $VM_NAME" >&2
      /usr/bin/unlink "$BOOT_LOG" >/dev/null 2>&1 || true
      return 1
    fi
    RUN_PID=""
    tartci_vm_lease_guard_run "$TART_BIN" delete "$VM_NAME" >/dev/null 2>&1 || delete_rc=$?
    if canary_vm_exists_or_unknown; then
      echo "canary teardown could not prove VM deletion (delete_rc=$delete_rc); lease preserved for $VM_NAME" >&2
      /usr/bin/unlink "$BOOT_LOG" >/dev/null 2>&1 || true
      return 1
    fi
    VM_OWNED=0
  fi
  if [ "$LEASE_ACQUIRED" = 1 ]; then
    lease_id="${TARTCI_ACTIVE_VM_LEASE_ID:-}"
    lease_cores="${TARTCI_ACTIVE_VM_LEASE_CORES:-}"
    [ -n "$lease_id" ] || {
      echo "canary lost its active lease identity before release" >&2
      return 1
    }
    tartci_release_vm_lease || return 1
    if ! canary_lease_absence_proved "$lease_id"; then
      TARTCI_ACTIVE_VM_LEASE_ID="$lease_id"
      TARTCI_ACTIVE_VM_LEASE_CORES="$lease_cores"
      tartci_start_vm_lease_heartbeat "$lease_id"
      echo "canary teardown could not prove lease release for $lease_id" >&2
      return 1
    fi
    LEASE_ACQUIRED=0
  fi
  /usr/bin/unlink "$BOOT_LOG" >/dev/null 2>&1 || true
}
on_exit() {
  local rc=$?
  trap - EXIT
  trap '' INT TERM
  while ! cleanup; do
    echo "canary teardown authority remains held by PID $$; retrying exact cleanup in 30s" >&2
    sleep 30
  done
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 143' INT TERM

tartci_acquire_vm_lease \
  "$VM_NAME" 4 tart-macos-vm gate softnet-negative-control 8192 "$TART_HOME"
LEASE_ACQUIRED=1
if canary_vm_exists_or_unknown; then
  echo "refusing canary because unique VM-name absence could not be proved" >&2
  exit 1
fi
VM_OWNED=1
tartci_vm_lease_guard_run "$TART_BIN" clone "$GOLDEN" "$VM_NAME"
tartci_vm_lease_guard_run "$TART_BIN" set "$VM_NAME" --cpu 4
SSH_OPTS=(
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes -i "$SSH_KEY"
)
wait_for_guest() {
  local ssh_ready=0
  VM_IP=""
  for _ in $(seq 1 90); do
    VM_IP="$("$TART_BIN" ip "$VM_NAME" 2>/dev/null || true)"
    [ -n "$VM_IP" ] && break
    kill -0 "$RUN_PID" 2>/dev/null || break
    sleep 2
  done
  [ -n "$VM_IP" ] || { tail -30 "$BOOT_LOG" >&2; return 1; }
  for _ in $(seq 1 90); do
    if ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" true 2>/dev/null; then
      ssh_ready=1
      break
    fi
    sleep 2
  done
  [ "$ssh_ready" = 1 ] || { tail -30 "$BOOT_LOG" >&2; return 1; }
}
stop_active_vm_phase() {
  run_cleanup_command_bounded "$TART_BIN" stop "$VM_NAME" || return 1
  wait_process_exit_bounded "$RUN_PID" 10 || return 1
  RUN_PID=""
}

# Positive controls use the same guest, destinations, and tools without the
# Softnet deny rules. A later nonzero result can count as policy enforcement
# only when this baseline proved the path was actually viable.
: >"$BOOT_LOG"
tartci_vm_lease_guard_run "$TART_BIN" run --no-graphics \
  "$VM_NAME" >"$BOOT_LOG" 2>&1 &
RUN_PID=$!
wait_for_guest
NO_PROXY_CURL='env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY -u https_proxy -u http_proxy -u all_proxy curl --noproxy "*"'
ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
  'command -v curl >/dev/null && command -v nc >/dev/null'
ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
  "$NO_PROXY_CURL -fsS --connect-timeout 5 --max-time 20 https://github.com/robots.txt >/dev/null"
ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
  "$NO_PROXY_CURL -kfsS --connect-timeout 5 --max-time 20 https://1.1.1/ >/dev/null"
ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
  "nc -z -w 5 192.168.64.1 '$PROXY_PORT' >/dev/null 2>&1"
BASELINE_IPV6="NO_ROUTE"
if ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
  'route -n get -inet6 default >/dev/null 2>&1'; then
  ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
    "$NO_PROXY_CURL -g -6 -fsS --connect-timeout 5 --max-time 20 'http://[2606:4700:4700::1111]/' >/dev/null"
  BASELINE_IPV6="REACHABLE"
fi
stop_active_vm_phase || {
  echo "positive-control VM phase did not stop cleanly; lease preserved" >&2
  exit 1
}

: >"$BOOT_LOG"
PATH="$(dirname "$SOFTNET_BIN"):$PATH" tartci_vm_lease_guard_run "$TART_BIN" run --no-graphics \
  --net-softnet-allow="in @host" \
  --net-softnet-block=0.0.0.0/0 \
  "$VM_NAME" >"$BOOT_LOG" 2>&1 &
RUN_PID=$!
wait_for_guest

ssh "${SSH_OPTS[@]}" -N -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
  -o ForwardAgent=no -o ForwardX11=no -o RequestTTY=no \
  -R "127.0.0.1:$PROXY_PORT:127.0.0.1:$PROXY_PORT" \
  "$VM_USER@$VM_IP" >/dev/null 2>&1 &
TUNNEL_PID=$!
sleep 1
kill -0 "$TUNNEL_PID" 2>/dev/null || { tail -30 "$BOOT_LOG" >&2; exit 1; }
EFFECTIVE_PROXY="http://127.0.0.1:$PROXY_PORT"

ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
  "HTTPS_PROXY=$EFFECTIVE_PROXY NO_PROXY=localhost curl -fsS --max-time 20 https://github.com/robots.txt >/dev/null"

ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
  'command -v curl >/dev/null && command -v nc >/dev/null'
blocked_probe() {
  local command="$1" marker output
  marker="TARTCI_EXPECTED_BLOCKED_${RANDOM}_$$"
  output="$(ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
    "if $command; then exit 42; else printf '%s' '$marker'; fi")" || return 1
  [ "$output" = "$marker" ]
}
blocked_probe "$NO_PROXY_CURL -fsS --connect-timeout 5 --max-time 8 https://github.com/robots.txt >/dev/null 2>&1" \
  || { echo "direct hostname block was not proved" >&2; exit 1; }
blocked_probe "nc -z -w 3 192.168.64.1 '$PROXY_PORT' >/dev/null 2>&1" \
  || { echo "direct host proxy block was not proved" >&2; exit 1; }
blocked_probe "$NO_PROXY_CURL -kfsS --connect-timeout 5 --max-time 8 https://1.1.1/ >/dev/null 2>&1" \
  || { echo "direct IPv4 block was not proved" >&2; exit 1; }
if [ "$BASELINE_IPV6" = REACHABLE ]; then
  blocked_probe "$NO_PROXY_CURL -g -6 -fsS --connect-timeout 5 --max-time 8 'http://[2606:4700:4700::1111]/' >/dev/null 2>&1" \
    || { echo "direct IPv6 block was not proved" >&2; exit 1; }
elif ssh "${SSH_OPTS[@]}" "$VM_USER@$VM_IP" \
  'route -n get -inet6 default >/dev/null 2>&1'; then
  echo "hardened guest gained an IPv6 default absent from its baseline" >&2
  exit 1
fi

trap '' INT TERM
cleanup || exit 1
trap - EXIT INT TERM
printf '%s\n' \
  "softnet_canary=PASS vm=$VM_NAME ip=$VM_IP proxied_https=PASS direct_hostname=BLOCKED direct_gateway_proxy=BLOCKED direct_ipv4=BLOCKED ipv6_escape=ABSENT host_ssh=PASS implicit_bootstrap=dhcp_v4_client vm_teardown=PASS"
