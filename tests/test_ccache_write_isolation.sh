#!/usr/bin/env bash
# Shell-level contract for providers/tart-macos/ccache-layer.lib.sh, driven the
# way runner.sh drives it (attach -> guest writes -> settle -> background
# promotion) against a fake host ccache directory and a fake `tart`.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

fail(){ printf 'FAIL: %s\n' "$*" >&2; exit 1; }

mkdir -p "$tmp/bin"
# The VM is always gone by the time promotion looks.
printf '#!/bin/sh\nprintf "[]"\n' >"$tmp/bin/tart"
chmod +x "$tmp/bin/tart"

# A ccache entry header: magic cc ac, version 1, type (0 result, 1 manifest).
write_entry(){
  local dir="$1" key="$2" type="$3" payload="$4"
  local file="$dir/${key:0:2}/${key:2}"
  mkdir -p "$dir/${key:0:2}"
  printf '\314\254\001' >"$file"
  # shellcheck disable=SC2059 # the format IS the computed octal escape
  printf "\\$(printf '%03o' "$type")" >>"$file"
  printf '%016d%s' 0 "$payload" >>"$file"
}
# The m3 poison: a direct-mode manifest header with no include paths behind it.
write_zero_include_manifest(){
  local dir="$1" key="$2"
  mkdir -p "$dir/${key:0:2}"
  printf '\314\254\001\001\000\000\000\000\000\000\000\152\270\157\162' >"$dir/${key:0:2}/${key:2}"
}

run_lib(){
  # $1 = isolation value; remaining args = a shell snippet using the lib.
  local value="$1"; shift
  TARTCI_ROOT="$repo_root" CACHE_ROOT="$tmp/cache" CCACHE_MAX_SIZE=1G \
  TARTCI_CCACHE_WRITE_ISOLATION="$value" TARTCI_CCACHE_LAYER_TART="$tmp/bin/tart" \
  STATE_DIR="$tmp/state" bash -c '
    set -euo pipefail
    event(){ printf "%s %s\n" "$1" "${2:-}" >>"$STATE_DIR/events"; }
    mkdir -p "$STATE_DIR"
    source "$TARTCI_ROOT/providers/tart-macos/ccache-layer.lib.sh"
    '"$*"'
    wait
  '
}

layers="$tmp/cache/ccache/tartci-layers-v1"
audit="$tmp/cache/ccache-layer-state/audit.jsonl"
good_result="ab$(printf '1%.0s' $(seq 38))"
good_manifest="cd$(printf '2%.0s' $(seq 38))"
poison="6c$(printf '0%.0s' $(seq 38))"
orphan_poison="7d$(printf '0%.0s' $(seq 38))"
printf 'x: Job macos completed with result: Succeeded\n' >"$tmp/success.log"

# ── Off: nothing is created and the guest command is unchanged ─────────────
run_lib 0 '
  tartci_ccache_layer_attach vm-off
  [ -z "$CCACHE_LAYER_GUEST_PREP" ] || exit 3
  [ -z "$CCACHE_LAYER_GUEST_ENV" ] || exit 3
  tartci_ccache_layer_settle vm-off 0 "'"$tmp"'/success.log"
' || fail "isolation off must be a no-op"
[ ! -e "$layers" ] || fail "isolation off created $layers"
[ ! -e "$tmp/state/events" ] || fail "isolation off emitted events"

# ── Green: the job's entries reach the shared store ────────────────────────
run_lib 1 '
  tartci_ccache_layer_attach vm-green
  case "$CCACHE_LAYER_GUEST_PREP" in *"jobs/vm-green"*) ;; *) exit 3 ;; esac
  '"$(declare -f write_entry)"'
  write_entry "$CCACHE_LAYER_ROOT/jobs/vm-green/remote" '"$good_result"' 0 result
  write_entry "$CCACHE_LAYER_ROOT/jobs/vm-green/remote" '"$good_manifest"' 1 manifest
  CURRENT_ASSIGNMENT_QUARANTINE=none
  tartci_ccache_layer_settle vm-green 0 "'"$tmp"'/success.log"
' || fail "green job run failed"
[ -f "$layers/shared/${good_result:0:2}/${good_result:2}" ] || fail "green result was not promoted"
[ -f "$layers/shared/${good_manifest:0:2}/${good_manifest:2}" ] || fail "green manifest was not promoted"
[ ! -e "$layers/jobs/vm-green" ] || fail "green layer was left under jobs/"
[ ! -e "$layers/green/vm-green" ] || fail "green layer was not cleaned up"
grep -q '"event": "promote"' "$audit" || fail "no promotion in the audit log"
grep -q 'ccache_layer_settle vm=vm-green verdict=green' "$tmp/state/events" || fail "no green settle event"

# ── Killed mid-build: the zero-include manifest never reaches shared ───────
run_lib 1 '
  tartci_ccache_layer_attach vm-killed
  '"$(declare -f write_zero_include_manifest)"'
  write_zero_include_manifest "$CCACHE_LAYER_ROOT/jobs/vm-killed/remote" '"$poison"'
  CURRENT_ASSIGNMENT_QUARANTINE=signal_teardown_unknown
  tartci_ccache_layer_settle vm-killed 124 "'"$tmp"'/success.log"
' || fail "killed job run failed"
[ ! -e "$layers/shared/${poison:0:2}/${poison:2}" ] || fail "a killed job's manifest reached the shared store"
[ ! -e "$layers/discard/vm-killed" ] || fail "killed layer was not deleted"
[ ! -e "$layers/jobs/vm-killed" ] || fail "killed layer was left under jobs/"
grep -q 'ccache_layer_settle vm=vm-killed verdict=red reason=runner_rc=124' "$tmp/state/events" \
  || fail "no red settle event for the killed job"

# ── Supervisor died before settling: the next attach's sweep discards it ───
run_lib 1 '
  tartci_ccache_layer_attach vm-orphan
  '"$(declare -f write_zero_include_manifest)"'
  write_zero_include_manifest "$CCACHE_LAYER_ROOT/jobs/vm-orphan/remote" '"$orphan_poison"'
' || fail "orphan setup failed"
true & dead_pid=$!; wait "$dead_pid"
printf '%s\n' "$dead_pid" >"$tmp/cache/ccache-layer-state/owners/vm-orphan"
run_lib 1 'tartci_ccache_layer_attach vm-next' || fail "next attach failed"
[ ! -e "$layers/jobs/vm-orphan" ] || fail "orphaned layer survived the sweep"
[ ! -e "$layers/shared/${orphan_poison:0:2}/${orphan_poison:2}" ] || fail "an orphaned layer was promoted"
[ -d "$layers/jobs/vm-next" ] || fail "the live job's own layer was swept"
grep -q '"reason": "orphaned_active_layer"' "$audit" || fail "orphan discard not audited"

promotions="$(grep -c '"event": "promote"' "$audit")"
[ "$promotions" = 1 ] || fail "expected exactly one promotion, saw $promotions"
echo "ok: ccache write isolation (off no-op, green promotes, killed/orphaned never promote)"
