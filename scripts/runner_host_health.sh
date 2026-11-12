#!/bin/bash
# Host-local health check for one persistent GitHub Actions runner.
#
# A persistent runner's launchd job (written by the runner's own svc.sh) has no
# KeepAlive, so a listener that crashes or exits stays down. And when GitHub
# deletes the registration, the runner deletes its own .runner and
# .credentials and exits 0. launchd then reports the job as loaded and not
# running, which looks like an idle host.
#
# This check needs no GitHub credential. It repairs what can be repaired
# locally and records what cannot:
#
#   configured, listener running   -> healthy
#   configured, listener not running -> kickstart the runner's launchd job
#   not configured (no .runner)    -> write a `deconfigured` marker; only a
#                                     fresh registration token fixes this, and
#                                     the central `persistent_runner_heal.py`
#                                     reads the marker over ssh and re-registers
#
# Usage: runner_host_health.sh RUNNER_DIR SERVICE_LABEL [STATE_DIR]
# Prints one `runner_host_health status=... ...` line per run. Exit 0 healthy
# or repaired, 1 deconfigured or the repair failed, 2 usage.

set -u

RUNNER_DIR="${1:-}"
SERVICE="${2:-}"
STATE_DIR="${3:-${RUNNER_HEALTH_STATE_DIR:-$HOME/.local/state/runner-health}}"
LAUNCHCTL="${RUNNER_HEALTH_LAUNCHCTL:-launchctl}"
PGREP="${RUNNER_HEALTH_PGREP:-pgrep}"

if [ -z "$RUNNER_DIR" ] || [ -z "$SERVICE" ] || [ ! -d "$RUNNER_DIR" ]; then
    echo "usage: runner_host_health.sh RUNNER_DIR SERVICE_LABEL [STATE_DIR]" >&2
    exit 2
fi

mkdir -p "$STATE_DIR"
marker="$STATE_DIR/$SERVICE.deconfigured"
now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

emit() { echo "runner_host_health ts=$now service=$SERVICE $*"; }

listener_running() {
    "$PGREP" -f "$RUNNER_DIR/bin[^/ ]*/Runner.Listener" >/dev/null 2>&1
}

if [ ! -f "$RUNNER_DIR/.runner" ]; then
    [ -f "$marker" ] || echo "$now" > "$marker"
    emit "status=deconfigured since=$(cat "$marker") fix=re-register"
    exit 1
fi
rm -f "$marker"

if listener_running; then
    emit "status=healthy"
    exit 0
fi

target="gui/$(id -u)/$SERVICE"
if ! "$LAUNCHCTL" kickstart -k "$target" >/dev/null 2>&1; then
    # Not loaded at all: bootstrap it from its plist first.
    plist="$HOME/Library/LaunchAgents/$SERVICE.plist"
    "$LAUNCHCTL" bootstrap "gui/$(id -u)" "$plist" >/dev/null 2>&1 || true
    "$LAUNCHCTL" kickstart -k "$target" >/dev/null 2>&1 || {
        emit "status=restart_failed target=$target"
        exit 1
    }
fi
sleep "${RUNNER_HEALTH_SETTLE_SECS:-10}"
if listener_running; then
    emit "status=restarted"
    exit 0
fi
emit "status=restart_failed target=$target reason=listener_not_running_after_kickstart"
exit 1
