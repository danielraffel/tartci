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
# `class-label|exact workflow name` entry per line. A matching optional
# TARTCI_RUNNER_WORKFLOW_TIER_GROUPS map contains `class-label|runner-group-id`
# entries, allowing protected merge-group jobs to use an organization group
# while PR-head jobs register at repository scope. First-seen labels are
# the priority order; workflows sharing a class label share one FIFO
# class. The runner scans each class in order and registers its JIT runner with
# only the selected class labels, so GitHub cannot assign lower-priority work to
# a runner reserved for a higher-priority class. Before minting a lower-tier JIT
# config, the supervisor rechecks every higher tier and discards the still-
# unregistered VM if higher-priority demand arrived during boot.
# Exclusive event-class assignment V2 is staged with
# TARTCI_RUNNER_ASSIGNMENT_MODE=legacy|observe|event-class-v2. `legacy` is the
# code default. `observe` preserves legacy minting while logging legacy/V2
# parity. V2 strips TARTCI_ASSIGNMENT_V2_OMIT_LABELS from the base, advertises
# exactly one allowed class, requires that class on the queued job, consumes all
# run/job pages fail closed, and freshly rechecks higher + selected demand at
# the final pre-mint boundary. See docs/assignment-v2-rollout.md.
# Work-conserving idle retarget (opt-in, V2 only): a registered runner serves
# exactly one class. TARTCI_ASSIGNMENT_V2_IDLE_RETARGET_SECS=N (0 = off) makes
# a runner that has sat unassigned for N seconds re-observe both classes; when
# its own class has no queued job at all and another class has admissible
# demand, the runner is discarded and the supervisor returns to its ordered
# selection, so the slot serves the waiting class instead of idling to the
# full idle timeout. Uncertainty holds; merge-group still wins when both wait.
# `--print-idle-retarget <tier>` reports that decision as a safe preflight.
# Per-slot class preference (opt-in, V2 only): TARTCI_ASSIGNMENT_V2_TIER_ORDER
# is a comma-separated permutation of the configured class labels. Selection,
# pre-mint admission, and idle retarget consult classes in that order instead
# of TARTCI_RUNNER_WORKFLOW_TIERS order; tier numbers stay the configured index,
# so events, runner groups and lease priority keep one meaning per class. Empty
# (the default) is the configured order, byte for byte.
# Fallback lane (opt-in, V2 only): TARTCI_FALLBACK_PEERS names the preferred
# hosts (`host_id=ssh-target`, comma or newline separated). While a class has
# queued work younger than TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS, the lane asks
# each preferred host for its free, leasable gate slots (`tartci pool supply`)
# and boots now only when queued demand exceeds what they and this host's
# sibling lanes already cover. Unknown or stale peer state keeps the minimum
# age rule, which stays the upper bound on the delay. Empty = off.
# `--print-fallback-decision <tier>` prints that decision as a safe preflight.
# Priority-aware idle gate (opt-in): set TARTCI_YIELD_TO_WORKFLOW_NAME +
# TARTCI_YIELD_TO_LABELS to make a SECONDARY lane yield its VM slot to a
# higher-priority lane. When set, the loop boots only when that priority lane
# has NO queued/in-progress work (in addition to the usual queue + cap checks),
# so on a shared-cap host (e.g. Apple's 2-running-macOS-guest limit) a long
# advisory job can never starve the required gate. Unset = no yielding, so the
# primary gate runner and existing lanes are byte-for-byte unchanged.
# `--print-priority-demand` reports that yield count (0 when the feature is off);
# a safe preflight for the gate.
# Bounded yield (opt-in): TARTCI_YIELD_MAX_WAIT_SECONDS=N (N>0) caps how long the
# idle gate above may hold this lane back. Once one of THIS lane's own queued
# jobs, in the class it selected, has been queued for at least N seconds (by the
# job's created_at), the lane stops yielding and takes a free slot on the next
# poll even while priority demand is non-zero. The host cap still applies: the
# bound only lifts the priority yield, never the slot claim or host-health yield.
# Unset/0 = unbounded yielding, byte-for-byte today's behavior. A failed age scan
# fails CLOSED (keep yielding), matching priority_demand. Tier and single-label
# lanes only; an event-class-v2 lane reports 0. `--print-yield-bound` reports how
# many selected jobs have exceeded the bound (0 when off) as a safe preflight.
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
# JIT registration is the only provider write that may need a narrowly-scoped
# host credential. Keep all polling and ordinary control-plane calls on GH_CLI
# (normally ghapp); an explicit JIT override is never an implicit PAT fallback.
JIT_GH_CLI="${TARTCI_JIT_GH_CLI:-$GH_CLI}"
SSH_KEY_PRIV="${TARTCI_VM_SSH_KEY:-${PULP_VM_SSH_KEY:-$HOME/.ssh/id_ed25519}}"
VM_USER="${TARTCI_VM_USER:-${PULP_VM_USER:-admin}}"
CACHE_ROOT="${TARTCI_CI_CACHE:-${PULP_CI_CACHE:-$HOME/.cache/pulp-ci}}"
CCACHE_MAX_SIZE="${TARTCI_CCACHE_MAX_SIZE:-40G}"
[[ "$CCACHE_MAX_SIZE" =~ ^[1-9][0-9]*[KMGT]$ ]] \
  || { printf 'invalid TARTCI_CCACHE_MAX_SIZE: expected a positive ccache size such as 40G\n' >&2; exit 1; }
FETCHCONTENT_SOURCE_ROOT="${PULP_SHARED_FETCHCONTENT_SOURCE_DIR:-$HOME/Library/Caches/Pulp/fetchcontent-src}"
# Optional read-only pip wheelhouse served to each guest. Opt-in by content: the
# mount is added only while the directory holds at least one wheel, so a host
# that never ran scripts/pip-wheelhouse.sh boots exactly as before.
PIP_WHEELHOUSE_ROOT="${TARTCI_PIP_WHEELHOUSE_DIR:-$CACHE_ROOT/pip-wheelhouse}"
GUEST_PIP_WHEELHOUSE="/Volumes/My Shared Files/pip-wheelhouse"
GOLDEN="${TARTCI_MACOS_GOLDEN:-${PULP_RUNNER_GOLDEN:-pulp-build-runner:latest}}"
REPO="${TARTCI_RUNNER_REPO:-${PULP_RUNNER_REPO:-Generous-Corp/pulp}}"
LABELS="${TARTCI_RUNNER_LABELS:-${PULP_RUNNER_LABELS:-self-hosted,macOS,ARM64,pulp-build-vm}}"
RUNNER_GROUP_ID="${TARTCI_RUNNER_GROUP_ID:-${PULP_RUNNER_GROUP_ID:-1}}"
RUNNER_VERSION="${TARTCI_RUNNER_VERSION:-${PULP_RUNNER_VERSION:-2.336.0}}"
RUNNER_SHA256="${TARTCI_RUNNER_SHA256:-${PULP_RUNNER_SHA256:-}}"
GUEST_HTTP_PROXY="${TARTCI_GUEST_HTTP_PROXY:-}"
if [ -n "$GUEST_HTTP_PROXY" ]; then
  [[ "$GUEST_HTTP_PROXY" =~ ^http://192\.168\.64\.1:([0-9]{1,5})$ ]] \
    || { printf 'invalid TARTCI_GUEST_HTTP_PROXY: expected http://192.168.64.1:PORT\n' >&2; exit 1; }
  [ "${BASH_REMATCH[1]}" -ge 1 ] && [ "${BASH_REMATCH[1]}" -le 65535 ] \
    || { printf 'invalid TARTCI_GUEST_HTTP_PROXY port\n' >&2; exit 1; }
fi
[ -n "$RUNNER_SHA256" ] || [ "$RUNNER_VERSION" != 2.336.0 ] || \
  RUNNER_SHA256="8e8839c49b7060b6b2154f4931f815df330c27f167d53ef2239ee3dfce28b079"
WORKFLOW_NAME="${TARTCI_RUNNER_WORKFLOW_NAME:-Build and Test}"
WORKFLOW_NAMES="${TARTCI_RUNNER_WORKFLOW_NAMES:-}"
WORKFLOW_TIERS="${TARTCI_RUNNER_WORKFLOW_TIERS:-}"
WORKFLOW_TIER_GROUPS="${TARTCI_RUNNER_WORKFLOW_TIER_GROUPS:-}"
ASSIGNMENT_MODE="${TARTCI_RUNNER_ASSIGNMENT_MODE:-legacy}"
# shellcheck disable=SC2034 # consumed by sourced assignment-v2.lib.sh
ASSIGNMENT_V2_OMIT_LABELS="${TARTCI_ASSIGNMENT_V2_OMIT_LABELS:-pulp-gate-fast}"
# shellcheck disable=SC2034 # consumed by sourced assignment-v2.lib.sh
ASSIGNMENT_V2_REQUIRED_OMIT_LABELS="${TARTCI_ASSIGNMENT_V2_REQUIRED_OMIT_LABELS:-pulp-gate-fast}"
# shellcheck disable=SC2034 # consumed by sourced assignment-v2.lib.sh
ASSIGNMENT_V2_CLASS_LABELS="${TARTCI_ASSIGNMENT_V2_CLASS_LABELS:-pulp-build-merge-group,pulp-build-pr-head}"
ASSIGNMENT_V2_BASE_LABELS=""
# Work-conserving idle retarget (opt-in; see header). 0 = OFF. Validated by
# tartci_assignment_v2_configure and consulted only by event-class-v2 lanes.
ASSIGNMENT_V2_IDLE_RETARGET_SECS="${TARTCI_ASSIGNMENT_V2_IDLE_RETARGET_SECS:-0}"
# run_runner_until_done's distinct exit for an idle runner discarded so the
# slot can serve another class. Not 124: that is a timeout, this is a decision.
IDLE_RETARGET_RC=125
# Per-slot class preference order (opt-in; see header). Empty = configured order.
# shellcheck disable=SC2034 # consumed by sourced assignment-v2.lib.sh
ASSIGNMENT_V2_TIER_ORDER="${TARTCI_ASSIGNMENT_V2_TIER_ORDER:-}"
# Newline-delimited class labels in preference order; set by configure.
# shellcheck disable=SC2034 # consumed by sourced assignment-v2.lib.sh
ASSIGNMENT_V2_ORDER_LABELS=""
MIN_QUEUED_AGE="${TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS:-0}"
case "$MIN_QUEUED_AGE" in
  ''|*[!0-9]*) printf 'invalid TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS: %s\n' "$MIN_QUEUED_AGE" >&2; exit 1 ;;
esac
# Fallback lane (opt-in; see header). Validated by tartci_assignment_v2_configure.
# shellcheck disable=SC2034 # consumed by sourced assignment-v2.lib.sh
FALLBACK_PEERS="${TARTCI_FALLBACK_PEERS:-}"
# shellcheck disable=SC2034 # consumed by sourced assignment-v2.lib.sh
FALLBACK_PEER_MAX_AGE="${TARTCI_FALLBACK_PEER_MAX_AGE_SECS:-60}"
WORKFLOW_ARGS=()
WORKFLOW_DISPLAY=""
WORKFLOW_CONFIG=""
TIER_LABELS_CONFIG=""
TIER_GROUP_IDS_CONFIG=""
# Priority-aware idle gate (opt-in; see header). YIELD_WORKFLOW empty = OFF.
YIELD_WORKFLOW="${TARTCI_YIELD_TO_WORKFLOW_NAME:-}"
YIELD_LABELS="${TARTCI_YIELD_TO_LABELS:-}"
# Bounded yield (opt-in; see header). 0 = unbounded (today's behavior).
YIELD_MAX_WAIT="${TARTCI_YIELD_MAX_WAIT_SECONDS:-0}"
case "$YIELD_MAX_WAIT" in
  ''|*[!0-9]*) printf 'invalid TARTCI_YIELD_MAX_WAIT_SECONDS: %s\n' "$YIELD_MAX_WAIT" >&2; exit 1 ;;
esac
# Host-health auto-yield (opt-in; see header): the decision lives in the shared
# providers/common/host-health.lib.sh, reading TARTCI_HOST_VITALS_YIELD[_ON_WARN]
# / TARTCI_HOST_VITALS_BIN directly. Empty/0 = OFF (no host_vitals call).
LOOP=0
CAP="${TARTCI_MACOS_VM_CAP:-${PULP_VM_CAP:-2}}"
POLL="${TARTCI_VM_POLL:-${PULP_VM_POLL:-20}}"; case "$POLL" in ''|*[!0-9]*|0) POLL=20;; esac  # positive int only (self-heal arithmetic)
JOB_TIMEOUT="${TARTCI_JOB_TIMEOUT_SECS:-7200}"
TEARDOWN_STEP_TIMEOUT="${TARTCI_TEARDOWN_STEP_TIMEOUT_SECS:-5}"
case "$TEARDOWN_STEP_TIMEOUT" in
  ''|*[!0-9]*|0) printf 'invalid TARTCI_TEARDOWN_STEP_TIMEOUT_SECS: expected 1-20\n' >&2; exit 1 ;;
esac
[ "$TEARDOWN_STEP_TIMEOUT" -le 20 ] \
  || { printf 'invalid TARTCI_TEARDOWN_STEP_TIMEOUT_SECS: expected 1-20\n' >&2; exit 1; }
