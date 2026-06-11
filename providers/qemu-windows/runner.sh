#!/usr/bin/env bash
# qemu-windows/runner.sh — ephemeral, per-job GitHub Actions runner on a QEMU
# WINDOWS VM. The pool-serving sibling of run.sh: where run.sh does ONE on-demand
# build+ctest in a CoW overlay and exits, this mints a JIT (single-job) runner
# config, makes a CoW overlay off the Windows golden qcow2 on a dynamic free SSH
# port, boots it, runs the Actions agent ONCE, then discards the overlay. The
# WORKFLOW (build.yml on GitHub) drives the build — the supervisor only supplies a
# clean VM per job. Reuses the validated boot mechanics from run.sh.
#
# Ported from Pulp's tools/ci/qemu-runner-windows.sh (the proven supervisor) into
# the project-agnostic tartci provider shape: repo/golden/labels are env-driven.
# Defaults target danielraffel/pulp (the first consumer); override for any repo.
#
# The runner agent (actions-runner-win-arm64) is installed into C:\actions-runner
# install-if-missing, so this works whether or not the golden has it pre-baked;
# baking it into the golden later just skips the per-job download.
#
# Pilot-safe by default: label `<repo>-build-windows` (NOT a required check).
#
# Usage:
#   providers/qemu-windows/runner.sh                 # one ephemeral job then exit (pilot)
#   providers/qemu-windows/runner.sh --loop          # keep serving (LaunchAgent uses this)
#   providers/qemu-windows/runner.sh --labels self-hosted,Windows,ARM64,pulp-build
set -euo pipefail

GOLDEN="${TARTCI_WIN_GOLDEN:-${TARTCI_GOLDENS:-$HOME/.tartci/goldens}/pulp-windows-build-24h2-arm64-2026-06-11.qcow2}"
KEY="${TARTCI_WIN_SSH_KEY:-$HOME/.ssh/id_ed25519}"
WUSER="${TARTCI_WIN_SSH_USER:-admin}"
REPO="${TARTCI_RUNNER_REPO:-${PULP_RUNNER_REPO:-danielraffel/pulp}}"
LABELS="${TARTCI_RUNNER_LABELS:-${PULP_RUNNER_LABELS:-self-hosted,Windows,ARM64,pulp-build-windows}}"
RUNNER_GROUP_ID="${TARTCI_RUNNER_GROUP_ID:-${PULP_RUNNER_GROUP_ID:-1}}"
RUNNER_VERSION="${TARTCI_RUNNER_VERSION:-${PULP_RUNNER_VERSION:-2.335.1}}"
VCVARS_ARCH="${TARTCI_WIN_VCVARS_ARCH:-${PULP_WIN_VCVARS_ARCH:-arm64}}"
WORKROOT="${TARTCI_WIN_WORK:-${TMPDIR:-/tmp}/tartci-win}"
LOGROOT="${TARTCI_WIN_LOGS:-${PULP_WIN_LOGS:-$WORKROOT/logs}}"
# Workflow name the --loop gate counts as "queued work". Override per repo.
WORKFLOW_NAME="${TARTCI_RUNNER_WORKFLOW_NAME:-Build and Test}"
# Ignore stale queued workflow shells by default. Without this guard, old queued
# runs with no matching self-hosted Windows job can keep waking QEMU forever.
MAX_QUEUED_AGE_SECONDS="${TARTCI_RUNNER_MAX_QUEUED_AGE_SECONDS:-${PULP_RUNNER_MAX_QUEUED_AGE_SECONDS:-21600}}"
KEEP_FAILED="${TARTCI_KEEP_FAILED:-${PULP_KEEP_FAILED:-0}}"
# By default, only boot when a fresh queued job's requested labels can be
# satisfied by this runner's labels. This keeps the supervisor safe while repo
# defaults still route Windows to GitHub-hosted `windows-latest`.
QUEUE_MATCH_LABELS="${TARTCI_RUNNER_QUEUE_MATCH_LABELS:-${PULP_RUNNER_QUEUE_MATCH_LABELS:-1}}"
LOOP=0; POLL="${TARTCI_VM_POLL:-${PULP_VM_POLL:-20}}"
IDLE_TIMEOUT="${TARTCI_RUNNER_IDLE_TIMEOUT_SECS:-${PULP_RUNNER_IDLE_TIMEOUT_SECS:-900}}"
HOST_SLUG="$(hostname -s 2>/dev/null || hostname)"
HOST_SLUG="$(printf '%s' "$HOST_SLUG" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-*//;s/-*$//')"
RUNNER_NAME_PREFIX="${TARTCI_RUNNER_NAME_PREFIX:-${PULP_RUNNER_NAME_PREFIX:-win-ephr-${HOST_SLUG:-host}}}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o IdentitiesOnly=yes -o BatchMode=yes)

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }
die(){ printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
now_epoch(){ date +%s; }
elapsed(){ awk -v start="$1" -v end="$2" 'BEGIN { printf "%.1f", end - start }'; }
prefix_guest_log(){ [ -f "$1" ] && LC_ALL=C sed 's/^/[guest] /' "$1" >&2 || true; }
command -v qemu-system-aarch64 >/dev/null 2>&1 || die "qemu not installed"
command -v gh >/dev/null 2>&1 || die "gh not installed / authed (need admin to mint JIT)"

while [ $# -gt 0 ]; do case "$1" in
  --loop) LOOP=1; shift;;
  --once) LOOP=0; shift;;
  --golden) GOLDEN="$2"; shift 2;;
  --labels) LABELS="$2"; shift 2;;
  --repo) REPO="$2"; shift 2;;
  -h|--help) sed -n '2,30p' "$0"; exit 0;;
  *) die "unknown arg: $1";;
