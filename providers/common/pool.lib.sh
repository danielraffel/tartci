# tartci pool — host-level CI participation and admission state.
#
# A single host-level switch, deliberately decoupled from any per-lane GUI
# toggle or placement engine, so "opt this Mac out" cannot silently vanish when
# lanes change or placement moves. Two effects, matching how the pool actually
# works (GitHub is the scheduler; every Mac is a label-matched runner):
#   1. native-build participation file  -> the lease governor refuses to place a
#      native-build lease on an opted-out host (covers non-launchd build paths).
#   2. admission state                   -> provider loops refuse to mint a new
#      JIT runner while draining/off, without disturbing an assigned job.
#   3. runner LaunchAgents               -> drain disables restart and lets the
#      current exact job finish; off bootouts immediately.
#
# Pure logic lives here (participation r/w, agent enumeration) so it is unit
# testable; the dispatcher's cmd_pool wires launchctl + JSON on top.
# shellcheck shell=bash

TARTCI_POOL_PARTICIPATION_FILE="${TARTCI_POOL_PARTICIPATION_FILE:-$HOME/.config/tartci/native-build-participation}"
TARTCI_POOL_STATE_FILE="${TARTCI_POOL_STATE_FILE:-$HOME/.config/tartci/pool-state}"
TARTCI_POOL_PERSISTENT_HOLD_FILE="${TARTCI_POOL_PERSISTENT_HOLD_FILE:-$HOME/.config/tartci/persistent-runner-admission-hold}"
TARTCI_POOL_TRANSITION_LOCK="${TARTCI_POOL_TRANSITION_LOCK:-$HOME/.config/tartci/pool-transition.lock}"

# Read participation: 1 = participating, 0 = opted out. Absent means
# participating — opting out is an explicit act, and a missing/garbage file must
# never silently pull a host out of the pool.
# The optional path is an intentional test seam; production callers use the
# host-level default, so ShellCheck cannot see a shell caller passing `$1`.
# shellcheck disable=SC2120
tartci_pool_read_participation() {
  local f="${1:-$TARTCI_POOL_PARTICIPATION_FILE}"
  if [ -f "$f" ]; then
    local v; v="$(tr -d '[:space:]' < "$f" 2>/dev/null)"
    case "$v" in
      0) echo 0 ;;
      *) echo 1 ;;
    esac
  else
    echo 1
  fi
}

tartci_pool_write_participation() {
  local v="$1" f="${2:-$TARTCI_POOL_PARTICIPATION_FILE}"
  mkdir -p "$(dirname "$f")"
  local tmp="${f}.tmp.$$"
  printf '%s\n' "$v" > "$tmp"
  mv -f "$tmp" "$f"
}

# on = admit work, draining = finish assigned work but admit nothing new,
# off = immediate stop. The separate state keeps the existing numeric
# participation contract intact for Shipyard/native-build leases.
# The optional path is an intentional test seam; see read_participation above.
# shellcheck disable=SC2120
tartci_pool_read_state() {
  local f="${1:-$TARTCI_POOL_STATE_FILE}"
  if [ -f "$f" ]; then
    local v; v="$(tr -d '[:space:]' < "$f" 2>/dev/null)"
    case "$v" in
      on|draining|off) printf '%s\n' "$v"; return 0 ;;
    esac
  fi
  # Backward compatibility: an old host with only participation=0 is off.
  if [ "$(tartci_pool_read_participation)" = 0 ]; then
    printf 'off\n'
  else
    printf 'on\n'
  fi
}

tartci_pool_write_state() {
  local v="$1" f="${2:-$TARTCI_POOL_STATE_FILE}"
  case "$v" in on|draining|off) ;; *) return 2 ;; esac
  mkdir -p "$(dirname "$f")"
  local tmp="${f}.tmp.$$"
  printf '%s\n' "$v" > "$tmp"
  mv -f "$tmp" "$f"
}

tartci_pool_admission_open() {
  # Both records must be open. Drain writes participation=0 first, so native
  # leases and provider JIT admission close at the same transition boundary.
  [ "$(tartci_pool_read_participation)" = 1 ] \
    && [ "$(tartci_pool_read_state)" = on ]
}

