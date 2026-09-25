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
#      current exact job finish; off bootouts immediately (and so refuses
#      while an owned lane is mid-job unless --now names that consequence). Both halves are
#      scoped to the services this host's pool receipt can restore, because an
#      operation that stops more than its inverse starts is not a pause, it is
#      a deletion (see tartci_pool_owned_runner_agents).
#
# Pure logic lives here (participation r/w, agent enumeration) so it is unit
# testable; the dispatcher's cmd_pool wires launchctl + JSON on top.
# shellcheck shell=bash

TARTCI_POOL_PARTICIPATION_FILE="${TARTCI_POOL_PARTICIPATION_FILE:-$HOME/.config/tartci/native-build-participation}"
TARTCI_POOL_STATE_FILE="${TARTCI_POOL_STATE_FILE:-$HOME/.config/tartci/pool-state}"
TARTCI_POOL_PERSISTENT_HOLD_FILE="${TARTCI_POOL_PERSISTENT_HOLD_FILE:-$HOME/.config/tartci/persistent-runner-admission-hold}"
TARTCI_POOL_TRANSITION_LOCK="${TARTCI_POOL_TRANSITION_LOCK:-$HOME/.config/tartci/pool-transition.lock}"
TARTCI_POOL_FLEET_RECEIPT="${TARTCI_POOL_FLEET_RECEIPT:-$HOME/.config/tartci/macos-fleet-install.json}"
# The plist glob that is tartci's own by construction: the installer renders
# exactly these, so they stay in scope even when the receipt is unreadable.
TARTCI_POOL_FLEET_PLIST_GLOB='com.danielraffel.tartci.tart-runner-macos-fleet.*.plist'
# Per-lane mid-job probe shared with `tartci launchd reload` (one definition of
# "mid-job": the label's launchd pid owns a `tart run` or Runner.Worker).
TARTCI_POOL_LANE_BUSY="${TARTCI_POOL_LANE_BUSY:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../scripts" 2>/dev/null && pwd)/lane_busy.py}"

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

# The service labels named by the installed macOS fleet receipt: its rendered
# lane plists plus the persistent runner plists the profile declares. This is a
# cheap structural read, deliberately NOT the full cohort verification `pool on`
# performs — a receipt that fails verification still records which services this
# host installed, and the destructive half of a transition must keep working on
# a host whose cohort has drifted. Prints nothing when the receipt is absent,
# unreadable, or malformed.
tartci_pool_receipt_services() {
  local receipt="${1:-$TARTCI_POOL_FLEET_RECEIPT}"
  [ -f "$receipt" ] || return 0
  python3 - "$receipt" 2>/dev/null <<'PY' || true
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        receipt = json.load(handle)
except Exception:
    raise SystemExit(0)
if not isinstance(receipt, dict):
    raise SystemExit(0)
names = set()
for key in ("plists", "persistent_plists"):
    value = receipt.get(key)
    if isinstance(value, dict):
        names.update(name for name in value if isinstance(name, str))
    elif isinstance(value, list):
        names.update(name for name in value if isinstance(name, str))
for name in sorted(names):
    print(name[:-6] if name.endswith(".plist") else name)
PY
}

# Is this host's runner fleet receipt-managed at all? A host with neither a
# receipt nor an installed fleet plist is a legacy/unmanaged host, where
# `pool on` enumerates and activates every runner agent it finds.
tartci_pool_fleet_managed() {
  local dir="${1:-$HOME/Library/LaunchAgents}"
  local receipt="${2:-$TARTCI_POOL_FLEET_RECEIPT}"
  [ -f "$receipt" ] && return 0
  [ -d "$dir" ] || return 1
  [ -n "$(find "$dir" -maxdepth 1 -name "$TARTCI_POOL_FLEET_PLIST_GLOB" -print -quit 2>/dev/null)" ]
}