# Pending-delete reconciliation (reconcile_pending_delete): how many in-loop
# delete retries, and how long to wait between them, before a teardown whose
# deletion stays unproved falls back to the fail-closed supervisor restart.
PENDING_DELETE_MAX_ATTEMPTS="${TARTCI_PENDING_DELETE_MAX_ATTEMPTS:-5}"
case "$PENDING_DELETE_MAX_ATTEMPTS" in ''|*[!0-9]*|0) PENDING_DELETE_MAX_ATTEMPTS=5 ;; esac
PENDING_DELETE_RETRY_SECS="${TARTCI_PENDING_DELETE_RETRY_SECS:-10}"
case "$PENDING_DELETE_RETRY_SECS" in ''|*[!0-9]*|0) PENDING_DELETE_RETRY_SECS=10 ;; esac
PENDING_DELETE_ATTEMPTS=0
CURRENT_TEARDOWN_PENDING=""
JOB_WARN="${TARTCI_JOB_WARN_SECS:-5400}"
IDLE_TIMEOUT="${TARTCI_RUNNER_IDLE_TIMEOUT_SECS:-900}"
STATE_DIR="${TARTCI_STATE_DIR:-$HOME/.tartci/state/macos}"
JIT_DENIAL_FILE=""
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
PRINT_RUNNER_API_ROOT=0
PRINT_RUNNER_CONTRACT=""
PRINT_CHROME_MOUNT=0
PRINT_ASSIGNMENT_PARITY=0
PRINT_PRE_MINT_SELECTION=""
PRINT_IDLE_RETARGET=""
PRINT_FALLBACK_DECISION=""
PRINT_HIGHER_PRIORITY=""
PRINT_PRIORITY=0
PRINT_YIELD_BOUND=0
PRINT_HOST_HEALTH=0
PRINT_RUNNER_VERSION=0
CURRENT_VM=""
CURRENT_RPID=""
CURRENT_RUN_ID=""
# Set only by handle_supervisor_signal, and only when a run was still in flight
# when the signal arrived. A signal is not proof the job is over: launchd
# delivers SIGTERM for any bootout, including one the launchd watchdog issues on
# a misread, while the guest may still be executing a required gate job.
# Deleting that VM force-fails a live job with no failed step, so teardown
# refuses the destructive half in this window.
SIGNAL_LIVE_ASSIGNMENT=0
CURRENT_JOB_ID=""
CURRENT_WORKFLOW_NAME=""
CURRENT_JOB_CAPTURE_STATUS="not-attempted"
CURRENT_JOB_RECEIPT=""
CURRENT_JOB_SCAN_SPENT=0
CURRENT_JOB_SCAN_FAILURES=0
CURRENT_JOB_SCAN_NEXT_AT=0
CURRENT_CANCEL_DISCOVERY_SCAN_SPENT=0
CURRENT_CANCEL_REVALIDATION_SCAN_SPENT=0
CURRENT_CANCEL_TERMINAL_SCAN_SPENT=0
CURRENT_ASSIGNMENT_QUARANTINE="none"
CURRENT_SCAN_PID=""
CURRENT_SCAN_TMP=""
CURRENT_LABELS="$LABELS"
CURRENT_IP=""
CURRENT_REGISTERED_RUNNER=""
CURRENT_RUNNER_API_ROOT=""
CURRENT_AQUA_LABEL=""
# The lease this guest booted with, declared to the job so an in-guest build
# governor can size itself from the lease rather than infer it.
CURRENT_GUEST_CORES=""
CURRENT_GUEST_MEM_MB=""
CURRENT_PIP_WHEELHOUSE=0
CLEANED_UP=0
# Set when a work entry ends without serving a job, cleared when a job is
# actually assigned or when the queue drains. Carries the START of the blocked
# streak, not the latest failure, so a supervisor that keeps re-entering work
# and keeps failing stays measurable across the other phases it cycles through
# while blocked.
#
# The cause is deliberately not enumerated. A lease denial, an admission
# refusal and a cause nobody has written down yet all produce the same
# observable: the lane took a slot against real queued demand and served
# nothing. So the streak is counted at the one place every cause returns
# through, rather than at each cause in turn. Only an assignment clears it: a
# granted lease, a booted VM and a registered runner each prove a step, never
# that the lane is serving.
SERVING_BLOCKED_SINCE=""
SERVING_BLOCKED_STREAK=0
SERVING_BLOCKED_LAST_PHASE=""
# 1 once the current work entry has had a job assigned to it.
CURRENT_SERVED=0
# The queued count the loop selected with; the per-job claim reads it.
CURRENT_SELECTED_QUEUED=""
LAST_HEARTBEAT_PHASE=""
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

configure_workflow_tier_groups(){
  local entry tier_label group_id expected_labels="" configured_labels=""
  [ -n "$WORKFLOW_TIER_GROUPS" ] || return 0
  [ -n "$WORKFLOW_TIERS" ] \
    || die "TARTCI_RUNNER_WORKFLOW_TIER_GROUPS requires TARTCI_RUNNER_WORKFLOW_TIERS"
  while IFS= read -r entry; do
    entry="${entry%$'\r'}"
    [ -n "$entry" ] || continue
    case "$entry" in
      *'|'*) ;;
      *) die "invalid TARTCI_RUNNER_WORKFLOW_TIER_GROUPS entry (expected class-label|runner-group-id): $entry" ;;
    esac
    tier_label="${entry%%|*}"
    group_id="${entry#*|}"
    case "$group_id" in
      ''|0*|*[!0-9]*) die "invalid workflow-tier runner group for $tier_label: expected a positive integer" ;;
    esac
    [ -n "$tier_label" ] \
      || die "invalid TARTCI_RUNNER_WORKFLOW_TIER_GROUPS entry (empty class label): $entry"
    if [ -n "$configured_labels" ]; then
      configured_labels="$configured_labels
$tier_label"
      TIER_GROUP_IDS_CONFIG="$TIER_GROUP_IDS_CONFIG
$group_id"
    else
      configured_labels="$tier_label"
      TIER_GROUP_IDS_CONFIG="$group_id"
    fi
  done <<< "$WORKFLOW_TIER_GROUPS"
  expected_labels="$TIER_LABELS_CONFIG"
  [ -n "$configured_labels" ] && [ "$configured_labels" = "$expected_labels" ] \
    || die "workflow-tier runner groups must exactly match workflow tiers in priority order"
}

runner_group_id_for_tier(){
  local selected_tier="$1" group_id tier=0
  if [ -z "$TIER_GROUP_IDS_CONFIG" ]; then
    printf '%s\n' "$RUNNER_GROUP_ID"
    return 0
  fi
  while IFS= read -r group_id; do
    [ -n "$group_id" ] || continue
    if [ "$tier" -eq "$selected_tier" ]; then
      printf '%s\n' "$group_id"
      return 0
    fi
    tier=$((tier + 1))
  done <<< "$TIER_GROUP_IDS_CONFIG"
  return 1
}

# shellcheck source=providers/common/vm-lease.lib.sh
source "$TARTCI_ROOT/providers/common/vm-lease.lib.sh"
# shellcheck source=providers/common/vm-state.lib.sh
source "$TARTCI_ROOT/providers/common/vm-state.lib.sh"
# shellcheck source=providers/common/host-health.lib.sh
source "$TARTCI_ROOT/providers/common/host-health.lib.sh"
# shellcheck source=providers/common/admission-clean.lib.sh
source "$TARTCI_ROOT/providers/common/admission-clean.lib.sh"
# shellcheck source=providers/tart-macos/assignment-v2.lib.sh
source "$TARTCI_ROOT/providers/tart-macos/assignment-v2.lib.sh"
# shellcheck source=providers/tart-macos/boundary-proof.lib.sh
source "$TARTCI_ROOT/providers/tart-macos/boundary-proof.lib.sh"
# shellcheck source=providers/tart-macos/job-claim.lib.sh
source "$TARTCI_ROOT/providers/tart-macos/job-claim.lib.sh"
# shellcheck source=providers/tart-macos/lease-fit.lib.sh
source "$TARTCI_ROOT/providers/tart-macos/lease-fit.lib.sh"
# shellcheck source=providers/tart-macos/chrome-mount.lib.sh
source "$TARTCI_ROOT/providers/tart-macos/chrome-mount.lib.sh"
# shellcheck source=providers/tart-macos/pip-wheelhouse.lib.sh
source "$TARTCI_ROOT/providers/tart-macos/pip-wheelhouse.lib.sh"

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

runner_api_root_for_group(){
  local runner_group_id="$1" runner_org runner_repo
  case "$runner_group_id" in
    ''|0*|*[!0-9]*) die "invalid TARTCI_RUNNER_GROUP_ID: expected a positive integer";;
  esac
  case "$REPO" in
    ''|/*|*/|*/*/*) die "invalid runner repository for group $runner_group_id: expected OWNER/REPO";;
    */*) runner_org="${REPO%%/*}"; runner_repo="${REPO#*/}";;
    *) die "invalid runner repository for group $runner_group_id: expected OWNER/REPO";;
  esac
  case "$runner_org" in
    -*|*-|*[!A-Za-z0-9-]*) die "invalid runner repository for group $runner_group_id: expected OWNER/REPO";;
  esac
  case "$runner_repo" in
    *[!A-Za-z0-9_.-]*) die "invalid runner repository for group $runner_group_id: expected OWNER/REPO";;
  esac
  if [ "$runner_group_id" = 1 ]; then
    printf 'repos/%s/actions/runners\n' "$REPO"
    return
  fi
  printf 'orgs/%s/actions/runners\n' "$runner_org"
}

configure_runner_api_root(){
  RUNNER_API_ROOT="$(runner_api_root_for_group "$RUNNER_GROUP_ID")"
}

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
  --print-assignment-parity) PRINT_ASSIGNMENT_PARITY=1; shift;;
  --print-pre-mint-selection) PRINT_PRE_MINT_SELECTION="$2"; shift 2;;
  --print-idle-retarget) PRINT_IDLE_RETARGET="$2"; shift 2;;
  --print-fallback-decision) PRINT_FALLBACK_DECISION="$2"; shift 2;;
  --print-higher-priority-demand) PRINT_HIGHER_PRIORITY="$2"; shift 2;;
  --print-priority-demand) PRINT_PRIORITY=1; shift;;
  --print-yield-bound) PRINT_YIELD_BOUND=1; shift;;
  --print-host-health) PRINT_HOST_HEALTH=1; shift;;
  --print-runner-version) PRINT_RUNNER_VERSION=1; shift;;
  --print-runner-api-root) PRINT_RUNNER_API_ROOT=1; shift;;
  --print-runner-contract) PRINT_RUNNER_CONTRACT="$2"; shift 2;;
  --print-chrome-mount) PRINT_CHROME_MOUNT=1; shift;;
  --yield-to-workflow) YIELD_WORKFLOW="$2"; shift 2;;
  --yield-to-labels) YIELD_LABELS="$2"; shift 2;;
  -h|--help) usage; exit 0;;
  *) die "unknown arg: $1";;
esac; done

configure_runner_api_root
CURRENT_RUNNER_API_ROOT="$RUNNER_API_ROOT"
[ "$PRINT_RUNNER_API_ROOT" = 1 ] && { printf '%s\n' "$RUNNER_API_ROOT"; exit 0; }
configure_chrome_mount
[ "$PRINT_CHROME_MOUNT" = 1 ] && {
  [ -n "$CHROME_MOUNT_ARG" ] || die "TARTCI_RUNNER_CHROME_APP_DIR is not configured"
  printf '%s\n' "$CHROME_MOUNT_ARG"
  exit 0
}

case "$RUNNER_VERSION" in
  ''|*[!0-9.]*) die "invalid Actions Runner version: $RUNNER_VERSION";;
esac
[ "$PRINT_RUNNER_VERSION" = 1 ] && { printf '%s\n' "$RUNNER_VERSION"; exit 0; }
case "$RUNNER_SHA256" in
  ''|*[!0-9a-fA-F]*) die "set a 64-character TARTCI_RUNNER_SHA256 when overriding Actions Runner version $RUNNER_VERSION";;
esac
[ "${#RUNNER_SHA256}" -eq 64 ] || die "TARTCI_RUNNER_SHA256 must contain 64 hexadecimal characters"

configure_workflows
configure_workflow_tier_groups
tartci_assignment_v2_configure
# The retarget is a V2 decision: a legacy or observe lane registers with the
# tier labels but never consults it, so those lanes stay byte-for-byte unchanged.
[ "$ASSIGNMENT_MODE" = event-class-v2 ] || ASSIGNMENT_V2_IDLE_RETARGET_SECS=0
CURRENT_LABELS="$LABELS"
[ -z "$PRINT_RUNNER_CONTRACT" ] || {
  contract_group="$(runner_group_id_for_tier "$PRINT_RUNNER_CONTRACT")" \
    || die "no workflow-tier registration contract at index $PRINT_RUNNER_CONTRACT"
  printf '%s\t%s\n' "$contract_group" "$(runner_api_root_for_group "$contract_group")"
  exit 0
}

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
command -v "$JIT_GH_CLI" >/dev/null 2>&1 || die "GitHub CLI '$JIT_GH_CLI' (TARTCI_JIT_GH_CLI) not installed / authed"
mkdir -p "$STATE_DIR"

jit_denial_file_for(){
  local runner_group_id="$1" labels="$2" key
  key="$(printf '%s' "$REPO|$runner_group_id|$labels|$JIT_GH_CLI" | shasum -a 256 | awk '{print $1}')"
  printf '%s/jit-admission-denied.%s\n' "$STATE_DIR" "$key"
}
jit_admission_denied(){
  local runner_group_id="$1" labels="$2"
  JIT_DENIAL_FILE="$(jit_denial_file_for "$runner_group_id" "$labels")"
  [ -f "$JIT_DENIAL_FILE" ]
}
record_jit_admission_denied(){
  local runner_group_id="$1" labels="$2" detail="$3"
  JIT_DENIAL_FILE="$(jit_denial_file_for "$runner_group_id" "$labels")"
  umask 077
  printf 'repo=%s\nrunner_group_id=%s\nlabels=%s\ngh_cli=%s\ndetail=%s\n' \
    "$REPO" "$runner_group_id" "$labels" "$JIT_GH_CLI" "$detail" >"$JIT_DENIAL_FILE"
}
clear_jit_admission_denied(){
  local runner_group_id="$1" labels="$2"
  rm -f "$(jit_denial_file_for "$runner_group_id" "$labels")"
}

# -- Scan diagnostics -------------------------------------------------------
# A queue/assignment scanner reports WHY it failed on stderr; its stdout is only
# a count. Discarding that stderr leaves the supervisor able to report that it is
# blind but never why, which is what turned a one-line interpreter fault into a
# multi-hour outage. Keep the last diagnostic so the blind path can print it.
SCAN_ERROR_FILE="$STATE_DIR/$RUNNER_NAME.scan-last-error"

