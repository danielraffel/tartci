#!/usr/bin/env bash
# tart-macos/runner.sh — ephemeral, per-job GitHub Actions runner on Tart macOS.
# It mints a single-job JIT config, boots a fresh clone, runs the Actions agent
# once, emits state/heartbeat/events, then tears everything down. Defaults are
# pilot-safe (`pulp-build-vm`, not required `pulp-build`).
# `--print-queue` reports queued jobs whose requested labels are satisfiable by
# the configured runner labels; it is a safe preflight for the loop gate. It prints
# the queued COUNT on a successful scan (`0` = genuinely idle), or the sentinel `ERR`
# when the gh scan itself FAILS (rate-limit / timeout / degraded token / network) — so a
# failed poll is never misread as an empty queue. On sustained blindness (ERR for
# ~TARTCI_SCAN_BLIND_MAX polls ≈ 3 min) the loop self-restarts via `exit 75` (launchd
# KeepAlive respawns → fresh auth), instead of idling silent for hours (a real 5h wedge).
# One lane may watch multiple exact workflow names by setting newline-delimited
# TARTCI_RUNNER_WORKFLOW_NAMES. The plural setting replaces the legacy singular
# TARTCI_RUNNER_WORKFLOW_NAME; the singular setting remains the default.
# Ordered workflow tiers (opt-in): TARTCI_RUNNER_WORKFLOW_TIERS contains one
# `class-label|exact workflow name` entry per line. First-seen labels are
# the priority order; workflows sharing a class label share one FIFO
# class. The runner scans each class in order and registers its JIT runner with
# only the selected class labels, so GitHub cannot assign lower-priority work to
# a runner reserved for a higher-priority class. Before minting a lower-tier JIT
# config, the supervisor rechecks every higher tier and discards the still-
# unregistered VM if higher-priority demand arrived during boot.
# Priority-aware idle gate (opt-in): set TARTCI_YIELD_TO_WORKFLOW_NAME +
# TARTCI_YIELD_TO_LABELS to make a SECONDARY lane yield its VM slot to a
# higher-priority lane. When set, the loop boots only when that priority lane
# has NO queued/in-progress work (in addition to the usual queue + cap checks),
# so on a shared-cap host (e.g. Apple's 2-running-macOS-guest limit) a long
# advisory job can never starve the required gate. Unset = no yielding, so the
# primary gate runner and existing lanes are byte-for-byte unchanged.
# `--print-priority-demand` reports that yield count (0 when the feature is off);
# a safe preflight for the gate.
# Host-health auto-yield (opt-in): set TARTCI_HOST_VITALS_YIELD=1 to make the
# loop stop booting NEW VMs while the host is saturated (memory-pressure critical
# / fresh jetsam), reading the shared `host_vitals.sh` signal. Off by default, so
# a host that never installs host_vitals is byte-for-byte unchanged. Unlike the
# priority gate this is FAIL-OPEN: a probe error prints 0 (do not yield), so a
# missing/broken host_vitals never wedges the required gate — the worst case is
# the crash-avoidance we simply don't get, never a stalled runner. Yields only on
# CRITICAL by default; TARTCI_HOST_VITALS_YIELD_ON_WARN=1 also drains on WARN.
# `--print-host-health` reports the yield decision (0 boot / 1 yield) as a safe
# preflight, mirroring `--print-priority-demand`.
set -euo pipefail

TARTCI_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=providers/common/pool.lib.sh
source "$TARTCI_ROOT/providers/common/pool.lib.sh"
export TART_HOME="${TART_HOME:-$HOME/VMs}"
# GitHub CLI used for every API call (queue polling, JIT mint, runner reclaim,
# job/run polling, cancel). Default `gh` (the personal/host auth) keeps generic
# tartci behavior unchanged. Hosts that authenticate as a GitHub App can set
# TARTCI_GH_CLI=ghapp to move ALL provider API traffic off the personal PAT and
# onto the App's separate rate-limit bucket — the per-poll calls (every VM_POLL
# seconds × every host) are the dominant throttle source. Exported so the inline
# python pollers below inherit it.
export TARTCI_GH_CLI="${TARTCI_GH_CLI:-gh}"
GH_CLI="$TARTCI_GH_CLI"
SSH_KEY_PRIV="${TARTCI_VM_SSH_KEY:-${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}}"
VM_USER="${TARTCI_VM_USER:-${PULP_VM_USER:-admin}}"
CACHE_ROOT="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/pulp-ci}}"
FETCHCONTENT_SOURCE_ROOT="${PULP_SHARED_FETCHCONTENT_SOURCE_DIR:-$HOME/Library/Caches/Pulp/fetchcontent-src}"
HOST_CCACHE="${TARTCI_MACOS_HOST_CCACHE:-1}"
case "$HOST_CCACHE" in
  0|1) ;;
  *) printf 'invalid TARTCI_MACOS_HOST_CCACHE: expected 0 or 1\n' >&2; exit 1 ;;
esac
GOLDEN="${TARTCI_MACOS_GOLDEN:-${PULP_RUNNER_GOLDEN:-pulp-build-runner:latest}}"
REPO="${TARTCI_RUNNER_REPO:-${PULP_RUNNER_REPO:-Generous-Corp/pulp}}"
LABELS="${TARTCI_RUNNER_LABELS:-${PULP_RUNNER_LABELS:-self-hosted,macOS,ARM64,pulp-build-vm}}"
RUNNER_GROUP_ID="${TARTCI_RUNNER_GROUP_ID:-${PULP_RUNNER_GROUP_ID:-1}}"
RUNNER_VERSION="${TARTCI_RUNNER_VERSION:-${PULP_RUNNER_VERSION:-2.336.0}}"
RUNNER_SHA256="${TARTCI_RUNNER_SHA256:-${PULP_RUNNER_SHA256:-}}"
GUEST_HTTP_PROXY="${TARTCI_GUEST_HTTP_PROXY:-}"
GUEST_PROXY_PORT=""
if [ -n "$GUEST_HTTP_PROXY" ]; then
  [[ "$GUEST_HTTP_PROXY" =~ ^http://192\.168\.64\.1:([0-9]{1,5})$ ]] \
    || { printf 'invalid TARTCI_GUEST_HTTP_PROXY: expected http://192.168.64.1:PORT\n' >&2; exit 1; }
  GUEST_PROXY_PORT="${BASH_REMATCH[1]}"
  [ "$GUEST_PROXY_PORT" -ge 1 ] && [ "$GUEST_PROXY_PORT" -le 65535 ] \
    || { printf 'invalid TARTCI_GUEST_HTTP_PROXY port\n' >&2; exit 1; }
fi
SOFTNET_PROXY_ONLY="${TARTCI_TART_SOFTNET_PROXY_ONLY:-0}"
SOFTNET_BIN="${TARTCI_SOFTNET_BIN:-/usr/local/libexec/tartci/softnet}"
case "$SOFTNET_PROXY_ONLY" in
  0|1) ;;
  *) printf 'invalid TARTCI_TART_SOFTNET_PROXY_ONLY: expected 0 or 1\n' >&2; exit 1 ;;
esac
TART_NETWORK_ARGS=(--no-graphics)
if [ "$SOFTNET_PROXY_ONLY" = 1 ]; then
  [ -n "$GUEST_HTTP_PROXY" ] || {
    printf 'TARTCI_TART_SOFTNET_PROXY_ONLY=1 requires TARTCI_GUEST_HTTP_PROXY\n' >&2
    exit 1
  }
  [ -x "$SOFTNET_BIN" ] || {
    printf 'TARTCI_TART_SOFTNET_PROXY_ONLY=1 requires pinned Softnet at %s\n' "$SOFTNET_BIN" >&2
    exit 1
  }
  # Permit replies only for flows initiated by the host (SSH), then deny every
  # guest-initiated IPv4 destination. This is Softnet's stateful @host rule,
  # not a gateway CIDR exception: the guest still cannot initiate a connection
  # to any service on 192.168.64.1. The SSH reverse forward exposes the CONNECT
  # proxy on guest loopback without opening a gateway port.
  TART_NETWORK_ARGS+=(--net-softnet-allow="in @host" --net-softnet-block=0.0.0.0/0)
fi
EFFECTIVE_GUEST_HTTP_PROXY="$GUEST_HTTP_PROXY"
[ "$SOFTNET_PROXY_ONLY" = 1 ] && EFFECTIVE_GUEST_HTTP_PROXY="http://127.0.0.1:$GUEST_PROXY_PORT"
[ -n "$RUNNER_SHA256" ] || [ "$RUNNER_VERSION" != 2.336.0 ] || \
  RUNNER_SHA256="8e8839c49b7060b6b2154f4931f815df330c27f167d53ef2239ee3dfce28b079"
WORKFLOW_NAME="${TARTCI_RUNNER_WORKFLOW_NAME:-Build and Test}"
WORKFLOW_NAMES="${TARTCI_RUNNER_WORKFLOW_NAMES:-}"
WORKFLOW_TIERS="${TARTCI_RUNNER_WORKFLOW_TIERS:-}"
MIN_QUEUED_AGE="${TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS:-0}"
case "$MIN_QUEUED_AGE" in
  ''|*[!0-9]*) printf 'invalid TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS: %s\n' "$MIN_QUEUED_AGE" >&2; exit 1 ;;
