#!/usr/bin/env bash
# Per-job ccache write isolation for disposable macOS guests (opt-in).
#
# Every guest mounts the host ccache directory read-write, so without this a
# job torn down mid-build (a Tart hang, a timeout, a cancel) can leave half-
# written entries that every later job trusts. With
# TARTCI_CCACHE_WRITE_ISOLATION=1 each job instead:
#
#   * reads the host's shared store through ccache remote storage marked
#     `read-only`, so the guest's ccache never writes it;
#   * writes new entries only to its own job layer
#     (`remote_only`: the guest keeps no local copy of what it read);
#   * and the host promotes that layer into the shared store only after the
#     job's verdict is green AND the VM is provably gone
#     (scripts/ccache_layer.py). Red, cancelled, timed-out and orphaned layers
#     are deleted without ever touching the shared store.
#
# The guest never sees the directory that holds the shared store. The layer is
# attached on the host before the VM boots, and the boot mounts exactly two
# ccache directories instead of the legacy read-write `ccache` mount:
#
#   ccache-shared  <layers>/shared      read-only (enforced by the mount, not
#                                       only by ccache's `read-only` flag)
#   ccache-job     <layers>/jobs/<vm>   read-write, this VM's own layer
#
# so a hostile guest can neither write the shared store nor reach another
# job's layer. The layers root is $CACHE_ROOT/ccache-layers-v1, a SIBLING of
# the legacy $CACHE_ROOT/ccache directory, not inside it: a lane that still
# mounts the legacy directory read-write (a lane not yet switched, an operator
# run of run.sh) therefore cannot reach the shared store either. The store
# uses ccache's `file:` remote-storage layout (`<k0k1>/<rest>`), so it starts
# cold on each host; the first green job per host warms it.
#
# A promoted layer is still the guest's own claim about which object a key
# names: write isolation keeps red, cancelled, timed-out and torn jobs out of
# the shared store and stops any guest overwriting an existing key, but a GREEN
# job's new keys are trusted. Fork pull requests never reach these lanes.
#
# A VM discarded before its job settles (a boot failure, a parked VM that
# expired, a supervisor signal) has its layer settled red and deleted.
#
# Unset (the default) changes nothing: the boot mounts the legacy `ccache`
# directory, every variable the guest command uses below is empty, and the
# attach/settle hooks return immediately.

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
CCACHE_LAYER_GUEST_JOB="/Volumes/My Shared Files/ccache-job"
CCACHE_LAYER_PY="${TARTCI_CCACHE_LAYER_PY:-python3}"
CCACHE_LAYER_PROMOTER="${TARTCI_ROOT:-.}/scripts/ccache_layer.py"
# Shell fragments spliced into the guest command. Empty = legacy behaviour.
# shellcheck disable=SC2034 # consumed by runner.sh's guest command
CCACHE_LAYER_GUEST_PREP=""
# shellcheck disable=SC2034 # consumed by runner.sh's guest command
CCACHE_LAYER_GUEST_ENV=""
CCACHE_LAYER_VM=""
# The `tart run --dir` arguments for an attached VM (tartci_ccache_layer_boot).
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
  # so the guest reaches both stores through space-free symlinks.
  # shellcheck disable=SC2016,SC2034 # $HOME expands in the guest; runner.sh consumes it
  CCACHE_LAYER_GUEST_PREP="mkdir -p ~/.tartci-ccache && \
ln -sfn '$CCACHE_LAYER_GUEST_SHARED' ~/.tartci-ccache/shared && \
ln -sfn '$CCACHE_LAYER_GUEST_JOB' ~/.tartci-ccache/job && \
ln -sfn ~/.tartci-ccache/job/local ~/Library/Caches/ccache && \
export CCACHE_DIR=\"\$HOME/.tartci-ccache/job/local\" CCACHE_REMOTE_ONLY=true \
CCACHE_REMOTE_STORAGE=\"file:\$HOME/.tartci-ccache/shared|read-only file:\$HOME/.tartci-ccache/job/remote\" && \
printf 'TARTCI_DIAG ccache-write-isolation=on vm=$vm ccache=%s\n' \"\$(ccache --version 2>/dev/null | head -n1)\" && "
  # shellcheck disable=SC2016,SC2034 # awk fields and $HOME belong to the guest
  CCACHE_LAYER_GUEST_ENV="awk -F= '\$1 !~ /^(CCACHE_DIR|CCACHE_REMOTE_ONLY|CCACHE_REMOTE_STORAGE)\$/' .env.tartci > .env.tartci.layer && \
mv .env.tartci.layer .env.tartci && \
printf 'CCACHE_DIR=%s\nCCACHE_REMOTE_ONLY=true\nCCACHE_REMOTE_STORAGE=file:%s|read-only file:%s\n' \
\"\$HOME/.tartci-ccache/job/local\" \"\$HOME/.tartci-ccache/shared\" \"\$HOME/.tartci-ccache/job/remote\" >> .env.tartci && "
}