# Keep BOTH ends of a diagnostic stream, with an explicit elision marker.
#
# A wrapper prints the underlying CAUSE first and its own summary last, so any
# "keep the last N lines" rule drops the cause the moment the wrapper is more
# verbose than N. Deepening the tail does not fix last-line-wins — it only moves
# the cliff: `tail -n 3` keeps a two-line wrapper's cause and loses a four-line
# one's, and nothing lets a caller predict how chatty a wrapper will be. Keeping
# the head as well is what makes the cause survive at any length, and the marker
# means an elided middle is never mistaken for the whole stream.
scan_diagnostic_digest(){
  awk -v head_n="${2:-3}" -v tail_n="${3:-3}" -v max_col="${4:-240}" '
    /^[[:space:]]*$/ { next }
    {
      lines[++count] = (length($0) > max_col) \
        ? substr($0, 1, max_col) "..." : $0
    }
    END {
      if (count == 0) exit 0
      if (count <= head_n + tail_n) {
        for (i = 1; i <= count; i++) print lines[i]
        exit 0
      }
      for (i = 1; i <= head_n; i++) print lines[i]
      printf "[... %d line(s) elided ...]\n", count - head_n - tail_n
      for (i = count - tail_n + 1; i <= count; i++) print lines[i]
    }
  ' "$1"
}

run_scan_capture(){
  local err rc
  err="$(mktemp "${TMPDIR:-/tmp}/tartci-scan-err.XXXXXX")" || { "$@"; return $?; }
  "$@" 2>"$err"
  rc=$?
  if [ -s "$err" ]; then
    mkdir -p "$STATE_DIR" 2>/dev/null || true
    scan_diagnostic_digest "$err" >"$SCAN_ERROR_FILE" 2>/dev/null || true
  fi
  rm -f "$err"
  return $rc
}

# Most recent scanner diagnostic, flattened to a single line for the log.
scan_last_error(){
  [ -r "$SCAN_ERROR_FILE" ] || return 0
  tr '\n' '|' <"$SCAN_ERROR_FILE" 2>/dev/null | sed 's/|$//'
}

clear_scan_error(){ rm -f "$SCAN_ERROR_FILE" 2>/dev/null || true; }

read_blind_restarts(){
  local v=0
  [ -r "${BLIND_RESTART_FILE:-}" ] && read -r v <"$BLIND_RESTART_FILE" 2>/dev/null
  case "$v" in ''|*[!0-9]*) v=0;; esac
  printf '%s' "$v"
}

write_blind_restarts(){
  [ -n "${BLIND_RESTART_FILE:-}" ] || return 0
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  printf '%s\n' "$1" >"$BLIND_RESTART_FILE" 2>/dev/null || true
}

reset_blind_restarts(){
  [ -n "${BLIND_RESTART_FILE:-}" ] && rm -f "$BLIND_RESTART_FILE" 2>/dev/null
  [ -n "${BLIND_ESCALATION_FILE:-}" ] && rm -f "$BLIND_ESCALATION_FILE" 2>/dev/null
  return 0
}

# True while a recent escalation notice still stands, so the alert is raised
# once per window rather than on every poll.
blind_escalation_is_fresh(){
  local window="${TARTCI_SCAN_BLIND_ESCALATION_REPEAT_SECS:-900}" mtime now
  [ -n "${BLIND_ESCALATION_FILE:-}" ] || return 1
  [ -r "$BLIND_ESCALATION_FILE" ] || return 1
  # GNU `stat -f` means "filesystem", not "format": it prints a block of fs
  # detail AND fails, so chaining the two dialects with `||` concatenates that
  # dump onto the real answer. Try each independently and accept only a number.
  mtime="$(stat -c %Y "$BLIND_ESCALATION_FILE" 2>/dev/null)"
  case "$mtime" in ''|*[!0-9]*) mtime="";; esac
  if [ -z "$mtime" ]; then
    mtime="$(stat -f %m "$BLIND_ESCALATION_FILE" 2>/dev/null)"
    case "$mtime" in ''|*[!0-9]*) return 1;; esac
  fi
  now="$(date +%s)"
  [ $((now - mtime)) -lt "$window" ]
}

# A file a human (or `tartci status`) can find without reading a log tail.
write_blind_escalation(){
  [ -n "${BLIND_ESCALATION_FILE:-}" ] || return 0
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  printf '{"ts":"%s","runner":"%s","host":"%s","restarts":"%s","detail":"%s"}\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$RUNNER_NAME" "$HOST_NAME" \
    "$(json_sanitize "$1")" "$(json_sanitize "${2:-no diagnostic captured}")" \
    >"$BLIND_ESCALATION_FILE" 2>/dev/null || true
}

# Publish a diagnostic captured by a caller that runs its own scanner.
record_scan_error(){
  [ -n "${1:-}" ] || return 0
  mkdir -p "$STATE_DIR" 2>/dev/null || true
  printf '%s\n' "$1" >"$SCAN_ERROR_FILE" 2>/dev/null || true
}

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
  LAST_HEARTBEAT_PHASE="$phase"
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  state_file="$STATE_DIR/$RUNNER_NAME.state.json"
  tmp_file="$(mktemp "$state_file.tmp.XXXXXX")" || return 1
  if cat >"$tmp_file" <<EOF
{"ts":"$ts","provider":"tart-macos","host":"$(json_sanitize "$HOST_NAME")","runner":"$RUNNER_NAME","vm":"${CURRENT_VM:-}","vm_ip":"$(json_sanitize "${CURRENT_IP:-}")","phase":"$(json_sanitize "$phase")","lifecycle":"ephemeral","labels":"$(json_sanitize "$CURRENT_LABELS")","repo":"$(json_sanitize "$REPO")","run_id":"$(json_sanitize "${CURRENT_RUN_ID:-}")","job_id":"$(json_sanitize "${CURRENT_JOB_ID:-}")","assignment_observation":"$(json_sanitize "$CURRENT_JOB_CAPTURE_STATUS")","assignment_quarantine":"$(json_sanitize "$CURRENT_ASSIGNMENT_QUARANTINE")","serving_blocked_since":"$(json_sanitize "$SERVING_BLOCKED_SINCE")","serving_blocked_streak":$SERVING_BLOCKED_STREAK,"serving_blocked_last_phase":"$(json_sanitize "$SERVING_BLOCKED_LAST_PHASE")","supervisor_pid":"$SUPERVISOR_PID","supervisor_pid_started_at":"$(json_sanitize "$SUPERVISOR_PID_STARTED_AT")"}
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

# Running macOS guests per `tart list`, or `unknown` when the inventory cannot
# be read. A slow or failed listing is NOT a full host: reporting the hard cap
# here once made an empty host look 2/2 busy for as long as `tart list` stayed
# slow. One bounded retry with a longer timeout rides out a transient stall;
# after that the caller must treat occupancy as unknown and fall back to the
# reservation files (tartci_claim_macos_slot), which every lane writes before
# it boots and keeps until its VM is proved gone.
running_macos_vms(){
  local count inventory_timeout retry_timeout
  inventory_timeout="${TARTCI_TART_INVENTORY_TIMEOUT_SECS:-5}"
  retry_timeout="${TARTCI_TART_INVENTORY_RETRY_TIMEOUT_SECS:-15}"
  if count="$(python3 "$TARTCI_ROOT/scripts/tart_inventory.py" \
      --timeout-seconds "$inventory_timeout" 2>/dev/null)" \
    || count="$(python3 "$TARTCI_ROOT/scripts/tart_inventory.py" \
      --timeout-seconds "$retry_timeout" 2>/dev/null)"; then
    printf '%s\n' "$count"
    return 0
  fi
  printf 'unknown\n'
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
  run_scan_capture python3 "$TARTCI_ROOT/scripts/queue_scan.py" \
    --repo "$REPO" \
    "${WORKFLOW_ARGS[@]}" \
    --labels "$LABELS" \
    --provider tart-macos \
    --lane-id "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}" \
    --state-file "$STATE_DIR/queue-scan.json" \
    --shared-cache-file "${TARTCI_SHARED_QUEUE_CACHE:-$HOME/.tartci/state/queue-discovery.json}" \
    --max-age-seconds 0 \
    --min-age-seconds "$MIN_QUEUED_AGE" \
    --match-labels 1 || echo ERR
}

print_queued_work(){
  if [ "$ASSIGNMENT_MODE" = event-class-v2 ]; then
    tartci_assignment_v2_total_demand
  else
    queued_work
  fi
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
  run_scan_capture "${scan_cmd[@]}" --match-labels 1
}

# Print `count|registration labels|zero-based tier`. A scan error at any tier is
# fail-closed: never skip a blind higher class and hand its capacity to a lower
# one. With no tier config this is the legacy single-label queue scan.
select_work(){
  local tier_labels q tier=0 legacy_selection v2_selection
  if [ "$ASSIGNMENT_MODE" = observe ]; then
    legacy_selection="$(ASSIGNMENT_MODE=legacy select_work)"
    v2_selection="$(tartci_assignment_v2_observe)"
    if [ -n "$v2_selection" ]; then
      note "assignment-v2 observe legacy=$legacy_selection v2=$v2_selection"
      event assignment_v2_observe "legacy=$legacy_selection v2=$v2_selection"
    fi
    printf '%s\n' "$legacy_selection"
    return 0
  fi
  if [ "$ASSIGNMENT_MODE" = event-class-v2 ]; then
    tartci_assignment_v2_select
    return 0
  fi
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
# That widening has a cost, which `--exclude-assigned 1` pays back: an
# in_progress job that ALREADY holds a runner_name is being served, and on the
# hosted resolver leg it will never occupy a self-hosted slot at all -- yet it
# still reserved one, for as long as it ran. A host was observed with both VM
# slots free and a release job queued, indefinitely yielding to a priority lane
# whose own supervisors reported nothing queued. Excluding assigned jobs keeps
# the race guard (an unassigned in_progress job still counts) while dropping the
# reservation that cannot be used.
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
  run_scan_capture python3 "$TARTCI_ROOT/scripts/queue_scan.py" \
    --repo "$REPO" \
    --workflow "$YIELD_WORKFLOW" \
    --labels "$YIELD_LABELS" \
    --job-statuses queued,in_progress \
    --provider tart-macos-priority \
    --exclude-assigned 1 \
    --lane-id "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}-priority" \
    --state-file "$STATE_DIR/priority-queue-scan.json" \
    --shared-cache-file "${TARTCI_SHARED_QUEUE_CACHE:-$HOME/.tartci/state/queue-discovery.json}" \
    --max-age-seconds 0 \
    --match-labels 1 \
    || { printf '%s\n' 1; return 0; }
}

# yield_bound_reached <selected_labels> — how many of THIS lane's own queued
# jobs, in the class the loop selected, have been queued for at least
# YIELD_MAX_WAIT seconds. Non-zero lifts the priority yield for this poll.
#
# Prints 0 (and never scans) when the bound is off, so an unbounded lane is
# unchanged. FAILS CLOSED: a scan error prints 0, so the lane keeps yielding
# rather than preempting the priority lane blind -- the same direction as
# priority_demand. Age comes from queue_scan's --min-age-seconds, which reads
# each job's created_at. The scan uses its OWN state file: queue_scan records a
# run with no qualifying job in a short negative cache, and sharing the plain
# queued-work state would let a "not old enough yet" verdict hide that run from
# the next ordinary poll.
yield_bound_reached(){
  local selected_labels="$1" min_age count tier_labels workflow scan_args=()
  [ "$YIELD_MAX_WAIT" -gt 0 ] || { printf '%s\n' 0; return 0; }
  [ "$ASSIGNMENT_MODE" != event-class-v2 ] || { printf '%s\n' 0; return 0; }
  min_age="$YIELD_MAX_WAIT"
  [ "$MIN_QUEUED_AGE" -le "$min_age" ] || min_age="$MIN_QUEUED_AGE"
  if [ -n "$WORKFLOW_TIERS" ]; then
    tier_labels="${selected_labels#"$LABELS",}"
    [ -n "$tier_labels" ] && [ "$tier_labels" != "$selected_labels" ] \
      || { printf '%s\n' 0; return 0; }
    while IFS= read -r workflow; do
      [ -n "$workflow" ] && scan_args+=(--workflow "$workflow")
    done < <(tier_workflow_args "$tier_labels")
    [ "${#scan_args[@]}" -gt 0 ] || { printf '%s\n' 0; return 0; }
  else
    scan_args=("${WORKFLOW_ARGS[@]}")
  fi
  if ! count="$(run_scan_capture python3 "$TARTCI_ROOT/scripts/queue_scan.py" \
      --repo "$REPO" \
      "${scan_args[@]}" \
      --labels "$selected_labels" \
      --provider tart-macos-yield-bound \
      --lane-id "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}-yield-bound" \
      --state-file "$STATE_DIR/yield-bound-scan.json" \
      --shared-cache-file "${TARTCI_SHARED_QUEUE_CACHE:-$HOME/.tartci/state/queue-discovery.json}" \
      --max-age-seconds 0 \
      --min-age-seconds "$min_age" \
      --match-labels 1)"; then
    printf '%s\n' 0; return 0
  fi
  printf '%s' "$count" | grep -qxE '[0-9]+' || { printf '%s\n' 0; return 0; }
  printf '%s\n' "$count"
}

reclaim_runner_name(){
  local name="$1" runner_api_root="${2:-$RUNNER_API_ROOT}" max_attempts="${3:-18}" id attempt
  for attempt in $(seq 1 "$max_attempts"); do
    id="$("$GH_CLI" api "$runner_api_root" --paginate \
          --jq ".runners[] | select(.name==\"$name\") | .id" 2>/dev/null | head -n1 || true)"
    [ -n "$id" ] || break
    note "reclaiming static name '$name': deleting stale runner registration (id=$id attempt=$attempt)"
    "$GH_CLI" api -X DELETE "$runner_api_root/$id" >/dev/null 2>&1 && { id=""; break; }
    sleep 10
  done
  # A registration we found but could not delete becomes a ghost: it stays
  # offline while still advertising this lane's labels. It is not worth failing
  # the boot over, but it must not vanish silently -- a ghost with no
  # provenance is what makes "is this label served?" unanswerable later.
  [ -z "$id" ] || note "runner registration '$name' (id=$id) could not be deleted; it will linger as a ghost"
  tart delete "$name" >/dev/null 2>&1 || true
}

