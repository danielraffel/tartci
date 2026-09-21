#!/usr/bin/env bash
# Does the daemon-health watchdog tell apart a fault it can heal from one only
# a human can grant?
#
# The distinction is the whole point. A GitHub App installation missing
# `repository_hooks` and a dead credential both arrive as HTTP 403. The
# watchdog's remedy for the second — clear the token cache, refresh the daemon
# — cannot fix the first, so applying it there spends the refresh budget, fails
# identically every five minutes, and reports nothing an operator can act on.
#
# Every assertion below is paired with a control that must come out the other
# way on the SAME harness, because a watchdog that escalates everything is as
# useless as one that heals everything, and either failure mode would pass a
# one-sided test.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/../scripts/shipyard-daemon-health.sh"
failures=0

fail() { printf 'FAIL: %s\n' "$*" >&2; failures=$((failures + 1)); }

# Build an isolated HOME with a stubbed `shipyard`, a daemon log, and a token
# cache whose survival is the observable we care about.
#   $1 workdir  $2 daemon-log contents  $3 reconcile exit code ("unsupported"
#   to model a shipyard build that predates the subcommand)
setup() {
  local work="$1" log_body="$2" reconcile_exit="$3"
  mkdir -p "$work/.local/bin" \
    "$work/Library/Application Support/shipyard/daemon" \
    "$work/Library/Logs" \
    "$work/.config/shipyard"

  # The token cache. If the watchdog treats a permission fault as a credential
  # fault, this file disappears — which is exactly the thrash being prevented.
  printf 'cached-token' > "$work/.config/shipyard/.gh-app-token.json"

  cat > "$work/.local/bin/shipyard" <<STUB
#!/usr/bin/env bash
case "\$1 \$2" in
  "daemon status") echo "daemon running tunnel=tailscale repos=owner/repo" ;;
  "daemon refresh") exit 0 ;;
  "daemon reconcile")
    # A build that predates the subcommand rejects it outright, including the
    # support probe. A build that has it answers --help successfully and
    # reserves the verdict for a real run.
    if [ "$reconcile_exit" = "unsupported" ]; then exit 1; fi
    if [ "\${3:-}" = "--help" ]; then exit 0; fi
    exit $reconcile_exit
    ;;
  *) exit 1 ;;
esac
STUB
  chmod +x "$work/.local/bin/shipyard"

  local log="$work/Library/Application Support/shipyard/daemon/daemon.log"
  printf '%s\n' "$log_body" > "$log"
  # Fresh log: the watchdog only trusts recent evidence.
  touch "$log"
}

health_log() { cat "$1/Library/Logs/shipyard-daemon-health.log" 2>/dev/null || true; }
token_present() { [ -f "$1/.config/shipyard/.gh-app-token.json" ]; }
refresh_count() {
  local stamp="$1/Library/Application Support/shipyard/.health-refresh-stamps"
  [ -f "$stamp" ] || { printf '0'; return; }
  awk 'END { print NR }' < "$stamp"
}

# Five repetitions: enough to satisfy the legacy 403-loop threshold, so the
# permission case and the credential case present IDENTICAL evidence to that
# check and only the new classification can separate them.
repeat_line() {
  local line="$1" out="" i=0
  while [ "$i" -lt 5 ]; do out="$out$line"$'\n'; i=$((i + 1)); done
  printf '%s' "$out"
}

PERMISSION_LOG="$(repeat_line 'shipyard daemon: failed to register webhook for owner/repo: 403 Resource not accessible by integration')"
CREDENTIAL_LOG="$(repeat_line 'shipyard daemon: failed to register webhook for owner/repo: HTTP 401: Bad credentials')"

# ---------------------------------------------------------------------------
# 1. A permission fault escalates and heals nothing.
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
setup "$work" "$PERMISSION_LOG" unsupported
HOME="$work" bash "$SCRIPT" >/dev/null 2>&1

out="$(health_log "$work")"
case "$out" in
  *ESCALATE*) : ;;
  *) fail "a permission fault must escalate; health log said: $out" ;;
esac
case "$out" in
  *repository_hooks*) : ;;
  *) fail "the escalation must name the permission a human has to grant; got: $out" ;;