esac
WORKFLOW_ARGS=()
WORKFLOW_DISPLAY=""
WORKFLOW_CONFIG=""
TIER_LABELS_CONFIG=""
# Priority-aware idle gate (opt-in; see header). YIELD_WORKFLOW empty = OFF.
YIELD_WORKFLOW="${TARTCI_YIELD_TO_WORKFLOW_NAME:-}"
YIELD_LABELS="${TARTCI_YIELD_TO_LABELS:-}"
# Host-health auto-yield (opt-in; see header): the decision lives in the shared
# providers/common/host-health.lib.sh, reading TARTCI_HOST_VITALS_YIELD[_ON_WARN]
# / TARTCI_HOST_VITALS_BIN directly. Empty/0 = OFF (no host_vitals call).
LOOP=0
CAP="${TARTCI_MACOS_VM_CAP:-${PULP_VM_CAP:-2}}"
POLL="${TARTCI_VM_POLL:-${PULP_VM_POLL:-20}}"; case "$POLL" in ''|*[!0-9]*|0) POLL=20;; esac  # positive int only (self-heal arithmetic)
JOB_TIMEOUT="${TARTCI_JOB_TIMEOUT_SECS:-7200}"
JOB_WARN="${TARTCI_JOB_WARN_SECS:-5400}"
IDLE_TIMEOUT="${TARTCI_RUNNER_IDLE_TIMEOUT_SECS:-900}"
STATE_DIR="${TARTCI_STATE_DIR:-$HOME/.tartci/state/macos}"
EVENT_LOG="${TARTCI_EVENT_LOG:-$STATE_DIR/events.jsonl}"
EVENT_LOG_EXPLICIT=0
[ -n "${TARTCI_EVENT_LOG:-}" ] && EVENT_LOG_EXPLICIT=1
MACOS_LOGROOT="${TARTCI_MACOS_LOGS:-$HOME/VMs/logs/tartci-macos}"
RUNNER_NAME="${TARTCI_RUNNER_NAME:-${PULP_RUNNER_NAME:-}}"
RUNNER_NAME_PREFIX="${TARTCI_RUNNER_NAME_PREFIX:-${PULP_RUNNER_NAME_PREFIX:-}}"
SLOT="${TARTCI_RUNNER_SLOT:-${PULP_RUNNER_SLOT:-1}}"
PRINT_NAME=0
PRINT_EVENT_LOG=0
PRINT_IDENTITY=0
PRINT_QUEUE=0
PRINT_SELECTION=0
PRINT_HIGHER_PRIORITY=""
PRINT_PRIORITY=0
PRINT_HOST_HEALTH=0
PRINT_RUNNER_VERSION=0
PRINT_NETWORK_POLICY=0
CURRENT_VM=""
CURRENT_RPID=""
CURRENT_RUN_ID=""
CURRENT_JOB_ID=""
CURRENT_WORKFLOW_NAME=""
CURRENT_LABELS="$LABELS"
CURRENT_IP=""
CURRENT_REGISTERED_RUNNER=""
CURRENT_AQUA_LABEL=""
CURRENT_PROXY_TUNNEL_PID=""
PENDING_STATIC_RUNNER_RECLAIM=0
TEARDOWN_BLOCKED=0
CLEANED_UP=0
SUPERVISOR_PID="$$"
SUPERVISOR_PID_STARTED_AT="$(ps -p "$$" -o lstart= 2>/dev/null | tr -s ' ' | sed 's/^ //;s/ $//')"
HOST_NAME="$(hostname -s 2>/dev/null || hostname)"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes)

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
now_epoch(){ date +%s; }
elapsed(){ awk -v start="$1" -v end="$2" 'BEGIN { printf "%.1f", end - start }'; }

configure_workflows(){
  local entry tier_labels workflow
  if [ -n "$WORKFLOW_TIERS" ]; then
    while IFS= read -r entry; do
      entry="${entry%$'\r'}"
      [ -n "$entry" ] || continue
      case "$entry" in
        *'|'*) ;;
        *) die "invalid TARTCI_RUNNER_WORKFLOW_TIERS entry (expected additional-labels|workflow): $entry" ;;
      esac
      tier_labels="${entry%%|*}"
      workflow="${entry#*|}"
      [ -n "$tier_labels" ] && [ -n "$workflow" ] \
        || die "invalid TARTCI_RUNNER_WORKFLOW_TIERS entry (empty labels/workflow): $entry"
      case "$tier_labels" in
        *,*) die "workflow tier must use one exclusive class label, not a comma list: $tier_labels" ;;
      esac
      WORKFLOW_ARGS+=(--workflow "$workflow")
      if [ -n "$WORKFLOW_DISPLAY" ]; then
        WORKFLOW_DISPLAY="$WORKFLOW_DISPLAY | $workflow"
        WORKFLOW_CONFIG="$WORKFLOW_CONFIG
$workflow"
      else
        WORKFLOW_DISPLAY="$workflow"
        WORKFLOW_CONFIG="$workflow"
        WORKFLOW_NAME="$workflow"
      fi
      if ! printf '%s\n' "$TIER_LABELS_CONFIG" | grep -Fxq "$tier_labels"; then
        if [ -n "$TIER_LABELS_CONFIG" ]; then
          TIER_LABELS_CONFIG="$TIER_LABELS_CONFIG
$tier_labels"
        else
          TIER_LABELS_CONFIG="$tier_labels"
        fi
      fi
    done <<< "$WORKFLOW_TIERS"
    [ "${#WORKFLOW_ARGS[@]}" -gt 0 ] \
      || die "TARTCI_RUNNER_WORKFLOW_TIERS contains no workflow tiers"
  elif [ -n "$WORKFLOW_NAMES" ]; then
    while IFS= read -r workflow; do
      workflow="${workflow%$'\r'}"
      [ -n "$workflow" ] || continue
      WORKFLOW_ARGS+=(--workflow "$workflow")
      if [ -n "$WORKFLOW_DISPLAY" ]; then
        WORKFLOW_DISPLAY="$WORKFLOW_DISPLAY | $workflow"
        WORKFLOW_CONFIG="$WORKFLOW_CONFIG
$workflow"
      else
        WORKFLOW_DISPLAY="$workflow"
        WORKFLOW_CONFIG="$workflow"
        WORKFLOW_NAME="$workflow"
      fi
    done <<< "$WORKFLOW_NAMES"
    [ "${#WORKFLOW_ARGS[@]}" -gt 0 ] \
      || die "TARTCI_RUNNER_WORKFLOW_NAMES contains no workflow names"
  else
    WORKFLOW_ARGS=(--workflow "$WORKFLOW_NAME")
    WORKFLOW_DISPLAY="$WORKFLOW_NAME"
    WORKFLOW_CONFIG="$WORKFLOW_NAME"
  fi
}

# shellcheck source=providers/common/vm-lease.lib.sh
source "$TARTCI_ROOT/providers/common/vm-lease.lib.sh"
# shellcheck source=providers/common/vm-state.lib.sh
source "$TARTCI_ROOT/providers/common/vm-state.lib.sh"
# shellcheck source=providers/common/host-health.lib.sh
source "$TARTCI_ROOT/providers/common/host-health.lib.sh"
# shellcheck source=providers/common/admission-clean.lib.sh
source "$TARTCI_ROOT/providers/common/admission-clean.lib.sh"

usage(){ sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; }

# Per-BOOT GitHub runner registration name: <lane>-<supervisor pid>-<boot index>,
# mirroring the qemu-windows lane (`${RUNNER_NAME_PREFIX}-$$-$i`). This must be
# unique for every boot and never reused. Rationale: a fixed static name (the bare
# $RUNNER_NAME, e.g. `pulp-vm-01`) is reused across boots AND supervisor restarts,
# so a SIGKILL'd VM (kickstart / yield / crash) orphans a GitHub runner registration
# stuck "offline but running a job". The next boot then collides on that name
# (`generate-jitconfig` → HTTP 409 "already exists"), and reclaim_runner_name can't
# clear it without repo-admin — wedging the ENTIRE macOS gate until an admin deletes
# the ghost by hand (pulp-runner-ops "Sixth symptom", 2026-07-06). An
# never-reused name makes the collision impossible: a dead VM's registration just
# ages out. $$ is the supervisor PID even inside command substitution (bash keeps it
# the top-level shell's PID); $1 is the monotonic per-boot index.
ephemeral_boot_name(){ printf '%s-%s-%s' "$RUNNER_NAME" "$$" "$1"; }

while [ $# -gt 0 ]; do case "$1" in
  --loop) LOOP=1; shift;;
  --once) LOOP=0; shift;;
  --golden) GOLDEN="$2"; shift 2;;
  --labels) LABELS="$2"; shift 2;;
  --repo) REPO="$2"; shift 2;;
  --cap) CAP="$2"; shift 2;;
  --name) RUNNER_NAME="$2"; shift 2;;
  --name-prefix) RUNNER_NAME_PREFIX="$2"; shift 2;;
  --slot) SLOT="$2"; shift 2;;
  --state-dir) STATE_DIR="$2"; EVENT_LOG_EXPLICIT=0; shift 2;;
  --print-name) PRINT_NAME=1; shift;;
  --print-event-log) PRINT_EVENT_LOG=1; shift;;
  --print-identity) PRINT_IDENTITY=1; shift;;
  --print-boot-name) PRINT_BOOT_NAME="$2"; shift 2;;  # debug/test: emit ephemeral_boot_name <i>
  --print-queue) PRINT_QUEUE=1; shift;;
  --print-selection) PRINT_SELECTION=1; shift;;
  --print-higher-priority-demand) PRINT_HIGHER_PRIORITY="$2"; shift 2;;
  --print-priority-demand) PRINT_PRIORITY=1; shift;;
  --print-host-health) PRINT_HOST_HEALTH=1; shift;;
  --print-runner-version) PRINT_RUNNER_VERSION=1; shift;;
  --print-network-policy) PRINT_NETWORK_POLICY=1; shift;;
  --yield-to-workflow) YIELD_WORKFLOW="$2"; shift 2;;
  --yield-to-labels) YIELD_LABELS="$2"; shift 2;;
  -h|--help) usage; exit 0;;
  *) die "unknown arg: $1";;
esac; done

case "$RUNNER_VERSION" in
  ''|*[!0-9.]*) die "invalid Actions Runner version: $RUNNER_VERSION";;
esac
[ "$PRINT_RUNNER_VERSION" = 1 ] && { printf '%s\n' "$RUNNER_VERSION"; exit 0; }
case "$RUNNER_SHA256" in
  ''|*[!0-9a-fA-F]*) die "set a 64-character TARTCI_RUNNER_SHA256 when overriding Actions Runner version $RUNNER_VERSION";;
esac
[ "${#RUNNER_SHA256}" -eq 64 ] || die "TARTCI_RUNNER_SHA256 must contain 64 hexadecimal characters"

if [ "$PRINT_NETWORK_POLICY" = 1 ]; then
  python3 - "$SOFTNET_PROXY_ONLY" "$EFFECTIVE_GUEST_HTTP_PROXY" "$HOST_CCACHE" "${TART_NETWORK_ARGS[@]}" <<'PY'
import json
import sys