# A per-boot registration name is never reused, so any OFFLINE registration
# carrying this lane's exact per-boot shape, other than the one this boot just
# claimed, belongs to a finished boot. Ghosts are tolerated by design -- a
# never-reused name cannot collide, which is what keeps a SIGKILLed VM from
# wedging the whole gate -- but an offline registration still advertises its
# labels, so a label served only by ghosts reads as served. Reap this lane's
# own residue; the exact-shape match means another lane is never touched.
sweep_lane_ghost_runners(){
  local runner_api_root="$1" keep="$2" id name
  "$GH_CLI" api "$runner_api_root" --paginate \
    --jq ".runners[] | select(.status == \"offline\") | select(.name | test(\"^${RUNNER_NAME}-[0-9]+-[0-9]+$\")) | \"\\(.id)\\t\\(.name)\"" 2>/dev/null \
  | while IFS="$(printf '\t')" read -r id name; do
      [ -n "$id" ] && [ -n "$name" ] || continue
      [ "$name" = "$keep" ] && continue
      if "$GH_CLI" api -X DELETE "$runner_api_root/$id" >/dev/null 2>&1; then
        note "swept ghost runner registration '$name' (id=$id)"
      else
        note "ghost runner registration '$name' (id=$id) could not be swept"
      fi
    done
}

stop_current_aqua_runner(){
  if [ -n "$CURRENT_IP" ] && [ -n "$CURRENT_AQUA_LABEL" ]; then
    bounded_teardown_command aqua-stop \
      ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$CURRENT_IP" \
      "\$HOME/.tartci/bin/guest-aqua-runner.sh stop '$CURRENT_AQUA_LABEL'" \
      >/dev/null 2>&1 || true
  fi
}

bounded_teardown_command(){
  local operation="$1"; shift
  python3 "$TARTCI_ROOT/scripts/bounded_command.py" \
    --timeout "$TEARDOWN_STEP_TIMEOUT" --operation "$operation" -- "$@"
}

terminate_current_guardian(){
  local pid="${CURRENT_RPID:-}"
  [ -n "$pid" ] || return 0
  case "$pid" in *[!0-9]*) return 1 ;; esac
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  ! kill -0 "$pid" 2>/dev/null
}

discard_current_vm(){
  [ -n "$CURRENT_VM" ] || return 0
  if [ "${SIGNAL_LIVE_ASSIGNMENT:-0}" = 1 ]; then
    # Nonterminal on purpose: the caller keeps owning the lease and the
    # reservation, because the VM really is still consuming that capacity. The
    # janitor reaps the VM once it is genuinely residue; a deleted guest under a
    # live job is not recoverable at all.
    note "refusing teardown of $CURRENT_VM — run ${CURRENT_RUN_ID:-} was still in flight when the supervisor was signalled"
    event teardown_refused "vm=$CURRENT_VM reason=live_assignment run_id=${CURRENT_RUN_ID:-} job_id=${CURRENT_JOB_ID:-}"
    return 1
  fi
  CURRENT_TEARDOWN_PENDING=""
  note "stopping — tearing down in-flight VM $CURRENT_VM"
  stop_current_aqua_runner
  if ! terminate_current_guardian; then
    note "teardown incomplete — exact tart guardian $CURRENT_RPID remains live; preserving capacity ownership"
    event teardown_incomplete "vm=$CURRENT_VM reason=guardian_live pid=$CURRENT_RPID"
    return 1
  fi
  CURRENT_RPID=""
  bounded_teardown_command tart-stop tart stop "$CURRENT_VM" >/dev/null 2>&1 || true
  if ! bounded_teardown_command tart-delete tart delete "$CURRENT_VM" >/dev/null 2>&1 \
    && ! tart_vm_proved_absent "$CURRENT_VM"; then
    note "teardown incomplete — guardian is terminal but VM deletion was not proved"
    event teardown_incomplete "vm=$CURRENT_VM reason=delete_unproved"
    # The guardian is terminal, so the guest is not running; only its disk
    # remains unproved. The loop may keep that VM as pending-delete instead of
    # restarting the supervisor (see reconcile_pending_delete).
    CURRENT_TEARDOWN_PENDING=delete
    return 1
  fi
  CURRENT_VM=""
  CURRENT_IP=""
  CURRENT_AQUA_LABEL=""
}

# A timed-out `tart delete` is killed with its process group, so it is no
# longer mutating anything; a readable local inventory that no longer lists the
# VM is then proof it is gone. An unreadable inventory proves nothing.
tart_vm_proved_absent(){
  python3 "$TARTCI_ROOT/scripts/tart_inventory.py" \
    --timeout-seconds "$TEARDOWN_STEP_TIMEOUT" --vm-absent "$1" >/dev/null 2>&1
}

# Pending-delete: a teardown whose guardian is terminal but whose deletion was
# not proved. The lane keeps CURRENT_VM, its VM lease and its macOS-slot
# reservation, so host capacity (tartci_active_reservations and the lease
# store) keeps counting the VM as occupied until deletion is proved. The loop
# retries the delete on its next pass instead of paying a full supervisor
# restart. Only when the bound is exhausted does it fall back to the
# fail-closed restart, which is where the launchd process-group cleanup and the
# janitor take over exactly as before.
reconcile_pending_delete(){
  [ -n "$CURRENT_VM" ] && [ "$CURRENT_TEARDOWN_PENDING" = delete ] || return 2
  PENDING_DELETE_ATTEMPTS=$((PENDING_DELETE_ATTEMPTS + 1))
  if bounded_teardown_command tart-delete tart delete "$CURRENT_VM" >/dev/null 2>&1 \
    || tart_vm_proved_absent "$CURRENT_VM"; then
    note "pending-delete VM $CURRENT_VM proved gone (attempt $PENDING_DELETE_ATTEMPTS) — releasing its capacity"
    event teardown_reconciled "vm=$CURRENT_VM attempts=$PENDING_DELETE_ATTEMPTS"
    CURRENT_VM=""
    CURRENT_IP=""
    CURRENT_AQUA_LABEL=""
    CURRENT_TEARDOWN_PENDING=""
    PENDING_DELETE_ATTEMPTS=0
    tartci_release_vm_lease
    [ -n "${CURRENT_RESV:-}" ] && rm -f "$CURRENT_RESV" 2>/dev/null || true
    CURRENT_RESV=""
    return 0
  fi
  if [ "$PENDING_DELETE_ATTEMPTS" -ge "$PENDING_DELETE_MAX_ATTEMPTS" ]; then
    note "pending-delete VM $CURRENT_VM still unproved after $PENDING_DELETE_ATTEMPTS attempts — falling back to a fail-closed restart"
    return 2
  fi
  note "pending-delete VM $CURRENT_VM still unproved (attempt $PENDING_DELETE_ATTEMPTS/$PENDING_DELETE_MAX_ATTEMPTS); capacity stays held"
  return 1
}

cleanup(){
  tartci_pool_lock_release
  tartci_boundary_proof_abandon
  tartci_job_claim_release
  [ "$CLEANED_UP" = 1 ] && return 0
  [ -z "$CURRENT_SCAN_PID" ] || kill "$CURRENT_SCAN_PID" 2>/dev/null || true
  [ -z "$CURRENT_SCAN_TMP" ] || rm -f "$CURRENT_SCAN_TMP" 2>/dev/null || true
  CURRENT_SCAN_PID=""
  CURRENT_SCAN_TMP=""
  local teardown_terminal=1
  discard_current_vm || teardown_terminal=0
  if [ "$teardown_terminal" = 1 ]; then
    tartci_release_vm_lease
    [ -n "${CURRENT_RESV:-}" ] && rm -f "$CURRENT_RESV" 2>/dev/null || true
    CURRENT_RESV=""
  fi
  if [ -n "$CURRENT_REGISTERED_RUNNER" ]; then
    reclaim_runner_name "$CURRENT_REGISTERED_RUNNER" "$CURRENT_RUNNER_API_ROOT" 1 2>/dev/null || true
    CURRENT_REGISTERED_RUNNER=""
  fi
  reclaim_runner_name "$RUNNER_NAME" "$CURRENT_RUNNER_API_ROOT" 1 2>/dev/null || true
  CLEANED_UP=1
  heartbeat stopped
}

handle_supervisor_signal(){
  if [ -n "$CURRENT_RUN_ID" ] || [ -n "$CURRENT_SCAN_PID" ] || [ "${assigned:-0}" = 1 ]; then
    CURRENT_ASSIGNMENT_QUARANTINE="signal_teardown_unknown"
    CURRENT_JOB_CAPTURE_STATUS="terminal_unknown"
    CURRENT_JOB_RECEIPT='{"kind":"terminal_unknown","detail":"supervisor_signal"}'
  fi
  # A captured run id is the one signal that positively identifies work we would
  # be destroying. Scan/assignment state alone can outlive a finished job, so it
  # quarantines the observation above without also blocking reclamation.
  [ -z "$CURRENT_RUN_ID" ] || SIGNAL_LIVE_ASSIGNMENT=1
  event supervisor_signal "INT/TERM quarantine=$CURRENT_ASSIGNMENT_QUARANTINE"
  cleanup
  trap - EXIT
  exit 143
}

# The actions runner announces its own job boundaries in its log. The closing
# line is direct local proof that the job reached a terminal state, and it
# costs no API call and no share of the host observation lock.
runner_log_completion_result(){
  local log="${1:-}" line
  [ -n "$log" ] && [ -r "$log" ] || return 1
  line="$(grep -E ': Job .+ completed with result: .+' "$log" 2>/dev/null | tail -1)"
  [ -n "$line" ] || return 1
  printf '%s' "${line##*completed with result: }"
}

record_terminal_job_receipt(){
  local runner_rc="$1" runner_log="${2:-}" local_result
  if [ "$CURRENT_JOB_CAPTURE_STATUS" = terminal ]; then
    CURRENT_ASSIGNMENT_QUARANTINE="none"
    event job_terminal_receipt "runner_rc=$runner_rc observation=terminal evidence=github_api rerun_eligible=false receipt=$CURRENT_JOB_RECEIPT"
    return 0
  fi
  if [ "$CURRENT_JOB_CAPTURE_STATUS" = active ] || [ "$CURRENT_JOB_CAPTURE_STATUS" = terminal_pending_run ]; then
    # A completed observation that still saw the workflow running is positive
    # evidence of live work, so it outranks any local terminal proof and keeps
    # the assignment quarantined.
    CURRENT_ASSIGNMENT_QUARANTINE="listener_exited_workflow_active"
    event job_lifecycle_quarantine "runner_rc=$runner_rc observation=$CURRENT_JOB_CAPTURE_STATUS evidence=github_api quarantine=$CURRENT_ASSIGNMENT_QUARANTINE rerun_eligible=false receipt=$CURRENT_JOB_RECEIPT"
    return 0
  fi
  # The observation never resolved. A measurement that failed is not evidence
  # of a stall, so a clean listener exit plus the runner's own completion line
  # settles terminality instead.
  local_result="$(runner_log_completion_result "$runner_log")" || local_result=""
  if [ "$runner_rc" = 0 ] && [ -n "$local_result" ]; then
    CURRENT_ASSIGNMENT_QUARANTINE="none"
    event job_terminal_receipt "runner_rc=$runner_rc observation=$CURRENT_JOB_CAPTURE_STATUS evidence=runner_local result=$local_result rerun_eligible=false receipt=$CURRENT_JOB_RECEIPT"
    return 0
  fi
  # Nothing proved terminality and nothing observed a stall. The event names
  # the failed measurement so a reader cannot mistake it for an observed one.
  CURRENT_ASSIGNMENT_QUARANTINE="listener_exit_terminal_unknown"
  event job_lifecycle_quarantine "runner_rc=$runner_rc observation=$CURRENT_JOB_CAPTURE_STATUS evidence=measurement_failed quarantine=$CURRENT_ASSIGNMENT_QUARANTINE rerun_eligible=false receipt=$CURRENT_JOB_RECEIPT"
}

finalize_listener_receipt(){
  local runner_rc="$1" listener_assigned="$2" runner_log="${3:-}"
  [ "$listener_assigned" = 1 ] || return 0
  if [ -n "$CURRENT_RUN_ID" ] && [ -n "$CURRENT_JOB_ID" ]; then
    capture_current_job revalidate || true
  fi
  record_terminal_job_receipt "$runner_rc" "$runner_log"
}

