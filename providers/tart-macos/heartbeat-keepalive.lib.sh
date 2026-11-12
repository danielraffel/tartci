# Keep an idle lane's heartbeat current while a long, silent step runs.
# shellcheck shell=bash
#
# A supervisor writes its heartbeat only between steps, and the queue scan
# (select_work) is one step that can run 90-200 s on a busy host. Readers
# treat a heartbeat older than max(120 s, 6 polls) as a dead or wedged
# supervisor: `tartci pool supply` then reports the whole host `unknown`, so a
# fallback lane on another host holds when it could grant (m3, 2026-09-28: 7
# of 20 samples, every one a `backoff` phase that went stale mid-scan).
#
# tartci_heartbeat_keepalive_start re-writes the CURRENT phase every
# TARTCI_HEARTBEAT_KEEPALIVE_SECS (default 30; 0 = off) from a background
# child. It never invents a phase: it only refreshes the timestamp of the one
# the supervisor last wrote. The child stops:
#   * on the supervisor's next heartbeat (heartbeat() calls _stop first, so a
#     refresh can never overwrite a newer phase),
#   * on tartci_heartbeat_keepalive_stop (cleanup),
#   * by itself, within one interval, once the supervisor process is gone, so
#     a killed supervisor cannot look alive.
# The staleness window is unchanged: a supervisor wedged anywhere else still
# goes stale exactly as before.

HEARTBEAT_KEEPALIVE_PID=""

tartci_heartbeat_keepalive_validate(){
  case "${TARTCI_HEARTBEAT_KEEPALIVE_SECS:-30}" in
    ''|*[!0-9]*) printf 'invalid TARTCI_HEARTBEAT_KEEPALIVE_SECS: expected 0-110\n' >&2; return 2 ;;
  esac
  [ "${TARTCI_HEARTBEAT_KEEPALIVE_SECS:-30}" -le 110 ] \
    || { printf 'invalid TARTCI_HEARTBEAT_KEEPALIVE_SECS: expected 0-110\n' >&2; return 2; }
}

# Stop the refresher, if this is the supervisor process that owns it. A
# heartbeat written from a subshell (inside a command substitution) is not the
# owner and leaves it running.
tartci_heartbeat_keepalive_stop(){
  [ -n "$HEARTBEAT_KEEPALIVE_PID" ] || return 0
  [ "${BASHPID:-$$}" = "${SUPERVISOR_PID:-$$}" ] || return 0
  kill -TERM "$HEARTBEAT_KEEPALIVE_PID" 2>/dev/null || true
  wait "$HEARTBEAT_KEEPALIVE_PID" 2>/dev/null || true
  HEARTBEAT_KEEPALIVE_PID=""
}

tartci_heartbeat_keepalive_start(){
  local interval="${TARTCI_HEARTBEAT_KEEPALIVE_SECS:-30}" phase="${LAST_HEARTBEAT_PHASE:-}" owner
  tartci_heartbeat_keepalive_stop
  [ "$interval" -gt 0 ] && [ -n "$phase" ] || return 0
  owner="${SUPERVISOR_PID:-$$}"
  (
    sleeper=""
    trap '[ -z "$sleeper" ] || kill "$sleeper" 2>/dev/null; exit 0' TERM
    while :; do
      sleep "$interval" & sleeper=$!
      wait "$sleeper" 2>/dev/null || true
      sleeper=""
      kill -0 "$owner" 2>/dev/null || exit 0
      heartbeat "$phase" || true
    done
  ) </dev/null >/dev/null 2>&1 &
  HEARTBEAT_KEEPALIVE_PID=$!
}
