#!/bin/bash
# Installs the scoped tccd/sandboxd heal wrapper and its sudoers drop-in.
#
#   fleet/ops/install-tartci-heal-tcc.sh            # plan: print what apply would do
#   sudo fleet/ops/install-tartci-heal-tcc.sh --apply
#   sudo fleet/ops/install-tartci-heal-tcc.sh --rollback   # remove both again
#
# Idempotent: re-run after editing tartci-heal-tcc to push a new version. The
# sudoers fragment is validated with `visudo -cf` BEFORE it is placed and the
# whole sudoers set is validated after; a bad drop-in is removed, never left.
# The grant names the user who ran sudo (SUDO_USER): the account whose
# LaunchAgents run the tartci watchdog, which is the only caller.
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd -P)
WRAPPER_DST=/usr/local/sbin/tartci-heal-tcc
SUDOERS_DST=/etc/sudoers.d/tartci-heal-tcc
LOG=/var/log/tartci-heal-tcc.log
NEWSYSLOG_DST=/etc/newsyslog.d/tartci-heal-tcc.conf

mode=plan
case "${1:-}" in
  "") ;;
  --apply) mode=apply ;;
  --rollback) mode=rollback ;;
  -h|--help)
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "usage: $0 [--apply|--rollback]" >&2; exit 64 ;;
esac

user="${SUDO_USER:-$(id -un)}"
[[ "$user" != root ]] || { echo "run this through sudo from the tartci user's account, not as root directly" >&2; exit 64; }
[[ "$user" =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]] || { echo "refusing an unusual user name: $user" >&2; exit 64; }

if [[ $mode == plan ]]; then
  cat <<EOF
plan (nothing changed):
  install -o root -g wheel -m 0755 $src/tartci-heal-tcc $WRAPPER_DST
  $SUDOERS_DST (root:wheel 0440):
    $user ALL=(root) NOPASSWD: $WRAPPER_DST ""
  $LOG (root:wheel 0640), rotated by $NEWSYSLOG_DST
  /var/db/tartci-heal-tcc (root 0700): the 30-minute stamp
  refuses unless /, /usr, /usr/local and /usr/local/sbin are root-owned and not group/other writable
apply:    sudo $0 --apply
rollback: sudo $0 --rollback
EOF
  exit 0
fi

[[ $EUID -eq 0 ]] || { echo "run with sudo: sudo $0 $1" >&2; exit 77; }

if [[ $mode == rollback ]]; then
  rm -f "$SUDOERS_DST" "$WRAPPER_DST" "$NEWSYSLOG_DST" /var/db/tartci-heal-tcc/last-heal
  rmdir /var/db/tartci-heal-tcc 2>/dev/null || true
  visudo -c >/dev/null || { echo "WARNING: sudoers failed validation after rollback; inspect /etc/sudoers.d" >&2; exit 1; }
  echo "removed $WRAPPER_DST, $SUDOERS_DST and $NEWSYSLOG_DST (the log $LOG is kept)"
  exit 0
fi

bash -n "$src/tartci-heal-tcc"
# /usr/local/sbin does not exist on a stock macOS install; `install` writes its
# temp file INTO the target directory, so create it first.
install -d -o root -g wheel -m 0755 /usr/local/sbin
# A NOPASSWD root command is only as safe as every directory above it: on a
# host where Homebrew owns /usr/local, the login user could replace the
# wrapper. Refuse unless the whole chain is root-owned and writable by root only.
for node in / /usr /usr/local /usr/local/sbin; do
  owner=$(stat -f %u "$node"); mode=$(stat -f %Lp "$node")
  [[ -L "$node" ]] && { echo "refusing: $node is a symlink" >&2; exit 1; }
  [[ "$owner" == 0 ]] || { echo "refusing: $node is owned by uid $owner, not root; a NOPASSWD wrapper under it could be replaced" >&2; exit 1; }
  (( (mode & 022) == 0 )) || { echo "refusing: $node is writable by group or other (mode $mode)" >&2; exit 1; }
done
install -o root -g wheel -m 0755 "$src/tartci-heal-tcc" "$WRAPPER_DST"
echo "installed $WRAPPER_DST (root:wheel 0755; chain / -> /usr/local/sbin is root-owned)"
install -d -o root -g wheel -m 0700 /var/db/tartci-heal-tcc

tmp=$(mktemp); trap 'rm -f "$tmp"' EXIT
sed "s/^TARTCI_USER /$user /" "$src/tartci-heal-tcc.sudoers" >"$tmp"
chmod 0440 "$tmp"
grep -q "^$user ALL=(root) NOPASSWD: $WRAPPER_DST \"\"\$" "$tmp" \
  || { echo "sudoers template did not render the expected single grant; nothing installed" >&2; exit 1; }
visudo -cf "$tmp" >/dev/null || { echo "sudoers fragment FAILED validation; nothing installed" >&2; exit 1; }
install -o root -g wheel -m 0440 "$tmp" "$SUDOERS_DST"
visudo -c >/dev/null || { rm -f "$SUDOERS_DST"; echo "global sudoers validation failed; drop-in removed" >&2; exit 1; }
echo "installed $SUDOERS_DST (root:wheel 0440), validated: $user may run $WRAPPER_DST with no arguments"

touch "$LOG" && chown root:wheel "$LOG" && chmod 0640 "$LOG"
install -d -o root -g wheel -m 0755 /etc/newsyslog.d
install -o root -g wheel -m 0644 "$src/tartci-heal-tcc.newsyslog.conf" "$NEWSYSLOG_DST"
if newsyslog -n -f "$NEWSYSLOG_DST" >/dev/null 2>&1; then
  echo "installed $NEWSYSLOG_DST (validated)"
else
  rm -f "$NEWSYSLOG_DST"; echo "newsyslog rule FAILED validation; removed (the log is not rotated)" >&2
fi

cat <<EOF

Verify, as $user (not root):
  sudo -n $WRAPPER_DST --help     # expect: sudo refuses (the grant allows no arguments)
  sudo -n true                    # expect: 'a password is required'  <- proves the scope
  sudo -n $WRAPPER_DST            # restarts tccd and sandboxd ONCE; re-running within 30 min exits 75
  tartci doctor fleet             # the tcc_wedge finding reads the watchdog's outcome
EOF