esac; done

[ -f "$GOLDEN" ] || die "golden not found: $GOLDEN (set TARTCI_WIN_GOLDEN)"
FW=""; for c in /opt/homebrew/share/qemu/edk2-aarch64-code.fd /Applications/UTM.app/Contents/Resources/qemu/edk2-aarch64-code.fd; do [ -f "$c" ] && FW="$c" && break; done
[ -n "$FW" ] || die "no edk2-aarch64-code.fd"
VARS_TPL=""; for v in /opt/homebrew/share/qemu/edk2-aarch64-vars.fd /opt/homebrew/share/qemu/edk2-arm-vars.fd; do [ -f "$v" ] && VARS_TPL="$v" && break; done
[ -n "$VARS_TPL" ] || die "no edk2 vars template"
case "$MAX_QUEUED_AGE_SECONDS" in ''|*[!0-9]*) MAX_QUEUED_AGE_SECONDS=21600;; esac

delete_runner_registration(){
  local name="$1" ids id tries=0
  while [ "$tries" -lt 6 ]; do
    tries=$((tries + 1))
    ids="$(gh api "repos/$REPO/actions/runners" --paginate \
      --jq ".runners[] | select(.name==\"$name\" and .busy==false) | .id" 2>/dev/null || true)"
    if [ -n "$ids" ]; then
      for id in $ids; do
        note "deleting stale runner registration name=$name id=$id"
        gh api -X DELETE "repos/$REPO/actions/runners/$id" >/dev/null 2>&1 || true
      done
    fi
    ids="$(gh api "repos/$REPO/actions/runners" --paginate \
      --jq ".runners[] | select(.name==\"$name\") | .id" 2>/dev/null || true)"
    [ -z "$ids" ] && return 0
    sleep 2
  done
}

queued_work(){
  python3 - "$REPO" "$WORKFLOW_NAME" "$MAX_QUEUED_AGE_SECONDS" "$LABELS" "$QUEUE_MATCH_LABELS" <<'PY' 2>/dev/null || echo 0
import datetime as dt
import json
import subprocess
import sys

repo, workflow_name, max_age_raw, labels_csv, match_labels_raw = sys.argv[1:6]
try:
    max_age = int(max_age_raw)
except ValueError:
    max_age = 21600
wanted = {label.strip().lower() for label in labels_csv.split(",") if label.strip()}
match_labels = match_labels_raw.strip().lower() not in {"0", "false", "no"}
now = dt.datetime.now(dt.timezone.utc)

def gh(path):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True))