enabled = sys.argv[1] == "1"
host_ccache = sys.argv[3] == "1"
print(json.dumps({
    "version": 1,
    "enforced": enabled,
    "scope": "tart-macos-disposable-vm",
    "default": "deny" if enabled else "allow",
    "guest_proxy": sys.argv[2] or None,
    "gateway_allow": [],
    "host_initiated_stateful_allow": enabled,
    "implicit_bootstrap": ["dhcp_v4_client"] if enabled else [],
    "non_ipv4_egress": "drop_by_softnet_0.23.0" if enabled else None,
    "proxy_transport": "host-initiated-ssh-reverse-forward" if enabled else None,
    "writable_host_mounts": ["ccache"] if host_ccache else [],
    "tart_run_args": sys.argv[4:],
}, sort_keys=True))
PY
  exit 0
fi

configure_workflows
CURRENT_LABELS="$LABELS"

IDENTITY_JSON="$(python3 "$TARTCI_ROOT/scripts/macos_runner_identity.py" \
  --name "$RUNNER_NAME" \
  --name-prefix "$RUNNER_NAME_PREFIX" \
  --slot "$SLOT" \
  --labels "$LABELS" \
  --state-dir "$STATE_DIR" \
  --home "$HOME" \
  --hostname "$HOST_NAME")" \
  || die "could not derive macOS runner identity"
RUNNER_NAME="$(printf '%s' "$IDENTITY_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["runner_name"])')"
STATE_DIR="$(printf '%s' "$IDENTITY_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["state_dir"])')"
[ "$EVENT_LOG_EXPLICIT" = 1 ] || EVENT_LOG="$STATE_DIR/events.jsonl"
[ "$PRINT_IDENTITY" = 1 ] && { printf '%s\n' "$IDENTITY_JSON"; exit 0; }
[ "$PRINT_NAME" = 1 ] && { printf '%s\n' "$RUNNER_NAME"; exit 0; }
[ "$PRINT_EVENT_LOG" = 1 ] && { printf '%s\n' "$EVENT_LOG"; exit 0; }
[ -n "${PRINT_BOOT_NAME:-}" ] && { printf '%s\n' "$(ephemeral_boot_name "$PRINT_BOOT_NAME")"; exit 0; }
if [ -n "${TARTCI_LAUNCHD_LABEL:-}" ]; then
  python3 "$TARTCI_ROOT/scripts/macos_runner_identity_guard.py" \
    --current-label "$TARTCI_LAUNCHD_LABEL" \
    --runner-name "$RUNNER_NAME" \
    --state-dir "$STATE_DIR" \
    || die "another loaded LaunchAgent resolves to this runner/state identity"
fi
command -v tart >/dev/null 2>&1 || die "tart not installed"
command -v "$GH_CLI" >/dev/null 2>&1 || die "GitHub CLI '$GH_CLI' (TARTCI_GH_CLI) not installed / authed (need repo admin to mint JIT config)"
mkdir -p "$STATE_DIR"

json_sanitize(){ printf '%s' "$1" | tr '\n\r\t"' '    '; }
event(){
  local kind="$1" detail="${2:-}" ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '{"ts":"%s","event":"%s","runner":"%s","vm":"%s","detail":"%s"}\n' \
    "$ts" "$(json_sanitize "$kind")" "$(json_sanitize "$RUNNER_NAME")" \
    "$(json_sanitize "${CURRENT_VM:-}")" "$(json_sanitize "$detail")" >>"$EVENT_LOG"
}

heartbeat(){
  local phase="$1" ts state_file tmp_file
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  state_file="$STATE_DIR/$RUNNER_NAME.state.json"
  tmp_file="$(mktemp "$state_file.tmp.XXXXXX")" || return 1
  if cat >"$tmp_file" <<EOF
{"ts":"$ts","provider":"tart-macos","host":"$(json_sanitize "$HOST_NAME")","runner":"$RUNNER_NAME","vm":"${CURRENT_VM:-}","vm_ip":"$(json_sanitize "${CURRENT_IP:-}")","phase":"$(json_sanitize "$phase")","lifecycle":"ephemeral","labels":"$(json_sanitize "$CURRENT_LABELS")","repo":"$(json_sanitize "$REPO")","run_id":"$(json_sanitize "${CURRENT_RUN_ID:-}")","job_id":"$(json_sanitize "${CURRENT_JOB_ID:-}")","supervisor_pid":"$SUPERVISOR_PID","supervisor_pid_started_at":"$(json_sanitize "$SUPERVISOR_PID_STARTED_AT")"}
EOF
  then
    mv -f "$tmp_file" "$state_file"
  else
    rm -f "$tmp_file"
    return 1
  fi
}

runtime_emit_complete(){
  [ "${TARTCI_RUNTIME_MEASURE:-0}" = 1 ] || return 0
  local status="$1" failure_class="$2" exit_code="$3" timing_path="$4" log_dir="$5"
  python3 "$TARTCI_ROOT/scripts/runtime_measure.py" complete \
    --repo "$REPO" \
    --workflow "${CURRENT_WORKFLOW_NAME:-$WORKFLOW_NAME}" \
    --provider tart-macos \
    --platform macos \
    --arch arm64 \
    --runner-name "$RUNNER_NAME" \
    --vm-name "${CURRENT_VM:-$RUNNER_NAME}" \
    --labels "$CURRENT_LABELS" \
    --run-id "${CURRENT_RUN_ID:-}" \
    --job-id "${CURRENT_JOB_ID:-}" \
    --golden "$GOLDEN" \
    --cache-mode unknown \
    --cache-mode-source unknown \
    --status "$status" \
    --failure-class "$failure_class" \
    --exit-code "$exit_code" \
    --timing-path "$timing_path" \
    --log-dir "$log_dir" \
    --json >/dev/null 2>&1 || note "runtime measurement emit failed (ignored)"
}

running_macos_vms(){
  local payload fail_closed
  fail_closed="${TARTCI_MACOS_HARD_MAX:-2}"
  if ! payload="$(tart list --format json 2>/dev/null)"; then
    printf '%s\n' "$fail_closed"
    return 0
  fi
  TARTCI_TART_LIST_JSON="$payload" python3 - "$fail_closed" <<'PY' || printf '%s\n' "$fail_closed"
import json, os, subprocess, sys
try:
    vms = json.loads(os.environ["TARTCI_TART_LIST_JSON"])
except Exception:
    print(sys.argv[1])
    raise SystemExit
n = 0
for vm in vms if isinstance(vms, list) else []:
    if not str(vm.get("State", vm.get("state", ""))).lower().startswith("run"):
        continue
    name = vm.get("Name") or vm.get("name")
    os_name = ""
    if name:
        try:
            out = subprocess.check_output(["tart", "get", str(name), "--format", "json"], text=True, stderr=subprocess.DEVNULL)
            os_name = str(json.loads(out).get("OS", "")).lower()
        except Exception:
            os_name = ""
    # Missing OS is conservative: count it against the macOS cap.
    if os_name in ("", "darwin", "macos"):
        n += 1
print(n)
PY
}

queued_work(){
  if [ -n "$WORKFLOW_TIERS" ]; then
    local tier_labels q total=0
    while IFS= read -r tier_labels; do
      [ -n "$tier_labels" ] || continue
      q="$(tier_queued_work "$tier_labels")" || { printf '%s\n' ERR; return 0; }
      printf '%s' "$q" | grep -qxE '[0-9]+' || { printf '%s\n' ERR; return 0; }
      total=$((total + q))
    done <<< "$TIER_LABELS_CONFIG"
    printf '%s\n' "$total"
    return 0
  fi
  python3 "$TARTCI_ROOT/scripts/queue_scan.py" \
    --repo "$REPO" \
    "${WORKFLOW_ARGS[@]}" \
    --labels "$LABELS" \
    --provider tart-macos \
    --lane-id "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}" \
    --state-file "$STATE_DIR/queue-scan.json" \
    --shared-cache-file "${TARTCI_SHARED_QUEUE_CACHE:-$HOME/.tartci/state/queue-discovery.json}" \
    --max-age-seconds 0 \
    --min-age-seconds "$MIN_QUEUED_AGE" \
    --match-labels 1 2>/dev/null || echo ERR
}

tier_workflow_args(){
  local selected="$1" entry tier_labels workflow
  while IFS= read -r entry; do
    entry="${entry%$'\r'}"
    [ -n "$entry" ] || continue
    tier_labels="${entry%%|*}"
    workflow="${entry#*|}"
    [ "$tier_labels" = "$selected" ] && printf '%s\n' "$workflow"
  done <<< "$WORKFLOW_TIERS"
}

tier_queued_work(){
  local tier_labels="$1" force_refresh="${2:-0}" workflow tier_args=() scan_cmd=()
  while IFS= read -r workflow; do
    [ -n "$workflow" ] && tier_args+=(--workflow "$workflow")
  done < <(tier_workflow_args "$tier_labels")
  [ "${#tier_args[@]}" -gt 0 ] || return 1
  scan_cmd=(python3 "$TARTCI_ROOT/scripts/queue_scan.py" \
    --repo "$REPO" \
    "${tier_args[@]}" \
    --labels "$LABELS,$tier_labels" \
    --provider tart-macos-tier \
    --lane-id "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}-$tier_labels" \
    --state-file "$STATE_DIR/queue-scan.json" \
    --shared-cache-file "${TARTCI_SHARED_QUEUE_CACHE:-$HOME/.tartci/state/queue-discovery.json}" \
    --max-age-seconds 0 \
    --min-age-seconds "$MIN_QUEUED_AGE")
  [ "$force_refresh" = 1 ] && scan_cmd+=(--force-refresh)
  "${scan_cmd[@]}" --match-labels 1 2>/dev/null
}

