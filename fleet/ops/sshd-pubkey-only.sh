#!/usr/bin/env bash
# Turn off password and keyboard-interactive SSH login on a fleet Mac, leaving
# public-key login as the only way in.
#
#   fleet/ops/sshd-pubkey-only.sh                   # plan (default): what would change
#   sudo fleet/ops/sshd-pubkey-only.sh --apply      # write the drop-in, validate, re-probe
#   sudo fleet/ops/sshd-pubkey-only.sh --rollback   # remove the drop-in, re-probe
#
# --apply writes /etc/ssh/sshd_config.d/100-fleet-pubkey-only.conf with
#
#     PasswordAuthentication no
#     KbdInteractiveAuthentication no
#
# sshd keeps the FIRST value it reads for an option, and the stock
# /etc/ssh/sshd_config includes sshd_config.d/* (sorted) before its own
# settings, so "100-fleet-..." is read before macOS's "100-macos.conf" and
# before anything later in sshd_config.
#
# Lockout guard: --apply refuses unless the user who ran sudo (SUDO_USER) has
# a non-empty ~/.ssh/authorized_keys AND can already log in to localhost with
# a key, non-interactively, as themselves. sudo drops SSH_AUTH_SOCK, so the
# check uses that user's key files; a key that only lives in an agent fails
# it, which is the safe direction.
#
# The guard proves ONE key path: SUDO_USER, from this host, to localhost. It
# does not prove that any other account, or a key on another machine, still
# gets in. Recovery if remote SSH is lost: sign in at the local console or
# over Screen Sharing (neither uses sshd), then run
#     sudo fleet/ops/sshd-pubkey-only.sh --rollback
#
# No restart: macOS runs sshd from launchd in inetd mode (com.openssh.sshd,
# inetdCompatibility Wait=false), one sshd per connection, so every new
# connection reads the new file. The script checks that this is still how
# the host launches sshd and says so; existing sessions are not affected.
#
# Idempotent: an identical drop-in is left in place; --rollback with no
# drop-in is a no-op.
#
# Exit: 0 done/plan, 2 usage, 3 refused (not root, lockout guard),
#       4 sshd -t rejected the config (the previous state is restored).
#
# Test mode (FLEET_OPS_TEST=1 only): FLEET_OPS_TEST_SSHD_CONFIG_D replaces the
# drop-in directory, FLEET_OPS_TEST_EUID the effective uid, FLEET_OPS_TEST_HOME
# the user's home, FLEET_OPS_TEST_LAUNCHD_PLIST the sshd launchd plist, and
# ssh/sshd/sudo/plutil are taken from PATH instead of their absolute paths.
set -euo pipefail

PROG="sshd-pubkey-only"
DROPIN_NAME="100-fleet-pubkey-only.conf"
SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

CONFIG_D=/etc/ssh/sshd_config.d
EUID_NOW="$(id -u)"
HOME_OVERRIDE=""
LAUNCHD_PLIST=/System/Library/LaunchDaemons/ssh.plist
SSH=/usr/bin/ssh
SSHD=/usr/sbin/sshd
SUDO=/usr/bin/sudo
PLUTIL=/usr/bin/plutil
if [ "${FLEET_OPS_TEST:-}" = 1 ]; then
  CONFIG_D="${FLEET_OPS_TEST_SSHD_CONFIG_D:-$CONFIG_D}"
  EUID_NOW="${FLEET_OPS_TEST_EUID:-$EUID_NOW}"
  HOME_OVERRIDE="${FLEET_OPS_TEST_HOME:-}"
  LAUNCHD_PLIST="${FLEET_OPS_TEST_LAUNCHD_PLIST:-$LAUNCHD_PLIST}"
  SSH=ssh SSHD=sshd SUDO=sudo PLUTIL=plutil
fi
DROPIN="$CONFIG_D/$DROPIN_NAME"
DROPIN_BODY="# Written by tartci fleet/ops/sshd-pubkey-only.sh; remove with --rollback.
PasswordAuthentication no
KbdInteractiveAuthentication no"