def is_fresh(created_at):
    if max_age <= 0:
        return True
    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now - created).total_seconds() <= max_age

runs = []
seen = set()
for status in ("queued", "in_progress"):
    for run in gh(f"repos/{repo}/actions/runs?status={status}&per_page=100").get("workflow_runs", []):
        run_id = run.get("id")
        if run_id in seen:
            continue
        seen.add(run_id)
        runs.append(run)

count = 0
for run in runs:
    if run.get("name") != workflow_name:
        continue
    jobs = gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100").get("jobs", [])
    for job in jobs:
        if job.get("status") != "queued":
            continue
        job_freshness_ts = (
            job.get("created_at")
            or job.get("started_at")
            or run.get("updated_at")
            or run.get("created_at")
            or ""
        )
        if not is_fresh(job_freshness_ts):
            continue
        labels = {str(label).lower() for label in job.get("labels", [])}
        if match_labels and (not labels or not labels.issubset(wanted)):
            continue
        if not match_labels or labels:
            count += 1
print(count)
PY
}

run_one(){ # $1=iteration index
  local i="$1" jit job="${RUNNER_NAME_PREFIX}-$$-$1"
  local t_start t_booted t_preflight t_runner_done t_done
  t_start="$(now_epoch)"
  note "[$i] minting JIT runner config (labels=$LABELS, ephemeral)"
  local label_args=(); local l; IFS=',' read -ra _ls <<< "$LABELS"
  for l in "${_ls[@]}"; do label_args+=(-f "labels[]=$l"); done
  jit="$(gh api -X POST "repos/$REPO/actions/runners/generate-jitconfig" \
        -f "name=$job" -F "runner_group_id=$RUNNER_GROUP_ID" "${label_args[@]}" \
        --jq '.encoded_jit_config')" || die "JIT mint failed (need repo admin)"
  [ -n "$jit" ] || die "empty JIT config"

  local port jobdir logdir overlay efivars qpid
  port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
  jobdir="$WORKROOT/$job"; mkdir -p "$jobdir"
  logdir="$LOGROOT/$job"; mkdir -p "$logdir"
  overlay="$jobdir/overlay.qcow2"; efivars="$jobdir/efivars.fd"

  note "[$i] CoW overlay off $(basename "$GOLDEN") + boot (ssh 127.0.0.1:$port)"
  qemu-img create -f qcow2 -b "$GOLDEN" -F qcow2 "$overlay" >/dev/null
  cp "$VARS_TPL" "$efivars"
  qemu-system-aarch64 -name "$job" -accel hvf -machine virt,highmem=on -cpu host -smp 8 -m 8192 \
    -drive if=pflash,format=raw,readonly=on,file="$FW" -drive if=pflash,format=raw,file="$efivars" \
    -device ramfb -device qemu-xhci,id=usb -device usb-kbd -device usb-tablet \
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$port-:22" -device virtio-net-pci,netdev=net0 \
    -drive file="$overlay",if=none,id=nvm,format=qcow2 -device nvme,drive=nvm,serial=pulpwin \
    -display none >"$logdir/qemu.log" 2>&1 & qpid=$!

  cleanup_job(){
    local outcome="${1:-success}"
    delete_runner_registration "$job" || true
    if [ "$outcome" = "failure" ] && [ "$KEEP_FAILED" = 1 ]; then
      note "[$i] keeping failed VM for inspection: job=$job qemu_pid=$qpid ssh_port=$port dir=$jobdir"
      return 0
    fi
    note "[$i] host diagnostics: $logdir"
    kill "$qpid" 2>/dev/null || true
    sleep 1
    rm -rf "$jobdir"
  }

  wsh(){ ssh "${SSH_OPTS[@]}" -i "$KEY" -p "$port" "$WUSER@127.0.0.1" "$@"; }
  # Wait for SSH, but bail the moment QEMU dies — that's how a free-port TOCTOU
  # (another process grabbed $port between the probe close and QEMU's bind)
  # surfaces: qemu exits instantly. Without this check the wait would burn the
  # full ~10min before failing. Caller (--loop) retries with a fresh port.
  local up=0 qemu_died=0; local _
  for _ in $(seq 1 150); do
    kill -0 "$qpid" 2>/dev/null || { qemu_died=1; note "[$i] qemu exited early (well before the SSH window) — port $port likely grabbed (TOCTOU); see $logdir/qemu.log"; break; }
    wsh 'echo ok' >/dev/null 2>&1 && { up=1; break; }; sleep 4
  done
  if [ "$up" != 1 ]; then
    # qemu-death already logged the accurate cause above; only emit the generic
    # "waited the full window" message when QEMU stayed up but no SSH.
    [ "$qemu_died" = 1 ] || note "[$i] no SSH after ~10min (qemu alive but unreachable; see $logdir/qemu.log)"
    cleanup_job failure; return 1
  fi
  t_booted="$(now_epoch)"
  note "[$i] vm $job up — ensure runner version + run JIT agent (one job)"
  local host_utc enc_clock
  host_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  enc_clock="$(printf '%s' '$hostUtc="'"$host_utc"'"
try {
  Set-Date -Date ([DateTimeOffset]::Parse($hostUtc).LocalDateTime) | Out-Null
  Write-Output "TARTCI_DIAG early-clock-sync=$hostUtc"
} catch {
  Write-Output "TARTCI_DIAG early-clock-sync-failed=$($_.Exception.Message)"
}' | iconv -t UTF-16LE | base64)"
  mkdir -p "$logdir"
  wsh "powershell -NoProfile -EncodedCommand $enc_clock" >"$logdir/early-clock.log" 2>&1 \
    || note "[$i] early clock sync failed"

  # The runner agent + JIT run, in three small ssh calls. The JIT blob is
  # multi-KB; it must NEVER ride a command line — embedding it in a PowerShell
  # -EncodedCommand or passing it as a cmd arg blows cmd.exe's 8191-char limit
  # through the ssh→cmd→powershell chain ("The command line is too long").
  # So: (1) ensure the agent binary version [no blob], (2) STREAM the blob into a
  # file via ssh STDIN [unbounded], (3) run the agent reading that file [no blob].
  local enc_install enc_preflight enc_run enc_after
  enc_install="$(printf '%s' '$ProgressPreference="SilentlyContinue"
$dir="C:\actions-runner"
$runnerVersion="'"$RUNNER_VERSION"'"
$listener="$dir\bin\Runner.Listener.exe"
$currentVersion=""
if (Test-Path $listener) {
  try { $currentVersion = ((& $listener --version 2>$null | Select-Object -First 1).Trim()) } catch { $currentVersion = "" }
}
if ($currentVersion -ne $runnerVersion) {
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $dir
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $url="https://github.com/actions/runner/releases/download/v$runnerVersion/actions-runner-win-arm64-$runnerVersion.zip"
  Invoke-WebRequest -Uri $url -OutFile "$dir\r.zip"
  Expand-Archive -Path "$dir\r.zip" -DestinationPath $dir -Force
  Remove-Item "$dir\r.zip"
}
# Goldens may carry stale runner registration files from an older proof. JIT
# configs are single-use; leave only the runner binaries before each fresh boot.
Remove-Item -Force -ErrorAction SilentlyContinue "$dir\.runner","$dir\.credentials","$dir\.credentials_rsaparams","$dir\.env","$dir\.path","$dir\jit.cfg"
# Integrity gate: the agent binary must exist after install. The download is
# over authenticated HTTPS and Expand-Archive rejects a corrupt/truncated zip,
# but this catches a partial extract loudly rather than failing opaquely at run.
if (-not (Test-Path "$dir\bin\Runner.Listener.exe")) { Write-Error "Runner.Listener.exe missing after install (corrupt/truncated download?)"; exit 1 }' | iconv -t UTF-16LE | base64)"
  wsh "powershell -NoProfile -EncodedCommand $enc_install" \
    || { note "[$i] runner install failed"; cleanup_job failure; return 1; }

  # (2) stream the JIT config in via stdin → file (no command-line length limit).
  # Guard the pipeline: under `set -euo pipefail` a dropped SSH / PowerShell error
  # here would otherwise exit the whole supervisor BEFORE the cleanup below,
  # leaking the QEMU process + overlay for a launchd --loop runner to trip over.
  printf '%s' "$jit" | wsh "powershell -NoProfile -Command \"[Console]::In.ReadToEnd() | Out-File -FilePath C:\\actions-runner\\jit.cfg -Encoding ascii -NoNewline\"" \
    || { note "[$i] JIT config upload failed — discarding overlay"; cleanup_job failure; return 1; }

  # The JIT token is time-sensitive. QEMU Windows overlays can wake with stale
  # clocks, so sync the throwaway guest to the host UTC and emit lightweight
  # reachability diagnostics before the agent tries to create its session.
  host_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  enc_preflight="$(printf '%s' '$ErrorActionPreference="Continue"
$ProgressPreference="SilentlyContinue"
$hostUtc="'"$host_utc"'"
try {
  Set-Date -Date ([DateTimeOffset]::Parse($hostUtc).LocalDateTime) | Out-Null
  Write-Output "TARTCI_DIAG clock-sync=$hostUtc"
} catch {
  Write-Output "TARTCI_DIAG clock-sync-failed=$($_.Exception.Message)"
}
try {
  Set-ExecutionPolicy -Scope LocalMachine -ExecutionPolicy RemoteSigned -Force
  Write-Output "TARTCI_DIAG execution-policy-localmachine=RemoteSigned"
} catch {
  Write-Output "TARTCI_DIAG execution-policy-failed=$($_.Exception.Message)"
}
try {
  Get-ExecutionPolicy -List | ForEach-Object { Write-Output ("TARTCI_DIAG execution-policy {0}={1}" -f $_.Scope, $_.ExecutionPolicy) }
} catch {
  Write-Output "TARTCI_DIAG execution-policy-list-failed=$($_.Exception.Message)"
}
Write-Output ("TARTCI_DIAG guest-time=" + (Get-Date -Format o))
$runnerPathAdd = @(
  "C:\Program Files\Git\cmd",
  "C:\Program Files\Git\bin",
  "C:\Program Files\Git\usr\bin",
  "C:\ProgramData\chocolatey\bin"
)
$env:Path = (($runnerPathAdd + @($env:Path -split ";")) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique) -join ";"
foreach ($cmd in @("git", "bash", "choco", "ccache")) {
  $found = Get-Command $cmd -ErrorAction SilentlyContinue
  if ($found) {
    Write-Output ("TARTCI_DIAG command {0}={1}" -f $cmd, $found.Source)
  } else {
    Write-Output ("TARTCI_DIAG command {0}=missing" -f $cmd)
  }
}
$vcvarsArch="'"$VCVARS_ARCH"'"
$vcvars = Get-ChildItem "C:\Program Files\Microsoft Visual Studio" -Recurse -Filter vcvarsall.bat -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match "BuildTools" } | Select-Object -First 1 -ExpandProperty FullName
if ($vcvars) {
  Write-Output ("TARTCI_DIAG vcvars={0} arch={1}" -f $vcvars, $vcvarsArch)
  $tmp = Join-Path $env:TEMP ("tartci-vcvars-" + [guid]::NewGuid().ToString("N") + ".cmd")
  try {
    "@echo off",("call ""{0}"" {1} >nul" -f $vcvars, $vcvarsArch),"set" | Set-Content -Path $tmp -Encoding ASCII
    $lines = & cmd.exe /d /c $tmp
    foreach ($line in $lines) {
      if ($line -match "^(.*?)=(.*)$") {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
      }
    }
  } finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $tmp
  }
  $cl = Get-Command cl -ErrorAction SilentlyContinue
  if ($cl) { Write-Output ("TARTCI_DIAG command cl={0}" -f $cl.Source) } else { Write-Output "TARTCI_DIAG command cl=missing-after-vcvars" }
} else {
  Write-Output "TARTCI_DIAG vcvars=missing"
}
try {
  w32tm /query /status | ForEach-Object { Write-Output ("TARTCI_DIAG w32tm " + $_) }
} catch {
  Write-Output "TARTCI_DIAG w32tm-failed=$($_.Exception.Message)"
}
foreach ($target in @("github.com", "broker.actions.githubusercontent.com")) {
  try {
    $tcp = Test-NetConnection $target -Port 443 -WarningAction SilentlyContinue
    Write-Output ("TARTCI_DIAG tcp-443 {0}={1}" -f $target, $tcp.TcpTestSucceeded)
  } catch {
    Write-Output ("TARTCI_DIAG tcp-443 {0}=error:{1}" -f $target, $_.Exception.Message)
  }
}
try {
  $resp = Invoke-WebRequest -Uri "https://github.com" -Method Head -UseBasicParsing -TimeoutSec 20
  Write-Output ("TARTCI_DIAG github-head-status={0}" -f $resp.StatusCode)
} catch {
  Write-Output "TARTCI_DIAG github-head-failed=$($_.Exception.Message)"
}
$listener="C:\actions-runner\bin\Runner.Listener.exe"
if (Test-Path $listener) {
  try { Write-Output ("TARTCI_DIAG listener-version=" + ((& $listener --version 2>$null | Select-Object -First 1).Trim())) } catch { Write-Output "TARTCI_DIAG listener-version-failed=$($_.Exception.Message)" }
}
$jitPath="C:\actions-runner\jit.cfg"
  if (Test-Path $jitPath) {
  Write-Output ("TARTCI_DIAG jit-cfg-bytes=" + (Get-Item $jitPath).Length)
}' | iconv -t UTF-16LE | base64)"
  mkdir -p "$logdir"
  wsh "powershell -NoProfile -EncodedCommand $enc_preflight" >"$logdir/preflight.log" 2>&1 \
    || note "[$i] preflight diagnostics failed"
  t_preflight="$(now_epoch)"
  prefix_guest_log "$logdir/preflight.log"

  # (3) run the agent reading the jit FILE — small PS, no blob on the wire.
  # Use Runner.Listener.exe directly (not run.cmd) so the huge JIT config is not
  # expanded through cmd.exe's 8191-character command-line limit.
  enc_run="$(printf '%s' '$runnerPathAdd = @(
  "C:\Program Files\Git\cmd",
  "C:\Program Files\Git\bin",
  "C:\Program Files\Git\usr\bin",
  "C:\ProgramData\chocolatey\bin"
)
$env:Path = (($runnerPathAdd + @($env:Path -split ";")) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique) -join ";"
$vcvarsArch="'"$VCVARS_ARCH"'"
$vcvars = Get-ChildItem "C:\Program Files\Microsoft Visual Studio" -Recurse -Filter vcvarsall.bat -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match "BuildTools" } | Select-Object -First 1 -ExpandProperty FullName
if ($vcvars) {
  Write-Output ("TARTCI_DIAG runner_vcvars={0} arch={1}" -f $vcvars, $vcvarsArch)
  $tmp = Join-Path $env:TEMP ("tartci-vcvars-" + [guid]::NewGuid().ToString("N") + ".cmd")
  try {
    "@echo off",("call ""{0}"" {1} >nul" -f $vcvars, $vcvarsArch),"set" | Set-Content -Path $tmp -Encoding ASCII
    $lines = & cmd.exe /d /c $tmp
    foreach ($line in $lines) {
      if ($line -match "^(.*?)=(.*)$") {
        [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
      }
    }
  } finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $tmp
  }
  $cl = Get-Command cl -ErrorAction SilentlyContinue
  if ($cl) { Write-Output ("TARTCI_DIAG runner_command cl={0}" -f $cl.Source) } else { Write-Output "TARTCI_DIAG runner_command cl=missing-after-vcvars" }
} else {
  Write-Output "TARTCI_DIAG runner_vcvars=missing"
}
Set-Location C:\actions-runner
& "C:\actions-runner\bin\Runner.Listener.exe" run --jitconfig (Get-Content "C:\actions-runner\jit.cfg")
exit $LASTEXITCODE' | iconv -t UTF-16LE | base64)"
  local run_status=0
  local runner_output="$logdir/runner-output.log"
  local runner_pid runner_start runner_assigned=0 runner_timed_out=0 now idle_elapsed
  mkdir -p "$logdir"
  wsh "powershell -NoProfile -EncodedCommand $enc_run" >"$runner_output" 2>&1 &
  runner_pid=$!
  runner_start="$(now_epoch)"
  while kill -0 "$runner_pid" 2>/dev/null; do
    if [ "$runner_assigned" = 0 ] && grep -q 'Running job:' "$runner_output" 2>/dev/null; then
      runner_assigned=1
    fi
    if [ "$runner_assigned" = 0 ]; then
      now="$(now_epoch)"
      idle_elapsed=$((now - runner_start))
      if [ "$idle_elapsed" -ge "$IDLE_TIMEOUT" ]; then
        runner_timed_out=1
        note "[$i] runner idle timeout after ${idle_elapsed}s without claiming a job"
        kill "$runner_pid" 2>/dev/null || true
        break
      fi
    fi
    sleep 5
  done
  wait "$runner_pid" || run_status=$?
  if [ "$runner_timed_out" = 1 ]; then
    run_status=124
  elif [ "$run_status" -ne 0 ]; then
    note "[$i] runner exited non-zero (job failure or no job)"
  fi
  t_runner_done="$(now_epoch)"
  prefix_guest_log "$runner_output"
  if grep -qi 'runner registration has been deleted\|Failed to create a session' "$runner_output"; then
    run_status=1
    note "[$i] runner session failed before claiming a job"
  fi

  enc_after="$(printf '%s' '$ErrorActionPreference="Continue"
