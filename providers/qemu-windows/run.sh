#!/usr/bin/env bash
# qemu-windows/run.sh — run ONE Pulp build+test inside an EPHEMERAL Win11-24H2
# ARM64 QEMU clone, then discard it. Unlike qemu-run.sh (the one-shot installer /
# single-operator boot with a fixed disk + fixed 2222 port), this is per-job:
# a CoW overlay off the golden qcow2, a fresh efivars, and a dynamically-chosen
# free SSH port — so multiple jobs run concurrently without collisions (tartci#3).
#
# The golden carries the baked MSVC/CMake/Ninja/Git/Python toolchain but NO repo
# checkout — run.sh clones Pulp into the clone at job time. GPU/Skia is selected
# explicitly with --gpu now that immutable Windows ARM64 and x64 slices exist.
#
# Flow:
#   qemu-img create -b <golden> overlay.qcow2     # CoW, instant
#   seed per-job efivars; pick a free host SSH port
#   qemu ... nvme=overlay, hostfwd <port>->22      # boot headless
#   ssh admin@127.0.0.1:<port> → clone + build + ctest
#   kill qemu; rm overlay + efivars                # discard
#
# Usage:
#   providers/qemu-windows/run.sh                       # build origin/main
#   providers/qemu-windows/run.sh --ref origin/feat/x   # build a ref
#   providers/qemu-windows/run.sh --smoke               # boot + SSH + toolchain probe only
#   providers/qemu-windows/run.sh --keep                # hold the clone until stopped
set -euo pipefail

TARTCI_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=providers/common/toml-python.lib.sh
source "$TARTCI_ROOT/providers/common/toml-python.lib.sh"
# shellcheck source=providers/common/vm-lease.lib.sh
source "$TARTCI_ROOT/providers/common/vm-lease.lib.sh"
# shellcheck source=providers/common/vm-state.lib.sh
source "$TARTCI_ROOT/providers/common/vm-state.lib.sh"
GOLDEN="${TARTCI_WIN_GOLDEN:-${TARTCI_GOLDENS:-$HOME/.tartci/goldens}/pulp-windows-build-24h2-arm64-2026-06-12-cacheopt.qcow2}"
KEY="${TARTCI_WIN_SSH_KEY:-$HOME/.ssh/id_ed25519}"
WUSER="${TARTCI_WIN_SSH_USER:-admin}"
SRC_REPO="${TARTCI_WIN_SRC_REPO:-https://github.com/Generous-Corp/pulp}"
# Reuse an explicitly configured external VM store before falling back to the
# host's temporary volume; an overlay may grow by many gigabytes during a build.
WORKROOT="${TARTCI_WIN_WORK:-${TART_HOME:-${TMPDIR:-/tmp}}/tartci-win}"
WIN_CPUS="${TARTCI_WIN_VM_CORES:-4}"
WIN_MEMORY_MB="${TARTCI_WIN_MEMORY_MB:-8192}"
REF=""; BUILD_TYPE="Release"; SMOKE=0; KEEP=0
GPU="${TARTCI_WIN_GPU:-${PULP_WIN_GPU:-off}}"
# Default excludes the `validation`-labeled tests, matching the Windows CI leg.
# This is a scope selection, not a green claim: the command still fails closed
# on any selected test failure. Single label (no `|`) keeps it safe inside the
# cmd /c chain;
# pass a fuller exclude (e.g. the CI `validation|slow` + an --exclude-regex) via
# PULP_CTEST_ARGS/--ctest-args, quoting the regex so cmd.exe doesn't pipe on `|`.
CTEST_ARGS="${PULP_CTEST_ARGS:---output-on-failure --label-exclude validation}"
CTEST_JOBS="${TARTCI_WIN_CTEST_JOBS:-${PULP_CTEST_JOBS:-4}}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o IdentitiesOnly=yes -o BatchMode=yes)

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
command -v qemu-system-aarch64 >/dev/null 2>&1 || die "qemu not installed (brew install qemu)"
command -v qemu-img >/dev/null 2>&1 || die "qemu-img not found"

# Resolve once before reading the golden. This must not follow a mutable PATH
# entry when the provider is launched by launchd.
TARTCI_PYTHON="$(tartci_toml_python_path)" || die "no pinned Python 3.11+ interpreter for Windows golden admission"
export TARTCI_PYTHON

