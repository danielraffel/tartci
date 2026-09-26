# Warm pre-booted gate VM (opt-in, off by default). Sourced by runner.sh.
# shellcheck shell=bash
#
# A cold gate VM spends ~1 min between "a job is queued" and "VM up" (lease,
# CoW clone, boot, SSH). A warm VM pays that while the lane is idle: the lane
# boots one VM to "VM up" and parks it. At hand-off the parked VM becomes the
# job's VM and everything after boot runs exactly as for a cold VM: the
# runner-version check, the Aqua and Chrome preflights, the Shipyard admission
# check at the JIT boundary, the V2 pre-mint recheck and the single-use JIT
# mint. Nothing is registered with GitHub while parked: a JIT config is single
# use, fixes its class label at mint and can expire, so the class is chosen at
# hand-off, not at park.
#
# Invariants:
#   * At most one parked VM per host (a host-wide claim under WARM_DIR), and
#     only on a lane whose profile opted in (TARTCI_WARM_VM=1).
#   * Parked means a MEMORY-ONLY lease: the guest memory and disk growth are
#     reserved, no cores are. Hand-off upgrades the same lease in place
#     (leases.py resize, one store lock), so there is no released window and a
#     denial leaves the VM parked, waiting for cores exactly as a cold boot
#     would.
#   * The parked VM holds one of the host's macOS VM slots (Apple's 2-guest
#     limit) through an ordinary reservation, which every lane's slot claim
#     already counts.
#   * It yields: any other lane that cannot get a VM slot, or is denied a lease
#     on memory, while a warm VM is parked leaves a demand marker, and the
#     parked VM is torn down within seconds. It is also torn down after
#     TARTCI_WARM_VM_MAX_PARK_SECS, when the pool is off/draining (which is
#     how fleet self-update stops a host), when the pool transition lock is
#     held, when its guest dies, and on any supervisor exit.
#   * A sibling supervisor of the same repository defers to a parked VM while
#     it is fresh, and asks it to hand off, so one job does not get both a
#     warm hand-off and a cold boot.
#
# Events: warm_parked, warm_handoff, warm_handoff_denied, warm_expired
# (detail reason=max_park_age|pool_closed|yield_demand|vm_died|signal|...),
# warm_park_failed, warm_sibling_defer. State for `tartci pool status` and
# `tartci doctor` is WARM_DIR/parked.json.

WARM_VM_ENABLED="${TARTCI_WARM_VM:-0}"
WARM_MAX_PARK="${TARTCI_WARM_VM_MAX_PARK_SECS:-1800}"
WARM_COOLDOWN="${TARTCI_WARM_VM_COOLDOWN_SECS:-120}"
WARM_DEMAND_FRESH="${TARTCI_WARM_VM_DEMAND_FRESH_SECS:-60}"
WARM_DIR="${TARTCI_WARM_VM_DIR:-$HOME/.tartci/state/warm-vm}"
WARM_VM=""
WARM_IP=""
WARM_RPID=""
WARM_RESV=""
WARM_CORES=""
WARM_MEM=""
WARM_PIP=0
WARM_PARKED_AT=0
WARM_LAST_END=0
WARM_DEFER_REPORTED=0

tartci_warm_configure(){
  case "$WARM_VM_ENABLED" in
    0|1) ;;
    *) die "invalid TARTCI_WARM_VM: expected 0 or 1" ;;
  esac
  case "$WARM_MAX_PARK:$WARM_COOLDOWN:$WARM_DEMAND_FRESH" in
    *[!0-9:]*|:*|*::*|*:) die "invalid TARTCI_WARM_VM_* seconds: expected integers" ;;
  esac
  [ "$WARM_VM_ENABLED" = 1 ] || return 0
  [ "$WARM_MAX_PARK" -ge 300 ] && [ "$WARM_MAX_PARK" -le 14400 ] \
    || die "TARTCI_WARM_VM_MAX_PARK_SECS must be 300-14400"
  [ "$WARM_COOLDOWN" -le 3600 ] || die "TARTCI_WARM_VM_COOLDOWN_SECS must be 0-3600"
  [ "$WARM_DEMAND_FRESH" -ge 20 ] && [ "$WARM_DEMAND_FRESH" -le 600 ] \
    || die "TARTCI_WARM_VM_DEMAND_FRESH_SECS must be 20-600"
}

