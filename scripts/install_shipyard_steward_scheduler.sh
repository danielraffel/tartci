#!/usr/bin/env bash
# Install the single-controller Shipyard carrier scheduler (default off).
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$HERE/scripts/shipyard_steward_scheduler.py"
TEMPLATE="$HERE/launchd/com.danielraffel.shipyard.steward-scheduler.plist.template"
LABEL="com.danielraffel.shipyard.steward-scheduler"
MODE="disabled"
AUTHORITY=0
APPLY=0
CLASSES=()
SHIPYARD="$(command -v shipyard 2>/dev/null || true)"
LAUNCHCTL="${TARTCI_LAUNCHCTL_BIN:-/bin/launchctl}"
LAUNCHCTL_INTERPRETER="${TARTCI_LAUNCHCTL_INTERPRETER:-}"
REPOS=()

usage() {
  cat <<'EOF'
usage: install_shipyard_steward_scheduler.sh --repo OWNER/REPO=PATH [...]
       [--shipyard ABSOLUTE_PATH] [--mode disabled|plan|live]
       [--authority --class redispatch|rearm [...]] [--install]

Prints a plan by default. Installation is disabled by default and leaves the
legacy queue tick untouched. Plan mode reads GitHub through `shipyard runner
carrier` every tick and records each plan; it never passes --apply. Live mode
requires explicit --authority and at least one --class, and exactly one host
in the fleet may run it. The scheduler never launches an agent.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) REPOS+=("${2:-}"); shift 2 ;;
    --shipyard) SHIPYARD="${2:-}"; shift 2 ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --authority) AUTHORITY=1; shift ;;
    --class) CLASSES+=("${2:-}"); shift 2 ;;
    --install) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$MODE" in
  disabled|plan)
    [ "$AUTHORITY" = 0 ] || { echo "--authority is only valid with --mode live" >&2; exit 2; }
    [ "${#CLASSES[@]}" -eq 0 ] || { echo "--class is only valid with --mode live" >&2; exit 2; }
    ;;
  live)
    [ "$AUTHORITY" = 1 ] || { echo "--mode live requires --authority" >&2; exit 2; }
    [ "${#CLASSES[@]}" -ge 1 ] || { echo "--mode live requires at least one --class" >&2; exit 2; }
    for class in "${CLASSES[@]}"; do
      case "$class" in
        redispatch|rearm) ;;
        *) echo "invalid --class $class: live classes are redispatch and rearm" >&2; exit 2 ;;
      esac
    done
    ;;
  *) echo "invalid mode: $MODE" >&2; exit 2 ;;