while [ $# -gt 0 ]; do case "$1" in
  --golden) GOLDEN="$2"; shift 2;;
  --ref) REF="$2"; shift 2;;
  --build-type) BUILD_TYPE="$2"; shift 2;;
  --gpu) GPU="on"; shift;;
  --no-gpu) GPU="off"; shift;;
  --ctest-args) CTEST_ARGS="$2"; shift 2;;
  --key) KEY="$2"; shift 2;;
  --smoke) SMOKE=1; shift;;
  --keep) KEEP=1; shift;;
  -h|--help) sed -n '2,30p' "$0"; exit 0;;
  *) die "unknown arg: $1";;
esac; done

case "$CTEST_JOBS" in
  ''|*[!0-9]*) die "invalid TARTCI_WIN_CTEST_JOBS='$CTEST_JOBS'";;
esac
[ "$CTEST_JOBS" -gt 0 ] || die "TARTCI_WIN_CTEST_JOBS must be greater than zero"
case "$GPU" in
  on|ON|true|TRUE|1) GPU=ON;;
  off|OFF|false|FALSE|0) GPU=OFF;;
  *) die "invalid TARTCI_WIN_GPU='$GPU' (on|off)";;
esac
case "$WIN_CPUS:$WIN_MEMORY_MB" in
  *[!0-9:]*|:*|*:) die "invalid Windows VM CPU/memory size";;
esac
[ "$WIN_CPUS" -gt 0 ] && [ "$WIN_MEMORY_MB" -gt 0 ] || die "Windows VM CPU/memory must be positive"
tartci_vm_leases_enabled || die "manual Windows clone requires a VM lease"

[ -f "$GOLDEN" ] || die "golden not found: $GOLDEN (set TARTCI_WIN_GOLDEN or --golden)"
nice -n 20 "$TARTCI_PYTHON" "$TARTCI_ROOT/scripts/verify_windows_golden.py" "$GOLDEN" \
  || die "Windows clone golden failed SHA-256 admission"

# Firmware code + a vars TEMPLATE, copied fresh into every job.
FW=""
for c in /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
         /Applications/UTM.app/Contents/Resources/qemu/edk2-aarch64-code.fd; do
  [ -f "$c" ] && FW="$c" && break
done
[ -n "$FW" ] || die "no edk2-aarch64-code.fd"
# A golden whose boot entry lives in its own UEFI variable store ships that
# store beside it; TARTCI_WIN_VARS_TEMPLATE names it and TARTCI_WIN_VARS_SHA256
# pins it. Every job copies it and verifies the copy before QEMU writes to it.
# Without it, the stock edk2 template serves a golden that auto-boots
# \EFI\BOOT\BOOTAA64.EFI.
VARS_TPL="${TARTCI_WIN_VARS_TEMPLATE:-}"
VARS_SHA256="${TARTCI_WIN_VARS_SHA256:-}"
if [ -n "$VARS_TPL" ]; then
  [ -n "$VARS_SHA256" ] || die "TARTCI_WIN_VARS_TEMPLATE requires TARTCI_WIN_VARS_SHA256"
  "$TARTCI_PYTHON" "$TARTCI_ROOT/scripts/windows_efi_vars.py" \
    --template "$VARS_TPL" --sha256 "$VARS_SHA256" --check >/dev/null \
    || die "EFI vars template failed its pinned SHA-256: $VARS_TPL"
else
  [ -z "$VARS_SHA256" ] || die "TARTCI_WIN_VARS_SHA256 is set without TARTCI_WIN_VARS_TEMPLATE"
  for v in /opt/homebrew/share/qemu/edk2-aarch64-vars.fd /opt/homebrew/share/qemu/edk2-arm-vars.fd; do [ -f "$v" ] && VARS_TPL="$v" && break; done
  [ -n "$VARS_TPL" ] || die "no edk2 vars template"
fi
copy_efi_vars(){ # $1=destination
  if [ -n "$VARS_SHA256" ]; then
    "$TARTCI_PYTHON" "$TARTCI_ROOT/scripts/windows_efi_vars.py" \
      --template "$VARS_TPL" --sha256 "$VARS_SHA256" --dest "$1" >/dev/null
  else
    cp "$VARS_TPL" "$1"
  fi
}

# Pick a free host port for SSH hostfwd (per-job; no fixed 2222 collision).
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
JOB="tartci-win-job-$$"
JOBDIR="$WORKROOT/$JOB"
OVERLAY="$JOBDIR/overlay.qcow2"
JOBBASE="$JOBDIR/golden.qcow2"
EFIVARS="$JOBDIR/efivars.fd"
STATE_DIR="$(tartci_provider_state_dir qemu-windows)"