tartci_is_canonical_positive_decimal(){
  case "$1" in
    ''|0|0*|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

capture_current_job(){
  local mode="${1:-discover}" scan_mode result runner_registration rc previous_status
  local now started elapsed budget remaining attempt_timeout kind backoff scan_spent invalid_parameter
  local budget_parameter
  runner_registration="${CURRENT_REGISTERED_RUNNER:-$RUNNER_NAME}"
  scan_mode="$mode"
  case "$mode" in
    cancel_discover)
      scan_mode=discover
      budget="${TARTCI_CANCEL_DISCOVERY_BUDGET_SECS-30}"
      scan_spent="$CURRENT_CANCEL_DISCOVERY_SCAN_SPENT"
      budget_parameter="cancel_discovery_budget" ;;
    revalidate)
      budget="${TARTCI_CANCEL_REVALIDATION_BUDGET_SECS-30}"
      scan_spent="$CURRENT_CANCEL_REVALIDATION_SCAN_SPENT"
      budget_parameter="cancel_revalidation_budget" ;;
    terminal_revalidate)
      scan_mode=revalidate
      budget="${TARTCI_CANCEL_TERMINAL_OBSERVATION_BUDGET_SECS-30}"
      scan_spent="$CURRENT_CANCEL_TERMINAL_SCAN_SPENT"
      budget_parameter="cancel_terminal_observation_budget" ;;
    *)
      budget="${TARTCI_CAPTURE_CURRENT_JOB_LIFECYCLE_BUDGET_SECS-360}"
      scan_spent="$CURRENT_JOB_SCAN_SPENT"
      budget_parameter="lifecycle_budget" ;;
  esac
  # Discovery walks every in-progress run in the repository one call at a time,
  # so its cost tracks repository concurrency rather than a constant. Sixty
  # concurrent runs measure 93 to 106 seconds, and an attempt that cannot
  # outlast the scan can only ever report a failed measurement. The lifecycle
  # budget stays the larger of the two because the clamp below lowers an
  # attempt to whatever the budget has left.
  attempt_timeout="${TARTCI_CAPTURE_CURRENT_JOB_ATTEMPT_TIMEOUT_SECS-120}"
  invalid_parameter=""
  tartci_is_canonical_positive_decimal "$budget" || invalid_parameter="$budget_parameter"
  if [ -z "$invalid_parameter" ]; then
    tartci_is_canonical_positive_decimal "$attempt_timeout" \
      || invalid_parameter="attempt_timeout"
  fi
  if [ -n "$invalid_parameter" ]; then
    CURRENT_JOB_CAPTURE_STATUS="invalid_budget"
    CURRENT_JOB_RECEIPT="{\"kind\":\"invalid_budget\",\"detail\":\"$invalid_parameter\"}"
    CURRENT_ASSIGNMENT_QUARANTINE="observation_invalid_budget"
    event job_observation_error "kind=invalid_budget mode=$mode parameter=$invalid_parameter"
    return 2
  fi
  now="$(date +%s)"
  remaining=$((budget - scan_spent))
  [ "$remaining" -gt 0 ] || {
    CURRENT_JOB_CAPTURE_STATUS="budget_exhausted"
    CURRENT_JOB_RECEIPT='{"kind":"budget_exhausted"}'
    return 2
  }
  [ "$mode" != discover ] || [ "$now" -ge "$CURRENT_JOB_SCAN_NEXT_AT" ] || return 1
  [ "$attempt_timeout" -le "$remaining" ] || attempt_timeout="$remaining"
  local args=(
    "$TARTCI_ROOT/scripts/current_job_scan.py"
    --repo "$REPO"
    --runner "$runner_registration"
    --gh-cli "$GH_CLI"
    --max-pages "${TARTCI_CAPTURE_CURRENT_JOB_MAX_PAGES:-3}"
    --mode "$scan_mode"
    --gh-timeout "${TARTCI_CAPTURE_CURRENT_JOB_GH_TIMEOUT_SECS:-4}"
    --scan-timeout "$attempt_timeout"
    --result-cap "${TARTCI_CAPTURE_CURRENT_JOB_RESULT_CAP:-300}"
    --max-api-calls "${TARTCI_CAPTURE_CURRENT_JOB_MAX_API_CALLS:-310}"
  )
  if [ "$scan_mode" = revalidate ]; then
    [ -n "$CURRENT_RUN_ID" ] && [ -n "$CURRENT_JOB_ID" ] || return 1
    args+=(--run-id "$CURRENT_RUN_ID" --job-id "$CURRENT_JOB_ID")
  fi
  while IFS= read -r workflow; do
    [ -n "$workflow" ] && args+=(--workflow "$workflow")
  done <<<"$WORKFLOW_CONFIG"
  previous_status="$CURRENT_JOB_CAPTURE_STATUS"
  if ! CURRENT_SCAN_TMP="$(mktemp "$STATE_DIR/current-job-scan.XXXXXX")"; then
    CURRENT_JOB_CAPTURE_STATUS="setup_error"
    CURRENT_JOB_RECEIPT='{"kind":"setup_error","detail":"mktemp_failed"}'
    CURRENT_ASSIGNMENT_QUARANTINE="observation_setup_error"
    event job_observation_error "kind=setup_error stage=mktemp"
    return 2
  fi
  started="$(date +%s)"
  python3 "${args[@]}" >"$CURRENT_SCAN_TMP" 2>>"$EVENT_LOG" & CURRENT_SCAN_PID=$!
  while kill -0 "$CURRENT_SCAN_PID" 2>/dev/null; do
    heartbeat job-running
    sleep 1
  done
  wait "$CURRENT_SCAN_PID" || rc=$?
  rc="${rc:-0}"
  elapsed=$(( $(date +%s) - started ))
  [ "$elapsed" -gt 0 ] || elapsed=1
  case "$mode" in
    cancel_discover)
      CURRENT_CANCEL_DISCOVERY_SCAN_SPENT=$((CURRENT_CANCEL_DISCOVERY_SCAN_SPENT + elapsed)) ;;
    revalidate)
      CURRENT_CANCEL_REVALIDATION_SCAN_SPENT=$((CURRENT_CANCEL_REVALIDATION_SCAN_SPENT + elapsed)) ;;
    terminal_revalidate)
      CURRENT_CANCEL_TERMINAL_SCAN_SPENT=$((CURRENT_CANCEL_TERMINAL_SCAN_SPENT + elapsed)) ;;
    *)
      CURRENT_JOB_SCAN_SPENT=$((CURRENT_JOB_SCAN_SPENT + elapsed)) ;;
  esac
  result="$(tr -d '\n' <"$CURRENT_SCAN_TMP")"
  rm -f "$CURRENT_SCAN_TMP"
  CURRENT_SCAN_PID=""
  CURRENT_SCAN_TMP=""
  CURRENT_JOB_RECEIPT="$result"
  kind="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("kind", "invalid_receipt"))' 2>/dev/null || printf invalid_receipt)"
  CURRENT_JOB_CAPTURE_STATUS="$kind"
  case "$kind" in
    active)
      CURRENT_RUN_ID="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')"
      CURRENT_JOB_ID="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"
      CURRENT_WORKFLOW_NAME="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["workflow_name"])')"
      CURRENT_JOB_SCAN_FAILURES=0
      return 0 ;;
    no_assignment|terminal|terminal_pending_run|assignment_changed) return 1 ;;
    unexpected_assignment|ambiguous_assignment)
      [ "$previous_status" = "$kind" ] || event job_assignment_violation "receipt=$result"
      return 2 ;;
    *)
      CURRENT_JOB_SCAN_FAILURES=$((CURRENT_JOB_SCAN_FAILURES + 1))
      backoff=$((1 << (CURRENT_JOB_SCAN_FAILURES - 1)))
      [ "$backoff" -le 30 ] || backoff=30
      CURRENT_JOB_SCAN_NEXT_AT=$(( $(date +%s) + backoff ))
      [ "$previous_status" = "$kind" ] || event job_observation_error "scanner_rc=$rc kind=$kind mode=$mode spent=$((scan_spent + elapsed))s budget=${budget}s"
      return 2 ;;
  esac
}

cancel_current_run(){
  local receipt rc=0 deadline kind terminal_timeout run_conclusion
  terminal_timeout="${TARTCI_CANCEL_TERMINAL_TIMEOUT_SECS-30}"
  if ! tartci_is_canonical_positive_decimal "$terminal_timeout"; then
    CURRENT_JOB_CAPTURE_STATUS="invalid_budget"
    CURRENT_JOB_RECEIPT='{"kind":"invalid_budget","detail":"cancel_terminal_timeout"}'
    CURRENT_ASSIGNMENT_QUARANTINE="pre_cancel_invalid_budget"
    event run_cancel_suppressed "run_id=${CURRENT_RUN_ID:-} job_id=${CURRENT_JOB_ID:-} observation=invalid_budget scanner_rc=config rerun_eligible=false receipt=$CURRENT_JOB_RECEIPT"
    return 1
  fi
  if [ -z "$CURRENT_RUN_ID" ] || [ -z "$CURRENT_JOB_ID" ]; then
    if ! capture_current_job cancel_discover; then
      CURRENT_ASSIGNMENT_QUARANTINE="pre_cancel_discovery_${CURRENT_JOB_CAPTURE_STATUS}"
      event run_cancel_suppressed "run_id=${CURRENT_RUN_ID:-} job_id=${CURRENT_JOB_ID:-} observation=$CURRENT_JOB_CAPTURE_STATUS scanner_rc=discovery rerun_eligible=false receipt=$CURRENT_JOB_RECEIPT"
      return 1
    fi
  fi
  capture_current_job revalidate || rc=$?
  receipt="$CURRENT_JOB_RECEIPT"
  [ "$CURRENT_JOB_CAPTURE_STATUS" = active ] || {
    CURRENT_ASSIGNMENT_QUARANTINE="pre_cancel_${CURRENT_JOB_CAPTURE_STATUS}"
    event run_cancel_suppressed "run_id=${CURRENT_RUN_ID:-} job_id=${CURRENT_JOB_ID:-} observation=$CURRENT_JOB_CAPTURE_STATUS scanner_rc=$rc rerun_eligible=false receipt=$receipt"
    return 1
  }
  if ! "$GH_CLI" api -X POST "repos/$REPO/actions/runs/$CURRENT_RUN_ID/cancel" >/dev/null 2>&1; then
    event run_cancel_failed "run_id=$CURRENT_RUN_ID job_id=$CURRENT_JOB_ID rerun_eligible=false"
    CURRENT_ASSIGNMENT_QUARANTINE="cancel_post_failed"
    return 1
  fi
  event run_cancel_requested "run_id=$CURRENT_RUN_ID job_id=$CURRENT_JOB_ID rerun_eligible=pending-terminal"
  deadline=$(( $(date +%s) + terminal_timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    rc=0
    capture_current_job terminal_revalidate || rc=$?
    receipt="$CURRENT_JOB_RECEIPT"
    kind="$CURRENT_JOB_CAPTURE_STATUS"
    if [ "$kind" = terminal ]; then
      CURRENT_ASSIGNMENT_QUARANTINE="none"
      run_conclusion="$(printf '%s' "$receipt" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("run_conclusion") or "")' 2>/dev/null || true)"
      if [ "$run_conclusion" = cancelled ]; then
        event run_cancel_terminal "run_id=$CURRENT_RUN_ID job_id=$CURRENT_JOB_ID rerun_eligible=true receipt=$receipt"
        return 0
      fi
      event run_terminal_without_cancel "run_id=$CURRENT_RUN_ID job_id=$CURRENT_JOB_ID conclusion=$run_conclusion rerun_eligible=false receipt=$receipt"
      return 1
    fi
    case "$kind" in
      active|terminal_pending_run) ;;
      assignment_changed)
        CURRENT_ASSIGNMENT_QUARANTINE="orphaned_assignment"
        event run_cancel_orphaned "run_id=$CURRENT_RUN_ID job_id=$CURRENT_JOB_ID rerun_eligible=false receipt=$receipt"
        return 1 ;;
      *)
        CURRENT_ASSIGNMENT_QUARANTINE="cancel_terminal_unknown"
        event run_cancel_unknown "run_id=$CURRENT_RUN_ID job_id=$CURRENT_JOB_ID observation=$kind scanner_rc=$rc rerun_eligible=false receipt=$receipt"
        return 1 ;;
    esac
    heartbeat cancel-pending-terminal
    sleep 2
  done
  CURRENT_ASSIGNMENT_QUARANTINE="cancel_terminal_timeout"
  CURRENT_JOB_CAPTURE_STATUS="terminal_unknown"
  CURRENT_JOB_RECEIPT='{"kind":"terminal_unknown","detail":"cancel_poll_timeout"}'
  event run_cancel_unknown "run_id=$CURRENT_RUN_ID job_id=$CURRENT_JOB_ID observation=terminal_unknown rerun_eligible=false receipt=$CURRENT_JOB_RECEIPT"
  return 1
}

