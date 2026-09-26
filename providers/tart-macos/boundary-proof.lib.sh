# Pre-mint proofs started beside the clone instead of after the boot.
# shellcheck shell=bash
# shellcheck disable=SC2034 # the BOUNDARY_* results are consumed by runner.sh
#
# Two proofs gate every JIT mint: Shipyard's admission verdict for the lane's
# (repo, labels), and the runner group's repository-access proof. Neither reads
# the VM. Both used to start only after the clone, the boot, SSH and the guest
# preflights had finished, so their whole duration sat on every job's critical
# path (about a minute per job on the Pulp gate, a fifth of merge-group wait).
#
# This starts both in the background as soon as the VM lease is held, and the
# boundary consumes their results. Nothing about what they decide changes:
#
#   * The boundary still refuses exactly as before. A non-admit verdict or a
#     failed access proof discards the booted VM and returns the same code.
#   * An admission verdict older than TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS when
#     the boundary reads it is not used; the boundary asks Shipyard again,
#     synchronously, as it always did. That keeps the verdict's freshness at
#     mint bounded, which is what running it at the boundary was for.
#   * A result that is missing, partial or unreadable is not used either. The
#     synchronous path is the fallback for every uncertainty, so the parallel
#     path can only ever remove time, never a check.
#
# TARTCI_BOUNDARY_PROOF_PARALLEL=0 restores the fully sequential boundary.

BOUNDARY_PROOF_PID=""
BOUNDARY_PROOF_DIR=""
# Set by tartci_boundary_proof_take_admission / _take_access.
BOUNDARY_ADMISSION_JSON=""
BOUNDARY_ADMISSION_RC=0
BOUNDARY_ADMISSION_AGE=0
BOUNDARY_ACCESS_JSON=""
BOUNDARY_ACCESS_RC=0

tartci_boundary_proof_parallel_enabled(){
  [ "${TARTCI_BOUNDARY_PROOF_PARALLEL:-1}" = 1 ]
}

tartci_boundary_proof_validate(){
  case "${TARTCI_BOUNDARY_PROOF_PARALLEL:-1}" in
    0|1) ;;
    *) printf 'invalid TARTCI_BOUNDARY_PROOF_PARALLEL: expected 0 or 1\n' >&2; return 2 ;;
  esac
  case "${TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS:-120}" in
    ''|*[!0-9]*) printf 'invalid TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS: expected 0-600\n' >&2; return 2 ;;
  esac
  [ "${TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS:-120}" -le 600 ] \
    || { printf 'invalid TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS: expected 0-600\n' >&2; return 2; }
}

# Kill a process and every descendant. The proofs run behind two subshell
# layers and a python interpreter; killing only the outer subshell would leave
# the Shipyard call running and writing into a directory nobody reads.
_tartci_boundary_proof_kill_tree(){
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    _tartci_boundary_proof_kill_tree "$child"
  done
  kill -TERM "$pid" 2>/dev/null || true
}

# Drop any in-flight or unconsumed proof. Safe to call at any time and from
# every exit path; a no-op when nothing was started.
tartci_boundary_proof_abandon(){
  if [ -n "$BOUNDARY_PROOF_PID" ]; then
    _tartci_boundary_proof_kill_tree "$BOUNDARY_PROOF_PID"
    wait "$BOUNDARY_PROOF_PID" 2>/dev/null || true
  fi
  [ -z "$BOUNDARY_PROOF_DIR" ] || rm -rf "$BOUNDARY_PROOF_DIR" 2>/dev/null || true
  BOUNDARY_PROOF_PID=""
  BOUNDARY_PROOF_DIR=""
}

