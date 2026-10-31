#!/usr/bin/env bash
# Matrix of job results -> expected ci-required outcome.
set -uo pipefail
gate="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/ci_required_gate.sh"
fails=0
expect() {
  local want="$1" json="$2"
  "$gate" "$json" >/dev/null 2>&1
  local rc=$?
  if { [[ $want == pass && $rc -eq 0 ]] || [[ $want == fail && $rc -ne 0 ]]; }; then
    echo "ok: $want <- $json"
  else
    echo "not ok: wanted $want, rc=$rc <- $json" >&2
    fails=1
  fi
}
expect pass '{"lint":{"result":"success"},"a":{"result":"success"},"b":{"result":"success"}}'
expect fail '{"lint":{"result":"failure"},"a":{"result":"success"}}'
expect fail '{"lint":{"result":"success"},"a":{"result":"cancelled"}}'
expect fail '{"lint":{"result":"success"},"a":{"result":"skipped"}}'
expect fail '{"lint":{"result":"success"},"a":{}}'
expect fail '{}'
expect fail 'not json'
exit "$fails"
