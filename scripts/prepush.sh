#!/usr/bin/env bash
# Catch what broke agent PRs before CI does. CI is the full run; this is the
# quick local check.
#
#   scripts/prepush.sh          fast (target: under 3 minutes on an idle host)
#   scripts/prepush.sh --full   everything the lint and python-39-tests jobs run
#
# Fast mode:
#   lint            ./scripts/lint.sh (shellcheck, bash -n, py_compile, TOML,
#                   public-path lint)
#   python-39-tests the interpreter is a tomllib-less 3.9
#   python-39-tests py_compile of every tracked .py under 3.9
#   python-floor    py_compile under python3.11 (skipped with a note if absent)
#   python-39-tests every test module imports under 3.9 (catches an unguarded
#                   tomllib import in a test module)
#   python-39-tests under that 3.9, with it first on PATH as CI does: the 3.9
#                   skip-allowlist test (test_system_python_tests_run, which
#                   which pins which tests may skip without tomllib), the leak-needles
#                   test, the public-path lint test, and only the unit-test
#                   modules that changed against origin/main plus those that
#                   import a changed module (scripts/prepush_select.py)
#
# --full adds what the fast mode skips: the whole unit-test suite under 3.9, the
# whole suite and tests/test_*.sh under the default python3 (concurrently).
# If the default python3 IS the 3.9, that "lint" run repeats the 3.9 run and a
# warning says so.
#
# scripts/test_prepush_matches_ci.py fails when a command in the lint or
# python-39-tests job is missing from the full run, or when the fast mode stops
# covering a class of failure it exists to catch. Bash 3.2 safe.
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

PY39="${TARTCI_PREPUSH_PY39:-/usr/bin/python3}"
BASE="${TARTCI_PREPUSH_BASE:-origin/main}"
total_start=$SECONDS
FULL=0
case "${1:-}" in
  "") ;;
  --full) FULL=1 ;;
  *) echo "usage: scripts/prepush.sh [--full]" >&2; exit 2 ;;
esac

stage() { # stage <ci-job> <description> <command...>
  local job="$1" desc="$2"; shift 2
  local start=$SECONDS
  printf '\033[36m• [%s] %s\033[0m\n' "$job" "$desc"
  if "$@"; then
    printf '\033[32m✓ [%s] %s (%ss)\033[0m\n' "$job" "$desc" $((SECONDS - start))
  else
    printf '\033[31m✗ FAILED: CI job "%s" would be red (%s)\033[0m\n' "$job" "$desc" >&2
    exit 1
  fi
}

shell_tests() {
  local test_script rc
  for test_script in tests/test_*.sh; do
    bash "$test_script" || { rc=$?; echo "$test_script exited $rc" >&2; return 1; }
  done
}

require_py39() {
  [ -x "$PY39" ] || { echo "$PY39 not found; CI's python-39-tests job runs 3.9" >&2; return 1; }
  "$PY39" --version
  "$PY39" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 9) else 1)' \
    || { echo "$PY39 is not Python 3.9; CI's python-39-tests job runs 3.9" >&2; return 1; }
  if "$PY39" -c 'import tomllib' 2>/dev/null; then
    echo "$PY39 imports tomllib, so it is not the tomllib-less 3.9 CI runs." >&2
    return 1
  fi
}

py_floor() {
  local py311
  py311="$(command -v python3.11 || true)"
  if [ -z "$py311" ]; then
    echo "python3.11 not installed: python-floor not checked locally (CI will)"
    return 0
  fi
  git ls-files -z '*.py' | xargs -0 "$py311" -m py_compile
}

py_compile_39() {
  git ls-files -z '*.py' | xargs -0 "$PY39" -m py_compile
}

# Every test module must import under the 3.9: an unguarded `import tomllib`
# (or other 3.11-only name) in one fails the whole CI discover run. Loading, not
# running, the tests is enough to surface it; a failed import loads as a
# _FailedTest whose id names the module.
test_modules_import_under_39() {
  PYTHONPATH=scripts "$PY39" -c '
import sys, unittest
suite = unittest.defaultTestLoader.discover("scripts", pattern="test_*.py")
bad = []
def walk(s):
    for t in s:
        if isinstance(t, unittest.TestSuite):
            walk(t)
        elif type(t).__name__ == "_FailedTest":
            bad.append(t.id())
walk(suite)
if bad:
    print("test modules that fail to import under Python %d.%d:" % sys.version_info[:2], file=sys.stderr)
    for b in bad:
        print("  " + b, file=sys.stderr)
    raise SystemExit(1)
print("all test modules import under Python %d.%d" % sys.version_info[:2])
'
}