QPID=""
QPID_STARTED_AT=""
JOBDIR_OWNED=0
LEASE_OWNED=0
STATE_WRITTEN=0
write_state(){
  TARTCI_STATE_REPO=Generous-Corp/pulp \
    TARTCI_STATE_SUPERVISOR_PID="$$" \
    TARTCI_STATE_SUPERVISOR_PID_STARTED_AT="$(tartci_pid_started_at "$$")" \
    TARTCI_STATE_WORK_DIR="$JOBDIR" \
    TARTCI_STATE_OVERLAY="$OVERLAY" \
    TARTCI_STATE_GOLDEN_SNAPSHOT="$JOBBASE" \
    TARTCI_STATE_QEMU_PID="$QPID" \
    TARTCI_STATE_QEMU_PID_STARTED_AT="$QPID_STARTED_AT" \
    tartci_write_vm_state qemu-windows "$JOB" "$JOB" "$1" ephemeral "$STATE_DIR"
  STATE_WRITTEN=1
}
qemu_running(){
  local state
  state="$(ps -p "$QPID" -o stat= 2>/dev/null | tr -d '[:space:]')"
  case "$state" in
    ''|Z*) return 1 ;;
    *) [ -n "$QPID_STARTED_AT" ] \
         && [ "$(tartci_pid_started_at "$QPID")" = "$QPID_STARTED_AT" ] ;;
  esac
}
cleanup(){
  local had_jobdir=0 qemu_alive=0 state
  if [ -n "$QPID" ]; then
    state="$(ps -p "$QPID" -o stat= 2>/dev/null | tr -d '[:space:]')"
    if [[ -n "$state" && "$state" != Z* ]] \
      && { [ -z "$QPID_STARTED_AT" ] \
           || [ "$(tartci_pid_started_at "$QPID")" != "$QPID_STARTED_AT" ]; }; then
      note "QEMU PID identity is unknown or changed; preserving job and lease for recovery"
      return 1
    fi
  fi
  if [ -n "$QPID" ] && [ -n "$state" ] && [[ "$state" != Z* ]]; then
    qemu_running && kill "$QPID" 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      qemu_running || break
      sleep 1
    done
    if qemu_running; then
      kill -KILL "$QPID" 2>/dev/null || true
      for _ in 1 2 3 4 5; do
        qemu_running || break
        sleep 1
      done
    fi
    if qemu_running; then
      qemu_alive=1
      note "QEMU pid $QPID is still alive; preserving its lease and overlay for recovery"
    else
      wait "$QPID" 2>/dev/null || true
    fi
  fi
  [ "$qemu_alive" = 0 ] || return 1
  if [ "$JOBDIR_OWNED" = 1 ] && [ -d "$JOBDIR" ]; then had_jobdir=1; fi
  if [ "$JOBDIR_OWNED" = 1 ] && [ -d "$JOBDIR" ]; then
    chflags nouchg "$JOBBASE" "$JOBBASE.sha256" 2>/dev/null || true
    if ! rm -r "$JOBDIR" || [ -e "$JOBDIR" ]; then
      note "could not remove clone directory $JOBDIR; preserving VM lease for recovery"
      return 1
    fi
  fi
  if [ "$LEASE_OWNED" = 1 ]; then
    tartci_release_vm_lease || return 1
  fi
  if [ "$STATE_WRITTEN" = 1 ]; then
    tartci_delete_vm_state "$JOB" "$STATE_DIR" || return 1
  fi
  if [ "$had_jobdir" = 1 ] && [ ! -d "$JOBDIR" ]; then
    note "discarded ephemeral clone $JOB"
  fi
  return 0
}
on_exit(){
  local rc=$?
  trap - EXIT
  cleanup || rc=1
  exit "$rc"
}
trap on_exit EXIT
if ! tartci_prepare_and_check_disk_root_observed "$WORKROOT" \
  "$(tartci_vm_lease_disk_expected_mount_path qemu-windows "$WORKROOT")" \
  "$(tartci_vm_lease_disk_expected_device_id qemu-windows)" \
  qemu-windows manual "$JOB"; then
  die "Windows clone work root is not admitted"
fi
if ! tartci_acquire_vm_lease "$JOB" "$WIN_CPUS" qemu-windows-vm vm \
  spectr-windows-manual "$WIN_MEMORY_MB" "$WORKROOT" qemu-windows manual "$JOB"; then
  die "Windows clone VM lease denied"
fi
LEASE_OWNED=1
mkdir "$JOBDIR" || die "Windows job directory already exists or cannot be created: $JOBDIR"
JOBDIR_OWNED=1
write_state preparing

