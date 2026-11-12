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
# The layer attaches when the job starts in the guest (after the JIT mint), not
# at VM boot, so a VM booted ahead of any job is covered the same way. The
# boot-time mount itself is unchanged. Both the shared store and the layers
# therefore live INSIDE the mounted host ccache directory, under
# tartci-layers-v1/. They use ccache's `file:` remote-storage layout
# (`<k0k1>/<rest>`), which differs from a primary cache (`<k0>/<k1>/<rest>`),
# so the legacy primary cache beside them is neither read nor cleaned by them,
# and ccache's own cleanup of that primary cache never walks into them.
#
# Unset (the default) changes nothing: every variable the guest command uses
# below is empty, and the attach/settle hooks return immediately.

CCACHE_LAYER_ENABLED=0
case "${TARTCI_CCACHE_WRITE_ISOLATION:-}" in
  ''|0) ;;
  1) CCACHE_LAYER_ENABLED=1 ;;
  *) printf 'invalid TARTCI_CCACHE_WRITE_ISOLATION: expected 0 or 1\n' >&2; exit 1 ;;
esac
# Must stay inside the directory Tart mounts as `ccache` ($CACHE_ROOT/ccache):
# the guest reaches it at $CCACHE_LAYER_GUEST_ROOT.
CCACHE_LAYER_ROOT="${CACHE_ROOT:-$HOME/.cache/pulp-ci}/ccache/tartci-layers-v1"
CCACHE_LAYER_STATE="${TARTCI_CCACHE_LAYER_STATE_DIR:-${CACHE_ROOT:-$HOME/.cache/pulp-ci}/ccache-layer-state}"
CCACHE_LAYER_GUEST_ROOT="/Volumes/My Shared Files/ccache/tartci-layers-v1"
CCACHE_LAYER_PY="${TARTCI_CCACHE_LAYER_PY:-python3}"
CCACHE_LAYER_PROMOTER="${TARTCI_ROOT:-.}/scripts/ccache_layer.py"
# Shell fragments spliced into the guest command. Empty = legacy behaviour.
# shellcheck disable=SC2034 # consumed by runner.sh's guest command
CCACHE_LAYER_GUEST_PREP=""
# shellcheck disable=SC2034 # consumed by runner.sh's guest command
CCACHE_LAYER_GUEST_ENV=""
CCACHE_LAYER_VM=""

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
ln -sfn '$CCACHE_LAYER_GUEST_ROOT/shared' ~/.tartci-ccache/shared && \
ln -sfn '$CCACHE_LAYER_GUEST_ROOT/jobs/$vm' ~/.tartci-ccache/job && \
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

# Host side of the job start: create the empty layer, record its owner, and
# reclaim anything an earlier supervisor left. Off: a no-op.
tartci_ccache_layer_attach(){
  local vm="$1"
  CCACHE_LAYER_VM=""
  tartci_ccache_layer_guest_fragments "$vm"
  [ "$CCACHE_LAYER_ENABLED" = 1 ] || return 0
  if ! tartci_ccache_layer_py attach --vm "$vm" --owner-pid "$$" >/dev/null; then
    tartci_ccache_layer_guest_fragments_clear
    event ccache_layer_attach_failed "vm=$vm root=$CCACHE_LAYER_ROOT"
    return 1
  fi
  CCACHE_LAYER_VM="$vm"
  event ccache_layer_attach "vm=$vm"
  # Off the job's critical path: resumes interrupted promotions and deletes
  # orphaned layers. Never promotes a layer that has no green verdict.
  tartci_ccache_layer_py sweep --max-size "$CCACHE_MAX_SIZE" \
    >>"$CCACHE_LAYER_STATE/sweep.log" 2>&1 </dev/null &
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
