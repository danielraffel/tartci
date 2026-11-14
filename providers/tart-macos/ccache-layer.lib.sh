#!/usr/bin/env bash
# Per-job ccache write isolation for disposable macOS guests.
#
# Without it every guest mounts the host ccache directory read-write, so a job
# torn down mid-build (a Tart hang, a timeout, a cancel) can leave half-written
# entries that every later job trusts, and any job, red or green, can write or
# overwrite any entry. With TARTCI_CCACHE_WRITE_ISOLATION=1 each job instead:
#
#   * mounts exactly one ccache directory, the shared store, READ-ONLY
#     (`ccache-shared` = <layers>/shared), and reads it through ccache remote
#     storage also marked `read-only`;
#   * writes its new entries only to a guest-local directory
#     (~/.tartci-ccache/job/remote, `remote_only`), so the guest holds no
#     writable path, link or inode on the host cache filesystem;
#   * and when the job's verdict is green, the host pulls those entries over
#     ssh as a tar stream and validates every member (scripts/ccache_layer.py
#     receive) into a host-only job layer, which is promoted into the shared
#     store once the VM is provably gone. Red, cancelled, timed-out and
#     orphaned jobs are never pulled.
#
# The layers root is $CACHE_ROOT/ccache-layers-v1, a SIBLING of the legacy
# $CACHE_ROOT/ccache directory, not inside it: a lane that still mounts the
# legacy directory read-write (a lane not yet switched, an operator run of
# run.sh) cannot reach the shared store either. The store uses ccache's `file:`
# remote-storage layout (`<k0k1>/<rest>`), so it starts cold on each host; the
# first green job per host warms it.
#
# A promoted layer is still the guest's own claim about which object a key
# names: isolation keeps red, cancelled, timed-out and torn jobs out of the
# shared store and stops any job overwriting an existing result, but a GREEN
# job's new results are trusted, and a green job's manifest replaces an older
# one. Fork pull requests never reach these lanes.
#
# Unset (the default) changes nothing: the boot mounts the legacy `ccache`
# directory, every variable the guest command uses below is empty, and the
# hooks return immediately.

CCACHE_LAYER_ENABLED=0
case "${TARTCI_CCACHE_WRITE_ISOLATION:-}" in
  ''|0) ;;
  1) CCACHE_LAYER_ENABLED=1 ;;
  *) printf 'invalid TARTCI_CCACHE_WRITE_ISOLATION: expected 0 or 1\n' >&2; exit 1 ;;
esac
# Must stay OUTSIDE the directory legacy boots mount as `ccache`
# ($CACHE_ROOT/ccache), so no legacy read-write mount can reach it.
CCACHE_LAYER_ROOT="${CACHE_ROOT:-$HOME/.cache/pulp-ci}/ccache-layers-v1"
CCACHE_LAYER_STATE="${TARTCI_CCACHE_LAYER_STATE_DIR:-${CACHE_ROOT:-$HOME/.cache/pulp-ci}/ccache-layer-state}"
CCACHE_LAYER_GUEST_SHARED="/Volumes/My Shared Files/ccache-shared"
CCACHE_LAYER_PY="${TARTCI_CCACHE_LAYER_PY:-python3}"
CCACHE_LAYER_PROMOTER="${TARTCI_ROOT:-.}/scripts/ccache_layer.py"
# Upper bounds for one green job's pull: bytes received and wall-clock seconds.
CCACHE_LAYER_PULL_MAX="${TARTCI_CCACHE_LAYER_PULL_MAX:-20G}"
CCACHE_LAYER_PULL_SECS="${TARTCI_CCACHE_LAYER_PULL_SECS:-900}"
# Shell fragments spliced into the guest command. Empty = legacy behaviour.
# shellcheck disable=SC2034 # consumed by runner.sh's guest command
CCACHE_LAYER_GUEST_PREP=""
# shellcheck disable=SC2034 # consumed by runner.sh's guest command
CCACHE_LAYER_GUEST_ENV=""
CCACHE_LAYER_VM=""
# The `tart run --dir` arguments for an isolated boot (tartci_ccache_layer_boot).
CCACHE_LAYER_TART_DIRS=()

tartci_ccache_layer_py(){
  "$CCACHE_LAYER_PY" "$CCACHE_LAYER_PROMOTER" \
    --root "$CCACHE_LAYER_ROOT" --state "$CCACHE_LAYER_STATE" "$@"
}

tartci_ccache_layer_guest_fragments_clear(){
  # shellcheck disable=SC2034 # consumed by runner.sh's guest command
  CCACHE_LAYER_GUEST_PREP=""
  # shellcheck disable=SC2034 # consumed by runner.sh's guest command
  CCACHE_LAYER_GUEST_ENV=""
}

