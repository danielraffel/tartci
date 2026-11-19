#!/usr/bin/env bash
# bench.sh — clone a pristine CI *golden* into a persistent, customizable
# *bench* VM you open in UTM for GUI / DAW / plugin testing by hand.
#
# golden (CI):  pristine, generic, headless, NEVER mutated; CI clones it per job.
# bench (you):  a separate, persistent, snapshot-able clone you keep and
#               customize (install DAWs, the plugins under test, play). It
#               diverges freely from the golden; CI never touches it.
#
# Neither CI nor UTM ever boots the golden directly — both use clones.
#
# Usage:
#   bench.sh windows [bench-name]   # qcow2 golden → import into UTM (QEMU)
#   bench.sh linux   [bench-name]   # Tart golden → qcow2 export → UTM (QEMU)
#   bench.sh macos   [bench-name]   # documented manual path (Tart AVF bundle)
#
# Env:
#   TARTCI_GOLDENS  dir holding goldens   (default: $HOME/.tartci/goldens)
#   TARTCI_BENCH    dir holding benches   (default: $HOME/.tartci/bench)
#   TARTCI_BENCH_OWNER owner in each new clone marker (default: $USER)
#   TARTCI_BENCH_TTL_DAYS expiry in each new clone marker (default: 14)
set -euo pipefail

OS="${1:?usage: bench.sh <windows|linux|macos> [bench-name]}"
NAME="${2:-${OS}-bench}"
GOLDENS="${TARTCI_GOLDENS:-$HOME/.tartci/goldens}"
BENCH="${TARTCI_BENCH:-$HOME/.tartci/bench}"
mkdir -p "$BENCH"

open_in_utm() {   # $1 = qcow2 path
  local disk="$1"
  if [ -d "/Applications/UTM.app" ]; then
    echo "→ In UTM: New VM → Emulate/Virtualize → import existing disk: $disk"
    echo "  Windows bench: switch display ramfb → virtio-gpu for a real desktop."
    open -a UTM "$disk" 2>/dev/null || open -a UTM
  else
    echo "UTM not installed. Bench disk ready at: $disk"
  fi
}

write_marker() {   # $1 = newly created clone
  local clone="$1" owner="${TARTCI_BENCH_OWNER:-${USER:-unknown}}"
  local ttl="${TARTCI_BENCH_TTL_DAYS:-14}" created
  case "$ttl" in *[!0-9]*|"") echo "invalid TARTCI_BENCH_TTL_DAYS: $ttl" >&2; return 2;; esac
  if [ -z "${ttl//0/}" ]; then
    echo "invalid TARTCI_BENCH_TTL_DAYS: $ttl" >&2
    return 2
  fi
  created="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  TARTCI_MARKER_OWNER="$owner" TARTCI_MARKER_CREATED="$created" TARTCI_MARKER_TTL="$ttl" \
    /usr/bin/python3 -c 'import json, os, sys; json.dump({"owner": os.environ["TARTCI_MARKER_OWNER"], "created": os.environ["TARTCI_MARKER_CREATED"], "ttl_days": int(os.environ["TARTCI_MARKER_TTL"])}, open(sys.argv[1], "x"))' \
    "${clone}.tartci-bench.json"
}

validate_ttl() {
  local ttl="${TARTCI_BENCH_TTL_DAYS:-14}"
  case "$ttl" in *[!0-9]*|"") echo "invalid TARTCI_BENCH_TTL_DAYS: $ttl" >&2; return 2;; esac
  if [ -z "${ttl//0/}" ]; then
    echo "invalid TARTCI_BENCH_TTL_DAYS: $ttl" >&2
    return 2
  fi
}

case "$OS" in
  windows)
    # Golden is already qcow2 — clone (CoW copy) into the bench, import to UTM.
    src="$(ls -t "$GOLDENS"/*windows*.qcow2 2>/dev/null | head -1 || true)"
    [ -n "$src" ] || { echo "no windows golden in $GOLDENS"; exit 1; }
    dst="$BENCH/${NAME}.qcow2"
    [ -e "$dst" ] && { echo "bench already exists: $dst — pick another name or remove it first (won't clobber your customized bench)"; exit 1; }
    validate_ttl
    echo "cloning $src → $dst"
    cp -c "$src" "$dst" 2>/dev/null || cp "$src" "$dst"   # APFS CoW when possible
    if write_marker "$dst"; then
      :
    else
      rc=$?
      rm -f "$dst"
      exit "$rc"
    fi
    open_in_utm "$dst"
    ;;
  linux)
    # Tart golden isn't a direct UTM import — export to qcow2 first.
    src="${TARTCI_GOLDENS:-}"
    dst="$BENCH/${NAME}.qcow2"
    echo "Export the Linux Tart golden to qcow2, then import into UTM:"
    echo "  tart export <linux-golden> $dst        # or qemu-img convert the disk"
    echo "  then: open -a UTM and import $dst (virtio-gpu display for desktop)"
    ;;
  macos)
    echo "macOS bench (lowest priority): UTM can run macOS guests (AVF), but the"
    echo "Tart bundle isn't a direct import. Recreate from the same IPSW in UTM,"
    echo "or run a second Tart clone for headed use. See docs/runbook.md."
    ;;
  *) echo "unknown OS: $OS"; exit 1 ;;
esac
