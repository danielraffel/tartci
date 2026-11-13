#!/usr/bin/env bash
# fleet/ops dry runs: sshd-pubkey-only.sh and create-ci-runner-user.sh plan,
# refuse and (against a temp dir and stub binaries only) apply. Nothing here
# touches /etc/ssh, launchd or the account database: FLEET_OPS_TEST=1 makes
# both scripts take every system command from PATH, which is all stubs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SSHD_OPS="$ROOT/fleet/ops/sshd-pubkey-only.sh"
CI_OPS="$ROOT/fleet/ops/create-ci-runner-user.sh"
tmp="$(cd "$(mktemp -d "${TMPDIR:-/tmp}/fleet-ops-test.XXXXXX")" && pwd)"
trap 'rm -rf "$tmp"' EXIT
me="$(id -un)"
REAL_ID="$(command -v id)"

fail() { echo "FAIL: $*" >&2; exit 1; }
expect_rc() { # expect_rc <rc> <label> -- cmd...
  local want="$1" label="$2" rc=0; shift 3
  "$@" >"$tmp/out" 2>"$tmp/err" || rc=$?
  [ "$rc" = "$want" ] || { cat "$tmp/out" "$tmp/err" >&2; fail "$label: rc $rc, want $want"; }
}
has() { grep -qF -- "$2" "$tmp/$1" || { cat "$tmp/out" "$tmp/err" >&2; fail "missing in $1: $2"; }; }
lacks() { ! grep -qF -- "$2" "$tmp/$1" || fail "unexpected in $1: $2"; }

# ── stubs ────────────────────────────────────────────────────────────────────
bin="$tmp/bin"
mkdir -p "$bin"
cat >"$bin/ssh" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  *PreferredAuthentications=none*)
    echo "nobody@127.0.0.1: Permission denied (${STUB_METHODS:-publickey,password,keyboard-interactive})." >&2
    exit 255 ;;
  *PreferredAuthentications=publickey*) [ "${STUB_PUBKEY_OK:-0}" = 1 ] ;;
  *) exit 99 ;;
esac
EOF
cat >"$bin/sshd" <<'EOF'
#!/usr/bin/env bash
[ "$1" = -t ] && exit "${STUB_SSHD_T_RC:-0}"
exit 0
EOF
cat >"$bin/plutil" <<'EOF'
#!/usr/bin/env bash
echo false
EOF
cat >"$bin/sudo" <<'EOF'
#!/usr/bin/env bash
echo "stub sudo must not be called: $*" >&2; exit 98
EOF
# Account database stubs. `ci` exists once dscl gave it a UniqueID; markers
# in $STUB_STATE make it an admin or a staff member.
cat >"$bin/id" <<EOF
#!/usr/bin/env bash
case " \$* " in
  *" ci "*)
    [ -e "\$STUB_STATE/ci-exists" ] || exit 1
    case "\${1:-}" in
      -gn) if [ -e "\$STUB_STATE/ci-in-staff" ]; then echo staff; else echo ci; fi ;;
      *) echo 403 ;;
    esac
    exit 0 ;;
esac
exec "$REAL_ID" "\$@"
EOF
cat >"$bin/dseditgroup" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_STATE/dseditgroup.log"
case "$*" in
  "-o create "*) touch "$STUB_STATE/ci-group" ;;
  *"checkmember -m ci admin") [ -e "$STUB_STATE/ci-admin" ] ;;
  *"checkmember -m ci staff") [ -e "$STUB_STATE/ci-in-staff" ] ;;
  *) exit 1 ;;
esac
EOF
cat >"$bin/dscl" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_STATE/dscl.log"
case "$*" in
  ". -list /Users UniqueID") printf 'root 0\nother 401\n' ;;
  ". -list /Groups PrimaryGroupID") printf 'staff 20\nothergroup 402\n' ;;
  ". -read /Groups/ci PrimaryGroupID") [ -e "$STUB_STATE/ci-group" ] ;;
  ". -create /Users/ci UniqueID "*) touch "$STUB_STATE/ci-exists" ;;
