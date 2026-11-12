#!/usr/bin/env bash
# `tartci update` reaches scripts/tartci_update.py and answers in one line.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/tartci-update-test.XXXXXX")" && pwd)"
trap 'rm -rf "$tmp"' EXIT
bash "$ROOT/tartci" update --help | grep -q -- '--wait'
out="$(bash "$ROOT/tartci" update --status --home "$tmp")"
test "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" -eq 1
case "$out" in "tartci ? skew unknown"*) ;; *) echo "unexpected: $out" >&2; exit 1 ;; esac
bash "$ROOT/tartci" update --wait --home "$tmp" >/dev/null
echo 'tartci update dispatch: ok'
