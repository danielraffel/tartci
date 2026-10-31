#!/usr/bin/env bash
# Matrix of job results -> expected ci-required outcome.
set -uo pipefail
gate="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/ci_required_gate.sh"
fails=0
expect() {
  local want="$1"; shift
  "$gate" "$@" >/dev/null 2>&1
  local rc=$?
  if { [[ $want == pass && $rc -eq 0 ]] || [[ $want == fail && $rc -ne 0 ]]; }; then
    echo "ok: $want <- $*"
  else
    echo "not ok: wanted $want, rc=$rc <- $*" >&2
    fails=1
  fi
}
expect pass lint=success python-39-tests=success python-floor=success
expect fail lint=failure python-39-tests=success python-floor=success
expect fail lint=success python-39-tests=cancelled python-floor=success
expect fail lint=success python-39-tests=success python-floor=skipped
expect fail lint= python-39-tests=success python-floor=success
expect fail lint=failure python-39-tests=failure python-floor=failure
expect fail
exit "$fails"