esac
EOF
cat >"$bin/chown" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_STATE/chown.log"
EOF
cat >"$bin/launchctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$bin/ghapp" <<'EOF'
#!/usr/bin/env bash
echo shipyard,shipyard-macos
EOF
chmod +x "$bin"/*
export PATH="$bin:$PATH" FLEET_OPS_TEST=1
export STUB_STATE="$tmp/state"
mkdir -p "$STUB_STATE"

# ── sshd-pubkey-only.sh ─────────────────────────────────────────────────────
confd="$tmp/sshd_config.d"
home="$tmp/home"
mkdir -p "$confd" "$home/.ssh"
export FLEET_OPS_TEST_SSHD_CONFIG_D="$confd" FLEET_OPS_TEST_HOME="$home"
dropin="$confd/100-fleet-pubkey-only.conf"

expect_rc 2 "bad flag" -- bash "$SSHD_OPS" --bogus

# Plan: current methods, drop-in state, launch mode, guard preview.
expect_rc 0 "plan" -- env FLEET_OPS_TEST_EUID=501 bash "$SSHD_OPS"
has out "current login methods offered: publickey,password,keyboard-interactive"
has out "100-fleet-pubkey-only.conf: absent"
has out "one sshd per connection"
has out "--apply would refuse"
has out "PasswordAuthentication no"
[ ! -e "$dropin" ] || fail "plan wrote the drop-in"

# Not root: refused with the exact sudo command.
expect_rc 3 "apply as non-root" -- env FLEET_OPS_TEST_EUID=501 bash "$SSHD_OPS" --apply
has err "needs root; run: sudo $SSHD_OPS --apply"
expect_rc 3 "rollback as non-root" -- env FLEET_OPS_TEST_EUID=501 bash "$SSHD_OPS" --rollback
has err "run: sudo $SSHD_OPS --rollback"

# Root without SUDO_USER: cannot prove anyone's key login.
expect_rc 3 "apply without SUDO_USER" -- env -u SUDO_USER FLEET_OPS_TEST_EUID=0 bash "$SSHD_OPS" --apply
has err "SUDO_USER"

# Lockout guard: no authorized_keys.
expect_rc 3 "apply without authorized_keys" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" bash "$SSHD_OPS" --apply
has err "no non-empty $home/.ssh/authorized_keys"
[ ! -e "$dropin" ] || fail "guard let the drop-in through (no keys)"

# Lockout guard: keys present but key login fails.
echo "ssh-ed25519 AAAAtest test" >"$home/.ssh/authorized_keys"
expect_rc 3 "apply with failing key login" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" STUB_PUBKEY_OK=0 bash "$SSHD_OPS" --apply
has err "key-only login to localhost as $me failed"
[ ! -e "$dropin" ] || fail "guard let the drop-in through (key login failed)"

# sshd -t rejects: nothing left behind.
expect_rc 4 "apply with sshd -t failing" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" STUB_PUBKEY_OK=1 STUB_SSHD_T_RC=1 bash "$SSHD_OPS" --apply
[ ! -e "$dropin" ] || fail "drop-in left after sshd -t failed"

# Guard passes (temp dir only): writes, re-probes, and is idempotent.
expect_rc 0 "apply" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" STUB_PUBKEY_OK=1 STUB_METHODS=publickey bash "$SSHD_OPS" --apply
has out "wrote $dropin"
has out "login methods now:    publickey"
grep -qx "PasswordAuthentication no" "$dropin" || fail "drop-in lacks PasswordAuthentication no"
grep -qx "KbdInteractiveAuthentication no" "$dropin" || fail "drop-in lacks KbdInteractiveAuthentication no"
expect_rc 0 "apply again" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" STUB_PUBKEY_OK=1 STUB_METHODS=publickey bash "$SSHD_OPS" --apply
has out "already current"
# A different existing drop-in is restored when sshd -t rejects the new one.
echo "PasswordAuthentication yes" >"$dropin"
expect_rc 4 "apply over a different drop-in, sshd -t failing" -- env FLEET_OPS_TEST_EUID=0 SUDO_USER="$me" STUB_PUBKEY_OK=1 STUB_SSHD_T_RC=1 bash "$SSHD_OPS" --apply
[ "$(cat "$dropin")" = "PasswordAuthentication yes" ] || fail "previous drop-in not restored"
[ -z "$(find "$confd" -name '.100-fleet*')" ] || fail "temp files left in $confd"

expect_rc 0 "rollback" -- env FLEET_OPS_TEST_EUID=0 bash "$SSHD_OPS" --rollback
[ ! -e "$dropin" ] || fail "rollback left the drop-in"
expect_rc 0 "rollback again" -- env FLEET_OPS_TEST_EUID=0 bash "$SSHD_OPS" --rollback
has out "nothing to roll back"


# ── create-ci-runner-user.sh ────────────────────────────────────────────────
rhome="$tmp/runner-home"
rdir="$rhome/actions-ci/Shipyard-test-01"
mkdir -p "$rdir" "$rhome/Library/LaunchAgents" "$rhome/.ssh" "$rhome/.config/pulp/secrets"
printf '\357\273\277{\n  "agentId": 1,\n  "agentName": "Shipyard-test-01",\n  "gitHubUrl": "https://github.com/example/Shipyard",\n  "workFolder": "_work"\n}\n' >"$rdir/.runner"
plist="$rhome/Library/LaunchAgents/actions.runner.example-Shipyard.Shipyard-test-01.plist"
: >"$plist"
printf '%s\n' "$plist" >"$rdir/.service"
mkdir -p "$rhome/actions-ci/cache"   # no .runner: not a runner
# A stock macOS login home (750) and secret dirs: one right, one too open.
chmod 750 "$rhome"
chmod 700 "$rhome/.ssh"
: >"$rhome/.ssh/id_test" && chmod 600 "$rhome/.ssh/id_test"
chmod 755 "$rhome/.config/pulp/secrets"
: >"$rhome/.config/pulp/secrets/token" && chmod 644 "$rhome/.config/pulp/secrets/token"
cihome="$tmp/ci-home"
export FLEET_OPS_TEST_HOME="$rhome" FLEET_OPS_TEST_CI_HOME="$cihome"
mode_of() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }

expect_rc 2 "ci: bad flag" -- bash "$CI_OPS" --bogus
expect_rc 0 "ci: help" -- bash "$CI_OPS" --help
has out "fleet SSH keys"
has out "kept out of staff"

expect_rc 0 "ci: plan" -- env FLEET_OPS_TEST_EUID=501 bash "$CI_OPS"
has out "Shipyard-test-01"
has out "repo:        example/Shipyard"
has out "labels:      shipyard,shipyard-macos"
has out "actions.runner.example-Shipyard.Shipyard-test-01.plist (loaded)"
has out "user 'ci': absent"
# The home-mode check flags a 750 home; plan changes nothing.
has out "home $rhome: 750, group/other-accessible; --apply sets it to 700"
has out '  ~/.ssh: 700'
has out "WARNING ~/.config/pulp/secrets: 755, want 700"
has out "WARNING .config/pulp/secrets/token: 644, want 600"
lacks out "WARNING .ssh/id_test"
has out '  ~/.config/shipyard/github-apps: absent'
[ "$(mode_of "$rhome")" = 750 ] || fail "plan changed the home mode"
# Printed commands: quoted words, no values spliced into an sh -c script.
has out "launchctl bootout 'gui/"
has out "ghapp api -X POST 'repos/example/Shipyard/actions/runners/remove-token' --jq .token"
has out "./config.sh remove --token"
has out "ghapp api -X POST 'repos/example/Shipyard/actions/runners/registration-token'"
has out "sh -c 'cd \"\$1\" && shift && exec ./config.sh \"\$@\"' sh '$cihome/actions-ci/Shipyard-test-01'"
has out "--name 'Shipyard-test-01'"
has out "--labels 'shipyard,shipyard-macos'"
has out "<key>UserName</key><string>ci</string>"
has out "sudo chown -R ci:ci"
lacks out "ci:staff"
lacks out "cache"
[ ! -e "$STUB_STATE/dscl.log" ] || ! grep -q -- "-create" "$STUB_STATE/dscl.log" || fail "plan wrote to dscl"

# A runner whose name could break out of a quoted word is skipped, not printed.
evil="$rhome/actions-ci/evil"
mkdir -p "$evil"
printf '{\n  "agentName": "x'"'"'; touch pwned; #",\n  "gitHubUrl": "https://github.com/example/Shipyard"\n}\n' >"$evil/.runner"
expect_rc 0 "ci: plan with unsafe runner name" -- env FLEET_OPS_TEST_EUID=501 bash "$CI_OPS"
has out "name/repo has unexpected characters"
lacks out "touch pwned"
rm -rf "$evil"

expect_rc 3 "ci: apply as non-root" -- env FLEET_OPS_TEST_EUID=501 bash "$CI_OPS" --apply
has err "needs root; run: sudo $CI_OPS --apply"
[ ! -e "$STUB_STATE/dseditgroup.log" ] || ! grep -q -- "-o create" "$STUB_STATE/dseditgroup.log" || fail "non-root apply created a group"

# Root (stubbed): ci gets its own group (not staff), no password, hidden, a
# 0700 home; the invoking user's 750 home is closed to 700.
expect_rc 0 "ci: apply" -- env FLEET_OPS_TEST_EUID=0 bash "$CI_OPS" --apply
has out "created user 'ci' (uid/gid 403, group ci not staff"
grep -qx -- "-o create -r CI Runner -i 403 ci" "$STUB_STATE/dseditgroup.log" || fail "group ci not created with gid 403"
grep -qx ". -create /Users/ci PrimaryGroupID 403" "$STUB_STATE/dscl.log" || fail "ci primary group is not its own"
! grep -q "PrimaryGroupID 20$" "$STUB_STATE/dscl.log" || fail "ci given staff (20) as primary group"
grep -qx ". -create /Users/ci Password \*" "$STUB_STATE/dscl.log" || fail "ci has a password"
grep -qx ". -create /Users/ci IsHidden 1" "$STUB_STATE/dscl.log" || fail "ci not hidden"
grep -qx "ci:ci $cihome" "$STUB_STATE/chown.log" || fail "ci home not owned ci:ci"
[ "$(mode_of "$cihome")" = 700 ] || fail "ci home not 700"
[ "$(id -gn ci)" != staff ] || fail "ci primary group is staff"
has out "home $rhome: was 750, group/other-accessible; set to 700"
[ "$(mode_of "$rhome")" = 700 ] || fail "apply did not close the home"
[ "$(mode_of "$rhome/.config/pulp/secrets")" = 755 ] || fail "apply changed a secret dir (report only)"
has out "/bin/sh -c 'cd"

# Idempotent: an existing standard ci and a 700 home are left alone.
rm -f "$STUB_STATE/dseditgroup.log"
expect_rc 0 "ci: apply again" -- env FLEET_OPS_TEST_EUID=0 bash "$CI_OPS" --apply
has out "already exists, not admin, not in staff"
has out "home $rhome: 700 (closed to group/other)"
! grep -q -- "-o create" "$STUB_STATE/dseditgroup.log" || fail "second apply created a group"

# A ci in staff is refused (it could read group-readable files in homes).
touch "$STUB_STATE/ci-in-staff"
expect_rc 3 "ci: apply with ci in staff" -- env FLEET_OPS_TEST_EUID=0 bash "$CI_OPS" --apply
has err "is in staff"
expect_rc 0 "ci: plan with ci in staff" -- env FLEET_OPS_TEST_EUID=501 bash "$CI_OPS"
has out "IN STAFF (unsafe"
rm -f "$STUB_STATE/ci-in-staff"

# An existing admin ci is refused.
touch "$STUB_STATE/ci-admin"
expect_rc 3 "ci: apply with admin ci" -- env FLEET_OPS_TEST_EUID=0 bash "$CI_OPS" --apply
has err "is an admin"
rm -f "$STUB_STATE/ci-admin"

# Creation that ends up in staff is reported as a failure, with the undo.
rm -f "$STUB_STATE/ci-exists" "$STUB_STATE/ci-group"
rm -rf "$cihome"
touch "$STUB_STATE/ci-in-staff"
expect_rc 4 "ci: apply where ci lands in staff" -- env FLEET_OPS_TEST_EUID=0 bash "$CI_OPS" --apply
has err "came out in staff"

# ── tartci-heal-tcc: argument-less, rate-limited, one fixed command ──────────
# Unprivileged dry run: TARTCI_HEAL_TCC_TEST=1 points the stamp and the log at
# $tmp and takes killall from PATH (a stub that records its arguments). The
# installer is exercised in plan mode only (its apply needs root).
HEAL="$ROOT/fleet/ops/tartci-heal-tcc"
HEAL_INSTALL="$ROOT/fleet/ops/install-tartci-heal-tcc.sh"
heal_bin="$tmp/heal-bin"; mkdir -p "$heal_bin"
cat >"$heal_bin/killall" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$STUB_STATE/killall.log"
exit "${STUB_KILLALL_RC:-0}"
EOF
chmod +x "$heal_bin/killall"
heal_env=(env TARTCI_HEAL_TCC_TEST=1 TARTCI_HEAL_TCC_STAMP="$tmp/heal.stamp"
          TARTCI_HEAL_TCC_LOG="$tmp/heal.log" TARTCI_HEAL_TCC_PATH="$heal_bin:$PATH"
          STUB_STATE="$STUB_STATE")
: >"$STUB_STATE/killall.log"
if [ "$(uname -s)" = Darwin ]; then
  expect_rc 64 "heal: refuses an argument" -- "${heal_env[@]+"${heal_env[@]}"}" bash "$HEAL" --help
  has err "takes no arguments"
  lacks err "rate limited"
  [ ! -s "$STUB_STATE/killall.log" ] || fail "heal: an argument must run nothing"
  expect_rc 77 "heal: refuses to run unprivileged outside the test knob" -- env -u TARTCI_HEAL_TCC_TEST bash "$HEAL"
  has err "must run as root"
  expect_rc 0 "heal: first run" -- "${heal_env[@]+"${heal_env[@]}"}" bash "$HEAL"
  has out "restarted tccd and sandboxd"
  [ "$(cat "$STUB_STATE/killall.log")" = "tccd sandboxd" ] || fail "heal: killall must receive exactly 'tccd sandboxd', got: $(cat "$STUB_STATE/killall.log")"
  [ -e "$tmp/heal.stamp" ] || fail "heal: no stamp written"
  grep -q "heal uid=" "$tmp/heal.log" || fail "heal: no audit line"
  expect_rc 75 "heal: second run within the interval is rate limited" -- "${heal_env[@]+"${heal_env[@]}"}" bash "$HEAL"
  has err "rate limited"
  [ "$(wc -l <"$STUB_STATE/killall.log" | tr -d ' ')" = 1 ] || fail "heal: rate limit must run nothing"
  grep -q "rate-limited" "$tmp/heal.log" || fail "heal: rate limit not logged"
  # An old stamp (older than 30 min) allows a run again; a failing killall is reported and still stamps.
  touch -t "$(date -v-31M +%Y%m%d%H%M.%S)" "$tmp/heal.stamp"
  expect_rc 3 "heal: killall failure is passed through" -- "${heal_env[@]+"${heal_env[@]}"}" env STUB_KILLALL_RC=3 bash "$HEAL"
  has err "killall tccd sandboxd exited 3"
  [ "$(wc -l <"$STUB_STATE/killall.log" | tr -d ' ')" = 2 ] || fail "heal: an old stamp must allow one run"
  expect_rc 75 "heal: a failed attempt still spends the budget" -- "${heal_env[@]+"${heal_env[@]}"}" bash "$HEAL"
else
  expect_rc 78 "heal: refuses off macOS" -- "${heal_env[@]+"${heal_env[@]}"}" bash "$HEAL"
  has err "macOS only"
  [ ! -s "$STUB_STATE/killall.log" ] || fail "heal: off macOS nothing may run"
fi
# The installer plans without root and names the single no-argument grant.
expect_rc 0 "heal installer: plan" -- env -u SUDO_USER bash "$HEAL_INSTALL"
has out "NOPASSWD: /usr/local/sbin/tartci-heal-tcc \"\""
has out "nothing changed"
expect_rc 77 "heal installer: apply refuses without root" -- env -u SUDO_USER bash "$HEAL_INSTALL" --apply
has err "run with sudo"
expect_rc 64 "heal installer: unknown flag" -- bash "$HEAL_INSTALL" --bogus

# The chain check's mode predicate (stat prints octal; bash must read base 8).
# Owner is root only in real use, so only the mode is tested here.
# shellcheck source=../fleet/ops/install-tartci-heal-tcc.sh
( source "$HEAL_INSTALL"
  for m in 755 700 555 644; do
    heal_mode_ok "$m" || fail "heal mode: $m must be accepted"
  done
  for m in 775 757 777 770 722 "" 7x5 abc -755 "7 5"; do
    ! heal_mode_ok "$m" || fail "heal mode: '$m' must be refused"
  done
  # Real directories, mode read the way the installer reads it.
  for m in 0755 0700; do
    d="$tmp/mode-$m"; mkdir "$d"; chmod "$m" "$d"
    heal_mode_ok "$(mode_of "$d")" || fail "heal mode: dir $m must be accepted"
  done
  for m in 0775 0757; do
    d="$tmp/mode-$m"; mkdir "$d"; chmod "$m" "$d"
    ! heal_mode_ok "$(mode_of "$d")" || fail "heal mode: dir $m must be refused"
  done
) || exit 1

echo 'fleet ops scripts: ok'
