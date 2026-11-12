#!/usr/bin/env bash
# --help on a command without its own help prints that command's usage and
# runs nothing: `tartci setup --help` once meant a brew install.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/tartci-help-test.XXXXXX")" && pwd)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin" "$tmp/home"
for tool in brew tart qemu-system-aarch64 shipyard; do
  printf '#!/bin/sh\necho "%s $*" >>"%s/ran"\n' "$tool" "$tmp" >"$tmp/bin/$tool"
  chmod +x "$tmp/bin/$tool"
done

for cmd in doctor setup up serve prepare windows goldens hooks tart-image-sync; do
  for flag in -h --help; do
    out="$(HOME="$tmp/home" PATH="$tmp/bin:/usr/bin:/bin" bash "$ROOT/tartci" "$cmd" "$flag")"
    case "$out" in
      *"tartci $cmd"*) ;;
      *) echo "tartci $cmd $flag printed no usage for $cmd: $out" >&2; exit 1 ;;
    esac
    case "$out" in
      *"— host:"*|*prereqs:*) echo "tartci $cmd $flag ran the command: $out" >&2; exit 1 ;;
    esac
  done
done
if [ -e "$tmp/ran" ]; then
  echo "a --help ran a tool: $(cat "$tmp/ran")" >&2
  exit 1
fi
test -z "$(ls -A "$tmp/home")"
echo 'cli help never runs: ok'