usage() {
  cat <<EOF
usage: $PROG.sh [--plan | --apply | --rollback]

  --plan       (default) show the current login methods and what --apply would do
  --apply      root: write $DROPIN_NAME after the lockout guard passes
  --rollback   root: remove $DROPIN_NAME

The lockout guard proves only SUDO_USER's key login to localhost; other
accounts and keys on other machines are not checked. If SSH access is lost,
sign in at the local console or over Screen Sharing (neither uses sshd) and run:
  sudo $SCRIPT_PATH --rollback

Run --apply/--rollback with: sudo $SCRIPT_PATH --apply
EOF
}

MODE=plan
case "${1:-}" in
  ""|--plan) ;;
  --apply) MODE=apply ;;
  --rollback) MODE=rollback ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { usage >&2; exit 2; }

say() { printf '%s: %s\n' "$PROG" "$*"; }
refuse() { printf '%s: refused: %s\n' "$PROG" "$*" >&2; exit 3; }

# The methods sshd offers an unauthenticated client, e.g.
# "publickey,password,keyboard-interactive", from the denial line.
probe_methods() {
  local err
  err="$("$SSH" -n -o PreferredAuthentications=none -o PubkeyAuthentication=no \
    -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    nobody@127.0.0.1 true 2>&1 >/dev/null </dev/null || true)"
  case "$err" in
    *"Permission denied ("*)
      printf '%s\n' "$err" | sed -n 's/.*Permission denied (\([^)]*\)).*/\1/p' | head -1 ;;
    *) printf 'unknown (probe said: %s)\n' "$(printf '%s' "$err" | head -1)" ;;
  esac
}

# The user whose key login the guard proves: the one who ran sudo.
target_user() {
  if [ "$EUID_NOW" = 0 ]; then printf '%s\n' "${SUDO_USER:-}"; else id -un; fi
}

user_home() {
  local u="$1" h=""
  if [ -n "$HOME_OVERRIDE" ]; then printf '%s\n' "$HOME_OVERRIDE"; return; fi
  if command -v dscl >/dev/null 2>&1; then
    h="$(dscl . -read "/Users/$u" NFSHomeDirectory 2>/dev/null | sed -n 's/^NFSHomeDirectory: //p')"
  fi
  [ -n "$h" ] || h="$(getent passwd "$u" 2>/dev/null | cut -d: -f6 || true)"
  printf '%s\n' "$h"
}

# Key-only login to localhost as $1, run as $1 (not root) so their keys are used.
pubkey_login_works() {
  local u="$1"
  set -- "$SSH" -n -o BatchMode=yes -o PreferredAuthentications=publickey \
    -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR "$u@localhost" true
  if [ "$(id -un)" = "$u" ]; then
    "$@" </dev/null >/dev/null 2>&1
  else
    "$SUDO" -n -u "$u" -H "$@" </dev/null >/dev/null 2>&1
  fi
}

# "per-connection" when launchd starts one sshd per connection (inetd mode).
sshd_launch_mode() {
  local wait
  wait="$("$PLUTIL" -extract inetdCompatibility.Wait raw -o - "$LAUNCHD_PLIST" 2>/dev/null || true)"
  case "$wait" in
    false) echo per-connection ;;
    true) echo long-running ;;
    *) echo unknown ;;
  esac
}

report_launch_mode() {
  case "$(sshd_launch_mode)" in
    per-connection) say "sshd: launchd starts one sshd per connection ($LAUNCHD_PLIST, inetd Wait=false); new connections read the change, no restart needed" ;;
    *) say "WARNING: could not confirm sshd runs per connection from $LAUNCHD_PLIST; to reload it: sudo launchctl kickstart -k system/com.openssh.sshd" ;;
  esac
}

dropin_state() {
  if [ ! -e "$DROPIN" ]; then echo absent
  elif [ "$(cat "$DROPIN")" = "$DROPIN_BODY" ]; then echo current
  else echo different
  fi
}

# Print the effective values from sshd -T (root only; best effort).
report_effective() {
  [ "$EUID_NOW" = 0 ] || return 0
  "$SSHD" -T -C "user=nobody,host=localhost,addr=127.0.0.1" 2>/dev/null \
    | grep -E '^(passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) ' \
    | sed "s/^/$PROG: sshd -T: /" || true
}

