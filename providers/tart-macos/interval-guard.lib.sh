# Start fleet timer jobs that launchd has stopped starting.
# shellcheck shell=bash
#
# A stalled automatic macOS install can leave launchd's gui domain refusing
# every non-demand spawn: StartInterval agents stop running and a KeepAlive
# agent that exits is not respawned, while the lane supervisors started before
# the stall keep serving. Only a demand spawn (`launchctl kickstart`) works, so
# the guard cannot be a launchd job of its own: it runs as a background child
# of this already-running supervisor (scripts/launchd_interval_guard.py).
#
# Every lane supervisor starts one; an exclusive lock in the guard's state dir
# picks the one that acts, and another takes over within a minute when that
# supervisor exits. The child never touches the supervisor's loop: it sleeps
# between bounded passes, exits on its own once the supervisor is gone, and is
# stopped by tartci_interval_guard_stop (cleanup). TARTCI_INTERVAL_GUARD=0
# turns it off.

INTERVAL_GUARD_PID=""

tartci_interval_guard_stop(){
  [ -n "$INTERVAL_GUARD_PID" ] || return 0
  [ "${BASHPID:-$$}" = "${SUPERVISOR_PID:-$$}" ] || return 0
  kill -TERM "$INTERVAL_GUARD_PID" 2>/dev/null || true
  wait "$INTERVAL_GUARD_PID" 2>/dev/null || true
  INTERVAL_GUARD_PID=""
}

tartci_interval_guard_start(){
  [ "${TARTCI_INTERVAL_GUARD:-1}" = 1 ] || return 0
  [ -z "$INTERVAL_GUARD_PID" ] || return 0
  local script="$TARTCI_ROOT/scripts/launchd_interval_guard.py" log
  [ -r "$script" ] || return 0
  log="${TARTCI_INTERVAL_GUARD_LOG:-$HOME/Library/Logs/tartci/launchd-interval-guard.log}"
  mkdir -p "${log%/*}" 2>/dev/null || log=/dev/null
  python3 "$script" loop --owner-pid "${SUPERVISOR_PID:-$$}" \
    --cadence "${TARTCI_INTERVAL_GUARD_CADENCE_SECS:-60}" \
    --event-log "$EVENT_LOG" --runner "$RUNNER_NAME" </dev/null >>"$log" 2>&1 &
  INTERVAL_GUARD_PID=$!
}