$diagDir="C:\actions-runner\_diag"
if (Test-Path $diagDir) {
  Get-ChildItem $diagDir -Filter "*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 3 | ForEach-Object {
    Write-Output ("TARTCI_DIAG runner-log=" + $_.FullName)
    Get-Content $_.FullName -Tail 140
  }
} else {
  Write-Output "TARTCI_DIAG no-runner-diag-dir"
}' | iconv -t UTF-16LE | base64)"
  mkdir -p "$logdir"
  wsh "powershell -NoProfile -EncodedCommand $enc_after" >"$logdir/runner-diag.log" 2>&1 || true
  t_done="$(now_epoch)"
  prefix_guest_log "$logdir/runner-diag.log"
  {
    printf 'phase\tseconds\n'
    printf 'boot_to_ssh\t%s\n' "$(elapsed "$t_start" "$t_booted")"
    printf 'preflight\t%s\n' "$(elapsed "$t_booted" "$t_preflight")"
    printf 'runner_process\t%s\n' "$(elapsed "$t_preflight" "$t_runner_done")"
    printf 'post_diag\t%s\n' "$(elapsed "$t_runner_done" "$t_done")"
    printf 'total\t%s\n' "$(elapsed "$t_start" "$t_done")"
  } >"$logdir/timing.tsv"
  note "[$i] timing: boot=$(elapsed "$t_start" "$t_booted")s preflight=$(elapsed "$t_booted" "$t_preflight")s runner=$(elapsed "$t_preflight" "$t_runner_done")s total=$(elapsed "$t_start" "$t_done")s"

  if [ "$run_status" -ne 0 ]; then
    cleanup_job failure
    return "$run_status"
  else
    note "[$i] discarding ephemeral overlay $job"
    cleanup_job success
  fi
}

i=0
if [ "$LOOP" = 1 ]; then
  note "ephemeral Windows runner LOOP (Ctrl-C to stop); golden=$(basename "$GOLDEN") labels=$LABELS maxQueuedAge=${MAX_QUEUED_AGE_SECONDS}s queueMatchLabels=$QUEUE_MATCH_LABELS"
  while true; do
    q="$(queued_work)"
    if [ "${q:-0}" -gt 0 ]; then
      i=$((i+1)); note "[$i] queued=$q → booting ephemeral Windows VM"; run_one "$i" || true
    else
      note "waiting ${POLL}s (queued=$q — no '$WORKFLOW_NAME' work)"; sleep "$POLL"
    fi
  done
else
  note "ephemeral Windows runner ONCE; golden=$(basename "$GOLDEN") labels=$LABELS"
  run_one 1
fi
