# tartci Part F — live, GUI-adjustable host-wide macOS VM cap + cross-lane mutex.
# shellcheck shell=bash
# Sourced into the tart-macos provider's loop (and unit-tested standalone).
# macOS has no flock(1), so the host-wide lock is a mkdir mutex with dead-holder
# stealing and a fail-OPEN timeout (a stuck lock must never starve the gate).

: "${TARTCI_MACOS_CAP_FILE:=$HOME/.config/tartci/macos-vm-cap}"
: "${TARTCI_MACOS_LOCKDIR:=$HOME/.config/tartci/macos-vm.lock.d}"
: "${TARTCI_MACOS_RESV_DIR:=$HOME/.config/tartci/macos-vm-reservations}"
: "${TARTCI_MACOS_RESV_TTL:=7200}"   # prune a reservation older than this (crash safety); > any build
: "${TARTCI_MACOS_LOCK_TRIES:=30}"   # × 0.5s ≈ 15s before failing open
: "${TARTCI_MACOS_HARD_MAX:=2}"      # Apple allows 2 macOS guests/host — a hard ceiling no source may exceed

# Effective cap: live from the GUI-written file if it holds a valid int >=1,
# else the static $CAP the runner started with. Clamped to [1, HARD_MAX] so a
# stale/garbage/fat-fingered cap file (e.g. "20") can never drive boot thrash
# past Apple's 2-guest virtualization limit.
tartci_effective_cap(){
  local c="${CAP:-2}" v
  if [ -r "$TARTCI_MACOS_CAP_FILE" ]; then
    v="$(tr -dc '0-9' < "$TARTCI_MACOS_CAP_FILE" 2>/dev/null | head -c 2)"
    if [ -n "$v" ] && [ "$v" -ge 1 ] 2>/dev/null; then c="$v"; fi
  fi
  # Hard clamp both ends — protects against a bad cap file AND a bad startup $CAP.
  case "$c" in ""|*[!0-9]*) c=1;; esac
  [ "$c" -ge 1 ] 2>/dev/null || c=1
  [ "$c" -le "$TARTCI_MACOS_HARD_MAX" ] 2>/dev/null || c="$TARTCI_MACOS_HARD_MAX"
  printf '%s' "$c"
}

# Count live reservations = macOS slots claimed by in-flight boots/jobs. Each
# reservation file holds "<owner-supervisor-pid> <epoch>". Prune any whose owner
# process is dead (crash / reload-mid-job → the VM is already gone) or that is
# older than the TTL backstop, or that is empty/garbage — so an orphaned
# reservation can't dark the gate (critical at cap=1).
tartci_active_reservations(){
  local now line pid ts n=0 f
  now="$(date +%s)"
  [ -d "$TARTCI_MACOS_RESV_DIR" ] || { printf 0; return; }
  for f in "$TARTCI_MACOS_RESV_DIR"/resv.*; do
    [ -e "$f" ] || continue
    line="$(cat "$f" 2>/dev/null || echo)"
    if [ -z "$line" ]; then rm -f "$f" 2>/dev/null; continue; fi
    pid="${line%% *}"; ts="${line##* }"
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then rm -f "$f" 2>/dev/null; continue; fi
    case "$ts" in ""|*[!0-9]*) ts=0;; esac
    if [ $(( now - ts )) -gt "$TARTCI_MACOS_RESV_TTL" ]; then rm -f "$f" 2>/dev/null; continue; fi
    n=$((n+1))
  done
  printf '%s' "$n"
}

# Atomic host-wide lock via mkdir. Steals a dead holder's lock. Returns 1 (fail
# open) after the timeout so a pathological lock never blocks booting forever.
tartci_lock(){
  local lpid
  for _ in $(seq 1 "$TARTCI_MACOS_LOCK_TRIES"); do
    if mkdir "$TARTCI_MACOS_LOCKDIR" 2>/dev/null; then echo $$ > "$TARTCI_MACOS_LOCKDIR/pid" 2>/dev/null; return 0; fi
    lpid="$(cat "$TARTCI_MACOS_LOCKDIR/pid" 2>/dev/null)"
    if [ -n "$lpid" ] && ! kill -0 "$lpid" 2>/dev/null; then
      # Steal a DEAD holder's lock atomically: `mv` can only succeed for ONE
      # racer (the source can be renamed exactly once), so two waiters can't both
      # rm a live holder's freshly-created lockdir and both enter the CS. The
      # loser's mv fails (source already gone) → it just retries mkdir next pass.
      mv "$TARTCI_MACOS_LOCKDIR" "$TARTCI_MACOS_LOCKDIR.dead.$$" 2>/dev/null \
        && rm -rf "$TARTCI_MACOS_LOCKDIR.dead.$$" 2>/dev/null
      continue
    fi
    sleep 0.5
  done
  return 1
}
tartci_unlock(){ rm -rf "$TARTCI_MACOS_LOCKDIR" 2>/dev/null || true; }

# Claim a macOS VM slot if the host is under the effective cap. Echoes a
# reservation file path on success (caller MUST rm it when the job ends); echoes
# nothing when full. Uses max(running VMs, outstanding reservations) so a VM
# that's booting-but-not-yet-listed still counts. `running_macos_vms` must be
# defined by the caller (runner.sh provides it); falls back to 0 if absent.
tartci_claim_macos_slot(){
  local cap="$1" running reserv resv locked=0
  mkdir -p "$TARTCI_MACOS_RESV_DIR" 2>/dev/null || true
  tartci_lock && locked=1
  if command -v running_macos_vms >/dev/null 2>&1; then running="$(running_macos_vms)"; else running=0; fi
  reserv="$(tartci_active_reservations)"
  [ "${reserv:-0}" -gt "${running:-0}" ] && running="$reserv"
  if [ "${running:-0}" -lt "$cap" ]; then
    # Stamp the OWNING supervisor PID ($$ is the parent shell even inside this
    # command-substitution subshell) so a dead owner's slot can be reclaimed.
    resv="$(mktemp "$TARTCI_MACOS_RESV_DIR/resv.XXXXXX" 2>/dev/null)"
    if [ -n "$resv" ]; then
      printf '%s %s' "$$" "$(date +%s)" > "$resv" 2>/dev/null || true
      printf '%s' "$resv"
    else
      # FS failure (disk full / unwritable config dir): fail OPEN. Never dark a
      # REQUIRED gate on a transient disk hiccup — boot with an untracked slot;
      # Apple's 2-guest limit still backstops actual oversubscription. The sentinel
      # path doesn't exist, so the caller's `rm -f` is a harmless no-op and
      # tartci_active_reservations never counts it.
      printf '%s' "$TARTCI_MACOS_RESV_DIR/resv.failopen.$$"
    fi
  fi
  [ "$locked" = 1 ] && tartci_unlock
  return 0
}
