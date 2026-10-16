# Per-job boot claim for the macOS JIT supervisor (scripts/job_claim.py).
# shellcheck shell=bash
# shellcheck disable=SC2034 # JOB_CLAIM_* state is read by runner.sh
#
# Before a lane clones, it claims one queued job of its selected class. A lane
# whose claim is refused because other lanes (on this host, or already minted
# anywhere in the fleet) cover every queued job of that class does not boot;
# it returns to the loop and looks again next poll. A claim is released when
# the runner is assigned its job, when run_one returns for any reason, and in
# cleanup. A crashed supervisor's claim dies with it (pid + start-time
# identity) or at its TTL.
#
# Fail-open by design: if the claim store, the runner listing or an exact
# count is unavailable, the lane boots exactly as it did before claims
# existed. A claim can only ever remove a boot that another lane already
# covers; it can never be the reason a queued job goes unserved.
#
# TARTCI_JOB_CLAIM=0 disables claims; TARTCI_JOB_CLAIM_FLEET=0 keeps them
# host-local (no runner listing); TARTCI_JOB_CLAIM_TTL_SECS bounds a claim.
# TARTCI_JOB_CLAIM_FLEET_PEERS=1 (profile `assignment_fleet_claim_peers`) also
# reads every other published host's live claims over SSH before acquiring,
# within TARTCI_JOB_CLAIM_FLEET_READ_SECS (default 5); an unread peer counts
# nothing, so the lane boots as without peers.

JOB_CLAIM_ID=""
JOB_CLAIM_LABELS=""
# 1 when the last run_one declined to boot because its demand was covered.
# The loop treats that like an idle pass, not a blocked one.
JOB_CLAIM_CONTENDED=0

tartci_job_claim_enabled(){
  [ "${TARTCI_JOB_CLAIM:-1}" = 1 ]
}

tartci_job_claim_validate(){
  case "${TARTCI_JOB_CLAIM:-1}" in 0|1) ;; *)
    printf 'invalid TARTCI_JOB_CLAIM: expected 0 or 1\n' >&2; return 2 ;;
  esac
  case "${TARTCI_JOB_CLAIM_FLEET:-1}" in 0|1) ;; *)
    printf 'invalid TARTCI_JOB_CLAIM_FLEET: expected 0 or 1\n' >&2; return 2 ;;
  esac
  case "${TARTCI_JOB_CLAIM_FLEET_PEERS:-0}" in 0|1) ;; *)
    printf 'invalid TARTCI_JOB_CLAIM_FLEET_PEERS: expected 0 or 1\n' >&2; return 2 ;;
  esac
  case "${TARTCI_JOB_CLAIM_TTL_SECS:-1800}" in
    ''|*[!0-9]*|0) printf 'invalid TARTCI_JOB_CLAIM_TTL_SECS: expected a positive integer\n' >&2; return 2 ;;
  esac
}

# Online, idle runners at this API root as JSON lines {name, labels}. Empty on
# any failure: an unreadable listing counts no fleet claims (fail open).
tartci_job_claim_fleet_runners(){
  local runner_api_root="$1" out_file="$2"
  : >"$out_file"
  [ "${TARTCI_JOB_CLAIM_FLEET:-1}" = 1 ] || return 0
  "$GH_CLI" api "$runner_api_root" --paginate \
    --jq '.runners[] | select(.status == "online" and .busy == false) | {name: .name, labels: [.labels[].name]} | @json' \
    >"$out_file" 2>/dev/null || : >"$out_file"
}

# Other hosts' published claims, one JSON line per peer, into $1. Nothing at all
# unless the lane opted in; any failure leaves the file empty or marks the peer
# unread, and both count no claims (fail open).
tartci_job_claim_fleet_peer_claims(){
  local out_file="$1"
  : >"$out_file"
  [ "${TARTCI_JOB_CLAIM_FLEET_PEERS:-0}" = 1 ] || return 0
  [ -n "${TARTCI_RECEIPT_HOST_ID:-}" ] || return 0
  # Without TARTCI_JOB_CLAIM_SUPPLY, job_claim.py reads the selected fleet
  # source's supply (fleet_source.selected_published_supply).
  python3 "$TARTCI_ROOT/scripts/job_claim.py" gather-peers --out "$out_file" \
    --self-host "$TARTCI_RECEIPT_HOST_ID" \
    ${TARTCI_JOB_CLAIM_SUPPLY:+--supply "$TARTCI_JOB_CLAIM_SUPPLY"} \
    --read-secs "${TARTCI_JOB_CLAIM_FLEET_READ_SECS:-5}" \
    --ssh "${TARTCI_JOB_CLAIM_SSH:-ssh}" </dev/null >/dev/null 2>&1 || : >"$out_file"
}
# One job_claim_peer_unread event per peer per hour; the per-attempt count is
# carried on every job_claim / job_claim_contended event (peers_unread=N).
_tartci_job_claim_note_unread(){
  local peers_file="$1" stamp_dir host now last
  stamp_dir="${TARTCI_JOB_CLAIM_DIR:-$HOME/.tartci/state/job-claims}/peer-unread"
  mkdir -p "$stamp_dir" 2>/dev/null || return 0
  now="$(date +%s)"
  while IFS=$'\t' read -r host reason; do
    [ -n "$host" ] || continue
    last="$(cat "$stamp_dir/$host" 2>/dev/null || echo 0)"
    case "$last" in ''|*[!0-9]*) last=0 ;; esac
    [ $((now - last)) -ge 3600 ] || continue
    printf '%s\n' "$now" >"$stamp_dir/$host" 2>/dev/null || true
    event job_claim_peer_unread "peer=$host reason=$reason"
  done < <(python3 -c '
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        row = json.loads(line)
    except ValueError:
        continue
    if isinstance(row, dict) and row.get("ok") is not True and isinstance(row.get("host"), str):
        print("%s\t%s" % (row["host"], row.get("reason") or "unknown"))
' "$peers_file" 2>/dev/null)
}
# Exhaustive queued count for the selected class, or nothing. Only an
# event-class-v2 lane reports a lower bound, so only it ever needs this.
tartci_job_claim_exact_count(){
  local selected_tier="$1" tier_label tier=0 q
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    if [ "$tier" -eq "$selected_tier" ]; then
      q="$(tartci_assignment_v2_tier_demand "$tier_label" 1)" || return 1
      printf '%s' "$q" | grep -qxE '[0-9]+' || return 1
      printf '%s\n' "$q"
      return 0
    fi
    tier=$((tier + 1))
  done <<< "$TIER_LABELS_CONFIG"
  return 1
}

