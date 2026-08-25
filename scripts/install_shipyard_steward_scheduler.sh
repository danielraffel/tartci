#!/usr/bin/env bash
# Install the additive, default-off Shipyard stewardship scheduler.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
SOURCE="$HERE/scripts/shipyard_steward_scheduler.py"
TEMPLATE="$HERE/launchd/com.danielraffel.shipyard.steward-scheduler.plist.template"
LABEL="com.danielraffel.shipyard.steward-scheduler"
MODE="disabled"
AUTHORITY=0
APPLY=0
SHIPYARD="$(command -v shipyard 2>/dev/null || true)"
LAUNCHCTL="${TARTCI_LAUNCHCTL_BIN:-/bin/launchctl}"
LAUNCHCTL_INTERPRETER="${TARTCI_LAUNCHCTL_INTERPRETER:-}"
REPOS=()

usage() {
  cat <<'EOF'
usage: install_shipyard_steward_scheduler.sh --repo OWNER/REPO=PATH [...]
       [--shipyard ABSOLUTE_PATH] [--mode disabled|live] [--authority] [--install]

Prints a plan by default. Installation is disabled by default and leaves the
legacy queue tick untouched. Live mode requires explicit --authority. The
scheduler runs one bounded steward apply per repo, then one recovery-worker
apply; it never launches an agent directly.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) REPOS+=("${2:-}"); shift 2 ;;
    --shipyard) SHIPYARD="${2:-}"; shift 2 ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --authority) AUTHORITY=1; shift ;;
    --install) APPLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$MODE" in
  disabled) ENABLED=false ;;
  live)
    [ "$AUTHORITY" = 1 ] || { echo "--mode live requires --authority" >&2; exit 2; }
    ENABLED=true
    ;;
  *) echo "invalid mode: $MODE" >&2; exit 2 ;;
esac
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

INSTALL_DIR="$HOME/.local/share/tartci/scripts"
INSTALLED="$INSTALL_DIR/shipyard_steward_scheduler.py"
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
VERSION="$("$SHIPYARD" --version 2>/dev/null || true)"
python3 - "$VERSION" <<'PY'
import re, sys
match = re.fullmatch(r"shipyard (\d+)\.(\d+)\.(\d+)", sys.argv[1].strip())
if not match or tuple(map(int, match.groups())) < (0, 113, 0):
    raise SystemExit("Shipyard 0.113.0 or newer is required")
PY

echo "Shipyard stewardship scheduler install plan:"
echo "  mode=$MODE authority=$AUTHORITY"
echo "  shipyard=$SHIPYARD"
for repo in "${REPOS[@]}"; do echo "  repo=$repo"; done
echo "  executable=$INSTALLED"
echo "  config=$CONFIG (mode 600)"
echo "  launch_agent=$PLIST"
echo "  legacy_queue_tick=preserved"
[ "$APPLY" = 1 ] || { echo "  action=dry-run (pass --install to apply)"; exit 0; }

umask 077
LOG_DIR="$HOME/Library/Logs"
STATE_DIR="$HOME/.local/state/tartci"
mkdir -p "$INSTALL_DIR" "$CONFIG_DIR" "$PLIST_DIR" "$LOG_DIR" "$STATE_DIR"
python3 - "$HOME" "$INSTALL_DIR" "$CONFIG_DIR" "$PLIST_DIR" "$LOG_DIR" "$STATE_DIR" <<'PY'
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
STAGED_SCRIPT="$(mktemp "$INSTALL_DIR/.shipyard_steward_scheduler.py.XXXXXX")"
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

rollback() {
  rc=$?
  trap - EXIT
  if [ "$SWITCHED" = 1 ] && [ "$COMMITTED" != 1 ]; then
    set +e
    launchctl_command bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1
    for name in script config plist; do
      case "$name" in script) target="$INSTALLED" ;; config) target="$CONFIG" ;; plist) target="$PLIST" ;; esac
      if [ -f "$BACKUP/$name.present" ]; then cp -p "$BACKUP/$name" "$target"; else rm -f "$target"; fi
    done
    if [ "$PRIOR_LOADED" = 1 ] && [ -f "$PLIST" ]; then
      if ! launchctl_command bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 \
        || ! launchctl_command print "gui/$(id -u)/$LABEL" >/dev/null 2>&1
      then
        echo "ROLLBACK FAILURE: prior LaunchAgent files were restored but its registration was not" >&2
        [ "$rc" -ne 0 ] || rc=1
      fi
    fi
  fi
  rm -f "$STAGED_SCRIPT" "$STAGED_CONFIG" "$STAGED_PLIST"
  rm -rf "$BACKUP"
  exit "$rc"
}
trap rollback EXIT
install -m 755 "$SOURCE" "$STAGED_SCRIPT"
cmp -s "$SOURCE" "$STAGED_SCRIPT" || { echo "staged scheduler differs from source" >&2; exit 1; }

python3 - "$STAGED_CONFIG" "$ENABLED" "$AUTHORITY" "$SHIPYARD" "${REPOS[@]}" <<'PY'
import json, os, pathlib, re, stat, subprocess, sys
target, enabled, authority, shipyard, *entries = sys.argv[1:]
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
    if enabled == "true":
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
    "schema_version": 1,
    "enabled": enabled == "true",
    "authority": authority == "1",
    "shipyard": shipyard,
    "repositories": rows,
    "steward_timeout_seconds": 120,
    "recovery_timeout_seconds": 900,
    "max_log_bytes": 5 * 1024 * 1024,
    "log_generations": 3,
}
with open(target, "w", encoding="utf-8") as output:
    json.dump(value, output, indent=2, sort_keys=True)
    output.write("\n")
PY
chmod 600 "$STAGED_CONFIG"
sed -e "s|\$HOME|$HOME|g" "$TEMPLATE" > "$STAGED_PLIST"
plutil -lint "$STAGED_PLIST" >/dev/null

for name in script config plist; do
  case "$name" in script) target="$INSTALLED" ;; config) target="$CONFIG" ;; plist) target="$PLIST" ;; esac
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
mv "$STAGED_SCRIPT" "$INSTALLED"
mv "$STAGED_CONFIG" "$CONFIG"
mv "$STAGED_PLIST" "$PLIST"
rm -f "$HEALTH" "$STARTUP"
launchctl_command bootstrap "gui/$(id -u)" "$PLIST"
PRINTED="$(launchctl_command print "gui/$(id -u)/$LABEL")"
grep -Fq "$INSTALLED" <<<"$PRINTED" && grep -Fq "$CONFIG" <<<"$PRINTED" || {
  echo "live launchd registration does not match installed paths" >&2
  exit 1
}

healthy=0
for ((attempt=0; attempt<WAIT; attempt++)); do
  if python3 - "$HEALTH" "$STARTUP" "$MODE" <<'PY' >/dev/null 2>&1
import json, sys
health_path, startup_path, mode = sys.argv[1:]
path = startup_path if mode == "live" else health_path
with open(path, encoding="utf-8") as source:
    value = json.load(source)
expected = "started" if mode == "live" else "disabled"
raise SystemExit(0 if value.get("status") == expected else 1)
PY
  then healthy=1; break; fi
  sleep 1
done
[ "$healthy" = 1 ] || { echo "scheduler did not publish expected $MODE startup/health receipt" >&2; exit 1; }
COMMITTED=1
echo "installed $LABEL in $MODE mode with a fresh startup/health receipt"
