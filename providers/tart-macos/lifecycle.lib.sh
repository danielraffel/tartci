#!/usr/bin/env bash
# Per-VM lifecycle timings for a macOS lane: where a slot's time goes.
#
# A gate slot spends 6 to 12 min of overhead per job (2026-10-09 audit), but
# the event log only marks clone_start, boot_ip, mint_jit, job_assigned and
# teardown, and boot_ok fires when the runner is launched, not when the VM
# booted. So overhead could not be split. run_one records each boundary as a
# host epoch (LC_*, through tartci_lifecycle_mark), and the served path ends with one `vm_lifecycle` event
# carrying every phase in seconds. `tartci pool status --usage`
# (scripts/pool_usage.py) sums them per host.
#
# Phases (each omitted when a boundary is missing; a warm handoff has no
# clone or boot and says warm=1):
#   pre_clone_s  run_one start -> clone_start: admission precheck, job claim,
#                lease wait
#   clone_s      clone_start -> clone done and sized
#   boot_ip_s    clone done -> the VM network gave an address
#   ip_ssh_s     address -> SSH answered
#   prep_s       SSH -> JIT minted: preflights, admission boundary
#   register_s   JIT minted -> the runner logged "Listening for Jobs"
#   idle_s       listening -> "Running job:" (or -> runner exit when unserved)
#   job_s        "Running job:" -> runner exit
#   teardown_s   runner exit -> VM discarded and lease released
#   total_s      run_one start -> teardown done
# The runner log is read every 5 s, so register_s and idle_s are host-observed
# to within 5 s.

tartci_lifecycle_reset(){
  LC_CLONE_START=0
  LC_CLONED=0
  LC_IP=0
  LC_SSH=0
  LC_MINTED=0
  LC_LISTENING=0
  LC_ASSIGNED=0
  LC_WARM=0
  # shellcheck disable=SC2034 # set at clone_start in runner.sh
  CLONE_STARTED_AT=""
}

# Record one boundary: tartci_lifecycle_mark <name> [epoch] (default now).
# Names: clone_start cloned ip ssh minted listening assigned warm. A mark
# already set is kept, so the first sighting wins.
tartci_lifecycle_mark(){
  local at="${2:-$(date +%s)}"
  case "$1" in
    clone_start) [ "$LC_CLONE_START" != 0 ] || LC_CLONE_START="$at" ;;
    cloned)      [ "$LC_CLONED" != 0 ]      || LC_CLONED="$at" ;;
    ip)          [ "$LC_IP" != 0 ]          || LC_IP="$at" ;;
    ssh)         [ "$LC_SSH" != 0 ]         || LC_SSH="$at" ;;
    minted)      [ "$LC_MINTED" != 0 ]      || LC_MINTED="$at" ;;
    listening)   [ "$LC_LISTENING" != 0 ]   || LC_LISTENING="$at" ;;
    assigned)    [ "$LC_ASSIGNED" != 0 ]    || LC_ASSIGNED="$at" ;;
    warm)        LC_WARM=1 ;;
  esac
}

# Whether a boundary is still unmarked (0 = unmarked).
tartci_lifecycle_unmarked(){
  case "$1" in
    listening) [ "$LC_LISTENING" = 0 ] ;;
    assigned)  [ "$LC_ASSIGNED" = 0 ] ;;
    *) return 1 ;;
  esac
}

# The seconds from $1 to $2, or nothing when either is unset or they are out
# of order (a phase that did not happen).
tartci_lifecycle_span(){
  local from="${1%.*}" to="${2%.*}"
  case "$from$to" in *[!0-9]*|"") return 0 ;; esac
  [ "$from" -gt 0 ] && [ "$to" -ge "$from" ] || return 0
  printf '%s' $((to - from))
}

# name=seconds pairs, one per phase that happened, in lifecycle order.
# Args: t_start t_runner_done t_done.
tartci_lifecycle_pairs(){
  local start="$1" runner_done="$2" done="$3" idle_end prep_start
  idle_end="$LC_ASSIGNED"
  [ "$idle_end" != 0 ] || idle_end="$runner_done"
  # A warm handoff was reached by SSH when it was parked: its prep starts here.
  prep_start="$LC_SSH"
  [ "$prep_start" != 0 ] || prep_start="$start"
  local name value
  for name in pre_clone clone boot_ip ip_ssh prep register idle job teardown total; do
    case "$name" in
      pre_clone) value="$(tartci_lifecycle_span "$start" "$LC_CLONE_START")" ;;
      clone)     value="$(tartci_lifecycle_span "$LC_CLONE_START" "$LC_CLONED")" ;;
      boot_ip)   value="$(tartci_lifecycle_span "$LC_CLONED" "$LC_IP")" ;;
      ip_ssh)    value="$(tartci_lifecycle_span "$LC_IP" "$LC_SSH")" ;;
      prep)      value="$(tartci_lifecycle_span "$prep_start" "$LC_MINTED")" ;;
      register)  value="$(tartci_lifecycle_span "$LC_MINTED" "$LC_LISTENING")" ;;
      idle)      value="$(tartci_lifecycle_span "$LC_LISTENING" "$idle_end")" ;;
      job)       value="$(tartci_lifecycle_span "$LC_ASSIGNED" "$runner_done")" ;;
      teardown)  value="$(tartci_lifecycle_span "$runner_done" "$done")" ;;
      total)     value="$(tartci_lifecycle_span "$start" "$done")" ;;
    esac
    [ -z "$value" ] || printf '%s_s=%s\n' "$name" "$value"
  done
}

# One `vm_lifecycle` event for the VM that just tore down.
# Args: t_start t_runner_done t_done rc.
tartci_lifecycle_emit(){
  local pairs=() line served=0
  [ "$LC_ASSIGNED" = 0 ] || served=1
  while IFS= read -r line; do
    [ -n "$line" ] && pairs+=("$line")
  done < <(tartci_lifecycle_pairs "${1%.*}" "${2%.*}" "${3%.*}")
  event vm_lifecycle "served=$served warm=$LC_WARM rc=$4 ${pairs[*]}" \
    "served=$served" "warm=$LC_WARM" "rc=$4" "${pairs[@]}"
}

# The same phases as timing.tsv rows (phase<TAB>seconds), for runtime_measure.
# Args: t_start t_runner_done t_done.
tartci_lifecycle_tsv_rows(){
  local line
  while IFS= read -r line; do
    case "$line" in total_s=*|"") continue ;; esac
    printf 'lc_%s\t%s\n' "${line%%_s=*}" "${line#*=}"
  done < <(tartci_lifecycle_pairs "${1%.*}" "${2%.*}" "${3%.*}")
}