# Print `count|registration labels|zero-based tier`. A scan error at any tier is
# fail-closed: never skip a blind higher class and hand its capacity to a lower
# one. With no tier config this is the legacy single-label queue scan.
select_work(){
  local tier_labels q tier=0
  if [ -z "$WORKFLOW_TIERS" ]; then
    q="$(queued_work)"
    printf '%s|%s|0\n' "$q" "$LABELS"
    return 0
  fi
  while IFS= read -r tier_labels; do
    [ -n "$tier_labels" ] || continue
    if ! q="$(tier_queued_work "$tier_labels")"; then
      printf 'ERR|%s|%s\n' "$LABELS" "$tier"
      return 0
    fi
    if ! printf '%s' "$q" | grep -qxE '[0-9]+'; then
      printf 'ERR|%s|%s\n' "$LABELS" "$tier"
      return 0
    fi
    if [ "$q" -gt 0 ]; then
      printf '%s|%s,%s|%s\n' "$q" "$LABELS" "$tier_labels" "$tier"
      return 0
    fi
    tier=$((tier + 1))
  done <<< "$TIER_LABELS_CONFIG"
  printf '0|%s|%s\n' "$LABELS" "$tier"
}

higher_priority_demand(){
  local selected_tier="$1" tier_labels q tier=0
  [ "$selected_tier" -gt 0 ] || return 1
  while IFS= read -r tier_labels; do
    [ -n "$tier_labels" ] || continue
    [ "$tier" -lt "$selected_tier" ] || break
    q="$(tier_queued_work "$tier_labels" 1)" || return 0
    printf '%s' "$q" | grep -qxE '[0-9]+' || return 0
    [ "$q" -eq 0 ] || return 0
    tier=$((tier + 1))
  done <<< "$TIER_LABELS_CONFIG"
  return 1
}

# priority_demand — how many jobs a higher-priority lane currently has WAITING or
# RUNNING. Used by the opt-in idle gate so a secondary lane yields its VM slot.
#
# Returns 0 (and never calls gh) when the feature is OFF (YIELD_WORKFLOW empty),
# so the primary gate runner / release lane are unaffected. When ON, it counts
# queued + in_progress jobs of the YIELD_WORKFLOW whose requested labels are a
# SUBSET of the priority lane's labels (YIELD_LABELS) — GitHub's assignment rule:
# a runner serves a job iff it advertises every label the job requests. We scan
# BOTH queued and in_progress because a priority run can flip to in_progress
# (its GitHub-hosted resolver/classify job) before its self-hosted leg is queued.
#
# Use the same host-shared discovery cache as queued_work. Priority lanes often
# watch a second workflow (for example Sanitizers yielding to Build and Test);
# queue_scan budgets two shared workflows per host and serializes their refreshes.
# Non-zero output means "a priority job needs a slot — do not boot the secondary
# VM".
priority_demand(){
  [ -n "$YIELD_WORKFLOW" ] || { printf '%s\n' 0; return 0; }
  # FAIL CLOSED. If we cannot read priority-lane demand, assume there IS demand
  # (print 1 → the loop gate yields) rather than booting blind. gh errors
  # (rate-limit / 5xx) cluster during exactly the load spikes when the gate most
  # needs its slot, so a fail-OPEN guard would let the secondary grab the gate's
  # slot precisely when that is most harmful. Worst case of fail-closed is an
  # advisory lane that idles during a gh outage — strictly safer than risking the
  # required gate. (`local x=$(...)` masks the substitution's exit code, so vars
  # are declared first and assigned separately so `||` actually fires.)
  python3 "$TARTCI_ROOT/scripts/queue_scan.py" \
    --repo "$REPO" \
    --workflow "$YIELD_WORKFLOW" \
    --labels "$YIELD_LABELS" \
    --job-statuses queued,in_progress \
    --provider tart-macos-priority \
    --lane-id "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}-priority" \
    --state-file "$STATE_DIR/priority-queue-scan.json" \
    --shared-cache-file "${TARTCI_SHARED_QUEUE_CACHE:-$HOME/.tartci/state/queue-discovery.json}" \
    --max-age-seconds 0 \
    --match-labels 1 2>/dev/null \
    || { printf '%s\n' 1; return 0; }
}

reclaim_runner_name(){
  local name="$1" id attempt inventory
  # Attempts 1-18 may delete; attempt 19 is the final read-only absence proof.
  for attempt in $(seq 1 19); do
    inventory="$(capture_command_bounded 20 "$GH_CLI" api --paginate --slurp \
      "repos/$REPO/actions/runners?per_page=100")" || {
      note "runner inventory is indeterminate while reclaiming '$name'"
      return 1
    }
    id="$(TARTCI_RUNNER_INVENTORY="$inventory" python3 - "$name" <<'PY'
import json
import os
import sys

name = sys.argv[1]
try:
    pages = json.loads(os.environ["TARTCI_RUNNER_INVENTORY"])
except Exception:
    raise SystemExit(1)
if not isinstance(pages, list):
    raise SystemExit(1)
for page in pages:
    if not isinstance(page, dict) or not isinstance(page.get("runners"), list):
        raise SystemExit(1)
    for runner in page["runners"]:
        if isinstance(runner, dict) and runner.get("name") == name:
            print(runner.get("id", ""))
            raise SystemExit(0)
PY
    )" || return 1
    [ -n "$id" ] || return 0
    [ "$attempt" -lt 19 ] || break
    note "reclaiming static name '$name': deleting stale runner registration (id=$id attempt=$attempt)"
    capture_command_bounded 20 "$GH_CLI" api -X DELETE \
      "repos/$REPO/actions/runners/$id" >/dev/null || true
    sleep 10
  done
  note "runner registration absence is unconfirmed for '$name'"
  return 1
}

stop_current_aqua_runner(){
  local stop_pid
  if [ -n "$CURRENT_IP" ] && [ -n "$CURRENT_AQUA_LABEL" ]; then
    ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$CURRENT_IP" \
      "\$HOME/.tartci/bin/guest-aqua-runner.sh stop '$CURRENT_AQUA_LABEL'" \
      >/dev/null 2>&1 & stop_pid=$!
    wait_process_bounded "$stop_pid" 10
  fi
}

