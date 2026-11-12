#!/usr/bin/env bash
# tartci repo lint — the single source of truth for repo hygiene, run both
# locally and by .github/workflows/ci.yml (which calls this exact script).
#
# Checks:
#   1. shell  — `bash -n` syntax + `shellcheck -S warning` on every shell
#               script (*.sh plus extensionless files with a bash shebang,
#               so the front-door `tartci` dispatcher is covered).
#   2. python — `py_compile` on every *.py.
#   3. TOML   — every manifests/*.toml and profiles/*.toml parses (skipped with a note if the
#               local python predates tomllib / 3.11).
#   4. public paths — no real home directory, volume or host name.
#   5. bash 3.2 arrays — no array expansion that macOS /bin/bash 3.2 aborts on
#               when the array is empty under `set -u`.
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
  for f in ${sh_files[@]+"${sh_files[@]}"}; do
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
  if python3 -m py_compile ${py_files[@]+"${py_files[@]}"}; then
    ok "python: py_compile clean (${#py_files[@]} files)"
  else
    bad "python: py_compile failed"
  fi
fi

# ── 3. TOML manifests ───────────────────────────────────────────────────────
toml_files=()
while IFS= read -r f; do toml_files+=("$f"); done < <(
  find manifests profiles -type f -name '*.toml' 2>/dev/null | sort
)
if [ "${#toml_files[@]}" -gt 0 ]; then
  toml_rc=0
  python3 - <<'PY' || toml_rc=$?
import pathlib, sys
try:
    import tomllib
except ModuleNotFoundError:
    print("  (tomllib unavailable — python < 3.11; skipping TOML parse)")
    sys.exit(0)
errs = 0
paths = sorted(list(pathlib.Path("manifests").glob("*.toml")) + list(pathlib.Path("profiles").glob("*.toml")))
for p in paths:
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

# ── 4. public paths ─────────────────────────────────────────────────────────
# A public repo carries no real home directory, external volume or host name.
if python3 scripts/public_path_lint.py; then
  ok "public paths: no real /Users, /Volumes or host names in tracked files"
else
  bad "public paths: real paths or host names above (scripts/public_path_lint.py)"
fi

# ── 5. bash 3.2 empty arrays ────────────────────────────────────────────────
# macOS /bin/bash 3.2 aborts on "${a[@]}" of an empty array under `set -u`;
# the bash on this lint host does not, so only a static check sees it.
if python3 scripts/bash32_array_lint.py; then
  ok "bash 3.2 arrays: every array expansion under set -u is guarded"
else
  bad "bash 3.2 arrays: unguarded expansions above (scripts/bash32_array_lint.py)"
fi

echo
[ "$fail" = 0 ] && ok "lint: all checks passed" || bad "lint: failures above"
exit "$fail"