# Host side of the boot: create this VM's empty layer, record its owner, and
# fill CCACHE_LAYER_TART_DIRS with the two mounts. Returns 1 when the layer
# could not be attached; the caller then boots without any shared cache (a
# cold, private guest cache) rather than falling back to the legacy writable
# mount. Off: a no-op that leaves CCACHE_LAYER_TART_DIRS empty.
tartci_ccache_layer_boot(){
  local vm="$1"
  CCACHE_LAYER_TART_DIRS=()
  [ "$CCACHE_LAYER_ENABLED" = 1 ] || return 0
  if ! tartci_ccache_layer_py attach --vm "$vm" --owner-pid "$$" >/dev/null; then
    event ccache_layer_attach_failed "vm=$vm root=$CCACHE_LAYER_ROOT"
    return 1
  fi
  # shellcheck disable=SC2034 # consumed by runner.sh's tart run
  CCACHE_LAYER_TART_DIRS=(--dir="ccache-shared:$CCACHE_LAYER_ROOT/shared:ro"
    --dir="ccache-job:$CCACHE_LAYER_ROOT/jobs/$vm")
  event ccache_layer_attach "vm=$vm"
  # Off the boot's critical path: resumes interrupted promotions and deletes
  # orphaned layers. Never promotes a layer that has no green verdict.
  tartci_ccache_layer_py sweep --max-size "$CCACHE_MAX_SIZE" \
    >>"$CCACHE_LAYER_STATE/sweep.log" 2>&1 </dev/null &
  return 0
}

# Guest side of the job start: build the guest fragments for a VM whose layer
# was attached at boot. A VM booted with isolation on but no layer (removed
# under it) must not run the job: its guest has no ccache-job mount to write.
tartci_ccache_layer_attach(){
  local vm="$1"
  CCACHE_LAYER_VM=""
  tartci_ccache_layer_guest_fragments "$vm"
  [ "$CCACHE_LAYER_ENABLED" = 1 ] || return 0
  if [ -L "$CCACHE_LAYER_ROOT/jobs/$vm" ] || [ ! -d "$CCACHE_LAYER_ROOT/jobs/$vm" ]; then
    tartci_ccache_layer_guest_fragments_clear
    event ccache_layer_attach_failed "vm=$vm root=$CCACHE_LAYER_ROOT reason=no_boot_layer"
    return 1
  fi
  CCACHE_LAYER_VM="$vm"
  return 0
}

# A VM torn down before its job settled: settle its layer red (deleted, never
# promoted). Called once the guest is no longer running. Off: a no-op.
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

# Host side of the job end: decide the verdict now (a rename), promote or
# delete later in the background once the VM is gone. Off: a no-op.
tartci_ccache_layer_settle(){
  local vm="$1" runner_rc="$2" runner_log="${3:-}" settled layer
  [ "$CCACHE_LAYER_ENABLED" = 1 ] && [ "$CCACHE_LAYER_VM" = "$vm" ] || return 0
  CCACHE_LAYER_VM=""
  if ! settled="$(tartci_ccache_layer_py settle --vm "$vm" --runner-rc "$runner_rc" \
      --capture-status "${CURRENT_JOB_CAPTURE_STATUS:-}" \
      --receipt "${CURRENT_JOB_RECEIPT:-}" \
      --quarantine "${CURRENT_ASSIGNMENT_QUARANTINE:-}" \
      --runner-log "$runner_log" \
      --run-id "${CURRENT_RUN_ID:-}" --job-id "${CURRENT_JOB_ID:-}")"; then
    # The layer stays under jobs/ with a live owner; the next sweep after this
    # supervisor exits discards it. It can never be promoted without a verdict.
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
