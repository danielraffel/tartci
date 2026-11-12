#!/usr/bin/env bash
# PreToolUse hook shim (Claude Code, Codex): refuse raw `launchctl`
# kickstart/bootout/unload/remove/kill/disable/stop against tartci lanes.
# Reads the hook JSON on stdin; exit 2 blocks the tool call with the reason on
# stderr, exit 0 allows it. `tartci hooks print` emits the settings snippet.
set -u
src="${BASH_SOURCE[0]}"
# Resolve symlinks without GNU readlink -f (macOS bash 3.2).
while [ -L "$src" ]; do
  dir="$(cd -P "$(dirname "$src")" && pwd)"
  src="$(readlink "$src")"
  case "$src" in /*) ;; *) src="$dir/$src" ;; esac
done
here="$(cd -P "$(dirname "$src")" && pwd)"
exec "$here/../tartci" launchd guard --hook