ensure_runner_version(){
  local ip="$1"
  ssh "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    "bash -s -- '$RUNNER_VERSION' '$RUNNER_SHA256' '$GUEST_HTTP_PROXY'" <<'GUEST'
set -euo pipefail
desired="$1"
expected_sha256="$2"
guest_http_proxy="$3"
if [ -n "$guest_http_proxy" ]; then
  export HTTP_PROXY="$guest_http_proxy" HTTPS_PROXY="$guest_http_proxy"
  export http_proxy="$guest_http_proxy" https_proxy="$guest_http_proxy"
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
  local vm="$1" ip="$2" jit="$3" selected_tier="${4:-0}"
  local runner_log="$STATE_DIR/$vm.actions-runner.log"
  local aqua_label="com.tartci.aqua.$vm"
  local ssh_pid start assigned_at=0 now idle_elapsed job_elapsed assigned=0 warned=0 rc=0
  local retarget_next=0
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
     ln -sfn '/Volumes/My Shared Files/ccache' ~/Library/Caches/ccache && \
     export CCACHE_NODEPEND=true CCACHE_COMPILERCHECK=content CCACHE_MAXSIZE='$CCACHE_MAX_SIZE' && unset CCACHE_DEPEND && \
     mkdir -p \"\$HOME/Library/Caches/Pulp/fetchcontent-src\" && \
     fetchcontent_hydrated=false && \
     for attempt in 1 2 3; do if rsync -a '/Volumes/My Shared Files/fetchcontent/' \"\$HOME/Library/Caches/Pulp/fetchcontent-src/\"; then fetchcontent_hydrated=true; break; fi; [ \"\$attempt\" -eq 3 ] || sleep 1; done && \
     if [ \"\$fetchcontent_hydrated\" != true ]; then echo 'tartci: FetchContent seed changed during three hydration attempts' >&2; exit 1; fi && \
     cd ~/actions-runner && touch .env && \
     awk -F= '\$1 !~ /^(CCACHE_DEPEND|CCACHE_NODEPEND|CCACHE_COMPILERCHECK|CCACHE_MAXSIZE|PULP_SHARED_FETCHCONTENT_SOURCE_DIR|FETCHCONTENT_BASE_DIR|HTTP_PROXY|HTTPS_PROXY|NO_PROXY|http_proxy|https_proxy|no_proxy|TARTCI_GUEST_CORES|TARTCI_GUEST_MEM_MB|TARTCI_PIP_WHEELHOUSE)$/' .env > .env.tartci && \
     printf '%s\n' 'CCACHE_NODEPEND=true' 'CCACHE_COMPILERCHECK=content' 'CCACHE_MAXSIZE=$CCACHE_MAX_SIZE' >> .env.tartci && \
     printf 'PULP_SHARED_FETCHCONTENT_SOURCE_DIR=%s\n' \"\$HOME/Library/Caches/Pulp/fetchcontent-src\" >> .env.tartci && \
     if [ -n '$GUEST_HTTP_PROXY' ]; then printf '%s\n' 'HTTP_PROXY=$GUEST_HTTP_PROXY' 'HTTPS_PROXY=$GUEST_HTTP_PROXY' 'http_proxy=$GUEST_HTTP_PROXY' 'https_proxy=$GUEST_HTTP_PROXY' 'NO_PROXY=127.0.0.1,localhost,::1' 'no_proxy=127.0.0.1,localhost,::1' >> .env.tartci; fi && \
     if [ -n '$CURRENT_GUEST_CORES' ]; then printf 'TARTCI_GUEST_CORES=%s\n' '$CURRENT_GUEST_CORES' >> .env.tartci; fi && \
     if [ -n '$CURRENT_GUEST_MEM_MB' ]; then printf 'TARTCI_GUEST_MEM_MB=%s\n' '$CURRENT_GUEST_MEM_MB' >> .env.tartci; fi && \
     if [ '$CURRENT_PIP_WHEELHOUSE' = 1 ]; then printf 'TARTCI_PIP_WHEELHOUSE=%s\n' '$GUEST_PIP_WHEELHOUSE' >> .env.tartci; fi && \
     mv .env.tartci .env && \
     export PULP_SHARED_FETCHCONTENT_SOURCE_DIR=\"\$HOME/Library/Caches/Pulp/fetchcontent-src\" && \
     \$HOME/.tartci/bin/guest-aqua-runner.sh run '$aqua_label'" \
    >"$runner_log" 2>&1 & ssh_pid=$!
  if ! tartci_pool_lock_handoff_to_listener "$ssh_pid"; then
    note "[$vm] listener exited before pool transition handoff"
    kill "$ssh_pid" 2>/dev/null || true
    wait "$ssh_pid" 2>/dev/null || true
    return 1
  fi
  start="$(date +%s)"
  [ "$ASSIGNMENT_V2_IDLE_RETARGET_SECS" -le 0 ] \
    || retarget_next=$((start + ASSIGNMENT_V2_IDLE_RETARGET_SECS))
  while kill -0 "$ssh_pid" 2>/dev/null; do
    now="$(date +%s)"
    idle_elapsed=$((now - start))
    if [ "$assigned" = 0 ] && grep -q 'Running job:' "$runner_log" 2>/dev/null; then
      assigned=1
      assigned_at="$now"
      for _ in $(seq 1 6); do
        capture_current_job && break
        sleep 2
      done
      CURRENT_SERVED=1
      event job_assigned "$(grep 'Running job:' "$runner_log" | tail -1)"
      # The queued job this claim stood for is gone from the queue now.
      tartci_job_claim_release
      heartbeat job-running
    fi
    if [ "$assigned" = 0 ] && [ "$idle_elapsed" -ge "$IDLE_TIMEOUT" ]; then
      event idle_timeout "elapsed=${idle_elapsed}s rerun_eligible=false"
      kill "$ssh_pid" 2>/dev/null || true
      wait "$ssh_pid" 2>/dev/null || true
      sed 's/^/[actions-runner] /' "$runner_log" >&2 || true
      return 124
    fi
    if [ "$assigned" = 0 ] && [ "$retarget_next" -gt 0 ] && [ "$now" -ge "$retarget_next" ]; then
      heartbeat idle-retarget-check
      if tartci_assignment_v2_idle_retarget "$selected_tier" "$idle_elapsed"; then
        # The observation took real time. A job assigned to this runner while
        # it ran is exactly the work the retarget exists to protect, so the
        # log is re-read at the last moment and an assigned runner is kept.
        if grep -q 'Running job:' "$runner_log" 2>/dev/null; then
          event assignment_v2_idle_retarget_overtaken "elapsed=${idle_elapsed}s"
        else
          kill "$ssh_pid" 2>/dev/null || true
          wait "$ssh_pid" 2>/dev/null || true
          sed 's/^/[actions-runner] /' "$runner_log" >&2 || true
          return "$IDLE_RETARGET_RC"
        fi
      fi
      # Rearmed from after the scan, never from before it, so a slow
      # observation cannot make the next check due the instant this one ends.
      retarget_next=$(( $(date +%s) + ASSIGNMENT_V2_IDLE_RETARGET_SECS ))
    fi
    if [ "$assigned" = 1 ]; then
      job_elapsed=$((now - assigned_at))
      [ -n "$CURRENT_RUN_ID" ] || capture_current_job || true
      if [ "$warned" = 0 ] && [ "$job_elapsed" -ge "$JOB_WARN" ]; then
        warned=1
        event job_warn "elapsed=${job_elapsed}s"
      fi
      if [ "$job_elapsed" -ge "$JOB_TIMEOUT" ]; then
        event job_timeout "elapsed=${job_elapsed}s run_id=${CURRENT_RUN_ID:-} job_id=${CURRENT_JOB_ID:-} rerun_eligible=pending-revalidation observation=$CURRENT_JOB_CAPTURE_STATUS"
        cancel_current_run || true
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
  finalize_listener_receipt "$rc" "$assigned" "$runner_log"
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
  # Before the first early return, not beside the other CURRENT_* resets: the
  # pre-clone admission bail below returns above those.
  CURRENT_SERVED=0
  JOB_CLAIM_CONTENDED=0
  vm="$(ephemeral_boot_name "$i")"
  local jit="" label_args=() labels_split=() l boot_log rpid ip="" rc=0
  local selected_group_id selected_runner_api_root access_json access_rc access_error
  local lease_cores lease_mem lease_priority lease_rc
  local t_start t_booted t_runner_done t_done logdir=""
  t_start="$(now_epoch)"
  selected_group_id="$(runner_group_id_for_tier "$selected_tier")" \
    || { note "[$i] no runner-group contract for workflow tier $selected_tier"; return 1; }
  selected_runner_api_root="$(runner_api_root_for_group "$selected_group_id")"
  CURRENT_RUNNER_API_ROOT="$selected_runner_api_root"
  if jit_admission_denied "$selected_group_id" "$selected_labels"; then
    note "[$i] JIT admission remains blocked for this exact repository/group/class contract — no VM will boot; inspect $JIT_DENIAL_FILE"
    return 75
  fi
  if ! tartci_pool_lock_absent; then
    note "[$i] pool transition lock exists before VM allocation — deferring without boot"
    return 75
  fi
  # One booting VM per queued job: a lane whose class's queued jobs are all
  # covered by another lane (on this host, or already minted in the fleet)
  # does not clone. See providers/tart-macos/job-claim.lib.sh.
  # Only the explicit "covered" answer (75) stops the boot; anything else,
  # including a failure of the claim machinery itself, boots as before.
  local claim_rc=0
  tartci_job_claim_acquire "$vm" "$selected_labels" "$selected_tier" \
    "${CURRENT_SELECTED_QUEUED:-}" "$selected_runner_api_root" || claim_rc=$?
  if [ "$claim_rc" -eq 75 ]; then
    heartbeat job-claim-covered
    return 75
  fi
  # The verdict is a function of (repo, labels) alone — see
  # providers/common/admission-clean.lib.sh, which forwards exactly those two
  # plus the lane's static base branch — so it can be asked BEFORE the CoW
  # clone instead of only after a full clone and boot. A refusal here skips the
  # clone, the disk reservation and the VM lease entirely, so a lane whose
  # admission authority is down backs off cheaply instead of minting and
  # discarding a VM every cycle.
  #
  # This is an early bail, not the gate. The authoritative check still runs at
  # the JIT boundary below, where freshness is what matters; nothing here can
  # admit a VM that the boundary check would refuse.
  if tartci_admission_clean_enabled; then
    local precheck_json="" precheck_rc=0
    heartbeat admission-precheck
    event admission_precheck "repo=$REPO labels=$selected_labels"
    if precheck_json="$(tartci_admission_clean "$REPO" "$selected_labels")"; then
      precheck_rc=0
    else
      precheck_rc=$?
    fi
    # One rolling envelope per lane: the reason now travels in the event, so
    # this is a fallback copy and must not grow a file per attempt.
    [ -z "$precheck_json" ] \
      || printf '%s\n' "$precheck_json" >"$STATE_DIR/$RUNNER_NAME.admission-precheck.json"
    if [ "$precheck_rc" -ne 0 ]; then
      local precheck_detail
      precheck_detail="$(tartci_admission_clean_detail "$precheck_json")" \
        || precheck_detail="reason=unreadable"
      heartbeat "$([ "$precheck_rc" -eq 3 ] && printf admission-precheck-deferred || printf admission-precheck-error)"
      event "$([ "$precheck_rc" -eq 3 ] && printf admission_precheck_deferred || printf admission_precheck_error)" \
        "rc=$precheck_rc pre_clone=true $precheck_detail"
      note "[$i] Shipyard admission $([ "$precheck_rc" -eq 3 ] && printf deferred || printf failed) before clone — no VM will be cloned; backing off ($precheck_detail)"
      return "$precheck_rc"
    fi
  fi
  if [ "${TARTCI_RUNTIME_MEASURE:-0}" = 1 ]; then
    logdir="$MACOS_LOGROOT/$vm"
    tartci_prepare_and_check_disk_root_observed "$logdir" "" "" tart-macos \
      "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}" "$RUNNER_NAME" || return $?
  fi
  tartci_check_macos_disk_floor_with_cleanup_once "$TART_HOME" \
    "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}" "$RUNNER_NAME" || return $?
  tartci_prepare_and_check_disk_root_observed "$CACHE_ROOT" "" "" tart-macos \
    "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}" "$RUNNER_NAME" || return $?
  CLEANED_UP=0
  CURRENT_REGISTERED_RUNNER=""
  CURRENT_RUN_ID=""
  CURRENT_JOB_ID=""
  CURRENT_WORKFLOW_NAME=""
  CURRENT_JOB_CAPTURE_STATUS="not-attempted"
  CURRENT_JOB_RECEIPT=""
  CURRENT_JOB_SCAN_SPENT=0
  CURRENT_JOB_SCAN_FAILURES=0
  CURRENT_JOB_SCAN_NEXT_AT=0
  CURRENT_CANCEL_DISCOVERY_SCAN_SPENT=0
  CURRENT_CANCEL_REVALIDATION_SCAN_SPENT=0
  CURRENT_CANCEL_TERMINAL_SCAN_SPENT=0
  CURRENT_ASSIGNMENT_QUARANTINE="none"
  CURRENT_LABELS="$selected_labels"
  reclaim_runner_name "$vm" "$selected_runner_api_root"
  sweep_lane_ghost_runners "$selected_runner_api_root" "$vm"
  lease_cores="$(tartci_vm_lease_cores tart-macos)"
  lease_mem="$(tartci_vm_lease_mem_mb tart-macos)"
  lease_priority="$(tartci_vm_lease_priority "$selected_labels")"
  lease_rc=0
  tartci_acquire_vm_lease "$vm" "$lease_cores" "tart-macos-vm" "$lease_priority" "$selected_labels" "$lease_mem" "$TART_HOME" \
    tart-macos "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}" "$RUNNER_NAME" || lease_rc=$?
  if [ "$lease_rc" -ne 0 ]; then
    # The only loop path that reaches work and then fails before any heartbeat.
    # Without this the supervisor is silent while healthy, and a checker that
    # can only see heartbeat age has no choice but to call it stale.
    [ -n "$SERVING_BLOCKED_SINCE" ] \
      || SERVING_BLOCKED_SINCE="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    heartbeat vm-lease-denied
    return "$lease_rc"
  fi
  lease_cores="${TARTCI_ACTIVE_VM_LEASE_CORES:-$lease_cores}"
  lease_mem="${TARTCI_ACTIVE_VM_LEASE_MEM_MB:-$lease_mem}"
  # The admission verdict and the repository-access proof do not read the VM,
  # so they run beside the clone and boot and are consumed at the boundary.
  # See providers/tart-macos/boundary-proof.lib.sh for why this cannot admit
  # anything the sequential boundary would have refused.
  tartci_boundary_proof_start "$vm" "$selected_labels" "$selected_group_id"

  note "[$i] clone $GOLDEN → $vm (CoW) + boot with host ccache mounted"
  event clone_start "golden=$GOLDEN"
  # Own the unique per-boot name before the foreground clone so signal cleanup
  # cannot miss a clone completed immediately before the trap is delivered.
  CURRENT_VM="$vm"
  if ! tartci_vm_lease_guard_run tart clone "$GOLDEN" "$vm"; then
    discard_current_vm
    tartci_release_vm_lease
    runtime_emit_complete fail boot_failed 1 "" "$logdir"
    return 1
  fi
  if ! tartci_set_tart_vm_size "$vm" "$lease_cores" "$lease_mem"; then
    note "[$i] failed to size $vm to lease cores=$lease_cores mem_mb=${lease_mem:-golden}"
    discard_current_vm
    tartci_release_vm_lease
    runtime_emit_complete fail boot_failed 1 "" "$logdir"
    return 1
  fi
  if ! tartci_prepare_disk_root "$CACHE_ROOT/ccache"; then
    discard_current_vm
    tartci_release_vm_lease
    runtime_emit_complete fail cache_setup_failed 1 "" "$logdir"
    return 1
  fi
  if ! tartci_prepare_disk_root "$FETCHCONTENT_SOURCE_ROOT"; then
    discard_current_vm
    tartci_release_vm_lease
    runtime_emit_complete fail cache_setup_failed 1 "" "$logdir"
    return 1
  fi
  CURRENT_GUEST_CORES="$lease_cores"
  CURRENT_GUEST_MEM_MB="$lease_mem"
  boot_log="$(mktemp -t "tart-run-$vm")"
  local tart_dirs=(
    --dir="ccache:$CACHE_ROOT/ccache"
    --dir="fetchcontent:$FETCHCONTENT_SOURCE_ROOT:ro"
  )
  [ -z "$CHROME_MOUNT_ARG" ] || tart_dirs+=(--dir="$CHROME_MOUNT_ARG")
  CURRENT_PIP_WHEELHOUSE=0
  if pip_wheelhouse_ready "$PIP_WHEELHOUSE_ROOT"; then
    tart_dirs+=(--dir="pip-wheelhouse:$PIP_WHEELHOUSE_ROOT:ro")
    CURRENT_PIP_WHEELHOUSE=1
  fi
  tartci_vm_lease_guard_exec tart run --no-graphics "${tart_dirs[@]}" \
    "$vm" >"$boot_log" 2>&1 & rpid=$!
  CURRENT_RPID="$rpid"
  heartbeat booting

  for _ in $(seq 1 60); do ip="$(tart ip "$vm" 2>/dev/null || true)"; [ -n "$ip" ] && break; sleep 2; done
  if [ -z "$ip" ]; then
    note "[$i] no IP after 120s — last tart run lines:"; tail -10 "$boot_log" >&2 2>/dev/null || true
    rm -f "$boot_log"; event boot_failed "no_ip"; runtime_emit_complete fail boot_failed 1 "" "$logdir"
    discard_current_vm
    tartci_release_vm_lease
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
    discard_current_vm
    tartci_release_vm_lease
    return 1
  fi
  t_booted="$(now_epoch)"
  if [ "$ASSIGNMENT_MODE" != event-class-v2 ] && higher_priority_demand "$selected_tier"; then
    note "[$i] higher-priority workflow demand appeared during boot — discarding unregistered tier-$selected_tier VM"
    event yielded_to_workflow_tier "selected_tier=$selected_tier labels=$selected_labels"
    discard_current_vm
    tartci_release_vm_lease
    CURRENT_LABELS="$LABELS"
    return 75
  fi
  heartbeat ensuring-runner
  event runner_version "required=$RUNNER_VERSION"
  if ! ensure_runner_version "$ip"; then
    note "[$i] Actions Runner v$RUNNER_VERSION install/verification failed — discarding unregistered VM"
    event runner_version_failed "required=$RUNNER_VERSION"
    runtime_emit_complete fail runner_install_failed 1 "" "$logdir"
    discard_current_vm
    tartci_release_vm_lease
    return 1
  fi

  heartbeat aqua-preflight
  event aqua_preflight "uid=501 vm=$vm"
  if ! install_and_preflight_aqua_runner "$ip" "$vm"; then
    event aqua_preflight_failed "uid=501 unregistered=true"
    runtime_emit_complete fail aqua_preflight_failed 1 "" "$logdir"
    discard_current_vm
    tartci_release_vm_lease
    return 1
  fi

  if [ -n "$CHROME_MOUNT_ARG" ]; then
    heartbeat chrome-preflight
    if ! install_and_preflight_chrome "$ip"; then
      note "[$i] governed read-only Google Chrome mount failed preflight — discarding unregistered VM"
      event chrome_preflight_failed "unregistered=true"
      runtime_emit_complete fail chrome_preflight_failed 1 "" "$logdir"
      discard_current_vm
      tartci_release_vm_lease
      return 1
    fi
  fi

  if tartci_admission_clean_enabled; then
    local admission_json="" admission_rc=0
    heartbeat admission-check
    if tartci_boundary_proof_take_admission; then
      admission_json="$BOUNDARY_ADMISSION_JSON"
      admission_rc="$BOUNDARY_ADMISSION_RC"
      event admission_check "repo=$REPO labels=$selected_labels source=parallel age=${BOUNDARY_ADMISSION_AGE}s"
    else
      event admission_check "repo=$REPO labels=$selected_labels source=boundary"
      if admission_json="$(tartci_admission_clean "$REPO" "$selected_labels")"; then
        admission_rc=0
      else
        admission_rc=$?
      fi
    fi
    [ -z "$admission_json" ] \
      || printf '%s\n' "$admission_json" >"$STATE_DIR/$vm.admission-clean.json"
    if [ "$admission_rc" -ne 0 ]; then
      local admission_detail
      admission_detail="$(tartci_admission_clean_detail "$admission_json")" \
        || admission_detail="reason=unreadable"
      heartbeat "$([ "$admission_rc" -eq 3 ] && printf admission-deferred || printf admission-error)"
      event "$([ "$admission_rc" -eq 3 ] && printf admission_deferred || printf admission_error)" \
        "rc=$admission_rc unregistered=true $admission_detail"
      note "[$i] Shipyard admission $([ "$admission_rc" -eq 3 ] && printf deferred || printf failed) at the JIT boundary — discarding unregistered VM and backing off ($admission_detail)"
      discard_current_vm
      tartci_release_vm_lease
      return "$admission_rc"
    fi
  fi

  access_error="$STATE_DIR/$vm.repository-access-error"
  access_rc=0
  if tartci_boundary_proof_take_access "$access_error"; then
    access_json="$BOUNDARY_ACCESS_JSON"
    access_rc="$BOUNDARY_ACCESS_RC"
  elif access_json="$(SHIPYARD_GH_APP_REPO="$REPO" GH_REPO="$REPO" \
      python3 "$TARTCI_ROOT/scripts/runner_group_repository_access.py" \
      --repo "$REPO" --runner-group-id "$selected_group_id" \
      --gh-cli "$JIT_GH_CLI" 2>"$access_error")"; then
    access_rc=0
  else
    access_rc=$?
  fi
  tartci_boundary_proof_abandon
  [ -z "$access_json" ] \
    || printf '%s\n' "$access_json" >"$STATE_DIR/$vm.repository-access.json"
  if [ "$access_rc" -ne 0 ]; then
    if [ "$access_rc" -eq 3 ] \
       || grep -Eq 'HTTP (401|403|404)|Resource not accessible by integration' "$access_error"; then
      record_jit_admission_denied "$selected_group_id" "$selected_labels" \
        "repository access denied before JIT registration; inspect $access_error"
      event jit_repository_access_denied \
        "repo=$REPO group=$selected_group_id labels=$selected_labels"
    fi
    note "[$i] runner group cannot prove repository access — refusing JIT registration and discarding VM"
    discard_current_vm
    tartci_release_vm_lease
    return "$access_rc"
  fi
  rm -f "$access_error"

  heartbeat minting-jit
  if ! tartci_pool_lock_acquire; then
    note "[$i] pool transition busy before JIT mint — discarding unassigned VM"
    discard_current_vm
    tartci_release_vm_lease
    return 75
  fi
  if [ "$ASSIGNMENT_MODE" = event-class-v2 ] \
     && ! tartci_assignment_v2_pre_mint_admit "$selected_tier"; then
    tartci_pool_lock_release
    note "[$i] V2 assignment demand changed or became uncertain before JIT mint — discarding unassigned VM"
    event assignment_v2_pre_mint_denied \
      "selected_tier=$selected_tier labels=$selected_labels"
    discard_current_vm
    tartci_release_vm_lease
    return 75
  fi
  # Re-check emergency admission at the last possible boundary.
  if ! tartci_pool_admission_open; then
    tartci_pool_lock_release
    note "[$i] pool $(tartci_pool_read_state) after repository-access proof — discarding unassigned VM"
    discard_current_vm
    tartci_release_vm_lease
    return 75
  fi
  event mint_jit "labels=$selected_labels tier=$selected_tier"
  # Claim the exact per-boot registration name before minting. Cleanup can then
  # reclaim it even when a signal lands immediately after GitHub creates it.
  CURRENT_REGISTERED_RUNNER="$vm"
  IFS=',' read -r -a labels_split <<< "$selected_labels"
  for l in "${labels_split[@]}"; do label_args+=(-f "labels[]=$l"); done
  local jit_error="$STATE_DIR/$vm.jit-error"
  jit="$(SHIPYARD_GH_APP_REPO="$REPO" GH_REPO="$REPO" \
        "$JIT_GH_CLI" api -X POST "$selected_runner_api_root/generate-jitconfig" \
        -f "name=$vm" -F "runner_group_id=$selected_group_id" "${label_args[@]}" \
        --jq '.encoded_jit_config' 2>"$jit_error")" || {
    tartci_pool_lock_release
    if grep -Eq 'HTTP (401|403|404)|Resource not accessible by integration' "$jit_error"; then
      record_jit_admission_denied "$selected_group_id" "$selected_labels" \
        "GitHub rejected JIT registration; inspect $jit_error and change the explicit JIT auth route before retrying"
      event jit_admission_denied "repo=$REPO group=$selected_group_id gh_cli=$JIT_GH_CLI"
      note "[$i] JIT admission denied — blocking this exact auth/runner contract before another VM boots"
    fi
    note "[$i] JIT config mint failed — discarding VM"
    cleanup
    return 1
  }
  rm -f "$jit_error"
  clear_jit_admission_denied "$selected_group_id" "$selected_labels"
  if [ -z "$jit" ]; then
    tartci_pool_lock_release
    note "[$i] empty JIT config — discarding VM"
    cleanup
    return 1
  fi
  note "[$i] vm $vm up at $ip — launching JIT runner (idle_timeout=${IDLE_TIMEOUT}s idle_retarget=${ASSIGNMENT_V2_IDLE_RETARGET_SECS}s job_timeout=${JOB_TIMEOUT}s)"
  event boot_ok "ip=$ip"
  heartbeat idle-wait

  run_runner_until_done "$vm" "$ip" "$jit" "$selected_tier" || rc=$?
  tartci_pool_lock_release
  t_runner_done="$(now_epoch)"
  if [ "$rc" -eq "$IDLE_RETARGET_RC" ]; then
    # The cached selection is what booted this class; a fresh live selection
    # is what lets the next pass serve the class that is actually waiting.
    tartci_assignment_v2_invalidate_selection
    note "[$i] idle tier-$selected_tier runner retargeted — discarding it so this slot can serve the waiting class"
  elif [ "$rc" -ne 0 ]; then note "[$i] runner exited non-zero rc=$rc — VM will be discarded"; fi

  note "[$i] discarding ephemeral VM $vm"
  event teardown "rc=$rc"
  if ! discard_current_vm; then
    note "[$i] teardown ownership could not be proved — capacity stays held (pending-delete when the guardian is terminal, fail-closed restart otherwise)"
    return 75
  fi
  tartci_release_vm_lease
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
    elif [ "$rc" -eq "$IDLE_RETARGET_RC" ]; then
      runtime_emit_complete fail idle_retarget "$rc" "$logdir/timing.tsv" "$logdir"
    else
      runtime_emit_complete fail source_failure "$rc" "$logdir/timing.tsv" "$logdir"
    fi
  fi
  CURRENT_RUN_ID=""
  CURRENT_JOB_ID=""
  CURRENT_WORKFLOW_NAME=""
  CURRENT_LABELS="$LABELS"
  reclaim_runner_name "$vm" "$selected_runner_api_root"
  CURRENT_REGISTERED_RUNNER=""
  CURRENT_RUNNER_API_ROOT="$RUNNER_API_ROOT"
  heartbeat stopped
  CLEANED_UP=1
  return 0
}

