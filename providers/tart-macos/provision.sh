#!/usr/bin/env bash
# tart-macos/provision.sh — macOS golden helper entrypoint.
#
# Full macOS golden baking remains deliberately operator-led because Xcode,
# Apple-ID/2FA, runner-agent, and Skia pins are heavyweight and host-specific.
# This script captures the reusable, safe operations tartci owns today: list,
# resize, and date-tag/refresh a rolling :latest alias. The detailed tier recipe
# lives in the Pulp source script until Phase 4 retires those scripts into thin
# wrappers around tartci.
set -euo pipefail

export TART_HOME="${TART_HOME:-$HOME/VMs}"

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
command -v tart >/dev/null 2>&1 || die "tart not installed"

usage(){
  cat <<'USAGE'
tart-macos/provision.sh — macOS golden helper entrypoint.

Usage:
  providers/tart-macos/provision.sh list
  providers/tart-macos/provision.sh resize <vm> <GB>
  providers/tart-macos/provision.sh tag <src-vm> <base-name> [YYYY-MM-DD]
USAGE
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  list)
    tart list
    echo "--- store (du) ---"
    du -sh "$TART_HOME"/vms/* 2>/dev/null || true
    ;;
  resize)
    vm="${1:-}"; gb="${2:-}"
    [ -n "$vm" ] && [ -n "$gb" ] || die "usage: resize <vm> <GB>"
    tart stop "$vm" >/dev/null 2>&1 || true
    sleep 2
    tart set "$vm" --disk-size "$gb"
    note "resized $vm disk → ${gb}G; tart-guest-agent grows APFS on next boot"
    ;;
  tag)
    src="${1:-}"; name="${2:-}"; d="${3:-$(date +%Y-%m-%d)}"
    [ -n "$src" ] && [ -n "$name" ] || die "usage: tag <src-vm> <base-name> [YYYY-MM-DD]"
    tart clone "$src" "$name:$d"
    note "tagged $src → $name:$d"
    note "refreshing rolling alias $name:latest"
    tart delete "$name:latest" >/dev/null 2>&1 || true
    tart clone "$name:$d" "$name:latest"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    die "unknown command: $cmd"
    ;;
esac
