#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/bin" "$tmp/home"
cat >"$tmp/bin/sw_vers" <<'SH'
#!/usr/bin/env bash
printf '%s.0\n' "${OS_MAJOR:-26}"
SH
cat >"$tmp/bin/brew" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$BREW_CALLS"
if [ "$*" = "list cirruslabs/cli/tart" ]; then
  [ "${LEGACY_TART:-1}" = 1 ]
  exit $?
fi
if [ "$*" = "list cirruslabs/cli/softnet" ]; then
  [ "${LEGACY_SOFTNET:-0}" = 1 ]
  exit $?
fi
exit 1
SH
chmod +x "$tmp/bin/brew" "$tmp/bin/sw_vers"

if HOME="$tmp/home" BREW_CALLS="$tmp/brew.calls" \
  TARTCI_SW_VERS_BIN="$tmp/bin/sw_vers" PATH="$tmp/bin:/usr/bin:/bin" \
  "$repo_root/tartci" setup \
  >"$tmp/stdout" 2>"$tmp/stderr"; then
  echo "legacy Tart setup unexpectedly succeeded" >&2
  exit 1
fi

grep -q 'legacy cirruslabs/cli Tart or Softnet keg is installed' "$tmp/stderr"
if grep -q '^install ' "$tmp/brew.calls"; then
  echo "setup attempted an in-place Tart migration without an idle gate" >&2
  exit 1
fi

: >"$tmp/brew.calls"
if HOME="$tmp/home" BREW_CALLS="$tmp/brew.calls" \
  LEGACY_TART=0 LEGACY_SOFTNET=1 TARTCI_SW_VERS_BIN="$tmp/bin/sw_vers" \
  PATH="$tmp/bin:/usr/bin:/bin" \
  "$repo_root/tartci" setup >"$tmp/stdout" 2>"$tmp/stderr"; then
  echo "legacy Softnet-only setup unexpectedly succeeded" >&2
  exit 1
fi
grep -q 'legacy cirruslabs/cli Tart or Softnet keg is installed' "$tmp/stderr"
if grep -q '^install ' "$tmp/brew.calls"; then
  echo "setup ignored an installed legacy Softnet keg" >&2
  exit 1
fi

: >"$tmp/brew.calls"
cat >"$tmp/bin/tart" <<'SH'
#!/usr/bin/env bash
echo 'tart 2.32.1'
SH
chmod +x "$tmp/bin/tart"
HOME="$tmp/home" BREW_CALLS="$tmp/brew.calls" OS_MAJOR=14 \
  TARTCI_SW_VERS_BIN="$tmp/bin/sw_vers" PATH="$tmp/bin:/usr/bin:/bin" \
  "$repo_root/tartci" setup >"$tmp/stdout" 2>"$tmp/stderr"
grep -q 'retaining existing Tart on macOS 14' "$tmp/stderr"
if grep -q 'openai/tools' "$tmp/brew.calls"; then
  echo "macOS 14 setup attempted the Sequoia-only OpenAI channel" >&2
  exit 1
fi

rm "$tmp/bin/tart"
: >"$tmp/brew.calls"
if HOME="$tmp/home" BREW_CALLS="$tmp/brew.calls" OS_MAJOR=14 \
  LEGACY_TART=0 LEGACY_SOFTNET=0 TARTCI_SW_VERS_BIN="$tmp/bin/sw_vers" \
  PATH="$tmp/bin:/usr/bin:/bin" \
  "$repo_root/tartci" setup >"$tmp/stdout" 2>"$tmp/stderr"; then
  echo "fresh macOS 14 setup unexpectedly attempted an unsupported channel" >&2
  exit 1
fi
grep -q 'current Tart formulae require macOS 15+' "$tmp/stderr"
if grep -q '^install .*tart' "$tmp/brew.calls"; then
  echo "fresh macOS 14 setup attempted an incompatible Tart formula" >&2
  exit 1
fi

grep -q 'brew install openai/tools/tart' "$repo_root/docs/runbook.md"
grep -q 'brew install openai/tools/tart' "$repo_root/providers/tart-macos/run.sh"
grep -q 'brew install openai/tools/tart' "$repo_root/providers/tart-linux/run.sh"
grep -q 'brew fetch --force cirruslabs/cli/softnet cirruslabs/cli/tart' \
  "$repo_root/docs/runbook.md"
grep -q 'HOMEBREW_NO_AUTO_UPDATE=1 brew install' "$repo_root/docs/runbook.md"
if grep -q 'brew reinstall' "$repo_root/docs/runbook.md"; then
  echo "migration rollback still uses reinstall after uninstall" >&2
  exit 1
fi
grep -q 'immediate unload; NOT a drain' "$repo_root/docs/runbook.md"
grep -q "pgrep -fl 'tart run'" "$repo_root/docs/runbook.md"
canary_line="$(grep -n 'tartci serve macos --once --repo OWNER/REPO' \
  "$repo_root/docs/runbook.md" | cut -d: -f1)"
pool_on_line="$(grep -n 'tartci pool on' "$repo_root/docs/runbook.md" | \
  awk -F: -v canary="$canary_line" '$1 > canary { print $1; exit }')"
if [ -z "$canary_line" ] || [ -z "$pool_on_line" ] || \
   [ "$pool_on_line" -le "$canary_line" ]; then
  echo "pool participation resumes before the Tart VM canary" >&2
  exit 1
fi
if grep -R -qE 'finishes in-flight jobs|drains gracefully' \
  "$repo_root/README.md" "$repo_root/docs/runbook.md" \
  "$repo_root/providers/common/pool.lib.sh" "$repo_root/tartci"; then
  echo "pool off still claims to drain in-flight work" >&2
  exit 1
fi

echo "tart channel guard: ok"