# The result lands in the caller's variable named by $1. This function's own
# locals must not share a name a caller passes: bash scoping is dynamic, so
# `local out` here would shadow a caller's `out` and printf -v would write the
# local copy, leaving the caller's empty (every job_claim event once read
# detail=unreadable for exactly that reason).
_tartci_job_claim_call(){
  local _jc_out_var="$1"; shift
  local _jc_out _jc_rc=0
  _jc_out="$(python3 "$TARTCI_ROOT/scripts/job_claim.py" "$@" 2>/dev/null)" || _jc_rc=$?
  printf -v "$_jc_out_var" '%s' "$_jc_out"
  return "$_jc_rc"
}

# Decide whether this lane may boot for its selected class.
# Returns 0 to boot (claimed, or fail-open) and 75 when every queued job of
# the class is already covered by another lane.
tartci_job_claim_acquire(){
  local vm="$1" selected_labels="$2" selected_tier="$3" queued="$4" runner_api_root="$5"
  local runners peers out="" rc=0 lower=() exact detail
  JOB_CLAIM_CONTENDED=0
  JOB_CLAIM_ID=""
  tartci_job_claim_enabled || return 0
  case "$queued" in ''|*[!0-9]*) return 0 ;; esac
  [ "$queued" -gt 0 ] || return 0
  [ "$ASSIGNMENT_MODE" != event-class-v2 ] || lower=(--lower-bound)
  runners="$(mktemp "${TMPDIR:-/tmp}/tartci-job-claim.XXXXXX")" || return 0
  tartci_job_claim_fleet_runners "$runner_api_root" "$runners"
  peers="$(mktemp "${TMPDIR:-/tmp}/tartci-job-claim-peers.XXXXXX")" || peers=""
  if [ -n "$peers" ]; then
    tartci_job_claim_fleet_peer_claims "$peers"
    _tartci_job_claim_note_unread "$peers"
  fi
  local claim_id="$RUNNER_NAME-$SLOT-$vm"
  local base_args=(acquire --repo "$REPO" --labels "$selected_labels"
    --claim-id "$claim_id" --lane "$RUNNER_NAME" --vm "$vm" --pid "$$"
    --fleet-runners-file "$runners" --ttl "${TARTCI_JOB_CLAIM_TTL_SECS:-1800}")
  [ -z "$peers" ] || base_args+=(--fleet-claims-file "$peers")
  _tartci_job_claim_call out "${base_args[@]}" --queued "$queued" ${lower[@]+"${lower[@]}"} || rc=$?
  if [ "$rc" -eq 4 ]; then
    # A sibling holds a claim and our count was only "at least one". Buy the
    # real magnitude now, and only now.
    if exact="$(tartci_job_claim_exact_count "$selected_tier")"; then
      rc=0
      _tartci_job_claim_call out "${base_args[@]}" --queued "$exact" || rc=$?
    else
      rm -f "$runners" ${peers:+"$peers"}
      event job_claim_unavailable "reason=exact_count_unavailable labels=$selected_labels"
      return 0
    fi
  fi
  rm -f "$runners" ${peers:+"$peers"}
  detail="$(printf '%s' "$out" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    print("detail=unreadable"); raise SystemExit
print("queued=%s standing=%s local=%d fleet_idle=%d fleet_booting=%d peers_unread=%d" % (
    d.get("queued"), d.get("standing_claims"),
    len(d.get("local_claims") or []), len(d.get("fleet_idle_runners") or []),
    len(d.get("fleet_booting") or []), len(d.get("peers_unread") or [])))
' 2>/dev/null)" || detail="detail=unreadable"
  # The same numbers as typed event fields (word-split on purpose: every
  # token is key=value with no spaces).
  local fields=()
  read -r -a fields <<< "$detail"
  case "$rc" in
    0)
      JOB_CLAIM_ID="$claim_id"
      JOB_CLAIM_LABELS="$selected_labels"
      event job_claim "labels=$selected_labels $detail" ${fields[@]+"${fields[@]}"}
      return 0
      ;;
    3)
      JOB_CLAIM_CONTENDED=1
      event job_claim_contended "labels=$selected_labels $detail" ${fields[@]+"${fields[@]}"}
      note "every queued job of class $selected_labels is already covered ($detail) — not booting"
      return 75
      ;;
    *)
      event job_claim_unavailable "rc=$rc labels=$selected_labels"
      return 0
      ;;
  esac
}

tartci_job_claim_release(){
  [ -n "$JOB_CLAIM_ID" ] || return 0
  python3 "$TARTCI_ROOT/scripts/job_claim.py" release --repo "$REPO" \
    --labels "$JOB_CLAIM_LABELS" --claim-id "$JOB_CLAIM_ID" >/dev/null 2>&1 || true
  JOB_CLAIM_ID=""
  JOB_CLAIM_LABELS=""
}