# The runner agents a pool transition owns — exactly the set `pool on` can bring
# back.
#
# `tartci pool on` activates only the services named by the installed fleet
# receipt: "unreceipted persistent or legacy runner services require their own
# explicit install/activation authority; this fleet transaction will not start
# them incidentally." That restraint is correct, and it binds the destructive
# half too. When `pool off` booted out and disabled every runner agent on disk,
# it deleted lanes `pool on` would never restore — a foreign repository's
# persistent Actions runner went down with the fleet, stayed `disabled` in the
# launchd user domain across reboots, and the loss was invisible because the
# plist was still on disk. An operation that stops more than its inverse starts
# is not a pause, it is a deletion.
#
# Ownership therefore resolves the same way activation does:
#   * unmanaged host  -> every enumerated runner agent (what `pool on` activates)
#   * receipt present -> the services that receipt names
#   * receipt broken  -> the rendered fleet plists, which are tartci's own by
#                        construction; everything else keeps its own authority
#                        until the receipt is repaired, which is the safe
#                        direction (capacity is left running, and reported).
tartci_pool_owned_runner_agents() {
  local dir="${1:-$HOME/Library/LaunchAgents}"
  local receipt="${2:-$TARTCI_POOL_FLEET_RECEIPT}"
  local all owned
  all="$(tartci_pool_runner_agents "$dir")"
  [ -n "$all" ] || return 0
  if ! tartci_pool_fleet_managed "$dir" "$receipt"; then
    printf '%s\n' "$all"
    return 0
  fi
  owned="$(tartci_pool_receipt_services "$receipt")"
  if [ -z "$owned" ]; then
    owned="$(printf '%s\n' "$all" \
      | grep '^com[.]danielraffel[.]tartci[.]tart-runner-macos-fleet[.]' || true)"
  fi
  [ -n "$owned" ] || return 0
  # Intersect with what is actually installed: a receipt may name a service
  # whose plist has since been removed, and a transition only acts on services
  # this host still carries.
  printf '%s\n' "$all" | grep -Fx -f <(printf '%s\n' "$owned") || true
}

# The runner agents present on this host that a pool transition does NOT own —
# the inverse of tartci_pool_owned_runner_agents over the same enumeration.
# `pool off` reports these by name instead of stopping them, so the capacity it
# deliberately leaves alone is never silent.
tartci_pool_unowned_runner_agents() {
  local dir="${1:-$HOME/Library/LaunchAgents}"
  local receipt="${2:-$TARTCI_POOL_FLEET_RECEIPT}"
  local all owned
  all="$(tartci_pool_runner_agents "$dir")"
  [ -n "$all" ] || return 0
  owned="$(tartci_pool_owned_runner_agents "$dir" "$receipt")"
  if [ -z "$owned" ]; then
    printf '%s\n' "$all"
    return 0
  fi
  printf '%s\n' "$all" | grep -Fxv -f <(printf '%s\n' "$owned") || true
}

# Exact-line membership in a newline-separated set, without a pipe.
# `printf '%s\n' "$set" | grep -Fxq "$label"` looks equivalent and is not: grep
# exits on the first match, printf takes SIGPIPE, and under `set -o pipefail`
# the pipeline then reports failure for a MATCH. It is size-dependent, so it
# reads as correct until the set outgrows the pipe buffer.
tartci_pool_label_in_set() {
  local label="$1" haystack="$2" nl=$'\n'
  case "${nl}${haystack}${nl}" in
    *"${nl}${label}${nl}"*) return 0 ;;
  esac
  return 1
}

# Name, on stdout, every runner agent a transition deliberately did not touch.
# Both outages this scoping prevents were invisible precisely because nothing
# said this: the plist stayed on disk, so every file-existence check passed
# while the lane was gone. Reporting the untouched set by name is what keeps
# the ownership boundary an explicit decision rather than a silent one.
tartci_pool_report_unowned() {
  local action="$1" dir="${2:-$HOME/Library/LaunchAgents}" unowned label state
  unowned="$(tartci_pool_unowned_runner_agents "$dir")"
  [ -n "$unowned" ] || return 0
  echo "not touched by pool $action (outside this host's pool receipt, so \`pool on\`"
  echo "cannot restore them; stop them with their own install authority if needed):"
  while IFS= read -r label; do
    [ -n "$label" ] || continue
    state=stopped
    tartci_pool_agent_loaded "$label" && state="still running"
    printf '  %-56s %s\n' "$label" "$state"
  done <<EOF
$unowned
EOF
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

tartci_pool_lock_identity() {
  local lock="${1:-$TARTCI_POOL_TRANSITION_LOCK}"
  python3 - "$lock" <<'PY'
import os, stat, sys
try:
 s=os.lstat(sys.argv[1])
 if not stat.S_ISDIR(s.st_mode): raise ValueError("lock is not a directory")
 print(f"{s.st_dev}:{s.st_ino}:{s.st_mtime_ns}:{s.st_ctime_ns}")
except Exception:
 raise SystemExit(1)
PY
}

# Cheap pre-allocation fence. This does not replace the serialized late mint
# lock: a transition can still begin after this read, and the existing
# acquire-before-JIT fence remains authoritative for that race. It prevents a
# lock already known to exist from causing clone/boot/discard churn.
tartci_pool_lock_absent() {
  local lock="${1:-$TARTCI_POOL_TRANSITION_LOCK}"
  [ ! -e "$lock" ] && [ ! -L "$lock" ]
}

# Read-only, typed lock evidence.  This deliberately never removes or rewrites
# the lock.  Every field is emitted even when unavailable so callers can fail
# closed instead of treating a partial probe as an idle/dead owner.
tartci_pool_lock_health() {
  local lock="${1:-$TARTCI_POOL_TRANSITION_LOCK}" present=false pid="" inode="" mtime="" owner_alive=unknown owner_start=unknown boot_id=unknown
  if [ -e "$lock" ] || [ -L "$lock" ]; then
    present=true
  fi
  if [ "$present" = true ] && [ -d "$lock" ] && { [ ! -r "$lock" ] || [ ! -x "$lock" ]; }; then
    owner_alive=unobservable
  elif [ "$present" = true ] && [ -d "$lock" ] && [ ! -L "$lock" ]; then
    local identity; identity="$(tartci_pool_lock_identity "$lock" || true)"
    IFS=: read -r _dev inode mtime _ctime <<EOF
$identity
EOF
    pid="$(cat "$lock/pid" 2>/dev/null || true)"
    case "$pid" in
      ''|*[!0-9]*) owner_alive=unknown ;;
      *) if kill -0 "$pid" 2>/dev/null; then owner_alive=true; else owner_alive=false; fi
         owner_start="$(ps -p "$pid" -o lstart= 2>/dev/null | sed 's/^ *//' || true)" ;;
    esac
    boot_id="$(sysctl -n kern.boottime 2>/dev/null || awk '/btime/ {print $2; exit}' /proc/stat 2>/dev/null || true)"
  elif [ "$present" = false ]; then
    owner_alive=false
  fi
  printf 'lock_path=%s\nlock_present=%s\nlock_inode=%s\nlock_mtime=%s\nowner_pid=%s\nowner_alive=%s\nowner_start=%s\nboot_id=%s\n' \
    "$lock" "$present" "$inode" "$mtime" "$pid" "$owner_alive" "$owner_start" "$boot_id"
}

