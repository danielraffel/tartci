#!/usr/bin/env bash
# Scan-blindness diagnostics: prove the supervisor can say WHY it went blind,
# that the underlying cause survives a wrapper's own summary line, and that the
# restart remedy is bounded rather than retried forever.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# These are the production helpers' inputs. They look unused here because the
# only readers are the functions eval'd in from runner.sh below.
# shellcheck disable=SC2034
{
STATE_DIR="$TMP/state"
RUNNER_NAME="test-runner"
HOST_NAME="test-host"
SCAN_ERROR_FILE="$STATE_DIR/$RUNNER_NAME.scan-last-error"
BLIND_RESTART_FILE="$STATE_DIR/$RUNNER_NAME.scan-blind-restarts"
BLIND_ESCALATION_FILE="$STATE_DIR/$RUNNER_NAME.scan-blind-escalated"
}
mkdir -p "$STATE_DIR"

# Exercise the production helpers directly (json_sanitize included: the
# escalation record uses it).
eval "$(sed -n '/^scan_diagnostic_digest(){/,/^json_sanitize(){/p' \
  "$ROOT/providers/tart-macos/runner.sh")"

fail=0
check(){ if [ "$2" = "$3" ]; then printf '\033[32m✓ %s\033[0m\n' "$1"; else
  printf '\033[31m✗ %s\n    want: %s\n    got:  %s\033[0m\n' "$1" "$3" "$2" >&2; fail=1; fi; }

# 1. A clean scan records no diagnostic — and the POSITIVE CONTROL below proves
#    this emptiness is a real absence, not a dead instrument.
clear_scan_error
run_scan_capture sh -c 'echo 7' >/dev/null
check "clean scan leaves no diagnostic" "$(scan_last_error)" ""

# 2. Positive control: the same mechanism DOES record when there is something.
run_scan_capture sh -c 'echo boom >&2; exit 2' >/dev/null || true
check "failing scan records its stderr" "$(scan_last_error)" "boom"

# 3. The regression that cost hours: a wrapper prints the underlying CAUSE and
#    then its own misleading summary. Keeping only the last line discards the
#    only line that names the real fault.
clear_scan_error
run_scan_capture sh -c '
  echo "You have not agreed to the Xcode license agreements." >&2
  echo "ghapp: resolver context is malformed or unsafe" >&2
  exit 1' >/dev/null || true
got="$(scan_last_error)"
case "$got" in
  *"Xcode license"*"resolver context"*) printf '\033[32m✓ underlying cause survives the summary line\033[0m\n' ;;
  *) printf '\033[31m✗ underlying cause was discarded: %s\033[0m\n' "$got" >&2; fail=1 ;;
esac

# 3b. The same wrapper, more verbose. A tail-only rule survives the two-line
#     case above and still loses the cause here, so this is the case that
#     distinguishes "keep both ends" from "keep a deeper tail".
clear_scan_error
run_scan_capture sh -c '
  echo "You have not agreed to the Xcode license agreements." >&2
  echo "note: falling back to candidate 2" >&2
  echo "note: candidate 2 unusable" >&2
  echo "ghapp: resolver context is malformed or unsafe" >&2
  exit 1' >/dev/null 2>&1 || true
got="$(scan_last_error)"
case "$got" in
  *"Xcode license"*) printf '\033[32m✓ cause survives a 4-line wrapper\033[0m\n' ;;
  *) printf '\033[31m✗ 4-line wrapper lost the cause: %s\033[0m\n' "$got" >&2; fail=1 ;;
esac
case "$got" in
  *"resolver context"*) printf '\033[32m✓ summary also survives a 4-line wrapper\033[0m\n' ;;
  *) printf '\033[31m✗ 4-line wrapper lost the summary: %s\033[0m\n' "$got" >&2; fail=1 ;;
esac

# 3c. Arbitrary verbosity: both ends survive and the elision is declared, so a
#     truncated middle is never mistaken for the whole stream.
clear_scan_error
run_scan_capture sh -c '
  echo "CAUSE-FIRST-LINE" >&2
  i=2; while [ $i -lt 20 ]; do echo "note: filler $i" >&2; i=$((i+1)); done
  echo "SUMMARY-LAST-LINE" >&2
  exit 1' >/dev/null 2>&1 || true
got="$(scan_last_error)"
for needle in CAUSE-FIRST-LINE SUMMARY-LAST-LINE "elided"; do
  case "$got" in
    *"$needle"*) printf '\033[32m✓ 20-line wrapper keeps %s\033[0m\n' "$needle" ;;
    *) printf '\033[31m✗ 20-line wrapper lost %s: %s\033[0m\n' "$needle" "$got" >&2; fail=1 ;;
  esac
done

# 4. stdout still reaches the caller unchanged (the count is the contract).
check "stdout passes through" "$(run_scan_capture sh -c 'echo 3')" "3"

# 5. Exit status is preserved, so callers still detect failure.
run_scan_capture sh -c 'exit 4' >/dev/null && rc=0 || rc=$?
check "exit status preserved" "$rc" "4"

# 6. The restart remedy is bounded and survives the restart it triggers.
reset_blind_restarts
check "restart counter starts at zero" "$(read_blind_restarts)" "0"
write_blind_restarts 2
check "restart counter persists" "$(read_blind_restarts)" "2"
reset_blind_restarts
check "restart counter resets on recovery" "$(read_blind_restarts)" "0"

# 7. A corrupt counter reads as zero rather than crashing the supervisor.
printf 'garbage\n' >"$BLIND_RESTART_FILE"
check "corrupt restart counter is safe" "$(read_blind_restarts)" "0"
reset_blind_restarts

# 8. Escalation is throttled, but only after it has actually been raised.
if blind_escalation_is_fresh; then
  printf '\033[31m✗ escalation reported fresh before any escalation\033[0m\n' >&2; fail=1
else
  printf '\033[32m✓ no stale escalation before one is raised\033[0m\n'
fi
write_blind_escalation 3 "ghapp: resolver context is malformed or unsafe"
if blind_escalation_is_fresh; then
  printf '\033[32m✓ escalation throttles repeat alerts\033[0m\n'
else
  printf '\033[31m✗ escalation did not throttle\033[0m\n' >&2; fail=1
fi
grep -q "resolver context" "$BLIND_ESCALATION_FILE" \
  && printf '\033[32m✓ escalation record carries the diagnostic\033[0m\n' \
  || { printf '\033[31m✗ escalation record lost the diagnostic\033[0m\n' >&2; fail=1; }

# 9. Recovery clears the escalation so a healed lane stops alerting.
reset_blind_restarts
if blind_escalation_is_fresh; then
  printf '\033[31m✗ escalation survived recovery\033[0m\n' >&2; fail=1
else
  printf '\033[32m✓ recovery clears the escalation\033[0m\n'
fi

[ "$fail" = 0 ] && printf '\033[32m✓ scan-blind diagnostics: all checks passed\033[0m\n'
exit "$fail"