tartci_warm_now(){ date +%s; }

tartci_warm_state_file(){ printf '%s/parked.json\n' "$WARM_DIR"; }

# Host-wide claim: exactly one supervisor may own a parked (or parking) VM.
# A mkdir mutex like macos-vm-cap.lib.sh's; a dead holder's claim is stolen.
tartci_warm_claim(){
  local dir="$WARM_DIR/claim.d" holder
  mkdir -p "$WARM_DIR" 2>/dev/null || return 1
  if mkdir "$dir" 2>/dev/null; then
    printf '%s\n' "$$" > "$dir/pid"
    return 0
  fi
  holder="$(cat "$dir/pid" 2>/dev/null || true)"
  [ "$holder" = "$$" ] && return 0
  if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
    mv "$dir" "$dir.dead.$$" 2>/dev/null && rm -rf "$dir.dead.$$"
    mkdir "$dir" 2>/dev/null || return 1
    printf '%s\n' "$$" > "$dir/pid"
    return 0
  fi
  return 1
}

tartci_warm_release_claim(){
  local dir="$WARM_DIR/claim.d"
  [ "$(cat "$dir/pid" 2>/dev/null || true)" = "$$" ] || return 0
  rm -rf "$dir" 2>/dev/null || true
}

tartci_warm_publish(){
  local state="$1" file tmp now
  file="$(tartci_warm_state_file)"
  now="$(tartci_warm_now)"
  mkdir -p "$WARM_DIR" 2>/dev/null || return 0
  tmp="$(mktemp "$file.tmp.XXXXXX")" || return 0
  printf '{"schema":"tartci.warm-vm/v1","state":"%s","ts":%s,"host":"%s","runner":"%s","repo":"%s","lane":"%s","vm":"%s","supervisor_pid":%s,"parked_at":%s,"parked_seconds":%s,"max_park_seconds":%s,"lease_id":"%s","reserved_cores":0,"reserved_mem_mb":"%s","vm_cores":"%s"}\n' \
    "$state" "$now" "$(json_sanitize "$HOST_NAME")" "$(json_sanitize "$RUNNER_NAME")" \
    "$(json_sanitize "$REPO")" "$(json_sanitize "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}")" \
    "$(json_sanitize "$WARM_VM")" "$$" "$WARM_PARKED_AT" "$((now - WARM_PARKED_AT))" \
    "$WARM_MAX_PARK" "$(json_sanitize "${TARTCI_ACTIVE_VM_LEASE_ID:-}")" \
    "$(json_sanitize "$WARM_MEM")" "$(json_sanitize "$WARM_CORES")" > "$tmp"
  mv -f "$tmp" "$file"
}

tartci_warm_unpublish(){
  local file
  file="$(tartci_warm_state_file)"
  # Only our own record: never delete another supervisor's published state.
  if grep -q "\"supervisor_pid\":$$," "$file" 2>/dev/null; then
    rm -f "$file"
  fi
  rm -f "$WARM_DIR/handoff-request" 2>/dev/null || true
}

# Succeed when ANOTHER live supervisor's VM is parked on this host, fresh.
# Optional $1: only count one parked for that repository.
tartci_warm_other_parked(){
  local repo="${1:-}" file
  file="$(tartci_warm_state_file)"
  [ -r "$file" ] || return 1
  python3 - "$file" "$$" "$repo" "$(tartci_warm_now)" "$((POLL * 3 > 60 ? POLL * 3 : 60))" <<'PY'
import json, os, sys
path, me, repo, now, fresh = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
try:
    value = json.load(open(path, encoding="utf-8"))
    pid = int(value["supervisor_pid"])
    ts = int(value["ts"])
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit(1)
if pid == me or value.get("state") != "parked" or now - ts > fresh:
    raise SystemExit(1)
if repo and value.get("repo") != repo:
    raise SystemExit(1)
try:
    os.kill(pid, 0)
except ProcessLookupError:
    raise SystemExit(1)
except PermissionError:
    pass
PY
}