tartci_pool_lock_health_json() {
  tartci_pool_lock_health "${1:-$TARTCI_POOL_TRANSITION_LOCK}" | python3 -c '
import json, sys
fields = {}
for raw in sys.stdin:
    key, separator, value = raw.rstrip("\n").partition("=")
    if not separator:
        raise SystemExit(1)
    fields[key] = value
required = {
    "lock_path", "lock_present", "lock_inode", "lock_mtime", "owner_pid",
    "owner_alive", "owner_start", "boot_id",
}
if set(fields) != required:
    raise SystemExit(1)
present = fields["lock_present"] == "true"
identity_complete = bool(fields["lock_inode"] and fields["lock_mtime"])
pid_valid = fields["owner_pid"].isdigit() and int(fields["owner_pid"]) > 0
if not present:
    state = "absent"
elif fields["owner_alive"] == "unobservable":
    state = "unobservable"
elif not identity_complete or not pid_valid or fields["owner_alive"] == "unknown":
    state = "invalid"
elif fields["owner_alive"] == "true":
    state = "owned"
else:
    state = "orphaned"
print(json.dumps({
    "state": state,
    "present": present,
    "path": fields["lock_path"],
    "inode": int(fields["lock_inode"]) if fields["lock_inode"].isdigit() else None,
    "mtime_ns": int(fields["lock_mtime"]) if fields["lock_mtime"].isdigit() else None,
    "owner_pid": int(fields["owner_pid"]) if pid_valid else None,
    "owner_alive": {"true": True, "false": False}.get(fields["owner_alive"]),
    "owner_start": fields["owner_start"] or None,
    "boot_id": fields["boot_id"] or None,
}, separators=(",", ":"), sort_keys=True))
'
}

# Verify an exact lock observation immediately before an explicit repair. The
# repair command owns the pool-off, worker, and VM gates; this helper supplies
# the final TOCTOU identity fence and refuses partial observations.
tartci_pool_lock_identity_matches() {
  local lock="${1:-$TARTCI_POOL_TRANSITION_LOCK}" expected_identity="$2" expected_pid="$3" expected_start="${4:-}" expected_boot="${5:-}"
  [ -n "$expected_identity" ] || return 1
  [ -d "$lock" ] || return 1
  local identity pid start boot
  identity="$(tartci_pool_lock_identity "$lock" || true)"
  [ -n "$identity" ] || return 1
  pid="$(cat "$lock/pid" 2>/dev/null || true)"
  case "$pid" in
    ''|*[!0-9]*) return 1 ;;
    *) kill -0 "$pid" 2>/dev/null && return 1 ;;
  esac
  start="$(ps -p "$pid" -o lstart= 2>/dev/null | sed 's/^ *//' || true)"
  boot="$(sysctl -n kern.boottime 2>/dev/null || awk '/btime/ {print $2; exit}' /proc/stat 2>/dev/null || true)"
  [ "$identity" = "$expected_identity" ] && [ "$pid" = "$expected_pid" ] \
    && { [ -z "$expected_start" ] || [ "$start" = "$expected_start" ]; } \
    && { [ -z "$expected_boot" ] || [ "$boot" = "$expected_boot" ]; }
}