i=0
[ "$PRINT_QUEUE" = 1 ] && { print_queued_work; exit 0; }
[ "$PRINT_SELECTION" = 1 ] && { select_work | tr '|' '\t'; exit 0; }
[ "$PRINT_ASSIGNMENT_PARITY" = 1 ] && {
  [ "$ASSIGNMENT_MODE" != legacy ] \
    || die "--print-assignment-parity requires TARTCI_RUNNER_ASSIGNMENT_MODE=observe or event-class-v2"
  tartci_assignment_v2_parity
  exit 0
}
[ -n "$PRINT_PRE_MINT_SELECTION" ] && {
  if tartci_assignment_v2_pre_mint_admit "$PRINT_PRE_MINT_SELECTION"; then printf '1\n'; else printf '0\n'; fi
  exit 0
}
[ -n "$PRINT_IDLE_RETARGET" ] && {
  # The same decision the idle-wait loop makes, minus its interval gating: 1
  # means an idle runner of this tier would be discarded to serve another
  # class, 0 means it would be held. With the knob off it is 0 and makes no
  # GitHub call, which is the control every enabled-path test compares against.
  if [ "$ASSIGNMENT_V2_IDLE_RETARGET_SECS" -gt 0 ] \
     && tartci_assignment_v2_idle_retarget "$PRINT_IDLE_RETARGET" "$ASSIGNMENT_V2_IDLE_RETARGET_SECS"; then
    printf '1\n'
  else
    printf '0\n'
  fi
  exit 0
}
[ -n "$PRINT_FALLBACK_DECISION" ] && {
  # The decision a selection pass would make for this zero-based tier's young
  # demand: `grant`, `hold` or `unknown`, then its detail. Off prints `off`
  # and makes no GitHub or SSH call.
  tartci_fallback_decision_for_tier "$PRINT_FALLBACK_DECISION"
  exit 0
}
[ -n "$PRINT_HIGHER_PRIORITY" ] && {
  if higher_priority_demand "$PRINT_HIGHER_PRIORITY"; then printf '1\n'; else printf '0\n'; fi
  exit 0
}
[ "$PRINT_PRIORITY" = 1 ] && { priority_demand; exit 0; }
[ "$PRINT_YIELD_BOUND" = 1 ] && {
  selection="$(select_work)"
  IFS='|' read -r _ selected_labels _ <<< "$selection"
  yield_bound_reached "$selected_labels"
  exit 0
}
[ "$PRINT_HOST_HEALTH" = 1 ] && { tartci_host_health_yield; exit 0; }
trap 'handle_supervisor_signal' INT TERM
trap 'cleanup' EXIT
tartci_validate_admission_clean_config "$REPO" "$LABELS" \
  || die "invalid required Shipyard admission-clean configuration"
tartci_boundary_proof_validate \
  || die "invalid parallel boundary-proof configuration"
tartci_job_claim_validate || die "invalid job-claim configuration"
tartci_lease_fit_validate || die "invalid lease-fit configuration"