# Run the fast unit-test selection under the CI-like 3.9 (3.9 first on PATH),
# then the changed modules once more under the default python3 when that is a
# different interpreter, so tests that need tomllib are not skipped everywhere.
fast_unit_tests() {
  local selected m
  selected="$(python3 scripts/prepush_select.py "$BASE")" || {
    echo "test selection failed (see above); refusing to pass on the fixed modules alone" >&2
    return 1
  }
  local mods=() chosen=()
  mods+=(test_system_python_tests_run test_leak_needles test_public_path_lint)
  while IFS= read -r m; do
    [ -n "$m" ] && chosen+=("$m")
  done <<SELECTED_END
$selected
SELECTED_END
  # De-duplicate, keeping order (bash 3.2: no associative arrays).
  local uniq=() seen=" "
  for m in "${mods[@]}" ${chosen[@]+"${chosen[@]}"}; do  # bash32-lint: nonempty seeded with three fixed modules
    case "$seen" in *" $m "*) continue ;; esac
    seen="$seen$m "; uniq+=("$m")
  done
  echo "unit-test modules: ${uniq[*]}"  # bash32-lint: nonempty one per fixed module
  mkdir -p "$logdir/bin39" && ln -sf "$PY39" "$logdir/bin39/python3"
  PATH="$logdir/bin39:$PATH" PYTHONPATH=scripts "$PY39" -m unittest "${uniq[@]}"  # bash32-lint: nonempty one per fixed module
  if [ "${#chosen[@]}" -gt 0 ] && [ "$(python3 -c 'import sys; print(sys.version_info[:2])')" != "(3, 9)" ]; then
    echo "changed modules again under the default python3 (tomllib-gated tests run here)"
    PYTHONPATH=scripts python3 -m unittest "${chosen[@]}"  # bash32-lint: nonempty guarded by the -gt 0 test
  fi
}

# Cheap, serial gates first: lint, then the interpreter check.
stage lint "repo lint" ./scripts/lint.sh
stage python-39-tests "interpreter is a tomllib-less 3.9" require_py39
stage python-39-tests "py_compile of every tracked .py under $PY39" py_compile_39
stage python-floor "py_compile under 3.11" py_floor

if [ "$FULL" != 1 ]; then
  logdir="$(mktemp -d "${TMPDIR:-/tmp}/tartci-prepush.XXXXXX")"
  trap 'rm -rf "$logdir"' EXIT
  stage python-39-tests "every test module imports under $PY39" test_modules_import_under_39
  stage python-39-tests "fast unit tests (allowlist, leak needles, public paths, changed modules)" fast_unit_tests
  printf '\033[32m✓ prepush (fast) passed in %ss. CI runs the full suite; use --full to match it.\033[0m\n' $((SECONDS - total_start))
  exit 0
fi

# The slow gates are independent, so run them concurrently with output held per
# job; the first failure kills the rest (whole process groups) and prints that
# job's log. Each job gets its own scratch dir so the two unittest runs cannot
# race on a shared TMPDIR path.
logdir="$(mktemp -d "${TMPDIR:-/tmp}/tartci-prepush.XXXXXX")"
mkdir "$logdir/bin39"
ln -s "$PY39" "$logdir/bin39/python3"
pids=(); names=(); jobs_ci=(); done_flags=()
cleanup() {
  local i
  for i in ${!pids[@]+"${!pids[@]}"}; do
    [ "${done_flags[$i]:-}" = 1 ] && continue
    kill -- "-${pids[$i]}" 2>/dev/null || kill "${pids[$i]}" 2>/dev/null || true
  done
  chmod -R u+w "$logdir" 2>/dev/null || true
  rm -rf "$logdir" 2>/dev/null || true
}
trap cleanup EXIT

launch() { # launch <ci-job> <name> <command...>   (runs in its own process group)
  local job="$1" name="$2" n=$((${#pids[@]} + 1)); shift 2
  jobs_ci+=("$job"); names+=("$name")
  printf '\033[36m• [%s] %s (running in background)\033[0m\n' "$job" "$name"
  mkdir "$logdir/tmp$n"
  set -m
  ( TMPDIR="$logdir/tmp$n"; export TMPDIR; "$@" ) >"$logdir/$n.log" 2>&1 &
  pids+=("$!")
  set +m
}

launch_py39() { PATH="$logdir/bin39:$PATH" "$PY39" -m unittest discover -s scripts -p 'test_*.py'; }
launch python-39-tests "unit tests under $PY39" launch_py39
launch lint "unit tests under python3" python3 -m unittest discover -s scripts -p 'test_*.py'
launch lint "shell tests" shell_tests

if [ "$(python3 -c 'import sys; print(sys.version_info[:2])')" = "(3, 9)" ]; then
  echo "warning: default python3 is 3.9, so the lint job's newer-interpreter run is not covered here" >&2
fi

remaining=${#pids[@]}
while [ "$remaining" -gt 0 ]; do
  for i in "${!pids[@]}"; do
    [ "${done_flags[$i]:-}" = 1 ] && continue
    if ! kill -0 "${pids[$i]}" 2>/dev/null; then
      if wait "${pids[$i]}"; then
        done_flags[$i]=1; remaining=$((remaining - 1))
        printf '\033[32m✓ [%s] %s\033[0m\n' "${jobs_ci[$i]}" "${names[$i]}"
      else
        grep -E '^(FAIL|ERROR):' "$logdir/$((i + 1)).log" >&2 || true
        tail -40 "$logdir/$((i + 1)).log" >&2
        printf '\033[31m✗ FAILED: CI job "%s" would be red (%s)\033[0m\n' "${jobs_ci[$i]}" "${names[$i]}" >&2
        exit 1
      fi
    fi
  done
  if [ "$remaining" -gt 0 ]; then sleep 1; fi
done

printf '\033[32m✓ prepush: all gates passed in %ss\033[0m\n' $((SECONDS - total_start))
