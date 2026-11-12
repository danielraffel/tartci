#!/usr/bin/env bash
# Install (or refresh) the LaunchDaemon that takes a pf enable reference at boot.
#
# A VM host's shared network needs pf to hold an enable reference: without
# one pfd exits 3 ("no pf starter references held"), InternetSharing never
# creates bridge100, and every VM boots without an address. A reboot drops
# every reference; m5 lost its VM network that way on 2026-10-07 and
# 2026-10-09. This daemon runs `/sbin/pfctl -E` once at each boot and keeps the
# printed token (the undo key, `sudo pfctl -X <token>`) in
# /var/run/pf-enable-ref.token. It is the plist m5 has run since 2026-10-09.
#
# Run it by hand with sudo on a host the doctor names (pf_reference_missing,
# or pf_boot_holder_missing). tartci never runs it, never runs pfctl itself,
# and never runs `pfctl -d`.
#
#   sudo scripts/install_pf_enable_ref.sh            # plan: say what would change
#   sudo scripts/install_pf_enable_ref.sh --install  # write, bootstrap, verify
#
# Idempotent: when the installed plist matches and launchd holds it, nothing
# is touched, so no second reference is taken. A changed plist is booted out
# and bootstrapped again, which runs pfctl -E once more; pf references are
# counted, so an extra one is harmless.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.danielraffel.pf-enable-ref"
SOURCE="$HERE/launchd/system/$LABEL.plist"
DAEMONS_DIR="${TARTCI_PF_LAUNCHDAEMONS_DIR:-/Library/LaunchDaemons}"
TARGET="$DAEMONS_DIR/$LABEL.plist"
TOKEN="${TARTCI_PF_TOKEN_FILE:-/var/run/pf-enable-ref.token}"
LAUNCHCTL="${TARTCI_LAUNCHCTL_BIN:-/bin/launchctl}"
APPLY=0
case "${1:-}" in
  --install) APPLY=1 ;;
  ""|--plan) ;;
  -h|--help) echo "usage: sudo install_pf_enable_ref.sh [--plan|--install]"; exit 0 ;;
  *) echo "usage: sudo install_pf_enable_ref.sh [--plan|--install]" >&2; exit 2 ;;
esac

[ -f "$SOURCE" ] || { echo "install_pf_enable_ref: missing $SOURCE" >&2; exit 3; }
# The real system domain needs root. A test double (TARTCI_LAUNCHCTL_BIN) with a
# scratch daemons dir is the only way to run this unprivileged.
real=0
if [ "$LAUNCHCTL" = /bin/launchctl ] || [ "$LAUNCHCTL" = launchctl ] \
   || [ -z "${TARTCI_PF_LAUNCHDAEMONS_DIR:-}" ]; then
  real=1
fi
if [ "$APPLY" = 1 ] && [ "$real" = 1 ] && [ "$(id -u)" != 0 ]; then
  echo "install_pf_enable_ref: --install writes $TARGET and the system launchd domain; run it with sudo" >&2
  exit 4
fi

loaded=0 held_path=""
if held="$("$LAUNCHCTL" print "system/$LABEL" 2>/dev/null)"; then
  held_path="$(printf '%s\n' "$held" | sed -n 's/^[[:space:]]*path = //p' | head -1)"
  if [ "$held_path" = "$TARGET" ]; then loaded=1; else loaded=2; fi
fi
same=0
[ -f "$TARGET" ] && cmp -s "$SOURCE" "$TARGET" && same=1

if [ "$same" = 1 ] && [ "$loaded" = 1 ]; then
  echo "pf enable reference daemon: already installed and loaded ($LABEL); no new reference taken"
  exit 0
fi
[ "$loaded" != 2 ] || echo "plan: $LABEL is loaded from ${held_path:-an unknown plist}, not $TARGET; boot it out"
if [ "$same" = 1 ]; then
  echo "plan: $TARGET is current but $LABEL is not loaded from it; bootstrap (runs pfctl -E once)"
else
  echo "plan: write $TARGET (root:wheel 0644; at each boot runs /sbin/pfctl -E and saves the token to $TOKEN)"
  echo "plan: launchctl bootstrap system $TARGET (runs pfctl -E once now)"
fi
if [ "$APPLY" != 1 ]; then
  echo "(plan only; re-run with --install)"
  exit 0
fi

if [ "$loaded" = 2 ]; then
  "$LAUNCHCTL" bootout "system/$LABEL" 2>/dev/null || true
  loaded=0
fi
if [ "$same" != 1 ]; then
  if [ "$(id -u)" = 0 ]; then
    install -m 0644 -o root -g wheel "$SOURCE" "$TARGET"
  else
    install -m 0644 "$SOURCE" "$TARGET"
  fi
  # launchd runs its cached spec; a changed file needs bootout + bootstrap.
  [ "$loaded" = 1 ] && "$LAUNCHCTL" bootout "system/$LABEL" 2>/dev/null || true
  loaded=0
fi
[ "$loaded" = 1 ] || {
  "$LAUNCHCTL" bootstrap system "$TARGET"
  # bootstrap loads the job but can leave RunAtLoad pended on a stalled launchd.
  # Kickstart starts this boot's reference-taking run explicitly.
  "$LAUNCHCTL" kickstart "system/$LABEL"
}

# Verify: launchd holds it from $TARGET, its run exited 0, and a token exists.
ok=1
for _ in 1 2 3 4 5; do
  state="$("$LAUNCHCTL" print "system/$LABEL" 2>/dev/null || true)"
  exit_code="$(printf '%s\n' "$state" | sed -n 's/^[[:space:]]*last exit code = //p' | head -1)"
  [ "$exit_code" = 0 ] && [ -s "$TOKEN" ] && { ok=0; break; }
  sleep 1
done
if [ "$ok" != 0 ]; then
  echo "install_pf_enable_ref: installed, but not verified: last exit code '${exit_code:-unread}', token $TOKEN $([ -s "$TOKEN" ] && echo present || echo missing)" >&2
  exit 5
fi
echo "pf enable reference daemon: installed and loaded ($LABEL); token in $TOKEN"
echo "next: tartci doctor fleet should read pf_reference_ok ... taken at boot by $LABEL"
