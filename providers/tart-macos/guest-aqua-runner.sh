#!/bin/bash
# Launch one macOS Actions runner inside the console user's Aqua bootstrap.
#
# The Tart supervisor reaches the guest through SSH.  An SSH login has a
# different audit session from the auto-login console, even when both sessions
# use uid 501.  AppKit, SkyLight, and the virtual GPU require the console Aqua
# audit session, so the SSH process may prepare this launcher but must never run
# the Actions runner directly.
set -euo pipefail

LAUNCHCTL="${TARTCI_GUEST_LAUNCHCTL:-/bin/launchctl}"
STAT="${TARTCI_GUEST_STAT:-/usr/bin/stat}"
ID="${TARTCI_GUEST_ID:-/usr/bin/id}"
PGREP="${TARTCI_GUEST_PGREP:-/usr/bin/pgrep}"
SLEEP="${TARTCI_GUEST_SLEEP:-/bin/sleep}"
PLUTIL="${TARTCI_GUEST_PLUTIL:-/usr/bin/plutil}"
EXPECTED_UID="${TARTCI_AQUA_EXPECTED_UID:-501}"
WAIT_SECS="${TARTCI_AQUA_WAIT_SECS:-120}"
READY_SECS="${TARTCI_AQUA_READY_SECS:-30}"
SELF="$0"
case "$SELF" in /*) ;; *) SELF="$PWD/$SELF" ;; esac

# Bash 5 runs EXIT traps after unwinding the calling function's local scope.
# Keep every value needed for fail-closed cleanup at script scope.
PREFLIGHT_CLEANUP_LABEL=""
PREFLIGHT_CLEANUP_ROOT=""
RUNNER_CHILD_JIT_FILE=""
RUNNER_CHILD_STATE_DIR=""
RUNNER_CLEANUP_LABEL=""
RUNNER_CLEANUP_JIT_FILE=""
RUNNER_CLEANUP_PLIST=""
RUNNER_CLEANUP_TAIL_PID=""

note(){ printf 'TARTCI_AQUA %s\n' "$*" >&2; }
die(){ note "ERROR $*"; exit 78; }

valid_label(){
  case "$1" in
    ''|*[!A-Za-z0-9._-]*) return 1 ;;
  esac
}

extract_asid(){
  awk '
    /security context = \{/ { in_security = 1; next }
    in_security && /^[[:space:]]*}/ { in_security = 0 }
    in_security && /asid = [0-9]+/ {
      sub(/^.*asid = /, "")
      sub(/[^0-9].*$/, "")
      print
      exit
    }
  '
}

process_asid(){
  local process_domain
  process_domain="$("$LAUNCHCTL" print "pid/$1" 2>/dev/null)" || return 1
  extract_asid <<<"$process_domain"
}

xml_escape(){
  local value="$1"
  value="${value//&/&amp;}"
  value="${value//</&lt;}"
  value="${value//>/&gt;}"
  value="${value//\"/&quot;}"
  value="${value//\'/&apos;}"
  printf '%s' "$value"
}

aqua_snapshot_detail(){
  local actual_uid actual_user console_user domain domain_uid asid
  actual_uid="$($ID -u)" || { printf 'fail\tid-unavailable\n'; return 1; }
  actual_user="$($ID -un)" || { printf 'fail\tuser-unavailable\n'; return 1; }
  [ "$actual_uid" = "$EXPECTED_UID" ] \
    || { printf 'fail\tuid-mismatch:%s\n' "$actual_uid"; return 1; }
  console_user="$($STAT -f %Su /dev/console 2>/dev/null)" \
    || { printf 'fail\tconsole-user-unavailable\n'; return 1; }
  [ "$console_user" = "$actual_user" ] \
    || { printf 'fail\tconsole-user-mismatch:%s\n' "$console_user"; return 1; }
  domain="$($LAUNCHCTL print "gui/$EXPECTED_UID" 2>/dev/null)" \
    || { printf 'fail\tgui-domain-unavailable\n'; return 1; }
  grep -Eq '^[[:space:]]*session = Aqua$' <<<"$domain" \
    || { printf 'fail\tnon-aqua-domain\n'; return 1; }
  domain_uid="$(awk '
    /security context = \{/ { in_security = 1; next }
    in_security && /^[[:space:]]*}/ { in_security = 0 }
    in_security && /uid = [0-9]+/ { sub(/^.*uid = /, ""); sub(/[^0-9].*$/, ""); print; exit }
  ' <<<"$domain")"
  [ "$domain_uid" = "$EXPECTED_UID" ] \
    || { printf 'fail\tdomain-uid-mismatch:%s\n' "${domain_uid:-missing}"; return 1; }
  asid="$(extract_asid <<<"$domain")"
  case "$asid" in
    ''|*[!0-9]*|0) printf 'fail\tinvalid-asid:%s\n' "${asid:-missing}"; return 1 ;;
  esac
  "$PGREP" -x WindowServer >/dev/null 2>&1 \
    || { printf 'fail\twindowserver-unavailable\n'; return 1; }
  printf 'ok\t%s\n' "$asid"
}

aqua_snapshot(){
  local snapshot state value
  snapshot="$(aqua_snapshot_detail 2>/dev/null || true)"
  IFS=$'\t' read -r state value <<<"$snapshot"
  [ "$state" = ok ] || return 1
  printf '%s\n' "$value"
}

wait_for_aqua(){
  local elapsed=0 snapshot="" state="" value="" last_reason="no-snapshot"
  case "$WAIT_SECS" in ''|*[!0-9]*) die "invalid wait timeout: $WAIT_SECS" ;; esac
  while [ "$elapsed" -le "$WAIT_SECS" ]; do
    snapshot="$(aqua_snapshot_detail 2>/dev/null || true)"
    IFS=$'\t' read -r state value <<<"$snapshot"
    if [ "$state" = ok ]; then
      printf '%s\n' "$value"
      return 0
    fi
    [ "$state" != fail ] || last_reason="${value:-unknown}"
    [ "$elapsed" -lt "$WAIT_SECS" ] || break
    "$SLEEP" 1
    elapsed=$((elapsed + 1))
  done
  die "healthy console Aqua session gui/$EXPECTED_UID did not become ready within ${WAIT_SECS}s (last=$last_reason)"
}

write_plist(){
  local plist="$1" label="$2" program="$3"
  shift 3
  {
    printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>'
    printf '%s\n' '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    printf '%s\n' '<plist version="1.0"><dict>'
    printf '<key>Label</key><string>%s</string>\n' "$(xml_escape "$label")"
    printf '%s\n' '<key>ProgramArguments</key><array>'
    printf '<string>%s</string>\n' "$(xml_escape "$program")"
    for arg in "$@"; do
      printf '<string>%s</string>\n' "$(xml_escape "$arg")"
    done
    printf '%s\n' '</array>'
    printf '%s\n' '<key>LimitLoadToSessionType</key><string>Aqua</string>'
    printf '%s\n' '<key>ProcessType</key><string>Interactive</string>'
    printf '%s\n' '</dict></plist>'
  } >"$plist"
  chmod 600 "$plist"
  "$PLUTIL" -lint "$plist" >/dev/null \
    || die "generated LaunchAgent plist failed validation"
}

bootout(){
  "$LAUNCHCTL" bootout "gui/$EXPECTED_UID/$1" >/dev/null 2>&1 || true
}

cleanup_preflight(){
  local label="${PREFLIGHT_CLEANUP_LABEL:-}" root="${PREFLIGHT_CLEANUP_ROOT:-}"
  PREFLIGHT_CLEANUP_LABEL=""
  PREFLIGHT_CLEANUP_ROOT=""
  [ -z "$label" ] || bootout "$label"
  [ -z "$root" ] || rm -rf "$root"
}

finish_runner_child(){
  local rc=$? jit_file="${RUNNER_CHILD_JIT_FILE:-}" state_dir="${RUNNER_CHILD_STATE_DIR:-}"
  local tmp
  [ -z "$jit_file" ] || rm -f "$jit_file"
  if [ -n "$state_dir" ]; then
    tmp="$state_dir/exit.tmp.$$"
    printf '%s\n' "$rc" >"$tmp"
    mv "$tmp" "$state_dir/exit"
  fi
}

cleanup_runner(){
  local label="${RUNNER_CLEANUP_LABEL:-}" jit_file="${RUNNER_CLEANUP_JIT_FILE:-}"
  local plist="${RUNNER_CLEANUP_PLIST:-}" tail_pid="${RUNNER_CLEANUP_TAIL_PID:-}"
  RUNNER_CLEANUP_LABEL=""
  RUNNER_CLEANUP_JIT_FILE=""
  RUNNER_CLEANUP_PLIST=""
  RUNNER_CLEANUP_TAIL_PID=""
  if [ -n "$tail_pid" ]; then
    kill "$tail_pid" >/dev/null 2>&1 || true
    wait "$tail_pid" 2>/dev/null || true
  fi
  [ -z "$label" ] || bootout "$label"
  [ -z "$jit_file" ] || rm -f "$jit_file"
  [ -z "$plist" ] || rm -f "$plist"
}

run_probe(){
  local label="$1" expected_asid="$2" root="$3"
  local plist="$root/probe.plist" result="$root/probe.result"
  rm -f "$result"
  bootout "$label"
  write_plist "$plist" "$label" "$SELF" probe-child "$expected_asid" "$result"
  "$LAUNCHCTL" bootstrap "gui/$EXPECTED_UID" "$plist" >/dev/null
  "$LAUNCHCTL" kickstart -k "gui/$EXPECTED_UID/$label" >/dev/null
  local elapsed=0 expected="" console="" actual=""
  while [ "$elapsed" -le "$READY_SECS" ]; do
    if [ -s "$result" ]; then
      IFS=$'\t' read -r expected console actual <"$result"
      bootout "$label"
      [ "$expected" = "$expected_asid" ] || die "Aqua probe expected ASID record changed"
      [ -n "$console" ] && [ "$console" = "$expected_asid" ] \
        || die "Aqua probe console ASID changed: expected=$expected_asid live=${console:-missing}"
      [ -n "$actual" ] && [ "$actual" = "$console" ] \
        || die "Aqua probe ASID mismatch: console=$console probe=${actual:-missing}"
      return 0
    fi
    "$SLEEP" 1
    elapsed=$((elapsed + 1))
  done
  bootout "$label"
  die "Aqua probe LaunchAgent did not report within ${READY_SECS}s"
}

probe_child(){
  local expected_asid="$1" result="$2" actual_asid console_asid tmp
  console_asid="$(aqua_snapshot 2>/dev/null || true)"
  actual_asid="$(process_asid "$$" || true)"
  tmp="${result}.tmp.$$"
  printf '%s\t%s\t%s\n' "$expected_asid" "$console_asid" "$actual_asid" >"$tmp"
  mv "$tmp" "$result"
  [ -n "$console_asid" ] && [ "$console_asid" = "$expected_asid" ] \
    && [ -n "$actual_asid" ] && [ "$actual_asid" = "$console_asid" ]
}

preflight(){
  local label="$1" root expected_asid
  valid_label "$label" || die "invalid LaunchAgent label: $label"
  root="$HOME/.tartci/aqua-runner/$label.preflight"
  rm -rf "$root"
  umask 077
  mkdir -p "$root"
  PREFLIGHT_CLEANUP_LABEL="$label"
  PREFLIGHT_CLEANUP_ROOT="$root"
  trap cleanup_preflight EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  expected_asid="$(wait_for_aqua)"
  run_probe "$label" "$expected_asid" "$root"
  note "preflight-ok uid=$EXPECTED_UID asid=$expected_asid"
  cleanup_preflight
  trap - EXIT HUP INT TERM
}

runner_child(){
  local expected_asid="$1" runner_dir="$2" jit_file="$3" state_dir="$4" runner_log="$5"
  local actual_asid console_asid tmp rc jit_config
  RUNNER_CHILD_JIT_FILE="$jit_file"
  RUNNER_CHILD_STATE_DIR="$state_dir"
  trap finish_runner_child EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  exec >"$runner_log" 2>&1
  console_asid="$(aqua_snapshot 2>/dev/null || true)"
  actual_asid="$(process_asid "$$" || true)"
  if [ -z "$console_asid" ] || [ "$console_asid" != "$expected_asid" ] \
    || [ -z "$actual_asid" ] || [ "$actual_asid" != "$console_asid" ]; then
    printf 'TARTCI_AQUA ERROR runner-shell ASID mismatch expected=%s console=%s shell=%s\n' \
      "$expected_asid" "${console_asid:-missing}" "${actual_asid:-missing}" >&2
    exit 78
  fi
  printf 'TARTCI_DIAG aqua-runner-shell uid=%s asid=%s\n' "$($ID -u)" "$actual_asid"
  tmp="$state_dir/ready.tmp.$$"
  printf '%s\n' "$actual_asid" >"$tmp"
  mv "$tmp" "$state_dir/ready"
  [ -s "$jit_file" ] || { printf 'TARTCI_AQUA ERROR missing JIT config file\n' >&2; exit 78; }
  jit_config="$(cat "$jit_file")"
  export ACTIONS_RUNNER_INPUT_JITCONFIG="$jit_config"
  unset jit_config
  rm -f "$jit_file"
  eval "$(/opt/homebrew/bin/brew shellenv)"
  cd "$runner_dir"
  ./run.sh
  rc=$?
  unset ACTIONS_RUNNER_INPUT_JITCONFIG
  exit "$rc"
}

run_runner(){
  local label="$1" root
  root="$HOME/.tartci/aqua-runner/$label"
  local runner_dir="$HOME/actions-runner" jit_file="$root/jit.cfg"
  local plist="$root/runner.plist"
  local runner_log="$root/runner.log" expected_asid actual_asid elapsed=0 tail_pid="" rc=78
  valid_label "$label" || die "invalid LaunchAgent label: $label"
  umask 077
  mkdir -p "$root"
  rm -f "$root/ready" "$root/exit" "$runner_log"
  [ -s "$jit_file" ] || die "missing JIT config file: $jit_file"
  RUNNER_CLEANUP_LABEL="$label"
  RUNNER_CLEANUP_JIT_FILE="$jit_file"
  RUNNER_CLEANUP_PLIST="$plist"
  trap 'cleanup_runner' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  expected_asid="$(wait_for_aqua)"
  write_plist "$plist" "$label" "$SELF" runner-child "$expected_asid" \
    "$runner_dir" "$jit_file" "$root" "$runner_log"
  bootout "$label"
  "$LAUNCHCTL" bootstrap "gui/$EXPECTED_UID" "$plist" >/dev/null
  "$LAUNCHCTL" kickstart -k "gui/$EXPECTED_UID/$label" >/dev/null
  while [ "$elapsed" -le "$READY_SECS" ]; do
    if [ -s "$root/ready" ]; then
      actual_asid="$(head -n1 "$root/ready")"
      [ "$actual_asid" = "$expected_asid" ] \
        || die "runner-shell ASID record mismatch: console=$expected_asid shell=$actual_asid"
      break
    fi
    [ ! -s "$root/exit" ] || break
    "$SLEEP" 1
    elapsed=$((elapsed + 1))
  done
  if [ ! -s "$root/ready" ]; then
    [ ! -f "$runner_log" ] || sed 's/^/[aqua-runner] /' "$runner_log" >&2
    die "Aqua runner LaunchAgent failed its live ASID guard"
  fi
  note "live-ok uid=$EXPECTED_UID asid=$expected_asid label=$label"
  touch "$runner_log"
  tail -n +1 -F "$runner_log" & tail_pid=$!
  RUNNER_CLEANUP_TAIL_PID="$tail_pid"
  while [ ! -s "$root/exit" ]; do
    if ! "$LAUNCHCTL" print "gui/$EXPECTED_UID/$label" >/dev/null 2>&1; then
      "$SLEEP" 1
      [ -s "$root/exit" ] || die "Aqua runner LaunchAgent disappeared without an exit record"
      break
    fi
    "$SLEEP" 1
  done
  rc="$(head -n1 "$root/exit")"
  case "$rc" in ''|*[!0-9]*) rc=78 ;; esac
  "$SLEEP" 1
  cleanup_runner
  trap - EXIT HUP INT TERM
  return "$rc"
}

stop_runner(){
  local label="$1" root
  root="$HOME/.tartci/aqua-runner/$label"
  valid_label "$label" || die "invalid LaunchAgent label: $label"
  bootout "$label"
  rm -f "$root/jit.cfg" "$root/runner.plist"
}

command="${1:-}"
label="${2:-}"
case "$command" in
  probe-child) [ "$#" -eq 3 ] || die 'probe-child requires expected-asid and result'; probe_child "$2" "$3" ;;
  runner-child) [ "$#" -eq 6 ] || die 'runner-child requires expected-asid, runner-dir, jit-file, state-dir, and log'; runner_child "$2" "$3" "$4" "$5" "$6" ;;
  preflight) [ -n "$label" ] || die 'preflight requires a label'; preflight "$label" ;;
  run) [ -n "$label" ] || die 'run requires a label'; run_runner "$label" ;;
  stop) [ -n "$label" ] || die 'stop requires a label'; stop_runner "$label" ;;
  *) die 'usage: guest-aqua-runner.sh preflight|run|stop LABEL' ;;
esac
