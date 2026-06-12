#!/usr/bin/env bash
# optimize-golden.sh — run idempotent Windows CI golden checks/warmups before
# tagging a qcow2. Run this against the single-operator VM while it is booted;
# then run `tartci windows golden <name>` to shut down and snapshot it.
set -euo pipefail

KEY="${TARTCI_WIN_SSH_KEY:-$HOME/.ssh/id_ed25519}"
USER="${TARTCI_WIN_SSH_USER:-admin}"
PORT="${TARTCI_WIN_SSH_PORT:-2222}"
ARCHES="${TARTCI_WIN_VCVARS_ARCHES:-${TARTCI_WIN_VCVARS_ARCH:-arm64}}"
RUNNER_VERSION="${TARTCI_RUNNER_VERSION:-${PULP_RUNNER_VERSION:-2.335.1}}"

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 -o IdentitiesOnly=yes -o BatchMode=yes)
SSH=(ssh "${SSH_OPTS[@]}" -i "$KEY" -p "$PORT" "$USER@127.0.0.1")

note(){ printf '\033[36m• %s\033[0m\n' "$*" >&2; }

note "optimizing Windows golden over ssh 127.0.0.1:$PORT (vcvars arches: $ARCHES, runner: $RUNNER_VERSION)"

ps_script='$ErrorActionPreference = "Continue"
$arches = "'"$ARCHES"'".Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
$runnerVersion = "'"$RUNNER_VERSION"'"

Write-Output "TARTCI_OPT host=$env:COMPUTERNAME"
Write-Output "TARTCI_OPT arches=$($arches -join ",")"

New-Item -ItemType Directory -Force -Path "C:\tmp" | Out-Null
New-Item -ItemType Directory -Force -Path "C:\actions-runner" | Out-Null

$wantedPath = @(
  "C:\Program Files\Git\bin",
  "C:\Program Files\Git\usr\bin",
  "C:\ProgramData\chocolatey\bin",
  "C:\Program Files\ccache"
)
$machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$segments = @()
if ($machinePath) {
  $segments = $machinePath.Split(";") | Where-Object { $_ }
}
$changed = $false
foreach ($entry in $wantedPath) {
  if ((Test-Path $entry) -and -not ($segments -contains $entry)) {
    $segments += $entry
    $changed = $true
    Write-Output "TARTCI_OPT path-add=$entry"
  }
}
if ($changed) {
  [Environment]::SetEnvironmentVariable("Path", ($segments -join ";"), "Machine")
}
$env:Path = (($segments + $env:Path.Split(";")) | Where-Object { $_ } | Select-Object -Unique) -join ";"

Write-Output "TARTCI_OPT powershell-module-warmup=start"
Get-Module -ListAvailable Microsoft.PowerShell.Management,Microsoft.PowerShell.Utility,PackageManagement,PowerShellGet |
  Select-Object Name,Version,Path | Format-Table -AutoSize | Out-String | Write-Output
Write-Output "TARTCI_OPT powershell-module-warmup=done"

$missingCommands = @()
foreach ($cmd in @("git", "bash", "choco", "ccache")) {
  $found = Get-Command $cmd -ErrorAction SilentlyContinue
  if ($found) {
    Write-Output ("TARTCI_OPT command {0}={1}" -f $cmd, $found.Source)
  } else {
    Write-Output ("TARTCI_OPT command {0}=missing" -f $cmd)
    $missingCommands += $cmd
  }
}
if ($missingCommands.Count -gt 0) {
  Write-Output ("TARTCI_OPT missing-commands={0}" -f ($missingCommands -join ","))
  exit 1
}

$runnerDir = "C:\actions-runner"
$listener = Join-Path $runnerDir "bin\Runner.Listener.exe"
$currentVersion = ""
if (Test-Path $listener) {
  try {
    $currentVersion = (& $listener --version 2>$null | Select-Object -First 1).Trim()
  } catch {
    $currentVersion = ""
  }
}
if ($currentVersion -ne $runnerVersion) {
  Write-Output ("TARTCI_OPT runner-install current={0} wanted={1}" -f $currentVersion, $runnerVersion)
  Remove-Item -Recurse -Force $runnerDir -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $runnerDir | Out-Null
  $zip = Join-Path $env:TEMP ("actions-runner-win-arm64-" + $runnerVersion + ".zip")
  $url = "https://github.com/actions/runner/releases/download/v$runnerVersion/actions-runner-win-arm64-$runnerVersion.zip"
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri $url -OutFile $zip
  Expand-Archive -Path $zip -DestinationPath $runnerDir -Force
  Remove-Item $zip -Force -ErrorAction SilentlyContinue
} else {
  Write-Output ("TARTCI_OPT runner-current={0}" -f $currentVersion)
}
Remove-Item -Force (Join-Path $runnerDir ".runner") -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $runnerDir ".credentials") -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $runnerDir ".credentials_rsaparams") -ErrorAction SilentlyContinue

$vcvars = Get-ChildItem "C:\Program Files\Microsoft Visual Studio" -Recurse -Filter vcvarsall.bat -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -match "BuildTools" } |
  Select-Object -First 1 -ExpandProperty FullName
if (-not $vcvars) {
  Write-Output "TARTCI_OPT vcvars=missing"
  exit 1
}
Write-Output "TARTCI_OPT vcvars=$vcvars"

foreach ($arch in $arches) {
  $tmp = Join-Path $env:TEMP ("tartci-opt-vcvars-" + $arch + "-" + [guid]::NewGuid().ToString("N") + ".cmd")
  @(
    "@echo off",
    ("call ""{0}"" {1} >nul" -f $vcvars, $arch),
    "where cl",
    "cl /Bv"
  ) | Set-Content -Path $tmp -Encoding ASCII
  Write-Output "TARTCI_OPT vcvars-arch=$arch"
  cmd /c $tmp
  Remove-Item $tmp -Force -ErrorAction SilentlyContinue
}

Write-Output "TARTCI_OPT done=1"
'

tmp_remote='C:\actions-runner\tartci-optimize-golden.ps1'
upload_enc="$(printf '%s' '$ErrorActionPreference="Stop"
$p="'"$tmp_remote"'"
$script=[Console]::In.ReadToEnd()
Set-Content -LiteralPath $p -Value $script -Encoding UTF8
& powershell -NoProfile -ExecutionPolicy Bypass -File $p
exit $LASTEXITCODE' | iconv -t UTF-16LE | base64)"

printf '%s' "$ps_script" | "${SSH[@]}" "powershell -NoProfile -EncodedCommand $upload_enc"