"$TARTCI_ROOT/providers/qemu-windows/prepare-golden-snapshot.sh" "$GOLDEN" "$JOBBASE" \
  || die "Windows clone golden failed private snapshot admission"
note "CoW overlay off verified private snapshot → $OVERLAY"
tartci_vm_lease_guard_run qemu-img create -f qcow2 -b "$JOBBASE" -F qcow2 "$OVERLAY" >/dev/null
copy_efi_vars "$EFIVARS" || die "EFI vars copy failed its pinned SHA-256"

note "booting $JOB headless (ssh: 127.0.0.1:$PORT)"
tartci_vm_lease_guard_exec qemu-system-aarch64 \
  -name "$JOB" \
  -accel hvf -machine virt,highmem=on -cpu host -smp "${TARTCI_ACTIVE_VM_LEASE_CORES:-$WIN_CPUS}" -m "$WIN_MEMORY_MB" \
  -drive if=pflash,format=raw,readonly=on,file="$FW" \
  -drive if=pflash,format=raw,file="$EFIVARS" \
  -device ramfb -device qemu-xhci,id=usb -device usb-kbd -device usb-tablet \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$PORT-:22" -device virtio-net-pci,netdev=net0 \
  -drive file="$OVERLAY",if=none,id=nvm,format=qcow2 -device nvme,drive=nvm,serial=pulpwin \
  -display none >"$JOBDIR/qemu.log" 2>&1 & QPID=$!
QPID_STARTED_AT="$(tartci_pid_started_at "$QPID")"
write_state booting

wsh(){ ssh -n ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} -i "$KEY" -p "$PORT" "$WUSER@127.0.0.1" "$@"; }

note "waiting for SSH (Win boot ~2-4 min)…"
up=0; for _ in $(seq 1 150); do wsh 'echo ok' >/dev/null 2>&1 && { up=1; break; }; sleep 4; done
[ "$up" = 1 ] || { tail -5 "$JOBDIR/qemu.log" >&2 2>/dev/null || true; die "no SSH after ~10 min (see $JOBDIR/qemu.log)"; }
note "vm $JOB up — $(wsh 'cmd /c ver' 2>/dev/null | tr -d "\r")"

if [ "$SMOKE" = 1 ]; then
  note "smoke: toolchain probe"
  # A Windows `where a & where b` chain returns the status of the final
  # command, so the old probe could report success with missing tools.  Probe
  # every required tool and the architecture-specific MSVC environment, then
  # fail closed if any component is absent.
  PS_SMOKE='$ErrorActionPreference = "Stop"
