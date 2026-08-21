# tartci pool — host-level CI participation opt-out ("this machine, not now").
#
# A single host-level switch, deliberately decoupled from any per-lane GUI
# toggle or placement engine, so "opt this Mac out" cannot silently vanish when
# lanes change or placement moves. Two effects, matching how the pool actually
# works (GitHub is the scheduler; every Mac is a label-matched runner):
#   1. native-build participation file  -> the lease governor refuses to place a
#      native-build lease on an opted-out host (covers non-launchd build paths).
#   2. runner LaunchAgents unloaded      -> the host's GitHub Actions / tartci
#      serve runners stop polling. launchctl unload is immediate and can stop
#      an active provider job; callers must prove scheduler + local idleness
#      before using `pool off`. This command is not a drain operation.
#
# Pure logic lives here (participation r/w, agent enumeration) so it is unit
# testable; the dispatcher's cmd_pool wires launchctl + JSON on top.
# shellcheck shell=bash

TARTCI_POOL_PARTICIPATION_FILE="${TARTCI_POOL_PARTICIPATION_FILE:-$HOME/.config/tartci/native-build-participation}"

# Read participation: 1 = participating, 0 = opted out. Absent means
# participating — opting out is an explicit act, and a missing/garbage file must
# never silently pull a host out of the pool.
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
  printf '%s\n' "$v" > "$f"
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
      actions.runner.*)
        # NB: the bare `tart-runner` (no suffix) is the macOS GATE lane — it MUST
        # be matched, or `pool off` would leave the gate serving despite
        # participation=0. Match both bare and `-suffixed` runner labels.
        printf '%s\n' "$b"
        ;;
    esac
  done
}
