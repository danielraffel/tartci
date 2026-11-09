# Conditional claim delay for the macOS JIT supervisor.
# shellcheck shell=bash
#
# A claim delay makes a slower host leave queued work to a faster one. Sleeping
# unconditionally idles the slower host's free slot even when no faster host
# can take the job, which is exactly the idle-while-queued time the delay must
# not create. So the delay is spent only when the fleet's published supply
# shows it can pay off:
#
#   * TARTCI_CLAIM_DELAY_PEERS names the faster hosts (host_id=ssh-target,
#     comma separated), read with scripts/gate_supply.py, the same report the
#     fallback lane uses. No peers configured means no evidence, so no sleep.
#   * The decision must be `hold`: every peer report is fresh, readable and
#     `ok`, which also means each counted lane has a fresh heartbeat in a free
#     phase (a lane that is SCAN BLIND, rate limited or storage-yielded is
#     never counted free).
#   * The peers' FREE lanes alone (not their in-flight ones) cover every
#     queued job of the class. Queued jobs beyond that many free faster slots
#     are claimed now, without a delay.
#
# Anything else, including any failure to read demand or supply, claims at
# once: the delay can only ever postpone a claim that a faster host can
# demonstrably take, never cause one to be missed.
#
# The delay is re-read on every decision from TARTCI_CLAIM_DELAY_FILE
# (default ~/.config/tartci/claim-delay-seconds) when that file holds an
# integer 0-90, so an operator can change or disable it without a pool drain
# or a lane reload. Without a valid file the startup value
# TARTCI_CLAIM_DELAY_SECONDS stands. Either is capped at 90 s: the lane writes
# no heartbeat while it sleeps, and gate_supply, doctor and self-update read a
# heartbeat older than 120 s as a dead supervisor.
#
# The file can only tune or disable a lane that already has
# TARTCI_CLAIM_DELAY_PEERS; without peers the delay never sleeps. Only tier and
# event-class-v2 lanes have a class to ask about; a single-label lane never
# sleeps.

CLAIM_DELAY_MAX_SECONDS=90

tartci_claim_delay_seconds(){
  local file value=""
  file="${TARTCI_CLAIM_DELAY_FILE:-$HOME/.config/tartci/claim-delay-seconds}"
  if [ -r "$file" ]; then
    IFS= read -r value < "$file" 2>/dev/null || true
    value="$(printf '%s' "$value" | tr -d '[:space:]')"
    case "$value" in
      ''|*[!0-9]*) value="" ;;
      *)
        # Leading zeros would read as octal in $(( )); strip them first.
        value="$(printf '%s' "$value" | sed 's/^0*//')"
        [ -n "$value" ] || value=0
        [ "${#value}" -le 3 ] && [ "$value" -le "$CLAIM_DELAY_MAX_SECONDS" ] || value=""
        ;;
    esac
  fi
  value="${value:-${CLAIM_DELAY:-0}}"
  [ "$value" -le "$CLAIM_DELAY_MAX_SECONDS" ] || value="$CLAIM_DELAY_MAX_SECONDS"
  printf '%s\n' "$value"
}

# One gate_supply decision line for <class> at <demand>, or empty.
tartci_claim_delay_supply(){
  python3 "$TARTCI_ROOT/scripts/gate_supply.py" decide \
    --repo "$REPO" --class "$1" --demand "$2" \
    --peers "$TARTCI_CLAIM_DELAY_PEERS" --slot "$SLOT" --state-dir "$STATE_DIR" \
    --max-age-seconds "${TARTCI_CLAIM_DELAY_PEER_MAX_AGE_SECS:-60}" \
    --ssh "${TARTCI_CLAIM_DELAY_SSH:-${TARTCI_FALLBACK_SSH:-ssh}}" \
    </dev/null 2>/dev/null || true
}

# The class label configured at zero-based tier index <tier>, or nothing.
tartci_claim_delay_class(){
  local wanted="$1" tier_label tier=0
  while IFS= read -r tier_label; do
    [ -n "$tier_label" ] || continue
    if [ "$tier" = "$wanted" ]; then
      printf '%s\n' "$tier_label"
      return 0
    fi
    tier=$((tier + 1))
  done <<< "$TIER_LABELS_CONFIG"
  return 1
}

# Sum of `free=N` over the peer entries of a gate_supply decide detail line
# (`<host>:free=N,in_flight=M ...`).
tartci_claim_delay_peer_free(){
  local detail="$1" token sum=0 n
  for token in $detail; do
    case "$token" in
      *:free=*)
        n="${token#*:free=}"
        n="${n%%,*}"
        case "$n" in ''|*[!0-9]*) continue ;; esac
        sum=$((sum + n))
        ;;
    esac
  done
  printf '%s\n' "$sum"
}

# tartci_claim_delay_decision <tier> <queued> — prints `sleep <detail>` when a
# faster host demonstrably can take every queued job of the class, else
# `claim <reason>`.
tartci_claim_delay_decision(){
  local tier="$1" queued="${2:-}" peers class demand="" line verdict detail free
  peers="${TARTCI_CLAIM_DELAY_PEERS:-}"
  [ -n "$peers" ] || { printf 'claim reason=no_faster_hosts_configured\n'; return 0; }
  class="$(tartci_claim_delay_class "$tier")" \
    || { printf 'claim reason=no_class_for_tier tier=%s\n' "$tier"; return 0; }
  case "$queued" in ''|*[!0-9]*) queued=1 ;; esac
  [ "$queued" -gt 0 ] || queued=1
  if [ "$ASSIGNMENT_MODE" = event-class-v2 ]; then
    # The selection reports a witness (1), not a magnitude. Ask the peers at
    # that lower bound first: when no faster slot is free, no count can make
    # the delay pay, and the exhaustive scan is not spent.
    line="$(tartci_claim_delay_supply "$class" "$queued")"
    [ -n "$line" ] || line="unknown decision helper failed"
    free="$(tartci_claim_delay_peer_free "${line#* }")"
    if [ "${line%% *}" != hold ] || [ "$free" -lt 1 ]; then
      printf 'claim reason=supply_%s class=%s demand>=%s peer_free=%s %s\n' \
        "${line%% *}" "$class" "$queued" "$free" "${line#* }"
      return 0
    fi
    demand="$(tartci_assignment_v2_tier_demand "$class" 1 2>/dev/null)" || demand=""
  else
    demand="$queued"
  fi
  case "$demand" in
    ''|*[!0-9]*) printf 'claim reason=demand_unknown class=%s\n' "$class"; return 0 ;;
  esac
  [ "$demand" -gt 0 ] || { printf 'claim reason=no_demand class=%s\n' "$class"; return 0; }
  line="$(tartci_claim_delay_supply "$class" "$demand")"
  verdict="${line%% *}"
  detail="${line#* }"
  [ -n "$line" ] || { verdict=unknown; detail="decision helper failed"; }
  free="$(tartci_claim_delay_peer_free "$detail")"
  if [ "$verdict" = hold ] && [ "$free" -ge 1 ] && [ "$demand" -le "$free" ]; then
    printf 'sleep class=%s demand=%s peer_free=%s %s\n' "$class" "$demand" "$free" "$detail"
    return 0
  fi
  printf 'claim reason=supply_%s class=%s demand=%s peer_free=%s %s\n' \
    "${verdict:-unknown}" "$class" "$demand" "$free" "$detail"
}
