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
  printf '%s' "$kept" | grep -c . 2>/dev/null || printf '0'
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

status="$("$SY" daemon status 2>/dev/null || true)"

# (0) DOWN → start fresh.
if ! printf '%s' "$status" | grep -q 'daemon running'; then
  note "daemon DOWN — clearing token + starting"
  rm -f "$CACHE"
  record_refresh
  "$SY" daemon start >/dev/null 2>&1 && note "  started" || note "  start failed"
  exit 0
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
#   healthy: "... tunnel=tailscale ... repos=danielraffel/pulp"
#   wedged : "... tunnel=inactive ... repos=—"
# "repos=danielraffel/pulp" (registered) vs "repos=—" (wedged). ASCII-safe: a real
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