esac
CLASS_LIST="$(IFS=,; echo "${CLASSES[*]:-}")"
[ "${#REPOS[@]}" -ge 1 ] && [ "${#REPOS[@]}" -le 32 ] || {
  echo "provide 1..32 --repo OWNER/REPO=PATH entries" >&2
  exit 2
}
case "$SHIPYARD" in /*) ;; *) echo "--shipyard must be an absolute path" >&2; exit 2 ;; esac
[ -x "$SHIPYARD" ] || { echo "Shipyard executable is unavailable: $SHIPYARD" >&2; exit 2; }
[ -x "$LAUNCHCTL" ] || { echo "launchctl executable is unavailable: $LAUNCHCTL" >&2; exit 2; }
if [ -n "$LAUNCHCTL_INTERPRETER" ]; then
  case "$LAUNCHCTL_INTERPRETER" in /*) ;; *) echo "launchctl interpreter must be absolute" >&2; exit 2 ;; esac
  [ -x "$LAUNCHCTL_INTERPRETER" ] || { echo "launchctl interpreter is unavailable" >&2; exit 2; }
fi
[ -f "$SOURCE" ] && [ -f "$TEMPLATE" ] || {
  echo "installer must run from a complete tartci checkout" >&2
  exit 2
}

# The agent runs the scheduler through ~/.local/bin/tartci, so it follows the
# installed generation; nothing is copied, and a self-update reaches it.
ENTRYPOINT="$HOME/.local/bin/tartci"
CONFIG_DIR="$HOME/.config/shipyard"
CONFIG="$CONFIG_DIR/steward-scheduler.json"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/$LABEL.plist"
HEALTH="$HOME/Library/Logs/shipyard-steward-scheduler.health.json"
STARTUP="$HOME/Library/Logs/shipyard-steward-scheduler.startup.json"
WAIT="${SHIPYARD_STEWARD_INSTALL_HEALTH_WAIT_SECS:-60}"
case "$WAIT" in ''|*[!0-9]*|0) echo "health wait must be 1..600 seconds" >&2; exit 2 ;; esac
[ "$WAIT" -le 600 ] || { echo "health wait must be 1..600 seconds" >&2; exit 2; }

python3 - "$SHIPYARD" <<'PY'
import os, pathlib, stat, sys
path = pathlib.Path(sys.argv[1])
resolved = path.resolve(strict=True)
if resolved != path:
    raise SystemExit("Shipyard path must be absolute and canonical")
for current in (resolved, *resolved.parents):
    metadata = current.stat()
    if metadata.st_uid not in {0, os.geteuid()} or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise SystemExit(f"Shipyard path is writable by another local user: {current}")
if not stat.S_ISREG(resolved.stat().st_mode) or not os.access(resolved, os.X_OK):
    raise SystemExit("Shipyard must be an executable regular file")
PY
CARRIER_PROBE="$("$SHIPYARD" --json runner carrier --replay /dev/null 2>/dev/null || true)"
python3 - "$CARRIER_PROBE" <<'PY'
import json, sys
try:
    value = json.loads(sys.argv[1])
except json.JSONDecodeError:
    raise SystemExit("this Shipyard has no `runner carrier`; install a Shipyard that does")
if value.get("command") != "runner.carrier" or value.get("plans") != []:
    raise SystemExit("this Shipyard's `runner carrier --replay` envelope is unexpected")
PY

echo "Shipyard carrier scheduler install plan:"
echo "  mode=$MODE authority=$AUTHORITY classes=${CLASS_LIST:-none}"
echo "  shipyard=$SHIPYARD"
for repo in "${REPOS[@]}"; do echo "  repo=$repo"; done
echo "  executable=$ENTRYPOINT steward-scheduler (the installed generation)"
echo "  config=$CONFIG (mode 600)"
echo "  launch_agent=$PLIST"
echo "  legacy_queue_tick=preserved"
[ "$APPLY" = 1 ] || { echo "  action=dry-run (pass --install to apply)"; exit 0; }

umask 077
LOG_DIR="$HOME/Library/Logs"
STATE_DIR="$HOME/.local/state/tartci"
mkdir -p "$CONFIG_DIR" "$PLIST_DIR" "$LOG_DIR" "$STATE_DIR"
python3 - "$HOME" "$CONFIG_DIR" "$PLIST_DIR" "$LOG_DIR" "$STATE_DIR" <<'PY'
import os, pathlib, stat, sys
home = pathlib.Path(sys.argv[1]).resolve()
for raw in sys.argv[2:]:
    current = pathlib.Path(raw).resolve()
    while True:
        metadata = current.stat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise SystemExit(f"install parent is not a user-owned directory: {current}")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise SystemExit(f"install parent is writable by another local user: {current}")
        if current == home:
            break
        if home not in current.parents:
            raise SystemExit(f"install parent escapes HOME: {current}")
        current = current.parent
PY
STAGED_CONFIG="$(mktemp "$CONFIG_DIR/.steward-scheduler.json.XXXXXX")"
STAGED_PLIST="$(mktemp "$PLIST_DIR/.steward-scheduler.plist.XXXXXX")"
BACKUP="$(mktemp -d "${TMPDIR:-/tmp}/steward-scheduler-install.XXXXXX")"
PRIOR_LOADED=0
SWITCHED=0
COMMITTED=0

launchctl_command() {
  if [ -n "$LAUNCHCTL_INTERPRETER" ]; then
    "$LAUNCHCTL_INTERPRETER" "$LAUNCHCTL" "$@"
  else
    "$LAUNCHCTL" "$@"
  fi
}

# A RunAtLoad launch is speculative, and launchd can defer it indefinitely on a
# busy host (`runs = 0`, `pended nondemand spawn = speculative`). Start a job
# that has never run; leave one that already ran alone, so a tick is never
# doubled.
kickstart_if_never_ran() {
  launchctl_command print "gui/$(id -u)/$LABEL" 2>/dev/null \
    | grep -Eq '^[[:space:]]*runs = 0[[:space:]]*$' || return 0
  launchctl_command kickstart "gui/$(id -u)/$LABEL"
}

rollback() {
  rc=$?
  trap - EXIT
  if [ "$SWITCHED" = 1 ] && [ "$COMMITTED" != 1 ]; then
    set +e
    launchctl_command bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1
    for name in config plist; do
      case "$name" in config) target="$CONFIG" ;; plist) target="$PLIST" ;; esac
      if [ -f "$BACKUP/$name.present" ]; then cp -p "$BACKUP/$name" "$target"; else rm -f "$target"; fi
    done
    if [ "$PRIOR_LOADED" = 1 ] && [ -f "$PLIST" ]; then
      if ! launchctl_command bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 \
        || ! kickstart_if_never_ran >/dev/null 2>&1 \
        || ! launchctl_command print "gui/$(id -u)/$LABEL" >/dev/null 2>&1
      then
        echo "ROLLBACK FAILURE: prior LaunchAgent files were restored but its registration was not" >&2
        [ "$rc" -ne 0 ] || rc=1
      fi
    fi
  fi
  rm -f "$STAGED_CONFIG" "$STAGED_PLIST"
  rm -rf "$BACKUP"
  exit "$rc"
}
trap rollback EXIT

python3 - "$STAGED_CONFIG" "$MODE" "$AUTHORITY" "$CLASS_LIST" "$SHIPYARD" "${REPOS[@]}" <<'PY'
import json, os, pathlib, re, stat, subprocess, sys
target, mode, authority, class_list, shipyard, *entries = sys.argv[1:]
classes = [entry for entry in class_list.split(",") if entry]
if len(set(classes)) != len(classes):
    raise SystemExit("duplicate --class entries")
identity_re = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+")
remotes = (
    re.compile(r"https://github\.com/([^/]+)/([^/]+)"),
    re.compile(r"git@github\.com:([^/]+)/([^/]+)"),
    re.compile(r"ssh://git@github\.com/([^/]+)/([^/]+)"),
)
rows = []
seen = set()
for entry in entries:
    if "=" not in entry:
        raise SystemExit(f"invalid --repo entry: {entry}")
    identity, raw = entry.split("=", 1)
    path = pathlib.Path(raw).expanduser().resolve()
    if not identity_re.fullmatch(identity) or identity in seen:
        raise SystemExit(f"invalid or duplicate repository identity: {identity}")
    remote = subprocess.check_output(
        ["git", "-C", str(path), "remote", "get-url", "origin"], text=True, timeout=10
    ).strip()
    actual = None
    for pattern in remotes:
        match = pattern.fullmatch(remote)
        if match:
            actual = f"{match.group(1)}/{match.group(2).removesuffix('.git')}"
            break
    if actual is None or actual.casefold() != identity.casefold():
        raise SystemExit(f"checkout origin mismatch for {identity}: {path}")
    if mode != "disabled":
        for current in (path, *path.parents):
            metadata = current.stat()
            if metadata.st_uid not in {0, os.geteuid()} or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise SystemExit(f"checkout path is writable by another local user: {current}")
    folded = identity.casefold()
    if folded in seen:
        raise SystemExit(f"invalid or duplicate repository identity: {identity}")
    seen.add(folded)
    rows.append({"repo": identity, "checkout": str(path)})
value = {
    "schema_version": 2,
    "mode": mode,
    "authority": authority == "1",
    "classes": classes,
    "shipyard": shipyard,
    "repositories": rows,
    "carrier_timeout_seconds": 240,
    "max_log_bytes": 8 * 1024 * 1024,
    "log_generations": 4,
}
with open(target, "w", encoding="utf-8") as output:
    json.dump(value, output, indent=2, sort_keys=True)
    output.write("\n")
PY
chmod 600 "$STAGED_CONFIG"
sed -e "s|\$HOME|$HOME|g" "$TEMPLATE" > "$STAGED_PLIST"
plutil -lint "$STAGED_PLIST" >/dev/null

for name in config plist; do
  case "$name" in config) target="$CONFIG" ;; plist) target="$PLIST" ;; esac
  if [ -e "$target" ]; then cp -p "$target" "$BACKUP/$name"; : > "$BACKUP/$name.present"; fi
done
if launchctl_command print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then PRIOR_LOADED=1; fi

if [ "$PRIOR_LOADED" = 1 ]; then
  launchctl_command bootout "gui/$(id -u)/$LABEL" || {
    echo "refusing install: confirmed prior LaunchAgent could not be booted out" >&2
    exit 1
  }
else
  launchctl_command bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
fi
SWITCHED=1
mv "$STAGED_CONFIG" "$CONFIG"
mv "$STAGED_PLIST" "$PLIST"
rm -f "$HEALTH" "$STARTUP"
launchctl_command bootstrap "gui/$(id -u)" "$PLIST"
kickstart_if_never_ran
PRINTED="$(launchctl_command print "gui/$(id -u)/$LABEL")"
grep -Fq "$ENTRYPOINT" <<<"$PRINTED" && grep -Fq "$CONFIG" <<<"$PRINTED" || {
  echo "live launchd registration does not match installed paths" >&2
  exit 1
}

healthy=0
for ((attempt=0; attempt<WAIT; attempt++)); do
  if python3 - "$HEALTH" "$STARTUP" "$MODE" <<'PY' >/dev/null 2>&1
import json, sys
health_path, startup_path, mode = sys.argv[1:]
# A disabled tick finishes at once, so its health receipt is the proof. A plan
# or live tick reads GitHub for minutes, so the fresh startup receipt is the
# installation evidence and the first terminal health report is the canary.
path = health_path if mode == "disabled" else startup_path
with open(path, encoding="utf-8") as source:
    value = json.load(source)
expected = "disabled" if mode == "disabled" else "started"
raise SystemExit(0 if value.get("status") == expected and value.get("mode", "disabled") == mode else 1)
PY
  then healthy=1; break; fi
  sleep 1
done
[ "$healthy" = 1 ] || { echo "scheduler did not publish expected $MODE startup/health receipt" >&2; exit 1; }
COMMITTED=1
echo "installed $LABEL in $MODE mode with a fresh startup/health receipt"