# Build the guest fragments for one VM's job. Pure (no filesystem access), so
# the exact text the guest receives is testable without a VM.
tartci_ccache_layer_guest_fragments(){
  local vm="$1"
  if [ "$CCACHE_LAYER_ENABLED" != 1 ]; then
    tartci_ccache_layer_guest_fragments_clear
    return 0
  fi
  # ccache splits remote_storage on spaces, and the Tart share path has them,
  # so the guest reaches the shared store through a space-free symlink. The
  # job directory is guest-local: nothing the guest writes lands on the host.
  # shellcheck disable=SC2016,SC2034 # $HOME expands in the guest; runner.sh consumes it
  CCACHE_LAYER_GUEST_PREP="mkdir -p ~/.tartci-ccache/job/local ~/.tartci-ccache/job/remote && \
ln -sfn '$CCACHE_LAYER_GUEST_SHARED' ~/.tartci-ccache/shared && \
rm -f ~/Library/Caches/ccache && ln -sfn ~/.tartci-ccache/job/local ~/Library/Caches/ccache && \
export CCACHE_DIR=\"\$HOME/.tartci-ccache/job/local\" CCACHE_REMOTE_ONLY=true \
CCACHE_REMOTE_STORAGE=\"file:\$HOME/.tartci-ccache/shared|read-only file:\$HOME/.tartci-ccache/job/remote\" && \
printf 'TARTCI_DIAG ccache-write-isolation=on vm=$vm ccache=%s\n' \"\$(ccache --version 2>/dev/null | head -n1)\" && "
  # shellcheck disable=SC2016,SC2034 # awk fields and $HOME belong to the guest
  CCACHE_LAYER_GUEST_ENV="awk -F= '\$1 !~ /^(CCACHE_DIR|CCACHE_REMOTE_ONLY|CCACHE_REMOTE_STORAGE)\$/' .env.tartci > .env.tartci.layer && \
mv .env.tartci.layer .env.tartci && \
printf 'CCACHE_DIR=%s\nCCACHE_REMOTE_ONLY=true\nCCACHE_REMOTE_STORAGE=file:%s|read-only file:%s\n' \
\"\$HOME/.tartci-ccache/job/local\" \"\$HOME/.tartci-ccache/shared\" \"\$HOME/.tartci-ccache/job/remote\" >> .env.tartci && "
}

# Host side of the boot: check the layers root (no symlinks, real
# directories) and fill CCACHE_LAYER_TART_DIRS with the one read-only mount.
# Returns 1 when the root is unusable; the caller then boots without any shared
# cache (a cold, private guest cache) rather than falling back to the legacy
# writable mount. Off: a no-op that leaves CCACHE_LAYER_TART_DIRS empty.
tartci_ccache_layer_boot(){
  local vm="$1"
  CCACHE_LAYER_TART_DIRS=()
  [ "$CCACHE_LAYER_ENABLED" = 1 ] || return 0
  if ! tartci_ccache_layer_py prepare >/dev/null; then
    event ccache_layer_attach_failed "vm=$vm root=$CCACHE_LAYER_ROOT"
    return 1
  fi
  # shellcheck disable=SC2034 # consumed by runner.sh's tart run
  CCACHE_LAYER_TART_DIRS=(--dir="ccache-shared:$CCACHE_LAYER_ROOT/shared:ro")
  event ccache_layer_attach "vm=$vm"
  # Off the boot's critical path: resumes interrupted promotions and deletes
  # orphaned layers. Never promotes a layer that has no green verdict.
  tartci_ccache_layer_py sweep --max-size "$CCACHE_MAX_SIZE" \
    >>"$CCACHE_LAYER_STATE/sweep.log" 2>&1 </dev/null &
  return 0
}

# Guest side of the job start: build the guest fragments. Off: a no-op.
tartci_ccache_layer_attach(){
  local vm="$1"
  CCACHE_LAYER_VM=""
  tartci_ccache_layer_guest_fragments "$vm"
  [ "$CCACHE_LAYER_ENABLED" = 1 ] || return 0
  CCACHE_LAYER_VM="$vm"
  return 0
}

