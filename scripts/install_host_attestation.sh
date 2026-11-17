#!/usr/bin/env bash
# Install (or upgrade) the host attestation writer on this machine.
#
# The attestation is what lets Shipyard's landability preflight tell a
# just-in-time pool at rest apart from a persistent runner that crash-looped
# away. Without it the preflight reports Unknown and says so; with a stale one
# it reports Unknown and says THAT — never Served. So "is it deployed" has to
# be provable rather than assumed, which is what the receipt below is for.
#
# Versioning: the writer stamps its own SHA-256 into every record. A host
# running an older writer is therefore visible as *skewed*, not as stale or
# dead — the distinction a prior incident collapsed when a behind-the-times
# checker reported every lane on a healthy host as missing its heartbeat.
#
# Usage:
#   scripts/install_host_attestation.sh [--generation <id>] [--interval <secs>]
#   scripts/install_host_attestation.sh --verify      # receipt only, no changes
#   scripts/install_host_attestation.sh --print-python  # the interpreter it would pin, no changes
set -euo pipefail

LABEL="com.danielraffel.shipyard.host-attestation"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/tartci_host_attestation.py"
PREFIX="$HOME/.local/share/pulp-landing"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
LOG="$HOME/Library/Logs/tartci/host-attestation.log"
DOMAIN="gui/$(id -u)"
INTERVAL=300
GENERATION="${PULP_ATTESTATION_GENERATION:-}"
VERIFY_ONLY=0
PRINT_PYTHON=0
ADVERTISE=()

while [ $# -gt 0 ]; do
  case "$1" in
    --generation) GENERATION="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --advertise) ADVERTISE+=("$2"); shift 2 ;;
    --verify) VERIFY_ONLY=1; shift ;;
    --print-python) PRINT_PYTHON=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

OUT="${TARTCI_HOME:-$HOME/.tartci}/state/host-attestation.json"

receipt() {
  local installed_sha="" running_state="" file_age="-" file_sha=""
  if [ -f "$PREFIX/current/tartci_host_attestation.py" ]; then
    installed_sha="$(shasum -a 256 "$PREFIX/current/tartci_host_attestation.py" | cut -d' ' -f1)"
  fi
  running_state="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk -F'= ' '/^\tstate = /{print $2; exit}')"
  if [ -f "$OUT" ]; then
    file_age="$(python3 - "$OUT" <<'PY'
import json,sys,time
from datetime import datetime,timezone
try:
    d=json.load(open(sys.argv[1]))
    t=datetime.strptime(d["written_at"],"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    print(int(time.time()-t))
except Exception as exc:
    print(f"unreadable: {exc}")
PY
)"
    file_sha="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"].get("writer_sha256") or "")' "$OUT" 2>/dev/null || true)"
  fi
  echo "host:              $(hostname -s)"
  echo "label:             $LABEL"
  echo "launchd state:     ${running_state:-NOT LOADED}"
  echo "installed writer:  ${installed_sha:-ABSENT}"
  echo "attestation file:  $OUT"
  echo "  age (s):         $file_age"
  echo "  writer sha:      ${file_sha:-ABSENT}"
  if [ -n "$installed_sha" ] && [ -n "$file_sha" ] && [ "$installed_sha" != "$file_sha" ]; then
    echo "  SKEW:            the file was written by a DIFFERENT writer than the one installed"
    return 1
  fi
  local profile_ok=""
  if [ -f "$OUT" ]; then
    profile_ok="$(python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d.get("profile_readable"), "|", d.get("profile_detail",""), "| lanes:", len(d.get("jit_lanes",[])))' "$OUT" 2>/dev/null || true)"
    echo "  profile:         ${profile_ok:-unknown}"
  fi
  if [ -z "$running_state" ] || [ -z "$file_sha" ]; then
    echo "  VERDICT:         NOT PROVEN RUNNING"
    return 1
  fi
  case "$profile_ok" in
    False*) echo "  VERDICT:         RUNNING BUT BLIND - the profile was not read, so the record declares no lanes"; return 1 ;;
  esac
  echo "  VERDICT:         RUNNING (writer matches installed)"
}

if [ "$VERIFY_ONLY" -eq 1 ]; then
  receipt
  exit $?
fi

[ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }

