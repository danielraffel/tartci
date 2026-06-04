#!/usr/bin/env bash
# tartci repo lint — the single source of truth for repo hygiene, run both
# locally and by .github/workflows/ci.yml (which calls this exact script).
#
# Checks:
#   1. shell  — `bash -n` syntax + `shellcheck -S warning` on every shell
#               script (*.sh plus extensionless files with a bash shebang,
#               so the front-door `tartci` dispatcher is covered).
#   2. python — `py_compile` on every *.py (metrics/).
#   3. TOML   — every manifests/*.toml parses (skipped with a note if the
#               local python predates tomllib / 3.11).
#
# Portable to macOS's stock bash 3.2 (no `mapfile`, no associative arrays) —
# tartci's primary host is macOS, so `scripts/lint.sh` must run there too.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

fail=0
note() { printf '\033[36m• %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*"; }
bad()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; fail=1; }

# ── 1. Shell scripts ────────────────────────────────────────────────────────
# Discover *.sh plus any extensionless file whose first line is a bash shebang
# (3.2-safe: while-read into an array via `+=`, no mapfile).
# The two find branches are disjoint — `*.sh` (has a dot) can never match the
# extensionless `! -name '*.*'` branch — so no de-dup is needed. Read into the
# array with an explicit while-loop (3.2-safe; no `mapfile`, no SC2207
# command-substitution-into-array).
sh_files=()
while IFS= read -r f; do sh_files+=("$f"); done < <(
  {
    find . -path ./.git -prune -o -type f -name '*.sh' -print
    find . -path ./.git -prune -o -type f ! -name '*.*' -print | while IFS= read -r f; do
      head -1 "$f" 2>/dev/null | grep -qE '^#!.*\bbash\b' && printf '%s\n' "$f"
    done
  } | sort
)

note "Shell scripts: ${#sh_files[@]}"
have_shellcheck=1
command -v shellcheck >/dev/null 2>&1 || { have_shellcheck=0; bad "shellcheck not installed"; }
if [ "${#sh_files[@]}" -gt 0 ]; then
  for f in "${sh_files[@]}"; do
    bash -n "$f" 2>/dev/null || bad "bash -n: $f"
    if [ "$have_shellcheck" = 1 ]; then
      shellcheck -S warning "$f" || bad "shellcheck: $f"
    fi
  done
fi
[ "$fail" = 0 ] && ok "shell: bash -n + shellcheck -S warning clean"

# ── 2. Python ───────────────────────────────────────────────────────────────
py_files=()
while IFS= read -r f; do py_files+=("$f"); done < <(
  find . -path ./.git -prune -o -type f -name '*.py' -print | sort
)
if [ "${#py_files[@]}" -gt 0 ]; then
  if python3 -m py_compile "${py_files[@]}"; then
    ok "python: py_compile clean (${#py_files[@]} files)"
  else
    bad "python: py_compile failed"
  fi
fi

# ── 3. TOML manifests ───────────────────────────────────────────────────────
if ls manifests/*.toml >/dev/null 2>&1; then
  toml_rc=0
  python3 - <<'PY' || toml_rc=$?
import glob, sys
try:
    import tomllib
except ModuleNotFoundError:
    print("  (tomllib unavailable — python < 3.11; skipping TOML parse)")
    sys.exit(0)
errs = 0
for p in sorted(glob.glob("manifests/*.toml")):
    try:
        with open(p, "rb") as fh:
            tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        print(f"  TOML parse FAIL {p}: {exc}")
        errs += 1
sys.exit(1 if errs else 0)
PY
  if [ "$toml_rc" = 0 ]; then ok "manifests: all TOML parse"; else bad "manifests: TOML parse failed"; fi
fi

echo
[ "$fail" = 0 ] && ok "lint: all checks passed" || bad "lint: failures above"
exit "$fail"
