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
        # A reused admit was observed before this call answered; date the
        # result from that observation, so the boundary's age bound covers
        # the reuse window as well as the wait.
        reused_age="$(printf '%s' "$json" \
          | sed -n 's/.*"tartci_reused_admit_age_secs":\([0-9][0-9]*\).*/\1/p')"
        printf '%s\n' "$(( $(date +%s) - ${reused_age:-0} ))" >"$dir/admission.at"
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

# Overlapped admission precheck (opt-in: TARTCI_ADMISSION_PRECHECK_OVERLAP=1).
#
# A job boot asks Shipyard's admission question twice: the serial pre-clone
# precheck in run_one, and the parallel proof above, started once the lease is
# held and consumed at the JIT boundary. The precheck is an early bail only
# (see run_one): it never admits anything the boundary would refuse. It costs
# its whole duration on every job's critical path while the VM slot is held:
# p50 37-48 s per served job, about 6 slot-hours a day across the fleet in the
# 2026-11-07..09 lane logs, against clone+boot+preflights of about a minute
# that it could have overlapped.
#
# With the overlap on, run_one skips the serial precheck, so the parallel proof
# starts as soon as the lease is held and runs beside the clone, boot and guest
# preflights. The boundary consumes it exactly as before: a refusal discards the
# booted, unregistered VM with the same return code, a stale or missing result
# is asked again synchronously. Nothing is admitted that would not have been.
#
# What changes is the price of a refusal: a VM is cloned and booted before it
# is discarded, where the precheck would have skipped the clone. So a refusal
# seen at the boundary of an overlapped run opens a backoff: for
# TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS (default 600, 0 = none) this
# lane runs the serial precheck again, and the first serial admit closes the
# backoff early. A lane whose admission authority is down therefore boots at
# most one VM per backoff window instead of one per poll.

TARTCI_PRECHECK_OVERLAP_BACKOFF_DEFAULT=600

tartci_precheck_overlap_validate(){
  case "${TARTCI_ADMISSION_PRECHECK_OVERLAP:-0}" in
    0|1) ;;
    *) printf 'invalid TARTCI_ADMISSION_PRECHECK_OVERLAP: expected 0 or 1\n' >&2; return 2 ;;
  esac
  case "${TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS:-$TARTCI_PRECHECK_OVERLAP_BACKOFF_DEFAULT}" in
    ''|*[!0-9]*)
      printf 'invalid TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS: expected 0-3600\n' >&2; return 2 ;;
  esac
  [ "${TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS:-$TARTCI_PRECHECK_OVERLAP_BACKOFF_DEFAULT}" -le 3600 ] \
    || { printf 'invalid TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS: expected 0-3600\n' >&2; return 2; }
}

tartci_precheck_overlap_backoff_file(){
  printf '%s/%s.precheck-overlap-backoff' "$STATE_DIR" "$RUNNER_NAME"
}

# Seconds left in this lane's backoff, or nothing when none is open. An
# unreadable or future-dated record counts as open for the whole window: the
# safe side is the serial precheck, which is today's behaviour.
tartci_precheck_overlap_backoff_remaining(){
  local file at now window left
  window="${TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS:-$TARTCI_PRECHECK_OVERLAP_BACKOFF_DEFAULT}"
  [ "$window" -gt 0 ] || return 0
  file="$(tartci_precheck_overlap_backoff_file)"
  [ -e "$file" ] || return 0
  now="$(date +%s)"
  at="$(_tartci_boundary_proof_read_rc "$file")" || at=""
  if [ -z "$at" ] || [ "$at" -gt "$now" ]; then
    printf '%s' "$window"
    return 0
  fi
  left=$((at + window - now))
  [ "$left" -le 0 ] || printf '%s' "$left"
}

# 0 when this run may skip the serial precheck. Every input that is off or
# uncertain answers no, which runs the serial precheck exactly as before.
tartci_precheck_overlap_active(){
  [ "${TARTCI_ADMISSION_PRECHECK_OVERLAP:-0}" = 1 ] || return 1
  # The overlap IS the parallel proof; without it the boundary would ask
  # synchronously after the boot and the precheck's early bail would be lost
  # for nothing.
  tartci_boundary_proof_parallel_enabled || return 1
  # A warm hand-off has no clone or boot to overlap with.
  [ -z "${WARM_VM:-}" ] || return 1
  [ -z "$(tartci_precheck_overlap_backoff_remaining)" ]
}

# Called at the boundary when an overlapped run was refused: open the backoff.
tartci_precheck_overlap_note_refusal(){
  local rc="$1" file tmp window
  window="${TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS:-$TARTCI_PRECHECK_OVERLAP_BACKOFF_DEFAULT}"
  [ "$window" -gt 0 ] || return 0
  file="$(tartci_precheck_overlap_backoff_file)"
  tmp="$file.tmp.$$"
  if printf '%s\n' "$(date +%s)" >"$tmp" 2>/dev/null && mv -f "$tmp" "$file" 2>/dev/null; then
    event precheck_overlap_backoff "rc=$rc backoff=${window}s"
  else
    rm -f "$tmp" 2>/dev/null || true
    event precheck_overlap_backoff_unrecorded "rc=$rc"
  fi
}

# Called after a serial precheck admits: the authority answers again, so the
# next run may overlap.
tartci_precheck_overlap_clear(){
  local file
  file="$(tartci_precheck_overlap_backoff_file)"
  [ -e "$file" ] || return 0
  rm -f "$file" 2>/dev/null || true
  event precheck_overlap_backoff_cleared ""
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

# 0 when a GitHub error file shows a rate limit: the primary limit ("API rate
# limit exceeded"), a secondary or abuse limit, an HTTP 429, or a Retry-After.
# GitHub answers a primary-limit hit with HTTP 403, so this must be asked
# before any 403 is read as an auth refusal: a rate limit is transient and
# clears by itself, an auth refusal does not.
tartci_github_rate_limited(){
  grep -Eiq 'rate limit|abuse detection|HTTP 429|retry-after' "$1" 2>/dev/null
}

# 0 when GitHub refused the auth route itself (401/403/404 or an integration
# permission error) and the refusal is not a rate limit.
tartci_github_auth_rejected(){
  local error_file="$1"
  tartci_github_rate_limited "$error_file" && return 1
  grep -Eq 'HTTP (401|403|404)|Resource not accessible by integration' "$error_file" 2>/dev/null
}

# 0 when a failed repository-access proof is a real denial (the proof said
# deny, or GitHub refused the App), 1 when it only could not be answered.
tartci_repository_access_denied(){
  local rc="$1" error_file="$2"
  [ "$rc" -eq 3 ] && return 0
  tartci_github_auth_rejected "$error_file"
}

# One short, single-line cause for an event detail.
tartci_repository_access_reason(){
  local error_file="$1" line
  line="$(head -c 400 "$error_file" 2>/dev/null | tr '\n\t' '  ')" || line=""
  case "$line" in
    *"timed out"*) printf 'reason=timeout' ;;
    *[Rr]"ate limit"*|*"abuse detection"*|*"HTTP 429"*|*[Rr]"etry-"[Aa]"fter"*) printf 'reason=rate_limited' ;;
    "") printf 'reason=unknown' ;;
    *) printf 'reason=error detail=%s' "$(printf '%s' "$line" | cut -c1-160)" ;;
  esac
}