$ninja = Get-Command ninja -ErrorAction SilentlyContinue
if (-not $ninja) {
  $ninja = Get-ChildItem @("C:\Program Files\Microsoft Visual Studio", "C:\VS2026BuildTools") -Recurse -Filter ninja.exe -File -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($ninja) { $env:PATH = $ninja.DirectoryName + ";" + $env:PATH }
}
$required = @("cmake", "ninja", "git", "python")
foreach ($name in $required) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "required tool missing: $name" }
  Write-Output ("{0}: {1}" -f $name, $cmd.Source)
}
$vcv = (Get-ChildItem "C:\Program Files\Microsoft Visual Studio" -Recurse -Filter vcvarsall.bat -ErrorAction SilentlyContinue | Where-Object {$_.FullName -match "BuildTools"} | Select-Object -First 1).FullName
if (-not $vcv) { throw "no vcvarsall.bat under BuildTools" }
$probe = cmd /c "`"$vcv`" arm64 && where cl && cl 2>&1"
if ($LASTEXITCODE -ne 0) { throw "MSVC arm64 probe failed (exit $LASTEXITCODE)" }
$probe | ForEach-Object { Write-Output $_ }
Write-Output "vcvarsall: $vcv"
Write-Output "smoke: required Windows ARM64 toolchain is present"'
  ENC="$(printf '%s' "$PS_SMOKE" | iconv -t UTF-16LE | base64)"
  set +e
  wsh "powershell -NoProfile -EncodedCommand $ENC" 2>&1 | tr -d '\r'
  RC=${PIPESTATUS[0]}
  set -e
  [ "$RC" -eq 0 ] || die "smoke toolchain probe failed (exit $RC)"
  note "smoke OK (overlay boot + complete ARM64 toolchain probe)"
  if [ "$KEEP" = 1 ]; then
    note "--keep: holding $JOB on SSH port $PORT until this command is stopped"
    wait "$QPID"
  fi
  exit 0
fi

# Build + test. Mirrors runbook §4.8. cmd.exe is the
# default OpenSSH shell on Windows; C:\tmp is required (tests map POSIX /tmp).
wsh 'cmd /c "if not exist C:\tmp mkdir C:\tmp"' >/dev/null 2>&1

note "clone/checkout pulp (ref=${REF:-origin/main})"
CO="${REF:-origin/main}"
wsh "cmd /c \"if not exist C:\\pulp git clone $SRC_REPO C:\\pulp\"" 2>&1 | tr -d '\r' || true
# `reset --hard` (not `checkout`): the golden's working tree carries autocrlf
# line-ending churn that makes a plain checkout abort ("local changes would be
# overwritten"). Reset discards that churn and moves to the ref deterministically.
wsh "cmd /c \"cd C:\\pulp && git config core.autocrlf false && git fetch --quiet origin && git reset --hard --quiet $CO\"" 2>&1 | tr -d '\r'
wsh "cmd /c \"cd C:\\pulp && git rev-parse --short HEAD\"" 2>&1 | tr -d '\r'

# Build + test under the MSVC arm64 env. Driven as base64-encoded PowerShell
# (runbook §4.7): a Windows path round-tripped through bash double-quotes gets
# mangled (the `\v` in `...\vcvarsall.bat` is eaten), and vswhere returns empty
# for a BuildTools-only install — so PowerShell discovers vcvarsall via
# Get-ChildItem and `call`s it inside one cmd chain (cl.exe is only on PATH after
# `vcvarsall arm64`). base64 is opaque to bash, dodging all the quoting hazards.
note "build + ctest (Release, GPU=$GPU) via MSVC arm64 — the long step"
PS_BUILD='$ProgressPreference = "SilentlyContinue"
$ninja = Get-Command ninja -ErrorAction SilentlyContinue
if (-not $ninja) {
  $ninja = Get-ChildItem @("C:\Program Files\Microsoft Visual Studio", "C:\VS2026BuildTools") -Recurse -Filter ninja.exe -File -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($ninja) { $env:PATH = $ninja.DirectoryName + ";" + $env:PATH }
}
$vcv = (Get-ChildItem "C:\Program Files\Microsoft Visual Studio" -Recurse -Filter vcvarsall.bat -ErrorAction SilentlyContinue | Where-Object {$_.FullName -match "BuildTools"} | Select-Object -First 1).FullName
if (-not $vcv) { Write-Error "no vcvarsall.bat under BuildTools"; exit 1 }
cmd /c "`"$vcv`" arm64 && cd C:\pulp && cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE='"$BUILD_TYPE"' -DPULP_ENABLE_GPU='"$GPU"' && cmake --build build && ctest --test-dir build --parallel '"$CTEST_JOBS"' '"$CTEST_ARGS"' > C:\tmp\tartci-ctest.log 2>&1"
$rc = $LASTEXITCODE
if (Test-Path "C:\tmp\tartci-ctest.log") {
  Write-Output "--- ctest tail (C:\\tmp\\tartci-ctest.log) ---"
  Get-Content "C:\tmp\tartci-ctest.log" -Tail 240
}
if ($rc -eq 0) {
  $scanner = "C:\pulp\build\tools\scan-worker\Release\pulp-scan-worker.exe"
  $clap = Get-ChildItem "C:\pulp\build\CLAP\Release" -Filter "*Allpass.clap" -File -ErrorAction SilentlyContinue | Select-Object -First 1
  $vst3 = Get-ChildItem "C:\pulp\build\VST3" -Recurse -Filter "*Allpass.vst3" -File -ErrorAction SilentlyContinue | Select-Object -First 1
  if ((Test-Path $scanner) -and $clap -and $vst3) {
    Write-Output "--- plugin scan evidence ---"
    & $scanner $clap.FullName
    if ($LASTEXITCODE -ne 0) { Write-Error "CLAP scan failed"; exit 1 }
    & $scanner $vst3.FullName
    if ($LASTEXITCODE -ne 0) { Write-Error "VST3 scan failed"; exit 1 }
    Write-Output "plugin scan: PASS (CLAP + VST3)"
  } else {
    Write-Output "plugin scan: SKIP (this checkout did not produce both Allpass formats)"
  }
}
exit $rc'
ENC="$(printf '%s' "$PS_BUILD" | iconv -t UTF-16LE | base64)"
set +e
wsh "powershell -NoProfile -EncodedCommand $ENC"
RC=$?
set -e
note "in-guest build+ctest exit=$RC"
if [ "$KEEP" = 1 ]; then
  note "--keep: holding $JOB on SSH port $PORT until this command is stopped"
  wait "$QPID"
fi
exit $RC
