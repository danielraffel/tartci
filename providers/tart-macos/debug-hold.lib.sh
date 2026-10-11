#!/usr/bin/env bash
# Keep a failed gate VM for debugging without keeping its slot
# (scripts/debug_hold.py has the whole design; off unless [debug_hold]
# enabled = true in the fleet profile).
#
# tartci_debug_hold_current_vm runs after a served job, in place of the
# teardown's delete. It returns 0 only when the VM is stopped, renamed
# held-<vm>, recorded, and proved unable to take a job; CURRENT_VM is then
# empty so the lane releases its lease and slot as after a delete. Any other
# outcome returns 1 with CURRENT_VM unchanged, and the caller deletes the VM
# as usual: a VM that might still take a job is never kept.

# The actions runner's own closing line for the job, read from its log.
tartci_debug_hold_job_failed(){
  local log="$STATE_DIR/$CURRENT_VM.actions-runner.log" result
  result="$(runner_log_completion_result "$log")" || return 1
  [ "$result" = Failed ]
}

# Prove GitHub lists no runner named $1: delete it when listed, then read the
# list again. A read that fails proves nothing and refuses.
tartci_debug_hold_deregister(){
  local name="$1" api_root="${CURRENT_RUNNER_API_ROOT:-$RUNNER_API_ROOT}" ids id
  ids="$("$GH_CLI" api "$api_root" --paginate \
        --jq ".runners[] | select(.name==\"$name\") | .id" 2>/dev/null)" || return 1
  for id in $ids; do
    "$GH_CLI" api -X DELETE "$api_root/$id" >/dev/null 2>&1 || return 1
  done
  ids="$("$GH_CLI" api "$api_root" --paginate \
        --jq ".runners[] | select(.name==\"$name\") | .id" 2>/dev/null)" || return 1
  [ -z "$ids" ]
}

# Remove the guest's runner service and its JIT config, and prove both gone.
tartci_debug_hold_scrub_guest(){
  [ -n "$CURRENT_IP" ] && [ -n "$CURRENT_AQUA_LABEL" ] || return 1
  stop_current_aqua_runner
  bounded_teardown_command debug-hold-scrub \
    ssh -n "${SSH_OPTS[@]}" -i "$SSH_KEY_PRIV" "$VM_USER@$CURRENT_IP" \
    "root=\"\$HOME/.tartci/aqua-runner/$CURRENT_AQUA_LABEL\"; rm -rf \"\$root\" \
       \"\$HOME/actions-runner/.runner\" \"\$HOME/actions-runner/.credentials\" \
       \"\$HOME/actions-runner/.credentials_rsaparams\" && \
     [ ! -e \"\$root\" ] && [ ! -e \"\$HOME/actions-runner/.runner\" ] && \
     ! pgrep -f Runner.Listener >/dev/null" >/dev/null 2>&1
}

tartci_debug_hold_current_vm(){
  local vm="$CURRENT_VM" verdict held
  [ -n "$vm" ] || return 1
  tartci_debug_hold_job_failed || return 1
  verdict="$(python3 "$TARTCI_ROOT/scripts/debug_hold.py" admit --vm "$vm" \
    --tart-home "${TART_HOME:-$HOME/.tart}" 2>/dev/null)" || {
    case "$verdict" in
      "refused disabled"|"") ;;
      *) event debug_hold_refused "vm=$vm reason=${verdict#refused }" ;;
    esac
    return 1
  }
  held="${verdict#hold }"
  if ! tartci_debug_hold_scrub_guest; then
    event debug_hold_refused "vm=$vm reason=guest_scrub_unproved"
    return 1
  fi
  if ! tartci_debug_hold_deregister "$vm"; then
    event debug_hold_refused "vm=$vm reason=deregistration_unproved"
    return 1
  fi
  if ! terminate_current_guardian; then
    event debug_hold_refused "vm=$vm reason=guardian_live"
    return 1
  fi
  # shellcheck disable=SC2034 # read by runner.sh cleanup
  CURRENT_RPID=""
  bounded_teardown_command tart-stop tart stop "$vm" >/dev/null 2>&1 || true
  if ! bounded_teardown_command tart-rename tart rename "$vm" "$held" >/dev/null 2>&1; then
    event debug_hold_refused "vm=$vm reason=rename_failed"
    return 1
  fi
  python3 "$TARTCI_ROOT/scripts/debug_hold.py" record --name "$held" --vm "$vm" \
    --lane "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}" --repo "$REPO" \
    --run-id "${CURRENT_RUN_ID:-}" --job-id "${CURRENT_JOB_ID:-}" >/dev/null 2>&1 || true
  event debug_hold_kept "vm=$vm held=$held run_id=${CURRENT_RUN_ID:-} job_id=${CURRENT_JOB_ID:-}"
  note "kept failed VM as $held (stopped; tartci held inspect $held)"
  CURRENT_VM=""
  CURRENT_IP=""
  CURRENT_AQUA_LABEL=""
  return 0
}