# Enumerate the active runner LaunchAgent labels (basename minus .plist) in a
# directory — the agents this host uses to pick up CI work. Matches the tartci
# serve loops and the GitHub Actions runner services; the `*.plist` glob
# excludes .bak / .disabled-* / .pre-* variants by construction.
tartci_pool_runner_agents() {
  local dir="${1:-$HOME/Library/LaunchAgents}"
  [ -d "$dir" ] || return 0
  local f b
  for f in "$dir"/*.plist; do
    [ -e "$f" ] || continue
    b="$(basename "$f" .plist)"
    case "$b" in
      com.danielraffel.pulp.tart-runner|com.danielraffel.pulp.tart-runner-*|\
      com.danielraffel.pulp.qemu-runner|com.danielraffel.pulp.qemu-runner-*|\
      com.danielraffel.forge.tart-runner-*|\
      com.danielraffel.vellum.tart-runner-*|\
      com.danielraffel.tartci.tart-runner-*|\
      actions.runner.*)
        # NB: the bare `tart-runner` (no suffix) is the macOS GATE lane — it MUST
        # be matched, or `pool off` would leave the gate serving despite
        # participation=0. Match both bare and `-suffixed` runner labels.
        printf '%s\n' "$b"
        ;;
    esac
  done
}

tartci_pool_launchd_target() {
  printf 'gui/%s/%s\n' "${TARTCI_POOL_UID:-$(id -u)}" "$1"
}

tartci_pool_agent_loaded() {
  launchctl print "$(tartci_pool_launchd_target "$1")" >/dev/null 2>&1
}

tartci_pool_agent_pid() {
  launchctl print "$(tartci_pool_launchd_target "$1")" 2>/dev/null \
    | awk '/^[[:space:]]*pid = [0-9]+/ { print $3; exit }'
}

# Optional paths on the helpers below are deterministic test seams. Production
# always uses the configured host-global files/lock.
# shellcheck disable=SC2120
tartci_pool_persistent_hold_ready() {
  local f="${1:-$TARTCI_POOL_PERSISTENT_HOLD_FILE}"
  [ -f "$f" ] && [ "$(tr -d '[:space:]' < "$f" 2>/dev/null)" = held-idle ]
}

# shellcheck disable=SC2120
tartci_pool_lock_acquire() {
  local lock="${1:-$TARTCI_POOL_TRANSITION_LOCK}" attempt=0
  mkdir -p "$(dirname "$lock")"
  while [ "$attempt" -lt 100 ]; do
    attempt=$((attempt + 1))
    if mkdir "$lock" 2>/dev/null; then
      printf '%s\n' "$$" > "$lock/pid"
      return 0
    fi
    # Never reclaim in-band: deleting a stale-looking path has an unavoidable
    # TOCTOU with a new owner. Normal traps release this lock; a SIGKILL orphan
    # is fail-closed and surfaced for explicit idle-proven repair.
    sleep 0.1
  done
  return 1
}

# shellcheck disable=SC2120
tartci_pool_lock_release() {
  local lock="${1:-$TARTCI_POOL_TRANSITION_LOCK}"
  [ "$(cat "$lock/pid" 2>/dev/null || true)" = "$$" ] || return 0
  rm -f "$lock/pid" 2>/dev/null || true
  rmdir "$lock" 2>/dev/null || true
}

# End the host-global mint/drain transition only after this supervisor owns a
# live listener process. From this point through assignment or idle teardown,
# the listener's provider state, VM lease, and cleanup trap are the durable
# ownership boundary; drain disables provider restart but does not kill it.
# This keeps the accepted-job-before-Runner.Worker interval protected without
# serializing unrelated repository listeners behind an idle JIT registration.
tartci_pool_lock_handoff_to_listener() {
  local listener_pid="$1"
  case "$listener_pid" in ''|*[!0-9]*) return 1 ;; esac
  [ "$(cat "$TARTCI_POOL_TRANSITION_LOCK/pid" 2>/dev/null || true)" = "$$" ] || return 1
  kill -0 "$listener_pid" 2>/dev/null || return 1
  tartci_pool_lock_release
}

# Return success when PARENT owns a Runner.Worker descendant. Inspection errors
# fail closed as busy; a drain must never stop a job because process evidence
# was unavailable.
tartci_pool_pid_tree_has_worker() {
  local parent="$1" children rc child command
  children="$(pgrep -P "$parent" 2>/dev/null)" || {
    rc=$?
    [ "$rc" = 1 ] && return 1
    return 0
  }
  for child in $children; do
    command="$(ps -p "$child" -o command= 2>/dev/null)" || return 0
    case "$command" in *Runner.Worker*) return 0 ;; esac
    tartci_pool_pid_tree_has_worker "$child" && return 0
  done
  return 1
}

# A stock persistent Actions listener has no safe local drain primitive: worker
# absence misses the accepted-job-before-worker-spawn interval. An authoritative
# Shipyard integration must first remove this host from routing and publish an
# exact `held-idle` receipt after the scheduler reports every persistent runner
# idle. The supported CLI does not currently ship that producer; never invent
# the receipt locally. Only then may tartci perform the provider-side bootout.
# No second scheduler lives here.
tartci_pool_quiesce_persistent_agents_unlocked() {
  local dir="${1:-$HOME/Library/LaunchAgents}" label pid pending=0 target persistent=""
  persistent="$(tartci_pool_runner_agents "$dir" | grep '^actions[.]runner[.]' || true)"
  [ -n "$persistent" ] || return 0
  tartci_pool_persistent_hold_ready || return 1
  while IFS= read -r label; do
    [ -n "$label" ] || continue
    target="$(tartci_pool_launchd_target "$label")"
    pid="$(tartci_pool_agent_pid "$label")"
    # Already absent is the desired terminal state.
    [ -n "$pid" ] || continue
    if tartci_pool_pid_tree_has_worker "$pid"; then
      pending=1
      continue
    fi
    # Pool-on/off supersedes this watcher before it may bootout.
    [ "$(tartci_pool_read_state)" = draining ] || return 1
    tartci_pool_persistent_hold_ready || return 1
    launchctl bootout "$target" >/dev/null 2>&1 || true
    if launchctl print "$target" >/dev/null 2>&1; then
      pending=1
    fi
  done <<EOF
$persistent
EOF
  [ "$pending" = 0 ]
}

tartci_pool_quiesce_persistent_agents() {
  local rc=0
  tartci_pool_lock_acquire || return 1
  tartci_pool_quiesce_persistent_agents_unlocked "$@" || rc=$?
  tartci_pool_lock_release
  return "$rc"
}