# A VM torn down before its job settled. The guest's entries live only in the
# guest, so they go with it; a pull interrupted mid-stream left a host layer
# under jobs/, which is settled red (deleted, never promoted). Off: a no-op.
tartci_ccache_layer_abandon(){
  local vm="$1" settled layer
  [ "$CCACHE_LAYER_ENABLED" = 1 ] && [ -n "$vm" ] || return 0
  [ "$CCACHE_LAYER_VM" != "$vm" ] || CCACHE_LAYER_VM=""
  [ -d "$CCACHE_LAYER_ROOT/jobs/$vm" ] || return 0
  settled="$(tartci_ccache_layer_py settle --vm "$vm" --runner-rc 1 \
    --quarantine vm_discarded 2>/dev/null)" || return 0
  layer="$(printf '%s' "$settled" | cut -f3)"
  event ccache_layer_abandon "vm=$vm"
  [ -n "$layer" ] || return 0
  tartci_ccache_layer_py process --layer "$layer" --max-size "$CCACHE_MAX_SIZE" \
    >>"$CCACHE_LAYER_STATE/process.log" 2>&1 </dev/null &
  return 0
}

# The ssh that tars the guest's job directory to stdout. Overridable for
# tests (TARTCI_CCACHE_LAYER_PULL_CMD is a command that writes the tar).
tartci_ccache_layer_pull_argv(){
  local ip="$1"
  if [ -n "${TARTCI_CCACHE_LAYER_PULL_CMD:-}" ]; then
    printf '%s\n' /bin/sh -c "$TARTCI_CCACHE_LAYER_PULL_CMD"
    return 0
  fi
  # shellcheck disable=SC2016 # expands in the guest
  printf '%s\n' ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -i "$SSH_KEY_PRIV" "$VM_USER@$ip" \
    'cd "$HOME/.tartci-ccache/job" && CCACHE_DIR="$HOME/.tartci-ccache/job/local" ccache --print-stats >stats.txt 2>/dev/null; tar -cf - stats.txt remote'
}

# Host side of the job end: decide the verdict, pull a green guest's entries
# into a host layer, then settle (a rename) and promote in the background once
# the VM is gone. A failed or oversized pull settles red. Off: a no-op.
tartci_ccache_layer_settle(){
  local vm="$1" runner_rc="$2" runner_log="${3:-}" ip="${4:-}" settled layer decision
  local quarantine="${CURRENT_ASSIGNMENT_QUARANTINE:-}" pull=() line
  [ "$CCACHE_LAYER_ENABLED" = 1 ] && [ "$CCACHE_LAYER_VM" = "$vm" ] || return 0
  CCACHE_LAYER_VM=""
  decision="$(tartci_ccache_layer_py verdict --runner-rc "$runner_rc" \
      --capture-status "${CURRENT_JOB_CAPTURE_STATUS:-}" \
      --receipt "${CURRENT_JOB_RECEIPT:-}" \
      --quarantine "$quarantine" \
      --runner-log "$runner_log" 2>/dev/null | cut -f1)" || decision=""
  if [ "$decision" = green ]; then
    while IFS= read -r line; do pull+=("$line"); done < <(tartci_ccache_layer_pull_argv "$ip")
    if [ -z "$ip" ] && [ -z "${TARTCI_CCACHE_LAYER_PULL_CMD:-}" ]; then
      quarantine=ccache_pull_failed
      event ccache_layer_pull_failed "vm=$vm reason=no_ip"
    elif ! tartci_ccache_layer_py receive --vm "$vm" --owner-pid "$$" \
        --max-bytes "$CCACHE_LAYER_PULL_MAX" --timeout "$CCACHE_LAYER_PULL_SECS" \
        -- ${pull[@]+"${pull[@]}"} >>"$CCACHE_LAYER_STATE/receive.log" 2>&1 </dev/null; then
      quarantine=ccache_pull_failed
      event ccache_layer_pull_failed "vm=$vm"
    fi
  fi
  if ! settled="$(tartci_ccache_layer_py settle --vm "$vm" --runner-rc "$runner_rc" \
      --capture-status "${CURRENT_JOB_CAPTURE_STATUS:-}" \
      --receipt "${CURRENT_JOB_RECEIPT:-}" \
      --quarantine "$quarantine" \
      --runner-log "$runner_log" \
      --run-id "${CURRENT_RUN_ID:-}" --job-id "${CURRENT_JOB_ID:-}")"; then
    # A received layer stays under jobs/ with a live owner; the next sweep after
    # this supervisor exits discards it. It is never promoted without a verdict.
    event ccache_layer_settle_failed "vm=$vm runner_rc=$runner_rc"
    return 0
  fi
  layer="$(printf '%s' "$settled" | cut -f3)"
  event ccache_layer_settle "vm=$vm verdict=$(printf '%s' "$settled" | cut -f1) reason=$(printf '%s' "$settled" | cut -f2)"
  [ -n "$layer" ] || return 0
  tartci_ccache_layer_py process --layer "$layer" --max-size "$CCACHE_MAX_SIZE" \
    >>"$CCACHE_LAYER_STATE/process.log" 2>&1 </dev/null &
  return 0
}