# Part F — host-wide macOS VM cap (live, GUI-adjustable) + cross-lane mutex.
# shellcheck source=providers/tart-macos/macos-vm-cap.lib.sh
source "${BASH_SOURCE[0]%/*}/macos-vm-cap.lib.sh"

if [ "$LOOP" = 1 ]; then
  note "ephemeral macOS runner LOOP; golden=$GOLDEN labels=$LABELS workflows=$WORKFLOW_DISPLAY tiers=${TIER_LABELS_CONFIG:-<off>} assignment_mode=$ASSIGNMENT_MODE assignment_v2_base=${ASSIGNMENT_V2_BASE_LABELS:-<off>} tier_order=${ASSIGNMENT_V2_TIER_ORDER:-<configured>} cap=$CAP yield_to=${YIELD_WORKFLOW:-<off>} yield_max_wait=${YIELD_MAX_WAIT}s host_vitals_yield=${TARTCI_HOST_VITALS_YIELD:-<off>}"
  # Scan-blindness self-heal: `queued_work` prints `ERR` (not a count) when the gh queue scan fails.
  # Treating that as 0 silently idles the supervisor while jobs pile up (the observed multi-hour
  # wedge). Count consecutive blind polls; after ~this many seconds of continuous blindness,
  # self-restart so launchd (KeepAlive) respawns a fresh process with fresh gh auth — the exact
  # manual recovery, automated. A blip self-heals on the next successful poll (blind resets to 0).
  blind=0
  BLIND_MAX="${TARTCI_SCAN_BLIND_MAX:-$(( (180 + POLL - 1) / POLL ))}"
  # The restart remedy is bounded. It survives the restart it triggers, so it
  # must live on disk rather than in this process.
  BLIND_RESTART_MAX="${TARTCI_SCAN_BLIND_RESTART_MAX:-3}"
  BLIND_RESTART_FILE="$STATE_DIR/$RUNNER_NAME.scan-blind-restarts"
  BLIND_ESCALATION_FILE="$STATE_DIR/$RUNNER_NAME.scan-blind-escalated"
  heartbeat loop
  while true; do
    if [ -n "$CURRENT_VM" ]; then
      # Only a pending-delete VM can survive to the loop top (anything else
      # exits below). Reconcile it before any new admission; while it is
      # pending, this lane's lease and reservation keep the capacity occupied.
      pending_rc=0
      reconcile_pending_delete || pending_rc=$?
      if [ "$pending_rc" -eq 2 ]; then
        event teardown_restart "vm=$CURRENT_VM rc=75 pending_delete_attempts=$PENDING_DELETE_ATTEMPTS"
        exit 75
      fi
      if [ "$pending_rc" -ne 0 ]; then
        heartbeat teardown-pending
        sleep "$PENDING_DELETE_RETRY_SECS"
        continue
      fi
      heartbeat loop
    fi
    if ! tartci_pool_admission_open; then
      note "pool $(tartci_pool_read_state) — no new macOS admission; waiting ${POLL}s"
      heartbeat draining
      sleep "$POLL"
      continue
    fi
    # A lane whose VM lease cannot be granted right now (or ever) has nothing
    # to boot, so it neither scans the queue nor asks Shipyard. Local and
    # read-only; fails open. See providers/tart-macos/lease-fit.lib.sh.
    if ! tartci_lease_fit_gate; then
      sleep "$POLL"
      continue
    fi
    selection="$(select_work)"
    IFS='|' read -r q selected_labels selected_tier <<< "$selection"
    if printf '%s' "$q" | grep -qxE '[1-9][0-9]*'; then
      selected_group_id="$(runner_group_id_for_tier "$selected_tier")" || selected_group_id=""
      if [ -z "$selected_group_id" ]; then
        q=ERR
      elif jit_admission_denied "$selected_group_id" "$selected_labels"; then
        note "JIT admission remains blocked for repository=$REPO group=$selected_group_id class=$selected_labels — no VM will boot; inspect $JIT_DENIAL_FILE"
        heartbeat jit-admission-denied
        sleep "$POLL"
        continue
      fi
    fi
    cap="$(tartci_effective_cap)"; r="$(running_macos_vms)"
    if [ "$r" = unknown ]; then
      event inventory_unknown "reservations=$(tartci_active_reservations) cap=$cap"
    fi
    # Blind-aware: a non-numeric `q` (ERR) means the gh queue scan FAILED — do NOT treat it as an
    # empty queue. Count consecutive blind polls; after a sustained window, self-restart for fresh
    # gh auth (the supervisor is idle at the loop top — run_one blocks — so cleanup discards no live
    # VM). Any successful poll resets the counter, so a transient blip costs nothing.
    if ! printf '%s' "$q" | grep -qxE '[0-9]+'; then
      blind=$((blind + 1))
      scan_detail="$(scan_last_error)"
      # Report WHAT was observed. The old text named `gh`, a component this code
      # never observed failing, and sent every reader to audit a healthy CLI.
      note "SCAN BLIND ${blind}/${BLIND_MAX} — queue scan failed: ${scan_detail:-no diagnostic captured} — NOT idling as empty (running_macos_vms=$r/$cap)"
      event scan_blind "consecutive=$blind running=$r/$cap detail=${scan_detail:-no diagnostic captured}"
      heartbeat scan_blind
      if [ "$blind" -ge "$BLIND_MAX" ]; then
        blind_restarts="$(read_blind_restarts)"
        if [ "$blind_restarts" -ge "$BLIND_RESTART_MAX" ]; then
          # Restarting buys a fresh process. Causes that a fresh process cannot
          # clear (a broken interpreter, a revoked credential, an API change)
          # are unaffected, so repeating it forever hides a stuck host behind a
          # log that looks like it is recovering. Stay up and fail-closed — the
          # lane must still recover by itself when the cause clears — but stop
          # pretending a remedy is being applied, and make it visible.
          # Escalate loudly, then throttle: the condition is re-evaluated every
          # poll, and an alert repeated every ${POLL}s is one a human filters out.
          if ! blind_escalation_is_fresh; then
          note "SCAN BLIND UNRESOLVED after $blind_restarts supervisor restarts — restarting again will not help. Last diagnostic: ${scan_detail:-none captured}. This host is serving NOTHING for this lane; a human needs to look. Details: $SCAN_ERROR_FILE"
          event scan_blind_escalated \
            "restarts=$blind_restarts seconds=$((blind * POLL)) detail=${scan_detail:-no diagnostic captured}"
          heartbeat scan_blind_escalated
          write_blind_escalation "$blind_restarts" "$scan_detail"
          fi
          sleep "$POLL"; continue
        fi
        note "SCAN BLIND ~$((blind * POLL))s — restarting the supervisor (attempt $((blind_restarts + 1))/$BLIND_RESTART_MAX; launchd KeepAlive respawns)"
        event scan_blind_restart "seconds=$((blind * POLL)) restart=$((blind_restarts + 1))/$BLIND_RESTART_MAX detail=${scan_detail:-no diagnostic captured}"
        write_blind_restarts "$((blind_restarts + 1))"
        exit 75
      fi
      sleep "$POLL"; continue
    fi
    if [ "$blind" -ne 0 ]; then
      note "scan recovered after ${blind} blind poll(s)"
      event scan_recovered "after=$blind"
    fi
    blind=0
    reset_blind_restarts
    clear_scan_error
    # Only probe priority demand when THIS lane actually has work — no point
    # spending a gh round-trip (and the API quota the secondary-rate-limit cares
    # about) to decide whether to yield a slot we wouldn't use anyway. Stays 0
    # when there's no work, and the feature is off entirely for the gate runner.
    p=0
    [ "${q:-0}" -gt 0 ] && p="$(priority_demand)"
    # Bounded yield: only consulted when we would otherwise yield to priority
    # demand, so it costs no scan on a lane with the bound off or no demand.
    yb=0
    if [ "${q:-0}" -gt 0 ] && [ "${p:-0}" -gt 0 ] && [ "$YIELD_MAX_WAIT" -gt 0 ]; then
      yb="$(yield_bound_reached "$selected_labels")"
      if [ "${yb:-0}" -gt 0 ]; then
        note "yield bound reached: $yb job(s) of class $selected_labels queued >= ${YIELD_MAX_WAIT}s while priority lane '${YIELD_WORKFLOW}' demand=$p; taking the slot"
        event yield_bound_reached "workflow=$YIELD_WORKFLOW aged=$yb bound=${YIELD_MAX_WAIT}s priority_demand=$p labels=$selected_labels"
      fi
    fi
    # Host-health yield: only worth probing when we actually have work to boot.
    # Cheap local check (no gh call), fail-open, and 0 when the feature is off.
    hh=0
    [ "${q:-0}" -gt 0 ] && hh="$(tartci_host_health_yield)"
    # Idle gate: boot only when (1) this lane has work, (2) a VM slot is free,
    # (3) no higher-priority lane is waiting/running, and (4) the host is healthy.
    # (3) is always satisfied when the priority feature is off (priority_demand
    # returns 0) and (4) when host-health yield is off (host_health_yield returns
    # 0), so this is a no-op for a runner with neither feature enabled.
    if [ "${q:-0}" -gt 0 ] && { [ "${p:-0}" -eq 0 ] || [ "${yb:-0}" -gt 0 ]; } && [ "${hh:-0}" -eq 0 ] && resv="$(tartci_claim_macos_slot "$cap" "$r")" && [ -n "$resv" ]; then
      CURRENT_RESV="$resv"
      i=$((i+1)); note "[$i] queued=$q running_macos_vms=$r/$cap priority_demand=$p yield_bound=$yb workflow_tier=$selected_tier labels=$selected_labels host_health_yield=$hh → booting ephemeral VM"
      run_rc=0
      CURRENT_SELECTED_QUEUED="$q"
      run_one "$i" "$selected_labels" "$selected_tier" || run_rc=$?
      # Every cause of clone-without-serve returns through here, so the streak
      # is counted here rather than at each cause. Counted per work ENTRY, not
      # per error: a lane that alternates a failure with a served job never
      # accumulates, while a lane that only fails accumulates every cycle.
      # Demand another lane already covers is not demand this lane failed.
      if [ "$CURRENT_SERVED" = 1 ] || [ "${JOB_CLAIM_CONTENDED:-0}" = 1 ]; then
        SERVING_BLOCKED_SINCE=""
        SERVING_BLOCKED_STREAK=0
        SERVING_BLOCKED_LAST_PHASE=""
      else
        SERVING_BLOCKED_STREAK=$((SERVING_BLOCKED_STREAK + 1))
        SERVING_BLOCKED_LAST_PHASE="$LAST_HEARTBEAT_PHASE"
        [ -n "$SERVING_BLOCKED_SINCE" ] \
          || SERVING_BLOCKED_SINCE="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
      fi
      # Early returns (boot failure, yield, admission refusal) leave a proof
      # that nobody will consume.
      tartci_boundary_proof_abandon
      tartci_job_claim_release
      if [ -n "$CURRENT_VM" ] && [ "$CURRENT_TEARDOWN_PENDING" = delete ]; then
        PENDING_DELETE_ATTEMPTS=0
        note "teardown of $CURRENT_VM left deletion unproved — keeping it as pending-delete with its lease and reservation; reconciling in-loop instead of restarting"
        event teardown_pending_delete "vm=$CURRENT_VM rc=$run_rc"
        CURRENT_LABELS="$LABELS"
        continue
      fi
      if [ -n "$CURRENT_VM" ]; then
        note "teardown remained nonterminal — exiting for launchd process-group cleanup"
        event teardown_restart "vm=$CURRENT_VM rc=$run_rc"
        exit 75
      fi
      # run_one returned and its VM is gone (checked above): say so before any
      # backoff, or the last in-run phase (admission-deferred, minting-jit,
      # job-running) reads as busy for the whole sleep.
      if [ "$run_rc" = 0 ]; then heartbeat loop; else heartbeat backoff; sleep "$POLL"; fi
      CURRENT_LABELS="$LABELS"
      rm -f "$resv" 2>/dev/null || true; CURRENT_RESV=""
    elif [ "${q:-0}" -gt 0 ] && [ "${hh:-0}" -gt 0 ]; then
      note "yielding ${POLL}s (queued=$q host_health_yield=$hh running_macos_vms=$r/$cap) — host saturated, deferring new VM boot"
      event yielded_host_health "queued=$q host_health_yield=$hh running=$r/$cap"
      heartbeat yielding
      sleep "$POLL"
    elif [ "${q:-0}" -gt 0 ] && [ "${p:-0}" -gt 0 ] && [ "${yb:-0}" -eq 0 ]; then
      note "yielding ${POLL}s (queued=$q priority_demand=$p running_macos_vms=$r/$cap) — priority lane '${YIELD_WORKFLOW}' has the slot"
      event yielded_to_priority "workflow=$YIELD_WORKFLOW queued=$q priority_demand=$p running=$r/$cap"
      heartbeat yielding
      sleep "$POLL"
    else
      note "waiting ${POLL}s (queued=$q running_macos_vms=$r/$cap priority_demand=$p)"
      # No queued work means nothing is being refused; only demand that the
      # lane took and did not serve counts as blocked, so an idle pass ends the
      # streak rather than inflating it. A lane with no VMs and no demand is
      # the designed resting state of an ephemeral fleet, not a fault.
      if [ "${q:-0}" -le 0 ]; then
        SERVING_BLOCKED_SINCE=""
        SERVING_BLOCKED_STREAK=0
        SERVING_BLOCKED_LAST_PHASE=""
      fi
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
  selected_group_id="$(runner_group_id_for_tier "$selected_tier")" \
    || die "no runner-group contract for workflow tier $selected_tier"
  jit_admission_denied "$selected_group_id" "$selected_labels" \
    && die "JIT admission is blocked for this exact repository/group/class contract; inspect $JIT_DENIAL_FILE"
  note "ephemeral macOS runner ONCE; golden=$GOLDEN labels=$selected_labels workflow_tier=$selected_tier"
  run_one 1 "$selected_labels" "$selected_tier"
fi
