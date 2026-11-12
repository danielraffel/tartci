#!/usr/bin/env bash
# Refuse to register a LaunchAgent into the REAL gui/<uid> launchd domain from
# anything but the account's own home.
#
# Why: launchd's gui/<uid> domain is keyed by uid, not by $HOME. An installer
# run with a temporary HOME (every test fixture does this) writes its plist into
# that temp dir and then bootstraps it into the one real domain, where the label
# REPLACES the real agent: the installer boots the real job out first because
# the rendered plist "differs". On m3 a shell test ran `tartci setup` with
# HOME=$tmp/home, which reached install_reclaim_agent.sh --install; the disk
# reclaimer then ran $tmp/home/.local/bin/tartci from a deleted directory,
# exited 127 every hour for a day, and the Workshop volume filled until every
# gate VM lease was refused.
#
# A test double (TARTCI_LAUNCHCTL_BIN) cannot reach the real domain, so it is
# always allowed. The real launchctl is allowed only when HOME is the account's
# home and the plist lives in that home's LaunchAgents directory.
#
# TARTCI_LAUNCHD_GUARD_TREAT_AS_REAL=1 makes the guard judge a double exactly
# as it judges /bin/launchctl. Tests of the refusal use it, so that a guard
# broken on purpose (a break-confirmation run) calls a recording double rather
# than the host's real domain; breaking the guard against the real launchctl
# reproduces the very leak it prevents.
#
# Usage (sourced):  tartci_launchd_domain_guard TARGET_PLIST LAUNCHCTL
# Returns 0 when registering is safe; prints why and returns 1 otherwise.

tartci_account_home() {
  python3 -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)' 2>/dev/null
}

tartci_launchd_domain_guard() {
  local target="$1" launchctl="${2:-/bin/launchctl}" account resolved_home agents
  if [ "$launchctl" != "/bin/launchctl" ] && [ "$launchctl" != "launchctl" ] &&
     [ "${TARTCI_LAUNCHD_GUARD_TREAT_AS_REAL:-0}" != 1 ]; then
    return 0
  fi
  account="$(tartci_account_home)"
  if [ -z "$account" ]; then
    echo "launchd guard: cannot read this account's home; refusing to touch gui/$(id -u)" >&2
    return 1
  fi
  resolved_home="$(cd "$HOME" 2>/dev/null && pwd -P || printf '%s' "$HOME")"
  account="$(cd "$account" 2>/dev/null && pwd -P || printf '%s' "$account")"
  if [ "$resolved_home" != "$account" ]; then
    echo "launchd guard: HOME=$HOME is not this account's home ($account);" >&2
    echo "launchd guard: refusing to register into the real gui/$(id -u) domain." >&2
    echo "launchd guard: a test must set TARTCI_LAUNCHCTL_BIN to a test double." >&2
    return 1
  fi
  agents="$account/Library/LaunchAgents"
  local dir
  dir="$(cd "$(dirname "$target")" 2>/dev/null && pwd -P || dirname "$target")"
  case "$dir/$(basename "$target")" in
    "$agents"/*.plist) return 0 ;;
  esac
  echo "launchd guard: $target is not under $agents; refusing to register it" >&2
  echo "launchd guard: into the real gui/$(id -u) domain." >&2
  return 1
}