tart_vm_exists_or_unknown(){
  local vm="$1" inventory
  inventory="$(capture_command_bounded 10 tart list --format json)" || return 0
  TARTCI_VM_INVENTORY="$inventory" python3 - "$vm" <<'PY'
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

vm_lease_absence_proved(){
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

release_current_vm_lease_proved(){
  local lease_id="${TARTCI_ACTIVE_VM_LEASE_ID:-}"
  local lease_cores="${TARTCI_ACTIVE_VM_LEASE_CORES:-}"
  [ -n "$lease_id" ] || return 0
  tartci_release_vm_lease
  if vm_lease_absence_proved "$lease_id"; then
    return 0
  fi
  # Fail closed. If release failed, the supervisor is still the recorded owner;
  # restore its process-local identity and heartbeat instead of admitting a new
  # writer. If status itself failed after a successful release, the next cleanup
  # pass will prove absence before clearing this conservative hold.
  TARTCI_ACTIVE_VM_LEASE_ID="$lease_id"
  TARTCI_ACTIVE_VM_LEASE_CORES="$lease_cores"
  tartci_start_vm_lease_heartbeat "$lease_id"
  note "durable lease absence is unconfirmed for $lease_id; preserving authority"
  event teardown_unconfirmed "lease=$lease_id lease_preserved=true"
  TEARDOWN_BLOCKED=1
  return 1
}

discard_current_vm(){
  local stop_pid delete_rc=0
  [ -n "$CURRENT_VM" ] || return 0
  note "stopping — tearing down in-flight VM $CURRENT_VM"
  stop_current_aqua_runner
  if [ -n "$CURRENT_PROXY_TUNNEL_PID" ]; then
    terminate_process_bounded "$CURRENT_PROXY_TUNNEL_PID" 5
    CURRENT_PROXY_TUNNEL_PID=""
  fi
  tart stop "$CURRENT_VM" >/dev/null 2>&1 & stop_pid=$!
  wait_process_bounded "$stop_pid" 10
  if [ -n "$CURRENT_RPID" ] && ! wait_process_exit_bounded "$CURRENT_RPID" 10; then
    note "VM guardian remains live after stop; preserving identity and lease for $CURRENT_VM"
    event teardown_unconfirmed "vm=$CURRENT_VM guardian_live=true lease_preserved=true"
    TEARDOWN_BLOCKED=1
    return 1
  fi
  CURRENT_RPID=""
  tartci_vm_lease_guard_run tart delete "$CURRENT_VM" >/dev/null 2>&1 || delete_rc=$?
  if tart_vm_exists_or_unknown "$CURRENT_VM"; then
    note "VM deletion is unconfirmed (delete_rc=$delete_rc); preserving identity and lease for $CURRENT_VM"
    event teardown_unconfirmed "vm=$CURRENT_VM delete_rc=$delete_rc lease_preserved=true"
    TEARDOWN_BLOCKED=1
    return 1
  fi
  CURRENT_VM=""
  CURRENT_RPID=""
  CURRENT_IP=""
  CURRENT_AQUA_LABEL=""
}

start_proxy_tunnel(){
  local ip="$1"
  [ "$SOFTNET_PROXY_ONLY" = 1 ] || return 0
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" \
    -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o ForwardAgent=no -o ForwardX11=no -o RequestTTY=no \
    -R "127.0.0.1:$GUEST_PROXY_PORT:127.0.0.1:$GUEST_PROXY_PORT" \
    "$VM_USER@$ip" >/dev/null 2>&1 &
  CURRENT_PROXY_TUNNEL_PID=$!
  sleep 1
  kill -0 "$CURRENT_PROXY_TUNNEL_PID" 2>/dev/null || {
    wait "$CURRENT_PROXY_TUNNEL_PID" 2>/dev/null || true
    CURRENT_PROXY_TUNNEL_PID=""
    return 1
  }
}

guest_probe_must_be_blocked(){
  local ip="$1" command="$2" marker output
  marker="TARTCI_EXPECTED_BLOCKED_${RANDOM}_$$"
  output="$(ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "if $command; then exit 42; else printf '%s' '$marker'; fi")" || return 1
  [ "$output" = "$marker" ]
}

verify_proxy_boundary(){
  local ip="$1"
  [ "$SOFTNET_PROXY_ONLY" = 1 ] || return 0
  # Softnet currently enforces IPv4 rules only. Refuse an image with an IPv6
  # default route rather than silently exposing an ungoverned escape path.
  ! ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    'route -n get -inet6 default >/dev/null 2>&1' || return 1
  local no_proxy='env -u HTTPS_PROXY -u HTTP_PROXY -u ALL_PROXY -u https_proxy -u http_proxy -u all_proxy'
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    'command -v curl >/dev/null && command -v nc >/dev/null' || return 1
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "HTTPS_PROXY='$EFFECTIVE_GUEST_HTTP_PROXY' NO_PROXY=localhost curl -fsS --max-time 20 https://github.com/robots.txt >/dev/null" \
    || return 1
  guest_probe_must_be_blocked "$ip" \
    "$no_proxy curl --noproxy '*' -fsS --connect-timeout 3 --max-time 5 https://github.com/robots.txt >/dev/null 2>&1" \
    || return 1
  guest_probe_must_be_blocked "$ip" \
    "nc -z -w 3 192.168.64.1 '$GUEST_PROXY_PORT' >/dev/null 2>&1" \
    || return 1
  guest_probe_must_be_blocked "$ip" \
    "$no_proxy curl --noproxy '*' -kfsS --connect-timeout 3 --max-time 5 https://1.1.1/ >/dev/null 2>&1" \
    || return 1
}

proxy_connect_probe(){
  local target="$1"
  python3 - "$GUEST_PROXY_PORT" "$target" <<'PY'
import socket
import sys

port = int(sys.argv[1])
target = sys.argv[2]
with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
    sock.settimeout(5)
    request = f"CONNECT {target}:443 HTTP/1.1\r\nHost: {target}:443\r\n\r\n"
    sock.sendall(request.encode("ascii"))
    response = b""
    while b"\r\n\r\n" not in response and len(response) < 4096:
        chunk = sock.recv(4096 - len(response))
        if not chunk:
            break
        response += chunk
raise SystemExit(0 if response.startswith(b"HTTP/1.1 200") else 1)
PY
}

host_proxy_endpoint_healthy_bounded(){
  local attempt
  for attempt in 1 2 3; do
    if proxy_connect_probe github.com || proxy_connect_probe crates.io; then
      return 0
    fi
    [ "$attempt" = 3 ] || sleep 1
  done
  return 1
}

cleanup(){
  local teardown_vm="${CURRENT_VM:-${CURRENT_REGISTERED_RUNNER:-}}"
  [ -z "$teardown_vm" ] || PENDING_STATIC_RUNNER_RECLAIM=1
  tartci_pool_lock_release
  [ "$CLEANED_UP" = 1 ] && return 0
  discard_current_vm || { heartbeat cleanup-blocked; return 1; }
  [ -n "${CURRENT_RESV:-}" ] && rm -f "$CURRENT_RESV" 2>/dev/null || true
  if [ -n "$CURRENT_REGISTERED_RUNNER" ]; then
    reclaim_runner_name "$CURRENT_REGISTERED_RUNNER" \
      || { TEARDOWN_BLOCKED=1; heartbeat cleanup-blocked; return 1; }
    CURRENT_REGISTERED_RUNNER=""
  fi
  if [ "$PENDING_STATIC_RUNNER_RECLAIM" = 1 ]; then
    reclaim_runner_name "$RUNNER_NAME" \
      || { TEARDOWN_BLOCKED=1; heartbeat cleanup-blocked; return 1; }
    PENDING_STATIC_RUNNER_RECLAIM=0
  fi
  release_current_vm_lease_proved || { heartbeat cleanup-blocked; return 1; }
  if [ -n "$teardown_vm" ]; then
    CURRENT_VM="$teardown_vm"
    event teardown "cleanup=signal vm_absent=true runner_absent=true lease_absent=true"
    CURRENT_VM=""
  fi
  CLEANED_UP=1
  heartbeat stopped
}

hold_teardown_until_proved(){
  event teardown_hold "same_supervisor=true"
  trap '' INT TERM
  while [ "$TEARDOWN_BLOCKED" = 1 ]; do
    heartbeat cleanup-blocked
    note "teardown authority held by supervisor PID $$; retrying exact cleanup in 30s"
    sleep 30
    if cleanup; then
      TEARDOWN_BLOCKED=0
      event teardown_hold_recovered "same_supervisor=true"
      trap handle_supervisor_signal INT TERM
      return 0
    fi
  done
}

handle_supervisor_signal(){
  trap '' INT TERM
  event supervisor_signal "INT/TERM"
  if ! cleanup; then
    TEARDOWN_BLOCKED=1
    hold_teardown_until_proved
  fi
  trap - EXIT
  exit 143
}

handle_supervisor_exit(){
  local rc=$?
  trap - EXIT
  trap '' INT TERM
  if ! cleanup; then
    TEARDOWN_BLOCKED=1
    hold_teardown_until_proved
  fi
  exit "$rc"
}

capture_current_job(){
  local run_id run_workflow job_id runner_registration
  CURRENT_RUN_ID=""
  CURRENT_JOB_ID=""
  CURRENT_WORKFLOW_NAME=""
  runner_registration="${CURRENT_REGISTERED_RUNNER:-$RUNNER_NAME}"
  while IFS=$'\t' read -r run_id run_workflow; do
    [ -n "$run_id" ] || continue
    job_id="$("$GH_CLI" api --paginate --slurp "repos/$REPO/actions/runs/$run_id/jobs?per_page=100" 2>/dev/null \
      | TARTCI_CAPTURE_RUNNER="$runner_registration" python3 -c '
import json, os, sys
runner = os.environ["TARTCI_CAPTURE_RUNNER"]
for page in json.load(sys.stdin):
    for job in page.get("jobs", []):
        if job.get("runner_name") == runner and job.get("status") == "in_progress":
            print(job.get("id", ""))
            raise SystemExit
' 2>/dev/null | head -n1 || true)"
    if [ -n "$job_id" ]; then
      CURRENT_RUN_ID="$run_id"
      CURRENT_JOB_ID="$job_id"
      CURRENT_WORKFLOW_NAME="$run_workflow"
      return 0
    fi
  done < <("$GH_CLI" api --paginate --slurp "repos/$REPO/actions/runs?status=in_progress&per_page=100" 2>/dev/null \
    | TARTCI_CAPTURE_WORKFLOWS="$WORKFLOW_CONFIG" python3 -c '
import json, os, sys
workflows = set(os.environ["TARTCI_CAPTURE_WORKFLOWS"].splitlines())
for page in json.load(sys.stdin):
    for run in page.get("workflow_runs", []):
        if run.get("name") in workflows and isinstance(run.get("id"), int):
            print("{}\t{}".format(run["id"], run["name"]))
' 2>/dev/null || true)
  return 1
}

cancel_current_run(){
  [ -n "$CURRENT_RUN_ID" ] || capture_current_job || true
  if [ -n "$CURRENT_RUN_ID" ]; then
    event run_cancel "run_id=$CURRENT_RUN_ID job_id=${CURRENT_JOB_ID:-} reason=timeout"
    "$GH_CLI" api -X POST "repos/$REPO/actions/runs/$CURRENT_RUN_ID/cancel" >/dev/null 2>&1 || true
  fi
}

cancel_current_run_bounded(){
  local cancel_pid
  cancel_current_run >/dev/null 2>&1 & cancel_pid=$!
  for _ in $(seq 1 10); do
    kill -0 "$cancel_pid" 2>/dev/null || { wait "$cancel_pid" 2>/dev/null || true; return 0; }
    sleep 1
  done
  terminate_process_tree "$cancel_pid" TERM
  sleep 1
  terminate_process_tree "$cancel_pid" KILL
  wait "$cancel_pid" 2>/dev/null || true
}

terminate_process_tree(){
  local pid="$1" signal="$2" child
  while IFS= read -r child; do
    [ -z "$child" ] || terminate_process_tree "$child" "$signal"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  kill -s "$signal" "$pid" 2>/dev/null || true
}

capture_command_bounded(){
  local seconds="$1" output_file command_pid rc=0
  shift
  output_file="$(mktemp -t tartci-capture)" || return 1
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

capture_command_bounded_allow_warning(){
  local seconds="$1" output_file command_pid rc=0
  shift
  output_file="$(mktemp -t tartci-capture-warning)" || return 1
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

terminate_process_bounded(){
  local pid="$1" seconds="${2:-10}"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 "$seconds"); do
    kill -0 "$pid" 2>/dev/null || { wait "$pid" 2>/dev/null || true; return 0; }
    sleep 1
  done
  kill -9 "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

wait_process_bounded(){
  local pid="$1" seconds="${2:-10}"
  for _ in $(seq 1 "$seconds"); do
    kill -0 "$pid" 2>/dev/null || { wait "$pid" 2>/dev/null || true; return 0; }
    sleep 1
  done
  kill "$pid" 2>/dev/null || true
  sleep 1
  kill -9 "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
}

wait_process_exit_bounded(){
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

ensure_runner_version(){
  local ip="$1"
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "bash -s -- '$RUNNER_VERSION' '$RUNNER_SHA256' '$EFFECTIVE_GUEST_HTTP_PROXY' '$SOFTNET_PROXY_ONLY'" <<'GUEST'
set -euo pipefail
desired="$1"
expected_sha256="$2"
guest_http_proxy="$3"
proxy_only="$4"
if [ -n "$guest_http_proxy" ]; then
  export HTTPS_PROXY="$guest_http_proxy" https_proxy="$guest_http_proxy"
  if [ "$proxy_only" != 1 ]; then
    export HTTP_PROXY="$guest_http_proxy" http_proxy="$guest_http_proxy"
  fi
  export NO_PROXY="127.0.0.1,localhost,::1" no_proxy="127.0.0.1,localhost,::1"
fi
runner_dir="$HOME/actions-runner"
listener="$runner_dir/bin/Runner.Listener"
current=""
if [ -x "$listener" ]; then
  current="$($listener --version 2>/dev/null | head -n1 || true)"
fi

if [ "$current" != "$desired" ]; then
  update_dir="$(mktemp -d "$HOME/actions-runner.update.XXXXXX")"
  backup_dir="$HOME/actions-runner.tartci-backup"
  cleanup_update(){ rm -rf "$update_dir"; }
  trap cleanup_update EXIT
  curl -fsSL --retry 3 --retry-all-errors --connect-timeout 15 --max-time 300 \
    "https://github.com/actions/runner/releases/download/v${desired}/actions-runner-osx-arm64-${desired}.tar.gz" \
    -o "$update_dir/runner.tar.gz"
  actual_sha256="$(shasum -a 256 "$update_dir/runner.tar.gz" | awk '{print $1}')"
  [ "$actual_sha256" = "$expected_sha256" ] || {
    printf 'Actions Runner archive SHA-256 %s does not match expected %s\n' "$actual_sha256" "$expected_sha256" >&2
    exit 1
  }
  tar -xzf "$update_dir/runner.tar.gz" -C "$update_dir"
  rm "$update_dir/runner.tar.gz"
  installed="$($update_dir/bin/Runner.Listener --version 2>/dev/null | head -n1 || true)"
  [ "$installed" = "$desired" ] || {
    printf 'downloaded Actions Runner version %s, expected %s\n' "$installed" "$desired" >&2
    exit 1
  }

  rm -rf "$backup_dir"
  if [ -d "$runner_dir" ]; then
    mv "$runner_dir" "$backup_dir"
    for preserved in .env; do
      [ ! -f "$backup_dir/$preserved" ] || cp "$backup_dir/$preserved" "$update_dir/$preserved"
    done
  fi
  mv "$update_dir" "$runner_dir"
  update_dir=""
  rm -rf "$backup_dir"
  trap - EXIT
fi

actual="$($runner_dir/bin/Runner.Listener --version 2>/dev/null | head -n1 || true)"
[ "$actual" = "$desired" ] || {
  printf 'Actions Runner version %s does not match required %s\n' "$actual" "$desired" >&2
  exit 1
}
rm -f "$runner_dir/.runner" "$runner_dir/.credentials" \
  "$runner_dir/.credentials_rsaparams" "$runner_dir/.path" "$runner_dir/jit.cfg"
printf 'TARTCI_DIAG actions-runner-version=%s\n' "$actual"
GUEST
}

run_runner_until_done(){
  local vm="$1" ip="$2" jit="$3"
  local runner_log="$STATE_DIR/$vm.actions-runner.log"
  local aqua_label="com.tartci.aqua.$vm"
  local ssh_pid start assigned_at=0 now idle_elapsed job_elapsed assigned=0 warned=0 rc=0
  local last_proxy_probe=0
  local cancel_pid="" tart_stop_pid=""
  : >"$runner_log"
  # Claim the guest secret/service cleanup target before the first byte crosses
  # SSH, so a failed stream or pending signal cannot leave an unowned JIT file.
  CURRENT_AQUA_LABEL="$aqua_label"
  if ! printf '%s' "$jit" | ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "umask 077; root=\"\$HOME/.tartci/aqua-runner/$aqua_label\"; mkdir -p \"\$root\"; cat >\"\$root/jit.cfg\""; then
    note "[$vm] failed to stream JIT config into the guest"
    stop_current_aqua_runner
    return 1
  fi
  jit=""
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "mkdir -p ~/.ccache-tmp && \
     if [ '$HOST_CCACHE' = 1 ]; then ln -sfn '/Volumes/My Shared Files/ccache' ~/Library/Caches/ccache; else if [ -L ~/Library/Caches/ccache ]; then rm -f ~/Library/Caches/ccache; fi; mkdir -p ~/Library/Caches/ccache; fi && \
     export CCACHE_NODEPEND=true CCACHE_COMPILERCHECK=content && unset CCACHE_DEPEND && \
     mkdir -p \"\$HOME/Library/Caches/Pulp/fetchcontent-src\" && \
     rsync -a '/Volumes/My Shared Files/fetchcontent/' \"\$HOME/Library/Caches/Pulp/fetchcontent-src/\" && \
     cd ~/actions-runner && touch .env && \
     awk -F= '\$1 !~ /^(CCACHE_DEPEND|CCACHE_NODEPEND|CCACHE_COMPILERCHECK|PULP_SHARED_FETCHCONTENT_SOURCE_DIR|FETCHCONTENT_BASE_DIR|HTTP_PROXY|HTTPS_PROXY|NO_PROXY|http_proxy|https_proxy|no_proxy)$/' .env > .env.tartci && \
     printf '%s\n' 'CCACHE_NODEPEND=true' 'CCACHE_COMPILERCHECK=content' >> .env.tartci && \
     printf 'PULP_SHARED_FETCHCONTENT_SOURCE_DIR=%s\n' \"\$HOME/Library/Caches/Pulp/fetchcontent-src\" >> .env.tartci && \
     if [ -n '$EFFECTIVE_GUEST_HTTP_PROXY' ]; then if [ '$SOFTNET_PROXY_ONLY' != 1 ]; then printf '%s\n' 'HTTP_PROXY=$EFFECTIVE_GUEST_HTTP_PROXY' 'http_proxy=$EFFECTIVE_GUEST_HTTP_PROXY' >> .env.tartci; fi; printf '%s\n' 'HTTPS_PROXY=$EFFECTIVE_GUEST_HTTP_PROXY' 'https_proxy=$EFFECTIVE_GUEST_HTTP_PROXY' 'NO_PROXY=127.0.0.1,localhost,::1' 'no_proxy=127.0.0.1,localhost,::1' >> .env.tartci; fi && \
     mv .env.tartci .env && \
     export PULP_SHARED_FETCHCONTENT_SOURCE_DIR=\"\$HOME/Library/Caches/Pulp/fetchcontent-src\" && \
     \$HOME/.tartci/bin/guest-aqua-runner.sh run '$aqua_label'" \
    >"$runner_log" 2>&1 & ssh_pid=$!
  start="$(date +%s)"
  while kill -0 "$ssh_pid" 2>/dev/null; do
    if [ "$SOFTNET_PROXY_ONLY" = 1 ] && \
       ! kill -0 "$CURRENT_PROXY_TUNNEL_PID" 2>/dev/null; then
      event proxy_tunnel_lost "vm=$vm run_id=${CURRENT_RUN_ID:-} job_id=${CURRENT_JOB_ID:-}"
      cancel_current_run_bounded & cancel_pid=$!
      terminate_process_bounded "$ssh_pid" 5
      tart stop "$CURRENT_VM" >/dev/null 2>&1 & tart_stop_pid=$!
      wait_process_bounded "$tart_stop_pid" 10
      [ -z "$CURRENT_RPID" ] || wait_process_exit_bounded "$CURRENT_RPID" 10 || true
      wait_process_bounded "$cancel_pid" 12
      return 125
    fi
    now="$(date +%s)"
    if [ "$SOFTNET_PROXY_ONLY" = 1 ] && [ $((now - last_proxy_probe)) -ge 30 ]; then
      if ! host_proxy_endpoint_healthy_bounded; then
        event proxy_endpoint_lost "vm=$vm run_id=${CURRENT_RUN_ID:-} job_id=${CURRENT_JOB_ID:-}"
        cancel_current_run_bounded & cancel_pid=$!
        terminate_process_bounded "$ssh_pid" 5
        tart stop "$CURRENT_VM" >/dev/null 2>&1 & tart_stop_pid=$!
        wait_process_bounded "$tart_stop_pid" 10
        [ -z "$CURRENT_RPID" ] || wait_process_exit_bounded "$CURRENT_RPID" 10 || true
        wait_process_bounded "$cancel_pid" 12
        return 125
      fi
      last_proxy_probe="$now"
    fi
    idle_elapsed=$((now - start))
    if [ "$assigned" = 0 ] && grep -q 'Running job:' "$runner_log" 2>/dev/null; then
      assigned=1
      tartci_pool_lock_release
      assigned_at="$now"
      for _ in $(seq 1 6); do
        capture_current_job && break
        sleep 2
      done
      event job_assigned "$(grep 'Running job:' "$runner_log" | tail -1)"
      heartbeat job-running
    fi
    if [ "$assigned" = 0 ] && [ "$idle_elapsed" -ge "$IDLE_TIMEOUT" ]; then
      event idle_timeout "elapsed=${idle_elapsed}s rerun_eligible=false"
      kill "$ssh_pid" 2>/dev/null || true
      wait "$ssh_pid" 2>/dev/null || true
      sed 's/^/[actions-runner] /' "$runner_log" >&2 || true
      return 124
    fi
    if [ "$assigned" = 1 ]; then
      job_elapsed=$((now - assigned_at))
      [ -n "$CURRENT_RUN_ID" ] || capture_current_job || true
      if [ "$warned" = 0 ] && [ "$job_elapsed" -ge "$JOB_WARN" ]; then
        warned=1
        event job_warn "elapsed=${job_elapsed}s"
      fi
      if [ "$job_elapsed" -ge "$JOB_TIMEOUT" ]; then
        event job_timeout "elapsed=${job_elapsed}s run_id=${CURRENT_RUN_ID:-} job_id=${CURRENT_JOB_ID:-} rerun_eligible=true"
        cancel_current_run
        for _ in $(seq 1 30); do
          kill -0 "$ssh_pid" 2>/dev/null || break
          sleep 2
        done
        kill -0 "$ssh_pid" 2>/dev/null && kill "$ssh_pid" 2>/dev/null || true
        wait "$ssh_pid" 2>/dev/null || true
        sed 's/^/[actions-runner] /' "$runner_log" >&2 || true
        return 124
      fi
    fi
    heartbeat "$([ "$assigned" = 1 ] && printf job-running || printf idle-wait)"
    sleep 5
  done
  wait "$ssh_pid" || rc=$?
  sed 's/^/[actions-runner] /' "$runner_log" >&2 || true
  return "$rc"
}

install_and_preflight_aqua_runner(){
  local ip="$1" vm="$2" aqua_label
  aqua_label="com.tartci.aqua.$vm"
  if ! ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    'umask 077; mkdir -p ~/.tartci/bin; cat > ~/.tartci/bin/guest-aqua-runner.sh; chmod 700 ~/.tartci/bin/guest-aqua-runner.sh' \
    <"$TARTCI_ROOT/providers/tart-macos/guest-aqua-runner.sh"; then
    note "[$vm] failed to install Aqua runner launcher"
    return 1
  fi
  if ! ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "\$HOME/.tartci/bin/guest-aqua-runner.sh preflight '$aqua_label'"; then
    note "[$vm] console Aqua session preflight failed — refusing to mint JIT config"
    return 1
  fi
}

run_one(){
  # Per-boot EPHEMERAL registration name (see ephemeral_boot_name) — never the bare
  # static $RUNNER_NAME, which would collide with an orphaned registration and wedge
  # the gate. $RUNNER_NAME stays the stable lane identity for state/heartbeat.
  local i="$1" selected_labels="${2:-$LABELS}" selected_tier="${3:-0}" vm
  vm="$(ephemeral_boot_name "$i")"
  local jit="" label_args=() labels_split=() l boot_log rpid ip="" rc=0
  local stop_pid="" delete_rc=0
  local lease_cores lease_priority
  local t_start t_booted t_runner_done t_done logdir=""
  t_start="$(now_epoch)"
  if [ "$SOFTNET_PROXY_ONLY" = 1 ] && \
     ! python3 "$TARTCI_ROOT/scripts/verify_macos_softnet_install.py" --path "$SOFTNET_BIN" >/dev/null; then
    note "[$i] pinned Softnet enrollment is not verified — refusing hardened VM boot"
    event boot_failed "softnet_install_unverified"
    return 1
  fi
  if [ "${TARTCI_RUNTIME_MEASURE:-0}" = 1 ]; then
    logdir="$MACOS_LOGROOT/$vm"
    tartci_prepare_disk_root "$logdir" || return $?
  fi
  tartci_check_disk_floor "$TART_HOME" || return $?
  if [ "$HOST_CCACHE" = 1 ]; then
    tartci_prepare_disk_root "$CACHE_ROOT" || return $?
    tartci_check_disk_floor "$CACHE_ROOT" || return $?
  fi
  CLEANED_UP=0
  CURRENT_REGISTERED_RUNNER=""
  CURRENT_RUN_ID=""
  CURRENT_JOB_ID=""
  CURRENT_WORKFLOW_NAME=""
  CURRENT_LABELS="$selected_labels"
  if ! reclaim_runner_name "$vm"; then
    note "[$i] pre-boot JIT runner absence is unconfirmed — refusing VM admission"
    event boot_deferred "runner_absence_unconfirmed=true unregistered=true"
    return 1
  fi
  lease_cores="$(tartci_vm_lease_cores tart-macos)"
  lease_mem="$(tartci_vm_lease_mem_mb tart-macos)"
  lease_priority="$(tartci_vm_lease_priority "$selected_labels")"
  tartci_acquire_vm_lease "$vm" "$lease_cores" "tart-macos-vm" "$lease_priority" "$selected_labels" "$lease_mem" "$TART_HOME" || return $?
  lease_cores="${TARTCI_ACTIVE_VM_LEASE_CORES:-$lease_cores}"

  note "[$i] clone $GOLDEN → $vm (CoW) + boot with host_ccache=$HOST_CCACHE"
  event clone_start "golden=$GOLDEN"
  # Own the unique per-boot name before the foreground clone so signal cleanup
  # cannot miss a clone completed immediately before the trap is delivered.
  CURRENT_VM="$vm"
  if ! tartci_vm_lease_guard_run tart clone "$GOLDEN" "$vm"; then
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    runtime_emit_complete fail boot_failed 1 "" "$logdir"
    return 1
  fi
  if ! tartci_set_tart_vm_cpu "$vm" "$lease_cores"; then
    note "[$i] failed to set $vm CPU count to lease cores=$lease_cores"
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    runtime_emit_complete fail boot_failed 1 "" "$logdir"
    return 1
  fi
  if [ "$HOST_CCACHE" = 1 ] && ! tartci_prepare_disk_root "$CACHE_ROOT/ccache"; then
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    runtime_emit_complete fail cache_setup_failed 1 "" "$logdir"
    return 1
  fi
  if ! tartci_prepare_disk_root "$FETCHCONTENT_SOURCE_ROOT"; then
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    runtime_emit_complete fail cache_setup_failed 1 "" "$logdir"
    return 1
  fi
  boot_log="$(mktemp -t "tart-run-$vm")"
  local tart_dirs=(--dir="fetchcontent:$FETCHCONTENT_SOURCE_ROOT:ro")
  [ "$HOST_CCACHE" = 0 ] || tart_dirs+=(--dir="ccache:$CACHE_ROOT/ccache")
  PATH="$(dirname "$SOFTNET_BIN"):$PATH" tartci_vm_lease_guard_run tart run "${TART_NETWORK_ARGS[@]}" \
    "${tart_dirs[@]}" \
    "$vm" >"$boot_log" 2>&1 & rpid=$!
  CURRENT_RPID="$rpid"
  heartbeat booting

  for _ in $(seq 1 60); do ip="$(tart ip "$vm" 2>/dev/null || true)"; [ -n "$ip" ] && break; sleep 2; done
  if [ -z "$ip" ]; then
    note "[$i] no IP after 120s — last tart run lines:"; tail -10 "$boot_log" >&2 2>/dev/null || true
    rm -f "$boot_log"; event boot_failed "no_ip"; runtime_emit_complete fail boot_failed 1 "" "$logdir"
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    return 1
  fi
  CURRENT_IP="$ip"
  rm -f "$boot_log"
  local sshok=0
  for _ in $(seq 1 90); do
    ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" true 2>/dev/null \
      && { sshok=1; break; }
    sleep 2
  done
  if [ "$sshok" != 1 ]; then
    note "[$i] no SSH after 180s — discarding unregistered VM"
    event boot_failed "no_ssh"
    runtime_emit_complete fail ssh_failed 1 "" "$logdir"
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    return 1
  fi
  if ! start_proxy_tunnel "$ip"; then
    note "[$i] proxy-only reverse forward failed — discarding unregistered VM"
    event boot_failed "proxy_tunnel"
    runtime_emit_complete fail proxy_tunnel_failed 1 "" "$logdir"
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    return 1
  fi
  if ! verify_proxy_boundary "$ip"; then
    note "[$i] proxy-only network proof failed before JIT mint — discarding unregistered VM"
    event boot_failed "proxy_boundary"
    runtime_emit_complete fail proxy_boundary_failed 1 "" "$logdir"
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    return 1
  fi
  t_booted="$(now_epoch)"
  if higher_priority_demand "$selected_tier"; then
    note "[$i] higher-priority workflow demand appeared during boot — discarding unregistered tier-$selected_tier VM"
    event yielded_to_workflow_tier "selected_tier=$selected_tier labels=$selected_labels"
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    CURRENT_LABELS="$LABELS"
    return 75
  fi
  if tartci_admission_clean_enabled; then
    local admission_json="" admission_rc=0
    heartbeat admission-check
    event admission_check "repo=$REPO labels=$selected_labels"
    if admission_json="$(tartci_admission_clean "$REPO" "$selected_labels")"; then
      admission_rc=0
    else
      admission_rc=$?
    fi
    [ -z "$admission_json" ] \
      || printf '%s\n' "$admission_json" >"$STATE_DIR/$vm.admission-clean.json"
    if [ "$admission_rc" -ne 0 ]; then
      heartbeat "$([ "$admission_rc" -eq 3 ] && printf admission-deferred || printf admission-error)"
      event "$([ "$admission_rc" -eq 3 ] && printf admission_deferred || printf admission_error)" \
        "rc=$admission_rc unregistered=true"
      note "[$i] Shipyard admission $([ "$admission_rc" -eq 3 ] && printf deferred || printf failed) — discarding unregistered VM and backing off"
      discard_current_vm || return 1
      release_current_vm_lease_proved || return 1
      return "$admission_rc"
    fi
  fi

  heartbeat ensuring-runner
  event runner_version "required=$RUNNER_VERSION"
  if ! ensure_runner_version "$ip"; then
    note "[$i] Actions Runner v$RUNNER_VERSION install/verification failed — discarding unregistered VM"
    event runner_version_failed "required=$RUNNER_VERSION"
    runtime_emit_complete fail runner_install_failed 1 "" "$logdir"
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    return 1
  fi

  heartbeat aqua-preflight
  event aqua_preflight "uid=501 vm=$vm"
  if ! install_and_preflight_aqua_runner "$ip" "$vm"; then
    event aqua_preflight_failed "uid=501 unregistered=true"
    runtime_emit_complete fail aqua_preflight_failed 1 "" "$logdir"
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    return 1
  fi

  heartbeat minting-jit
  if ! tartci_pool_lock_acquire; then
    note "[$i] pool transition busy before JIT mint — discarding unassigned VM"
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    return 75
  fi
  if ! tartci_pool_admission_open; then
    tartci_pool_lock_release
    note "[$i] pool $(tartci_pool_read_state) before JIT mint — discarding unassigned VM"
    discard_current_vm || return 1
    release_current_vm_lease_proved || return 1
    return 75
  fi
  event mint_jit "labels=$selected_labels tier=$selected_tier"
  # Claim the exact per-boot registration name before minting. Cleanup can then
  # reclaim it even when a signal lands immediately after GitHub creates it.
  CURRENT_REGISTERED_RUNNER="$vm"
  IFS=',' read -r -a labels_split <<< "$selected_labels"
  for l in "${labels_split[@]}"; do label_args+=(-f "labels[]=$l"); done
  jit="$("$GH_CLI" api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
        -f "name=$vm" -F "runner_group_id=$RUNNER_GROUP_ID" "${label_args[@]}" \
        --jq '.encoded_jit_config')" || {
    tartci_pool_lock_release
    note "[$i] JIT config mint failed — discarding VM"
    cleanup
    return 1
  }
  if [ -z "$jit" ]; then
    tartci_pool_lock_release
    note "[$i] empty JIT config — discarding VM"
    cleanup
    return 1
  fi
  note "[$i] vm $vm up at $ip — launching JIT runner (idle_timeout=${IDLE_TIMEOUT}s job_timeout=${JOB_TIMEOUT}s)"
  event boot_ok "ip=$ip"
  heartbeat idle-wait

  run_runner_until_done "$vm" "$ip" "$jit" || rc=$?
  tartci_pool_lock_release
  t_runner_done="$(now_epoch)"
  if [ "$rc" -ne 0 ]; then note "[$i] runner exited non-zero rc=$rc — VM will be discarded"; fi

  note "[$i] discarding ephemeral VM $vm"
  event teardown_start "rc=$rc"
  stop_current_aqua_runner
  if [ -n "$CURRENT_PROXY_TUNNEL_PID" ]; then
    terminate_process_bounded "$CURRENT_PROXY_TUNNEL_PID" 5
    CURRENT_PROXY_TUNNEL_PID=""
  fi
  tart stop "$vm" >/dev/null 2>&1 & stop_pid=$!
  wait_process_bounded "$stop_pid" 10
  if ! wait_process_exit_bounded "$rpid" 10; then
    note "[$i] VM guardian remains live after stop — preserving lease and runner identity"
    event teardown_unconfirmed "vm=$vm guardian_live=true lease_preserved=true"
    TEARDOWN_BLOCKED=1
    return 1
  fi
  CURRENT_RPID=""
  delete_rc=0
  tartci_vm_lease_guard_run tart delete "$vm" >/dev/null 2>&1 || delete_rc=$?
  if tart_vm_exists_or_unknown "$vm"; then
    note "[$i] VM deletion is unconfirmed (delete_rc=$delete_rc) — preserving lease and runner identity"
    event teardown_unconfirmed "vm=$vm delete_rc=$delete_rc lease_preserved=true"
    TEARDOWN_BLOCKED=1
    return 1
  fi
  if ! reclaim_runner_name "$vm"; then
    note "[$i] JIT runner deletion is unconfirmed — preserving identity and lease"
    event teardown_unconfirmed "vm=$vm runner_absence_unconfirmed=true lease_preserved=true"
    TEARDOWN_BLOCKED=1
    return 1
  fi
  CURRENT_REGISTERED_RUNNER=""
  t_done="$(now_epoch)"
  if [ "${TARTCI_RUNTIME_MEASURE:-0}" = 1 ]; then
    {
      printf 'phase\tseconds\n'
      printf 'boot_to_ssh\t%s\n' "$(elapsed "$t_start" "$t_booted")"
      printf 'runner_process\t%s\n' "$(elapsed "$t_booted" "$t_runner_done")"
      printf 'cleanup\t%s\n' "$(elapsed "$t_runner_done" "$t_done")"
      printf 'total\t%s\n' "$(elapsed "$t_start" "$t_done")"
    } >"$logdir/timing.tsv"
    if [ "$rc" -eq 0 ]; then
      runtime_emit_complete pass unknown 0 "$logdir/timing.tsv" "$logdir"
    elif [ "$rc" -eq 124 ]; then
      runtime_emit_complete fail runner_timeout "$rc" "$logdir/timing.tsv" "$logdir"
    else
      runtime_emit_complete fail source_failure "$rc" "$logdir/timing.tsv" "$logdir"
    fi
  fi
  release_current_vm_lease_proved || return 1
  event teardown "rc=$rc vm_absent=true runner_absent=true lease_absent=true"
  CURRENT_VM=""
  CURRENT_RPID=""
  CURRENT_IP=""
  CURRENT_AQUA_LABEL=""
  CURRENT_RUN_ID=""
  CURRENT_JOB_ID=""
  CURRENT_WORKFLOW_NAME=""
  CURRENT_LABELS="$LABELS"
  heartbeat stopped
  CLEANED_UP=1
  return 0
}

i=0
[ "$PRINT_QUEUE" = 1 ] && { queued_work; exit 0; }
[ "$PRINT_SELECTION" = 1 ] && { select_work | tr '|' '\t'; exit 0; }
[ -n "$PRINT_HIGHER_PRIORITY" ] && {
  if higher_priority_demand "$PRINT_HIGHER_PRIORITY"; then printf '1\n'; else printf '0\n'; fi
  exit 0
}
[ "$PRINT_PRIORITY" = 1 ] && { priority_demand; exit 0; }
[ "$PRINT_HOST_HEALTH" = 1 ] && { tartci_host_health_yield; exit 0; }
trap handle_supervisor_signal INT TERM
trap handle_supervisor_exit EXIT
tartci_validate_admission_clean_config "$REPO" "$LABELS" \
  || die "invalid required Shipyard admission-clean configuration"

# Part F — host-wide macOS VM cap (live, GUI-adjustable) + cross-lane mutex.
# shellcheck source=providers/tart-macos/macos-vm-cap.lib.sh
source "${BASH_SOURCE[0]%/*}/macos-vm-cap.lib.sh"

if [ "$LOOP" = 1 ]; then
  note "ephemeral macOS runner LOOP; golden=$GOLDEN labels=$LABELS workflows=$WORKFLOW_DISPLAY tiers=${TIER_LABELS_CONFIG:-<off>} cap=$CAP yield_to=${YIELD_WORKFLOW:-<off>} host_vitals_yield=${TARTCI_HOST_VITALS_YIELD:-<off>}"
  # Scan-blindness self-heal: `queued_work` prints `ERR` (not a count) when the gh queue scan fails.
  # Treating that as 0 silently idles the supervisor while jobs pile up (the observed multi-hour
  # wedge). Count consecutive blind polls; after ~this many seconds of continuous blindness,
  # self-restart so launchd (KeepAlive) respawns a fresh process with fresh gh auth — the exact
  # manual recovery, automated. A blip self-heals on the next successful poll (blind resets to 0).
  blind=0
  BLIND_MAX="${TARTCI_SCAN_BLIND_MAX:-$(( (180 + POLL - 1) / POLL ))}"
  heartbeat loop
  while true; do
    if ! tartci_pool_admission_open; then
      note "pool $(tartci_pool_read_state) — no new macOS admission; waiting ${POLL}s"
      heartbeat draining
      sleep "$POLL"
      continue
    fi
    selection="$(select_work)"
    IFS='|' read -r q selected_labels selected_tier <<< "$selection"
    cap="$(tartci_effective_cap)"; r="$(running_macos_vms)"
    # Blind-aware: a non-numeric `q` (ERR) means the gh queue scan FAILED — do NOT treat it as an
    # empty queue. Count consecutive blind polls; after a sustained window, self-restart for fresh
    # gh auth (the supervisor is idle at the loop top — run_one blocks — so cleanup discards no live
    # VM). Any successful poll resets the counter, so a transient blip costs nothing.
    if ! printf '%s' "$q" | grep -qxE '[0-9]+'; then
      blind=$((blind + 1))
      note "SCAN BLIND (gh queue scan failed) ${blind}/${BLIND_MAX} — NOT idling as empty (running_macos_vms=$r/$cap)"
      event scan_blind "consecutive=$blind running=$r/$cap"
      heartbeat scan_blind
      if [ "$blind" -ge "$BLIND_MAX" ]; then
        note "SCAN BLIND ~$((blind * POLL))s — self-restarting the supervisor for fresh gh auth (launchd KeepAlive respawns)"
        event scan_blind_restart "seconds=$((blind * POLL))"
        exit 75
      fi
      sleep "$POLL"; continue
    fi
    blind=0
    # Only probe priority demand when THIS lane actually has work — no point
    # spending a gh round-trip (and the API quota the secondary-rate-limit cares
    # about) to decide whether to yield a slot we wouldn't use anyway. Stays 0
    # when there's no work, and the feature is off entirely for the gate runner.
    p=0
    [ "${q:-0}" -gt 0 ] && p="$(priority_demand)"
    # Host-health yield: only worth probing when we actually have work to boot.
    # Cheap local check (no gh call), fail-open, and 0 when the feature is off.
    hh=0
    [ "${q:-0}" -gt 0 ] && hh="$(tartci_host_health_yield)"
    # Idle gate: boot only when (1) this lane has work, (2) a VM slot is free,
    # (3) no higher-priority lane is waiting/running, and (4) the host is healthy.
    # (3) is always satisfied when the priority feature is off (priority_demand
    # returns 0) and (4) when host-health yield is off (host_health_yield returns
    # 0), so this is a no-op for a runner with neither feature enabled.
    if [ "${q:-0}" -gt 0 ] && [ "${p:-0}" -eq 0 ] && [ "${hh:-0}" -eq 0 ] && resv="$(tartci_claim_macos_slot "$cap")" && [ -n "$resv" ]; then
      CURRENT_RESV="$resv"
      i=$((i+1)); note "[$i] queued=$q running_macos_vms=$r/$cap priority_demand=$p workflow_tier=$selected_tier labels=$selected_labels host_health_yield=$hh → booting ephemeral VM"
      if ! run_one "$i" "$selected_labels" "$selected_tier"; then
        [ "$TEARDOWN_BLOCKED" = 0 ] || hold_teardown_until_proved
        sleep "$POLL"
      fi
      CURRENT_LABELS="$LABELS"
      rm -f "$resv" 2>/dev/null || true; CURRENT_RESV=""
    elif [ "${q:-0}" -gt 0 ] && [ "${hh:-0}" -gt 0 ]; then
      note "yielding ${POLL}s (queued=$q host_health_yield=$hh running_macos_vms=$r/$cap) — host saturated, deferring new VM boot"
      event yielded_host_health "queued=$q host_health_yield=$hh running=$r/$cap"
      heartbeat yielding
      sleep "$POLL"
    elif [ "${q:-0}" -gt 0 ] && [ "${p:-0}" -gt 0 ]; then
      note "yielding ${POLL}s (queued=$q priority_demand=$p running_macos_vms=$r/$cap) — priority lane '${YIELD_WORKFLOW}' has the slot"
      event yielded_to_priority "workflow=$YIELD_WORKFLOW queued=$q priority_demand=$p running=$r/$cap"
      heartbeat yielding
      sleep "$POLL"
    else
      note "waiting ${POLL}s (queued=$q running_macos_vms=$r/$cap priority_demand=$p)"
      heartbeat waiting
      sleep "$POLL"
    fi
  done
else
  tartci_pool_admission_open || die "pool $(tartci_pool_read_state): refusing one-shot admission"
  selected_labels="$LABELS"; selected_tier=0
  if [ -n "$WORKFLOW_TIERS" ]; then
    selection="$(select_work)"
    IFS='|' read -r q selected_labels selected_tier <<< "$selection"
    printf '%s' "$q" | grep -qxE '[1-9][0-9]*' \
      || die "no queued workflow-tier work to select for --once"
  fi
  note "ephemeral macOS runner ONCE; golden=$GOLDEN labels=$selected_labels workflow_tier=$selected_tier"
  run_one 1 "$selected_labels" "$selected_tier" || {
    run_rc=$?
    [ "$TEARDOWN_BLOCKED" = 0 ] || hold_teardown_until_proved
    exit "$run_rc"
  }
fi
