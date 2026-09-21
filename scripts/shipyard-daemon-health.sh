#!/usr/bin/env bash
# shipyard-daemon-health — auto-heal a wedged Shipyard live daemon.
#
# Runs from a LaunchAgent (com.danielraffel.shipyard-daemon-health) every 5 min.
# It heals the daemon from OUTSIDE, the same way the tartci VM-supervisor watchdog
# does — because a wedged daemon can't heal itself.
#
# TWO wedge signatures (the second is the 2026-07-06 lesson):
#   1. ACTIVE webhook-403 loop — the daemon holds a cached App-installation token
#      (~/.config/shipyard/.gh-app-token.json) minted before a permission change
#      propagated, so registration loops on HTTP 403 and live mode never goes
#      healthy. Detected by fresh (mtime<5m) repeated 403s in the daemon log.
#      Remedy: clear the token cache + `shipyard daemon refresh`.
#   1b. BLOCKED on a permission a human must grant — the daemon is trying to
#      register a webhook the GitHub App installation is not allowed to manage
#      (`repository_hooks`). GitHub reports this as HTTP 403 "Resource not
#      accessible by integration", which is byte-for-byte as much a 403 as a
#      dead credential — so signature #1's remedy (clear the token cache and
#      refresh) is applied to a credential that was never the problem, fails,
#      and repeats until the escalation limit. That is not a heal; it is a
#      watchdog thrashing against a fault it structurally cannot fix. Detected
#      BEFORE #1 and answered by escalating immediately: log loudly, name the
#      permission, and touch nothing.
#
#   1c. WEBHOOK URL DRIFT — the daemon's own tunnel URL and the URL GitHub has
#      registered have diverged, typically because this host's tailnet name
#      changed underneath a registration made under the old one. Every delivery
#      then fails to connect while every component reports healthy, because
#      each side is individually correct and nobody compares them. `shipyard
#      daemon reconcile` performs that comparison; its exit code classifies the
#      result (0 in sync, 1 warn, 2 alarm, 3 blocked on a human).
#
#   2. SILENT progress wedge — the daemon process is UP (`daemon status` says
#      "daemon running") but is NOT actually functional: tunnel inactive and/or
#      no repo registered, and its log has gone quiet (frozen), so signature #1
#      never fires. This is the "alive but not making progress" class: a liveness
#      check ("is it running?") passes while the daemon does no work. On
#      2026-07-06 a daemon sat like this for 4 DAYS — `shipyard run`/`ship` block
#      forever on it and the required `macos` gate can never post. Detected by
#      "running but not registered" from `daemon status` itself (a PROGRESS
#      signal), not by scraping error logs.
#
# Escalation (S3 anti-pattern guard): a refresh that restarts the daemon straight
# back into the same wedge is thrash, not a fix. After too many refreshes inside a
# window with the daemon STILL wedged, stop refreshing and log LOUDLY so a human/
# agent fixes the root cause instead of the watchdog silently looping forever.
#
# PATH is load-bearing: `shipyard daemon start/refresh` needs `gh`/`ghapp`, and a
# LaunchAgent runs with a minimal PATH (no /opt/homebrew/bin). Without this the
# daemon it spawns is gh-blind and re-wedges on "gh CLI not found on PATH".
set -u

# --- PATH so spawned daemon can find gh/ghapp/tart (minimal launchd PATH lacks these) ---
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.config/tartci/ghapp-shim:/usr/bin:/bin:/usr/sbin:/sbin"

SY="$HOME/.local/bin/shipyard"
DAEMON_LOG="$HOME/Library/Application Support/shipyard/daemon/daemon.log"
CACHE="$HOME/.config/shipyard/.gh-app-token.json"
HLOG="$HOME/Library/Logs/shipyard-daemon-health.log"
STAMP="$HOME/Library/Application Support/shipyard/.health-refresh-stamps"   # epoch per refresh
REFRESH_WINDOW_S=3600      # count refreshes within the last hour
REFRESH_MAX=4              # >= this many in-window + still wedged → escalate, stop thrashing
LOG_FRESH_MIN=5            # a log touched within this many minutes counts as live evidence
note(){ printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >> "$HLOG"; }

[ -x "$SY" ] || { note "shipyard not at $SY — skip"; exit 0; }

# Count recent refreshes (prune to window). Returns the in-window count via stdout.
recent_refreshes(){
  local now cutoff kept=""
  now="$(date +%s)"; cutoff=$((now - REFRESH_WINDOW_S))
  [ -f "$STAMP" ] && while IFS= read -r ts; do
    [ -n "$ts" ] && [ "$ts" -ge "$cutoff" ] 2>/dev/null && kept="$kept$ts"$'\n'
  done < "$STAMP"
  printf '%s' "$kept" > "$STAMP"
  # `grep -c` prints 0 *and* exits 1 when there are no matches. Appending a
  # fallback `printf 0` therefore returns "0\n0", which is not an integer and
  # disables the anti-thrash comparison on the first heal. `awk` always exits
  # successfully and emits exactly one integer.
  printf '%s' "$kept" | awk 'END { print NR }'
}
record_refresh(){ date +%s >> "$STAMP"; }