esac
token_present "$work" || fail "the token cache must survive: the credential is not the fault"
[ "$(refresh_count "$work")" = "0" ] || fail "a permission fault must not spend a refresh"

# 1b. CONTROL — identical shape, credential wording. Same harness, same
# threshold, opposite handling. Without this, test 1 would also pass against a
# watchdog that had simply stopped healing anything.
rm -rf "$work"; work="$(mktemp -d)"
setup "$work" "$CREDENTIAL_LOG" unsupported
HOME="$work" bash "$SCRIPT" >/dev/null 2>&1

token_present "$work" && fail "control: a credential fault must still clear the token cache"
[ "$(refresh_count "$work")" = "1" ] || fail "control: a credential fault must still spend exactly one refresh"
case "$(health_log "$work")" in
  *ESCALATE*) fail "control: a first-time credential fault must heal, not escalate" ;;
  *) : ;;
esac

# ---------------------------------------------------------------------------
# 2. Reconcile verdicts route by exit code.

# exit 3 — blocked on a human. Escalate, keep the credential, spend nothing.
rm -rf "$work"; work="$(mktemp -d)"
setup "$work" 'daemon: quiet' 3
HOME="$work" bash "$SCRIPT" >/dev/null 2>&1
case "$(health_log "$work")" in
  *ESCALATE*) : ;;
  *) fail "reconcile exit 3 must escalate; got: $(health_log "$work")" ;;
esac
token_present "$work" || fail "reconcile exit 3 must not clear the token cache"
[ "$(refresh_count "$work")" = "0" ] || fail "reconcile exit 3 must not spend a refresh"

# exit 2 — drift the daemon can re-register its way out of. Heal once.
rm -rf "$work"; work="$(mktemp -d)"
setup "$work" 'daemon: quiet' 2
HOME="$work" bash "$SCRIPT" >/dev/null 2>&1
[ "$(refresh_count "$work")" = "1" ] || fail "reconcile exit 2 must heal exactly once, got $(refresh_count "$work")"
case "$(health_log "$work")" in
  *drift*) : ;;
  *) fail "the heal must name drift as its reason; got: $(health_log "$work")" ;;
esac

# exit 0 — in sync. Touch nothing.
rm -rf "$work"; work="$(mktemp -d)"
setup "$work" 'daemon: quiet' 0
HOME="$work" bash "$SCRIPT" >/dev/null 2>&1
[ "$(refresh_count "$work")" = "0" ] || fail "an in-sync reconcile must not heal"
token_present "$work" || fail "an in-sync reconcile must not clear the token cache"
case "$(health_log "$work")" in
  *ESCALATE*) fail "an in-sync reconcile must not escalate" ;;
  *) : ;;
esac

# ---------------------------------------------------------------------------
# 3. A non-alarming verdict is not a clean bill of health.
#
# The bug class: a check that did not find ITS OWN fault is read as having
# found no fault at all, so it suppresses the checks that would have. Here a
# reconcile that reports only a warning runs alongside a daemon log full of
# genuine credential failures — the warning says nothing about the credential,
# so the credential check must still run and still heal.
rm -rf "$work"; work="$(mktemp -d)"
setup "$work" "$CREDENTIAL_LOG" 1
HOME="$work" bash "$SCRIPT" >/dev/null 2>&1
[ "$(refresh_count "$work")" = "1" ] \
  || fail "a warn-level reconcile must not suppress the credential check, got $(refresh_count "$work") refreshes"
token_present "$work" \
  && fail "a warn-level reconcile must not suppress the credential check's token clear"

# 3b. Same for a shipyard too old to reconcile at all: an unreadable verdict
# leaves the legacy checks in charge rather than silently certifying health.
rm -rf "$work"; work="$(mktemp -d)"
setup "$work" "$CREDENTIAL_LOG" unsupported
HOME="$work" bash "$SCRIPT" >/dev/null 2>&1
[ "$(refresh_count "$work")" = "1" ] \
  || fail "an unsupported reconcile must fall through to the legacy checks, not suppress them"

if [ "$failures" -ne 0 ]; then
  printf 'FAILED: %s assertion(s)\n' "$failures" >&2
  exit 1
fi
echo "PASS: the watchdog separates a grantable permission fault from a healable one"