# launchd does not inherit an interactive PATH, so a bare `python3` in the
# plist resolves to macOS's /usr/bin/python3, which is 3.9 and has no
# `tomllib`. The first deployment did exactly that: the host profile parsed as
# {} and the record declared zero lanes without reporting that it had failed to
# look. Pick an interpreter that can actually read the profile, and refuse to
# install rather than deploy a sensor that measures nothing.
#
# It must also be a stable macOS privacy identity. launchd attributes every
# privacy (TCC) request of the job to its program, and /opt/homebrew/bin/python3
# resolves into a Cellar directory that is ad-hoc signed and changes with every
# `brew upgrade`: each upgrade is a new identity that can raise a new consent
# alert, and while that alert waits every launchd open of the Tart store on
# /Volumes blocks (m3, 2026-11-16). So the order is:
#   1. TARTCI_ATTESTATION_PYTHON, when set (an absolute path; refused if unusable);
#   2. the python.org framework binary, pinned at its real versioned path
#      (Developer ID signed; stable across patch updates);
#   3. /usr/bin/python3, once macOS ships one with tomllib;
#   4. Homebrew or PATH, with a `WARN tcc-risk` line: the watchdog's launchd
#      lint reports the agent until a stable interpreter is installed.
FRAMEWORK="${TARTCI_PYTHON_FRAMEWORK_ROOT:-/Library/Frameworks/Python.framework}"
has_tomllib() { [ -n "$1" ] && [ -x "$1" ] && "$1" -c 'import tomllib' >/dev/null 2>&1; }

framework_python() {
  local current version dir
  # Versions/Current names the default minor version; prefer it, then the
  # newest 3.x present. Pin the versioned binary itself, never a symlink.
  current="$(readlink "$FRAMEWORK/Versions/Current" 2>/dev/null || true)"
  local listed=""
  for dir in "$FRAMEWORK"/Versions/3.*; do
    [ -d "$dir" ] && listed="$listed ${dir##*/}"
  done
  # shellcheck disable=SC2086 # version names are 3.N, split on purpose
  for version in "$current" $(printf '%s\n' $listed | sort -t. -k2,2nr); do
    case "$version" in 3.[0-9]|3.[0-9][0-9]) ;; *) continue ;; esac
    dir="$FRAMEWORK/Versions/$version/bin"
    [ -f "$dir/python$version" ] && [ ! -L "$dir/python$version" ] || continue
    if has_tomllib "$dir/python$version"; then
      printf '%s\n' "$dir/python$version"
      return 0
    fi
  done
  return 1
}

choose_python() {
  local candidate
  if [ -n "${TARTCI_ATTESTATION_PYTHON:-}" ]; then
    case "$TARTCI_ATTESTATION_PYTHON" in /*) ;; *)
      echo "refusing to install: TARTCI_ATTESTATION_PYTHON must be an absolute path: $TARTCI_ATTESTATION_PYTHON" >&2
      return 1 ;;
    esac
    if has_tomllib "$TARTCI_ATTESTATION_PYTHON"; then
      printf '%s\n' "$TARTCI_ATTESTATION_PYTHON"
      return 0
    fi
    echo "refusing to install: TARTCI_ATTESTATION_PYTHON=$TARTCI_ATTESTATION_PYTHON is missing or has no tomllib" >&2
    return 1
  fi
  if framework_python; then return 0; fi
  if has_tomllib /usr/bin/python3; then printf '%s\n' /usr/bin/python3; return 0; fi
  for candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
    if has_tomllib "$candidate"; then
      echo "WARN tcc-risk: no python.org framework python under $FRAMEWORK; pinning $candidate," \
           "whose privacy identity can change on upgrade (install python.org Python 3.11+ to fix)" >&2
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  echo "refusing to install: no python3 on this host has tomllib (3.11+), so the host profile could not be read and every attestation would declare zero lanes without saying so" >&2
  return 1
}

if ! PYTHON="$(choose_python)"; then
  exit 1
fi
if [ "$PRINT_PYTHON" -eq 1 ]; then
  printf '%s\n' "$PYTHON"
  exit 0
fi
echo "interpreter:       $PYTHON ($("$PYTHON" -c 'import sys;print(sys.version.split()[0])'))"
SHA="$(shasum -a 256 "$SRC" | cut -d' ' -f1)"
DEST="$PREFIX/$SHA"
mkdir -p "$DEST" "$AGENTS" "$(dirname "$LOG")"
cp "$SRC" "$DEST/tartci_host_attestation.py"
chmod 0755 "$DEST/tartci_host_attestation.py"
ln -sfn "$DEST" "$PREFIX/current"

ARGS_XML="    <string>--write</string>
    <string>--self-check</string>"
for entry in ${ADVERTISE+"${ADVERTISE[@]}"}; do
  ARGS_XML="$ARGS_XML
    <string>--advertise</string>
    <string>$entry</string>"
done

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$PREFIX/current/tartci_host_attestation.py</string>
$ARGS_XML
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PULP_ATTESTATION_GENERATION</key><string>${GENERATION:-$SHA}</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

# Give launchd a moment to run it once, then prove it rather than assume it.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -f "$OUT" ] && break
  sleep 1
done

receipt