guard() {
  local u home keys
  u="$(target_user)"
  [ -n "$u" ] && [ "$u" != root ] \
    || refuse "run through sudo from the account that keeps key access (SUDO_USER is '${u:-unset}'); a root shell cannot prove a user's key login"
  home="$(user_home "$u")"
  keys="$home/.ssh/authorized_keys"
  [ -n "$home" ] && [ -s "$keys" ] \
    || refuse "$u has no non-empty $keys; turning passwords off would lock $u out"
  pubkey_login_works "$u" \
    || refuse "key-only login to localhost as $u failed; fix key login first (try: ssh -n -o BatchMode=yes -o PreferredAuthentications=publickey localhost true </dev/null)"
  say "lockout guard: $u has $keys and key-only login to localhost works"
}

require_root() {
  [ "$EUID_NOW" = 0 ] || refuse "--$MODE needs root; run: sudo $SCRIPT_PATH --$MODE"
}

case "$MODE" in
  plan)
    u="$(target_user)"
    say "current login methods offered: $(probe_methods)"
    say "drop-in $DROPIN: $(dropin_state)"
    report_launch_mode
    if [ -n "$u" ] && [ "$u" != root ]; then
      home="$(user_home "$u")"
      if [ -n "$home" ] && [ -s "$home/.ssh/authorized_keys" ]; then
        say "lockout guard preview: $u has a non-empty $home/.ssh/authorized_keys"
      else
        say "lockout guard preview: $u has NO non-empty authorized_keys; --apply would refuse"
      fi
    fi
    cat <<EOF
$PROG: --apply would:
  1. refuse unless SUDO_USER has a non-empty ~/.ssh/authorized_keys and key-only login to localhost works
     (this proves only SUDO_USER's key path; other accounts lose password login unchecked)
  2. write $DROPIN:
$(printf '%s\n' "$DROPIN_BODY" | sed 's/^/       /')
  3. validate with sshd -t (on failure restore the previous state, exit 4)
  4. re-probe and print the methods sshd now offers (expected: publickey)
$PROG: run: sudo $SCRIPT_PATH --apply
EOF
    ;;

  apply)
    require_root
    guard
    before="$(probe_methods)"
    state="$(dropin_state)"
    backup=""
    if [ "$state" = current ]; then
      say "$DROPIN already current; leaving it"
    else
      [ -d "$CONFIG_D" ] || refuse "$CONFIG_D does not exist; this sshd does not read drop-ins"
      if [ "$state" = different ]; then
        backup="$(mktemp "$CONFIG_D/.$DROPIN_NAME.prev.XXXXXX")"
        cp -p "$DROPIN" "$backup"
      fi
      tmp="$(mktemp "$CONFIG_D/.$DROPIN_NAME.XXXXXX")"
      printf '%s\n' "$DROPIN_BODY" >"$tmp"
      chmod 644 "$tmp"
      [ "$(id -u)" = 0 ] && chown root:wheel "$tmp" 2>/dev/null || true
      mv -f "$tmp" "$DROPIN"
      say "wrote $DROPIN"
    fi
    if ! "$SSHD" -t; then
      if [ -n "$backup" ]; then mv -f "$backup" "$DROPIN"; else rm -f "$DROPIN"; fi
      say "sshd -t rejected the config; restored the previous state" >&2
      exit 4
    fi
    [ -z "$backup" ] || rm -f "$backup"
    say "sshd -t: ok"
    report_effective
    report_launch_mode
    after="$(probe_methods)"
    say "login methods before: $before"
    say "login methods now:    $after"
    case ",$after," in
      *,password,*|*,keyboard-interactive,*)
        say "WARNING: sshd still offers password/keyboard-interactive; another config line wins (check sshd -T and /etc/ssh/sshd_config)" >&2 ;;
    esac
    ;;

  rollback)
    require_root
    if [ ! -e "$DROPIN" ]; then
      say "$DROPIN absent; nothing to roll back"
    else
      rm -f "$DROPIN"
      say "removed $DROPIN"
      "$SSHD" -t && say "sshd -t: ok"
    fi
    say "login methods now: $(probe_methods)"
    ;;
esac
