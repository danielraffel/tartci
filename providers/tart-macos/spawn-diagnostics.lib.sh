# shellcheck shell=bash
# Guest diagnostics for a runner that could not start one of its own tools.
#
# A merge-group job on m5 passed every step, then failed "Post Run
# actions/checkout" with "An error occurred trying to start process
# '.../externals/node24/bin/node' ... Exec format error". The same binary had
# run a tenth of a second earlier in that job, and the guest, which held the
# only evidence of why, was destroyed at teardown. When the runner's own logs
# show such a spawn failure, this captures the guest's memory state, the
# externals' identity and signatures, and a short unified-log window before
# the VM is discarded. Every call is bounded in time and the saved output in
# size, so a wedged guest costs at most the stated budget and never the job.
#
# Requires from the caller: SSH_OPTS, SSH_KEY_PRIV, VM_USER, STATE_DIR,
# TARTCI_ROOT and an `event` function.

TARTCI_SPAWN_DIAG_PROBE_TIMEOUT="${TARTCI_SPAWN_DIAG_PROBE_TIMEOUT_SECS:-10}"
TARTCI_SPAWN_DIAG_CAPTURE_TIMEOUT="${TARTCI_SPAWN_DIAG_CAPTURE_TIMEOUT_SECS:-45}"
TARTCI_SPAWN_DIAG_MAX_BYTES="${TARTCI_SPAWN_DIAG_MAX_BYTES:-262144}"
# The runner reports a failed tool start with this sentence, in the job log
# and in the worker's diagnostic log the guest keeps.
TARTCI_SPAWN_ERROR_PATTERN='An error occurred trying to start process'

tartci_spawn_diag_guest(){
  local ip="$1" timeout="$2" operation="$3" script="$4"
  python3 "$TARTCI_ROOT/scripts/bounded_command.py" \
    --timeout "$timeout" --operation "$operation" -- \
    ssh -n ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -i "$SSH_KEY_PRIV" "$VM_USER@$ip" "$script"
}

# Capture diagnostics when the guest's runner logs record a failed tool start.
# Prints nothing and returns 0 when there is none; never fails the caller.
tartci_capture_guest_spawn_errors(){
  local vm="$1" ip="$2" errors dir file raw rc=0 bytes
  [ -n "$ip" ] || return 0
  errors="$(tartci_spawn_diag_guest "$ip" "$TARTCI_SPAWN_DIAG_PROBE_TIMEOUT" spawn-diag-probe \
    "grep -h -F '$TARTCI_SPAWN_ERROR_PATTERN' \"\$HOME\"/actions-runner/_diag/Worker_*.log 2>/dev/null | tail -n 20" \
    2>/dev/null)" || true
  [ -n "$errors" ] || return 0
  dir="$STATE_DIR/spawn-diagnostics"
  mkdir -p "$dir" 2>/dev/null || return 0
  file="$dir/$vm-$(date -u +%Y%m%dT%H%M%SZ).txt"
  raw="$file.raw"
  {
    printf '== spawn errors (runner worker logs)\n%s\n' "$errors"
    # The script runs in the guest; its $HOME and $f are the guest's.
    # shellcheck disable=SC2016
    tartci_spawn_diag_guest "$ip" "$TARTCI_SPAWN_DIAG_CAPTURE_TIMEOUT" spawn-diag-capture '
echo "== vm_stat"; vm_stat
echo "== uptime"; uptime
echo "== runner externals"
for f in "$HOME"/actions-runner/externals/*/bin/node; do
  ls -l "$f"; file "$f"; shasum -a 256 "$f"
  codesign -v "$f" 2>&1 && echo "codesign ok: $f"
done
echo "== log show (last 2m)"; log show --last 2m --style compact 2>&1 | tail -n 400
' 2>&1
  } >"$raw" 2>/dev/null || rc=$?
  head -c "$TARTCI_SPAWN_DIAG_MAX_BYTES" "$raw" >"$file" 2>/dev/null || true
  rm -f "$raw"
  bytes="$(wc -c <"$file" 2>/dev/null | tr -d ' ')"
  event guest_spawn_error_diagnostics "vm=$vm file=$file bytes=${bytes:-0} capture_rc=$rc"
  return 0
}