# Do a refresh unless we've already thrashed this window (then escalate instead).
heal(){
  local reason="$1" n
  n="$(recent_refreshes)"
  if [ "${n:-0}" -ge "$REFRESH_MAX" ]; then
    note "ESCALATE: $reason — still wedged after ${n} refreshes in the last $((REFRESH_WINDOW_S/60))m; NOT refreshing again. Root-cause needed (check daemon spawn PATH / App-token scope / 'shipyard daemon status')."
    return 0
  fi
  note "$reason — clear token cache + refresh (refresh ${n}+1/${REFRESH_MAX})"
  rm -f "$CACHE"
  record_refresh
  "$SY" daemon refresh >/dev/null 2>&1 && note "  refreshed" || note "  refresh failed"
}

# --- classification -------------------------------------------------------
#
# The distinction this whole script turns on: a fault the watchdog CAN fix by
# restarting something, versus a fault only a human can fix by granting
# something. Answering the second with the remedy for the first is how a
# watchdog burns its escalation budget while the actual defect goes unreported.

# Does this shipyard build know how to reconcile? Older binaries do not, and an
# unsupported subcommand must degrade to "no verdict" — never to a verdict.
reconcile_supported(){ "$SY" daemon reconcile --help >/dev/null 2>&1; }

# Fresh evidence in the daemon log that registration is blocked on a GitHub App
# permission rather than on a credential.
webhook_permission_blocked(){
  [ -f "$DAEMON_LOG" ] || return 1
  [ -n "$(find "$DAEMON_LOG" -mmin -"$LOG_FRESH_MIN" 2>/dev/null)" ] || return 1
  tail -50 "$DAEMON_LOG" 2>/dev/null \
    | grep -qiE 'resource not accessible by integration|BLOCKED on a GitHub App permission|repository_hooks'
}

# Escalate without healing. Used for every fault whose remedy is a human
# action: refreshing would change nothing and would consume the refresh budget
# that a genuinely wedged daemon needs.
escalate(){ note "ESCALATE (no auto-heal possible): $*"; }

status="$("$SY" daemon status 2>/dev/null || true)"

# (0) DOWN → start fresh.
if ! printf '%s' "$status" | grep -q 'daemon running'; then
  note "daemon DOWN — clearing token + starting"
  rm -f "$CACHE"
  record_refresh
  "$SY" daemon start >/dev/null 2>&1 && note "  started" || note "  start failed"
  exit 0
fi

# (1a) BLOCKED on a GitHub App permission. Checked FIRST, because the evidence
# for it also satisfies check (1) — and (1)'s remedy is clearing a credential
# that is working fine. Report and stop; do not spend a refresh.
if webhook_permission_blocked; then
  escalate "webhook registration is refused by GitHub App permissions (repository_hooks). \
A human must grant it in the GitHub App settings and accept it on the affected repositories. \
The credential is VALID — clearing the token cache or refreshing the daemon cannot fix this and has not been attempted."
  exit 0
fi

# (1b) WEBHOOK URL DRIFT / delivery failure, from shipyard's own comparison of
# the URL it intends against the one GitHub holds. Skipped silently on builds
# that predate the subcommand.
if reconcile_supported; then
  "$SY" daemon reconcile >/dev/null 2>&1
  verdict=$?
  case "$verdict" in
    0) : ;;                                    # desired and observed agree
    1) note "webhook reconcile: warning (non-blocking drift)" ;;
    3) escalate "webhook reconcile is BLOCKED on a human action (run: shipyard daemon reconcile — it names the specific permission)."; exit 0 ;;
    2)
      # A restart makes the daemon re-register under its CURRENT identity,
      # which is the fix when the drift is a stale registration. If drift
      # survives that, restarting again cannot help — escalate instead.
      heal "webhook URL drift or failing deliveries (shipyard daemon reconcile exit 2)"
      exit 0
      ;;
    *) note "webhook reconcile returned unexpected status $verdict — treating as no verdict" ;;
  esac
fi

# (1) ACTIVE webhook-403 loop (log fresh + repeated 403s).
if [ -f "$DAEMON_LOG" ] && [ -n "$(find "$DAEMON_LOG" -mmin -5 2>/dev/null)" ]; then
  hits="$(tail -30 "$DAEMON_LOG" 2>/dev/null | grep -ciE 'Resource not accessible by integration|failed to register webhook|gh CLI not found')"
  if [ "${hits:-0}" -ge 5 ]; then
    heal "daemon wedged on webhook errors (x$hits, log fresh)"
    exit 0
  fi
fi

# (2) SILENT progress wedge: process is running but NOT functional — no repo
# registered and/or tunnel inactive. This is the signature the old error-log-only
# check missed (a wedged-but-quiet daemon). `daemon status` is the progress signal.
#   healthy: "... tunnel=tailscale ... repos=Generous-Corp/pulp"
#   wedged : "... tunnel=inactive ... repos=—"
# "repos=Generous-Corp/pulp" (registered) vs "repos=—" (wedged). ASCII-safe: a real
# repo starts with a word char; the wedged placeholder is a non-word em-dash.
if ! printf '%s' "$status" | grep -qE 'repos=[A-Za-z0-9]' ; then
  # A daemon that JUST (re)started needs a grace period to register before we call
  # it wedged — key its freshness off the daemon log mtime (fresh = recently active).
  if [ -z "$(find "$DAEMON_LOG" -mmin -6 2>/dev/null)" ]; then
    heal "daemon running but NOT registered (no repo; tunnel/registration wedged) — silent progress wedge"
  else
    note "daemon running, no repo yet but log fresh (<6m) — registering, leaving alone"
  fi
fi
exit 0
