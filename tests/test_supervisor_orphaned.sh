#!/usr/bin/env bash
# A supervisor whose parent exits steps aside: tartci_supervisor_orphaned is
# false under the parent that started it and true once it has been reparented.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/tartci-orphan-test.XXXXXX")" && pwd)"
trap 'rm -rf "$tmp"' EXIT
fn="$tmp/fn.sh"
sed -n '/^tartci_supervisor_orphaned(){/,/^}/p' "$ROOT/providers/tart-macos/runner.sh" >"$fn"
grep -q 'tartci_supervisor_orphaned' "$fn"

# Owned: the parent is still alive while the check runs.
"$BASH" -c "source '$fn'; if tartci_supervisor_orphaned; then echo orphaned; else echo owned; fi" \
  >"$tmp/owned"
test "$(cat "$tmp/owned")" = owned

# Orphaned: the parent waits until the child has started under it, then
# exits; the child checks once it has been reparented. (A separate bash, not a
# ( ) subshell: a subshell keeps its shell's $$ and $PPID.)
"$BASH" -c "$BASH -c \"source '$fn'; : >'$tmp/started'; sleep 1
           if tartci_supervisor_orphaned; then echo orphaned; else echo owned; fi \
             >'$tmp/orphan'\" &
         for _ in \$(seq 1 50); do [ -e '$tmp/started' ] && exit 0; sleep 0.1; done; exit 1"
for _ in $(seq 1 50); do [ -s "$tmp/orphan" ] && break; sleep 0.1; done
test "$(cat "$tmp/orphan")" = orphaned

# The guard in the loop runs only between jobs.
grep -A1 'if \[ -z "\$CURRENT_VM" \]; then' "$ROOT/providers/tart-macos/runner.sh" \
  | grep -q 'step_aside=""'
grep -q '^      if tartci_supervisor_orphaned; then' "$ROOT/providers/tart-macos/runner.sh"
echo 'supervisor orphaned: ok'