# Start both proofs for this boot. Each writes its payload, then its rc file
# last, so a present rc file means a complete result.
tartci_boundary_proof_start(){
  local vm="$1" labels="$2" group_id="$3" dir
  tartci_boundary_proof_abandon
  tartci_boundary_proof_parallel_enabled || return 0
  dir="$STATE_DIR/$vm.boundary-proof"
  rm -rf "$dir" 2>/dev/null || true
  # Unable to stage results means the boundary runs both proofs itself.
  mkdir -p "$dir" 2>/dev/null || return 0
  BOUNDARY_PROOF_DIR="$dir"
  (
    set +e
    if tartci_admission_clean_enabled; then
      (
        json="$(tartci_admission_clean "$REPO" "$labels")"
        rc=$?
        printf '%s\n' "$json" >"$dir/admission.json"
        date +%s >"$dir/admission.at"
        printf '%s\n' "$rc" >"$dir/admission.rc"
      ) &
    fi
    (
      json="$(SHIPYARD_GH_APP_REPO="$REPO" GH_REPO="$REPO" \
        python3 "$TARTCI_ROOT/scripts/runner_group_repository_access.py" \
        --repo "$REPO" --runner-group-id "$group_id" \
        --gh-cli "$JIT_GH_CLI" 2>"$dir/access.err")"
      rc=$?
      printf '%s\n' "$json" >"$dir/access.json"
      printf '%s\n' "$rc" >"$dir/access.rc"
    ) &
    wait
  ) </dev/null >/dev/null 2>&1 &
  BOUNDARY_PROOF_PID=$!
  event admission_parallel_start "labels=$labels group=$group_id"
}

_tartci_boundary_proof_join(){
  [ -n "$BOUNDARY_PROOF_PID" ] || return 0
  wait "$BOUNDARY_PROOF_PID" 2>/dev/null || true
  BOUNDARY_PROOF_PID=""
}

_tartci_boundary_proof_read_rc(){
  local file="$1" value
  [ -r "$file" ] || return 1
  IFS= read -r value <"$file" || return 1
  case "$value" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s' "$value"
}

# Use the parallel admission verdict when it is complete and fresh. Returns 1
# (use the synchronous path) otherwise. The payload is taken as-is: it is the
# same adapter's output the synchronous call would have produced.
tartci_boundary_proof_take_admission(){
  local dir="$BOUNDARY_PROOF_DIR" rc at now max_age
  [ -n "$dir" ] || return 1
  _tartci_boundary_proof_join
  rc="$(_tartci_boundary_proof_read_rc "$dir/admission.rc")" || return 1
  at="$(_tartci_boundary_proof_read_rc "$dir/admission.at")" || return 1
  now="$(date +%s)"
  max_age="${TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS:-120}"
  BOUNDARY_ADMISSION_AGE=$((now - at))
  if [ "$BOUNDARY_ADMISSION_AGE" -lt 0 ] || [ "$BOUNDARY_ADMISSION_AGE" -gt "$max_age" ]; then
    event admission_parallel_stale "age=${BOUNDARY_ADMISSION_AGE}s max_age=${max_age}s"
    return 1
  fi
  BOUNDARY_ADMISSION_RC="$rc"
  BOUNDARY_ADMISSION_JSON="$(cat "$dir/admission.json" 2>/dev/null)" || BOUNDARY_ADMISSION_JSON=""
  return 0
}

# Use the parallel repository-access proof when it is complete. Copies its
# stderr to the caller's error file so the refusal path reads it where it
# always has.
tartci_boundary_proof_take_access(){
  local error_file="$1" dir="$BOUNDARY_PROOF_DIR" rc
  [ -n "$dir" ] || return 1
  _tartci_boundary_proof_join
  rc="$(_tartci_boundary_proof_read_rc "$dir/access.rc")" || return 1
  BOUNDARY_ACCESS_RC="$rc"
  BOUNDARY_ACCESS_JSON="$(cat "$dir/access.json" 2>/dev/null)" || BOUNDARY_ACCESS_JSON=""
  if [ -r "$dir/access.err" ]; then
    cp "$dir/access.err" "$error_file" 2>/dev/null || : >"$error_file"
  else
    : >"$error_file"
  fi
  return 0
}