# Fail-closed transition repair gates.  Callers may provide deterministic test
# seams; production probes the host process table and Tart inventory directly.
tartci_pool_zero_runner_workers() {
  local workers rc ps_bin
  if [ -n "${TARTCI_POOL_PS_BIN:-}" ]; then
    workers="$("$TARTCI_POOL_PS_BIN" 2>/dev/null)"; rc=$?
    [ "$rc" = 0 ] || return 1
    printf '%s\n' "$workers" | grep -Eq '(^|[[:space:]/])Runner[.]Worker([[:space:]]|$)' && return 1
    return 0
  fi
  for ps_bin in /bin/ps /usr/bin/ps; do [ -x "$ps_bin" ] && break; done
  [ -x "$ps_bin" ] || return 1
  if [ "$(uname -s 2>/dev/null || true)" = Darwin ]; then
    workers="$("$ps_bin" -axo command= 2>/dev/null)"
  else
    workers="$("$ps_bin" -eo command= 2>/dev/null)"
  fi
  rc=$?
  [ "$rc" = 0 ] || return 1
  printf '%s\n' "$workers" | grep -Eq '(^|[[:space:]/])Runner[.]Worker([[:space:]]|$)' && return 1
  return 0
}

tartci_pool_zero_running_vms() {
  local tart="${TARTCI_POOL_TART_BIN:-tart}"
  if [ -z "${TARTCI_POOL_TART_BIN:-}" ] && ! command -v "$tart" >/dev/null 2>&1; then
    local ps_bin
    for ps_bin in /bin/ps /usr/bin/ps; do [ -x "$ps_bin" ] && break; done
    [ -x "$ps_bin" ] || return 1
    # QEMU-only hosts have no Tart inventory; a live QEMU process is the
    # equivalent provider VM evidence.  Unknown process output refuses.
    local qemu_processes
    qemu_processes="$($ps_bin -eo command= 2>/dev/null)" || return 1
    printf '%s\n' "$qemu_processes" | grep -Eq '(^|[[:space:]/])qemu-system-' && return 1
    return 0
  fi
  python3 - "$tart" <<'PY'
import json, subprocess, sys
try:
 result=subprocess.run([sys.argv[1], "list", "--format", "json"], check=True,
                       capture_output=True, text=True, timeout=5)
 rows=json.loads(result.stdout)
 if not isinstance(rows,list): raise ValueError("inventory is not an array")
 for row in rows:
  if not isinstance(row,dict): raise ValueError("inventory entry is not an object")
  state=str(row.get("State",row.get("status",row.get("state","")))).lower()
  has_running="Running" in row or "running" in row
  running=row.get("Running",row.get("running"))
  if has_running and not isinstance(running, bool):
   raise ValueError("inventory Running value is not boolean")
  if isinstance(running, bool):
   if running or state.startswith("run") or state in ("booted","up"): raise SystemExit(1)
   if state in ("stopped","off","false",""): continue
   raise ValueError("inventory state disagrees with Running")
  if state.startswith("run") or state in ("booted","true","up"): raise SystemExit(1)
except Exception: raise SystemExit(2)
PY
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

# Which runner agents this host's pool owns are mid-job right now. Prints one
# `label<TAB>state<TAB>detail` row per owned agent (state busy|idle|absent|
# unknown) and returns 0 when none is busy or unknown, 1 when one is busy, 2
# when any answer is unknown (including a probe that could not run at all).
# Read-only: `launchctl print` plus one process-table read.
tartci_pool_mid_job() {
  local dir="${1:-$HOME/Library/LaunchAgents}" labels label rc=0
  local -a argv=()
  labels="$(tartci_pool_owned_runner_agents "$dir")"
  while IFS= read -r label; do
    [ -n "$label" ] && argv+=("$label")
  done <<EOF
$labels
EOF
  [ "${#argv[@]}" -gt 0 ] || return 0
  python3 "$TARTCI_POOL_LANE_BUSY" --agents-dir "$dir" "${argv[@]}" || rc=$?
  case "$rc" in
    0|1|2) return "$rc" ;;
    *) printf 'lane busy probe failed (exit %s)\n' "$rc"; return 2 ;;
  esac
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
#
# Scoped to receipt-owned agents for the same reason `off` is: this watcher
# bootouts a persistent listener, and `pool on` restores only what the receipt
# names. A persistent runner belonging to another repository's install authority
# is not this drain's to stop.
tartci_pool_quiesce_persistent_agents_unlocked() {
  local dir="${1:-$HOME/Library/LaunchAgents}" label pid pending=0 target persistent=""
  persistent="$(tartci_pool_owned_runner_agents "$dir" | grep '^actions[.]runner[.]' || true)"
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