# Any lane on this host: record that real demand could not get a VM slot or
# was denied memory while a warm VM is parked, so the parked VM yields. Costs
# one stat when nothing is parked, and nothing at all on a host with no warm VM.
tartci_warm_note_demand(){
  local reason="$1" tmp
  tartci_warm_other_parked || return 0
  mkdir -p "$WARM_DIR/demand" 2>/dev/null || return 0
  tmp="$(mktemp "$WARM_DIR/demand/.tmp.XXXXXX")" || return 0
  printf '%s %s\n' "$(tartci_warm_now)" "$reason" > "$tmp"
  mv -f "$tmp" "$WARM_DIR/demand/$RUNNER_NAME"
  event warm_yield_requested "reason=$reason"
}

# A lease denial frees nothing by yielding unless it was on the memory axis:
# a parked VM holds no cores.
tartci_warm_note_lease_denial(){
  case "${TARTCI_LAST_VM_LEASE_DENIAL:-}" in
    *'"memory": true'*) tartci_warm_note_demand memory_denied ;;
  esac
}

# A fresh demand marker left by another supervisor.
tartci_warm_demand_pending(){
  local file stamp _ now
  [ -d "$WARM_DIR/demand" ] || return 1
  now="$(tartci_warm_now)"
  for file in "$WARM_DIR/demand"/*; do
    [ -f "$file" ] || continue
    [ "$(basename "$file")" != "$RUNNER_NAME" ] || continue
    read -r stamp _ < "$file" 2>/dev/null || continue
    case "$stamp" in ''|*[!0-9]*) continue ;; esac
    [ $((now - stamp)) -le "$WARM_DEMAND_FRESH" ] && return 0
  done
  return 1
}

# The macOS slot for the next boot: the parked VM's own reservation when one
# is parked (it becomes the job's VM), else an ordinary claim.
tartci_warm_or_claim_slot(){
  local cap="$1" running="${2:-}"
  if [ -n "$WARM_VM" ] && [ -n "$WARM_RESV" ]; then
    printf '%s' "$WARM_RESV"
    return 0
  fi
  tartci_claim_macos_slot "$cap" "$running"
}

# Tear the parked VM down. Returns non-zero only when teardown could not be
# proved; the VM is then an ordinary CURRENT_VM holding its lease and (as
# CURRENT_RESV) its reservation, so the loop's pending-delete reconcile or its
# fail-closed exit, and cleanup, own it from there.
tartci_warm_discard(){
  local reason="$1" now parked
  [ -n "$WARM_VM" ] || return 0
  now="$(tartci_warm_now)"
  parked=$((now - WARM_PARKED_AT))
  CURRENT_VM="$WARM_VM"
  CURRENT_RPID="$WARM_RPID"
  CURRENT_IP="$WARM_IP"
  WARM_VM=""
  WARM_IP=""
  WARM_RPID=""
  event warm_expired "reason=$reason parked_seconds=$parked"
  note "warm VM $CURRENT_VM torn down after ${parked}s parked (reason=$reason)"
  if ! discard_current_vm; then
    if [ -n "$WARM_RESV" ] && [ -z "${CURRENT_RESV:-}" ]; then
      CURRENT_RESV="$WARM_RESV"
    fi
    WARM_RESV=""
    WARM_LAST_END="$now"
    tartci_warm_unpublish
    tartci_warm_release_claim
    return 1
  fi
  tartci_release_vm_lease
  # During a failed hand-off the reservation already belongs to the run that
  # will now boot cold; keep it so that boot's slot stays counted.
  if [ -n "$WARM_RESV" ] && [ "$WARM_RESV" != "${CURRENT_RESV:-}" ]; then
    rm -f "$WARM_RESV" 2>/dev/null || true
  fi
  WARM_RESV=""
  WARM_LAST_END="$now"
  tartci_warm_unpublish
  tartci_warm_release_claim
  return 0
}

# Park a VM when this idle lane is eligible. Called from the loop's idle
# branch only: the lane has no demand and nothing is booting.
tartci_warm_try_park(){
  local cap resv vm priority now rc=0 started
  [ "$WARM_VM_ENABLED" = 1 ] || return 1
  [ -z "$WARM_VM" ] || return 1
  now="$(tartci_warm_now)"
  [ $((now - WARM_LAST_END)) -ge "$WARM_COOLDOWN" ] || return 1
  tartci_pool_admission_open || return 1
  tartci_pool_lock_absent || return 1
  tartci_warm_demand_pending && return 1
  tartci_warm_claim || return 1
  cap="$(tartci_effective_cap)"
  resv="$(tartci_claim_macos_slot "$cap")"
  if [ -z "$resv" ]; then
    tartci_warm_release_claim
    return 1
  fi
  i=$((i + 1))
  vm="$(ephemeral_boot_name "$i")"
  priority="$(tartci_vm_lease_priority "$LABELS")"
  started="$now"
  note "[$i] lane idle — parking warm VM $vm (memory-only lease, no cores reserved)"
  TARTCI_VM_LEASE_MEMORY_ONLY=1 boot_vm_to_ssh "$i" "$vm" "$LABELS" "$priority" "" warm-parking || rc=$?
  if [ "$rc" -ne 0 ]; then
    # A boot that failed and could not be torn down is CURRENT_VM; its slot
    # stays reserved until the loop proves it gone.
    if [ -n "$CURRENT_VM" ]; then
      CURRENT_RESV="$resv"
    else
      rm -f "$resv" 2>/dev/null || true
    fi
    tartci_warm_release_claim
    WARM_LAST_END="$(tartci_warm_now)"
    event warm_park_failed "rc=$rc lease_denied=$BOOT_LEASE_DENIED"
    heartbeat waiting
    # A clone or boot that could not be torn down stays CURRENT_VM; the loop
    # exits fail-closed on it exactly as after a job.
    return 1
  fi
  WARM_VM="$CURRENT_VM"
  WARM_IP="$CURRENT_IP"
  WARM_RPID="$CURRENT_RPID"
  WARM_CORES="$CURRENT_GUEST_CORES"
  WARM_MEM="$CURRENT_GUEST_MEM_MB"
  WARM_PIP="$CURRENT_PIP_WHEELHOUSE"
  WARM_RESV="$resv"
  WARM_PARKED_AT="$(tartci_warm_now)"
  CURRENT_VM=""
  CURRENT_IP=""
  CURRENT_RPID=""
  tartci_warm_publish parked
  event warm_parked "vm=$WARM_VM reserved_cores=0 reserved_mem_mb=$WARM_MEM vm_cores=$WARM_CORES boot_seconds=$((WARM_PARKED_AT - started)) lease=${TARTCI_ACTIVE_VM_LEASE_ID:-none}"
  heartbeat warm-parked
  return 0
}

# Once per loop pass while parked: expire, yield, or refresh the published state.
tartci_warm_tick(){
  local now
  [ -n "$WARM_VM" ] || return 0
  now="$(tartci_warm_now)"
  if ! tartci_pool_admission_open || ! tartci_pool_lock_absent; then
    tartci_warm_discard pool_closed
  elif [ $((now - WARM_PARKED_AT)) -ge "$WARM_MAX_PARK" ]; then
    tartci_warm_discard max_park_age
  elif [ -n "$WARM_RPID" ] && ! kill -0 "$WARM_RPID" 2>/dev/null; then
    tartci_warm_discard vm_died
  elif tartci_warm_demand_pending; then
    tartci_warm_discard yield_demand
  else
    tartci_warm_publish parked
    return 0
  fi
}

# The loop's sleep while parked: short slices so a yield request, a closed
# pool or a sibling's hand-off request is acted on within seconds, not a poll.
tartci_warm_sleep(){
  local total="$1" slept=0 slice
  if [ -z "$WARM_VM" ]; then
    sleep "$total"
    return 0
  fi
  slice=5
  [ "$total" -ge "$slice" ] || slice="$total"
  while [ "$slept" -lt "$total" ]; do
    sleep "$slice"
    slept=$((slept + slice))
    if [ -e "$WARM_DIR/handoff-request" ]; then
      rm -f "$WARM_DIR/handoff-request"
      # The next pass must observe live, not replay a cached "no demand".
      [ "$ASSIGNMENT_MODE" != event-class-v2 ] || tartci_assignment_v2_invalidate_selection
      return 0
    fi
    if tartci_warm_demand_pending || ! tartci_pool_admission_open; then
      return 0
    fi
  done
}

# A sibling supervisor with demand, beside a parked VM of the same repository:
# defer this poll and ask the parked VM to hand off. Succeeds when deferring.
tartci_warm_sibling_defer(){
  tartci_warm_other_parked "$REPO" || { WARM_DEFER_REPORTED=0; return 1; }
  mkdir -p "$WARM_DIR" 2>/dev/null || true
  : > "$WARM_DIR/handoff-request"
  if [ "$WARM_DEFER_REPORTED" != 1 ]; then
    event warm_sibling_defer "repo=$REPO"
    WARM_DEFER_REPORTED=1
  fi
  return 0
}

# run_one's hand-off. 0: CURRENT_VM is the parked VM with its lease upgraded.
# 75: the upgrade was denied; the VM stays parked. 1: the parked VM was
# unusable and has been discarded; the caller boots cold.
tartci_warm_handoff(){
  local i="$1" labels="$2" priority="$3" api_root="$4" now parked
  [ -n "$WARM_VM" ] || return 1
  if [ -n "$WARM_RPID" ] && ! kill -0 "$WARM_RPID" 2>/dev/null; then
    tartci_warm_discard vm_died
    return 1
  fi
  if ! ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -i "$SSH_KEY_PRIV" "$VM_USER@$WARM_IP" true 2>/dev/null; then
    tartci_warm_discard vm_unreachable
    return 1
  fi
  heartbeat warm-handoff
  if ! tartci_resize_vm_lease "$WARM_CORES" "$WARM_MEM" "$priority" "$labels"; then
    event warm_handoff_denied "labels=$labels cores=$WARM_CORES mem_mb=$WARM_MEM"
    note "[$i] warm VM $WARM_VM cannot upgrade to cores=$WARM_CORES now — staying parked"
    heartbeat warm-parked
    return 75
  fi
  now="$(tartci_warm_now)"
  parked=$((now - WARM_PARKED_AT))
  CURRENT_VM="$WARM_VM"
  CURRENT_IP="$WARM_IP"
  CURRENT_RPID="$WARM_RPID"
  CURRENT_GUEST_CORES="$WARM_CORES"
  CURRENT_GUEST_MEM_MB="$WARM_MEM"
  CURRENT_PIP_WHEELHOUSE="$WARM_PIP"
  WARM_VM=""
  WARM_IP=""
  WARM_RPID=""
  # The reservation is now the job's: the loop claimed it for this run.
  WARM_RESV=""
  tartci_warm_unpublish
  tartci_warm_release_claim
  sweep_lane_ghost_runners "$api_root" "$CURRENT_VM"
  event warm_handoff "parked_seconds=$parked labels=$labels cores=$CURRENT_GUEST_CORES mem_mb=$CURRENT_GUEST_MEM_MB"
  note "[$i] handing off warm VM $CURRENT_VM (parked ${parked}s) — lease upgraded to cores=$CURRENT_GUEST_CORES"
  return 0
}
