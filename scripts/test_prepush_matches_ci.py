"""scripts/prepush.sh must run the commands the lint and python-39-tests CI jobs run.

Parses .github/workflows/ci.yml textually (no PyYAML: the 3.9 job has none).
Every `run:` step of those jobs is either a command prepush.sh must contain,
or listed in SETUP_STEPS as CI-runner setup with no local equivalent. A new CI
step that is in neither fails here, so the two cannot drift silently.

Runs under Python 3.9.
"""
import os
import re
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "ci.yml")
PREPUSH = os.path.join(ROOT, "scripts", "prepush.sh")

MIRRORED_JOBS = ("lint", "python-39-tests")

# Step names that prepare the hosted runner and have nothing to run locally.
SETUP_STEPS = {
    "Ensure shellcheck",
    "Show tool versions",
    "Match the macOS launch-interpreter contract",
    # Mirrored by require_py39 in prepush.sh (test_python39_stage_checks_...).
    "Refuse anything but a 3.9 interpreter on PATH",
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def job_steps(text, job):
    """Return [(step_name, run_command_text)] for the `run:` steps of a job."""
    m = re.search(r"^jobs:\s*$", text, re.M)
    block, cur = [], None
    for line in text[m.end():].splitlines():
        jm = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if jm:
            cur = jm.group(1)
        elif cur == job:
            block.append(line)
    steps, name, run, in_run = [], None, None, False
    for line in block + ["      - name: __end__"]:
        sm = re.match(r"^      - (.*)$", line)
        if sm:
            if name is not None and run is not None:
                steps.append((name, "\n".join(run)))
            nm = re.match(r"name:\s*(.+?)\s*$", sm.group(1))
            name = nm.group(1) if nm else "<unnamed: %s>" % sm.group(1).strip()
            run, in_run = None, False
            rm0 = re.match(r"run:\s*(.*)$", sm.group(1))
            if rm0:
                run = [rm0.group(1).strip()]
            continue
        rm = re.match(r"^        run:\s*(.*)$", line)
        if rm and name is not None:
            first = rm.group(1).strip()
            if first in ("|", ">", "|-", ">-"):
                run, in_run = [], True
            else:
                run, in_run = [first], False
            continue
        if in_run and run is not None:
            if re.match(r"^          \S", line) or line.strip() == "" or re.match(r"^           ", line):
                run.append(line.strip())
            else:
                in_run = False
    return steps


def commands(run_text):
    """Executable lines of a run block, comments and blanks dropped."""
    out = []
    for line in run_text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(re.sub(r"\s+-v\b", "", line))  # CI is verbose; prepush is quiet
    return out


class PrepushMatchesCiTest(unittest.TestCase):
    def setUp(self):
        self.ci = _read(WORKFLOW)
        raw = _read(PREPUSH)
        # Comments must not satisfy a check: only executable lines count.
        self.prepush = "\n".join(
            l for l in raw.splitlines() if not l.lstrip().startswith("#")
        )
        self.lines = [l.strip() for l in self.prepush.splitlines()]

    def test_every_mirrored_job_has_run_steps(self):
        for job in MIRRORED_JOBS:
            self.assertTrue(job_steps(self.ci, job), job)

    def test_every_ci_command_is_invoked_by_prepush(self):
        invocations = {
            "Run repo lint (shellcheck + bash -n + py_compile + TOML parse)":
                ['stage lint "repo lint" ./scripts/lint.sh'],
            "Run unit tests":
                ['launch lint "unit tests under python3" python3 -m unittest '
                 "discover -s scripts -p 'test_*.py'"],
            "Run the unit tests under Python 3.9 (the hosts' /usr/bin/python3)":
                ['launch python-39-tests "unit tests under $PY39" launch_py39',
                 "launch_py39() { PATH=\"$logdir/bin39:$PATH\" \"$PY39\" -m unittest "
                 "discover -s scripts -p 'test_*.py'"],
            "Run shell tests":
                ['launch lint "shell tests" shell_tests',
                 "for test_script in tests/test_*.sh; do",
                 'bash "$test_script" ||'],
        }
        seen = set()
        for job in MIRRORED_JOBS:
            for name, run in job_steps(self.ci, job):
                if name in SETUP_STEPS:
                    continue
                self.assertIn(name, invocations, "unclassified CI step %r" % name)
                seen.add(name)
                for needle in invocations[name]:
                    self.assertTrue(
                        any(l.startswith(needle) for l in self.lines),
                        "CI job %r step %r: prepush.sh has no executable line "
                        "starting %r" % (job, name, needle),
                    )
                # The CI command itself, so editing ci.yml flags the mapping.
                for cmd in commands(run):
                    if cmd.startswith("python3 -m unittest"):
                        self.assertIn(
                            "-m unittest discover -s scripts -p 'test_*.py'", cmd)
        self.assertEqual(seen, set(invocations) - set(), "a mapped CI step vanished")

    def test_python39_gate_puts_39_first_on_path(self):
        # CI puts 3.9 first on PATH so subprocesses calling python3 get it.
        self.assertTrue(any(l.startswith("launch_py39() { PATH=") and "$logdir/bin39:$PATH" in l and '"$PY39" -m unittest' in l
                            for l in self.lines))

    def test_no_unclassified_ci_step(self):
        known = {"Run repo lint (shellcheck + bash -n + py_compile + TOML parse)",
                 "Run unit tests", "Run shell tests",
                 "Run the unit tests under Python 3.9 (the hosts' /usr/bin/python3)"}
        for job in MIRRORED_JOBS:
            for name, _ in job_steps(self.ci, job):
                self.assertIn(
                    name, known | SETUP_STEPS,
                    "new CI step %r in job %r: mirror it in scripts/prepush.sh, "
                    "then add it to this test" % (name, job),
                )

    def test_python39_stage_checks_version_and_no_tomllib(self):
        self.assertIn('PY39="${TARTCI_PREPUSH_PY39:-/usr/bin/python3}"', self.prepush)
        self.assertIn("sys.version_info[:2] == (3, 9)", self.prepush)
        self.assertIn("import tomllib", self.prepush)
        self.assertIn('stage python-39-tests "interpreter is a tomllib-less 3.9"', self.prepush)

    def test_each_mirrored_job_is_named_in_prepush(self):
        for job in MIRRORED_JOBS:
            self.assertRegex(self.prepush, r"(stage|launch) %s " % re.escape(job))

    def test_prepush_is_executable_and_fails_fast(self):
        self.assertTrue(os.access(PREPUSH, os.X_OK))
        self.assertIn("set -euo pipefail", self.prepush)
        self.assertIn("exit 1", self.prepush)

# The failure classes that put agent PRs red in CI, and what in fast mode
# catches each. Each gate named here must be wired into the default run, and
# the module that implements it must exist.
FAST_CLASSES = {
    "a negative-test needle shorter than 4 characters": "test_leak_needles",
    "a tomllib-guarded test missing from the 3.9 skip allowlist": "test_system_python_tests_run",
    "a hardcoded /Users path failing the public-path lint": "test_public_path_lint",
}



class FastModeCoversTodaysFailureClassesTest(unittest.TestCase):
    def setUp(self):
        raw = _read(PREPUSH)
        self.text = "\n".join(l for l in raw.splitlines() if not l.lstrip().startswith("#"))
        self.fast = self.text.split('if [ "$FULL" != 1 ]; then', 1)[1].split("\nfi\n", 1)[0]
        self.serial = self.text.split('if [ "$FULL" != 1 ]; then', 1)[0]

    def test_default_run_is_fast_and_full_is_opt_in(self):
        self.assertIn('--full) FULL=1', self.text)
        self.assertIn("exit 0", self.fast)
        self.assertNotIn("launch ", self.fast)

    def test_fixed_modules_are_in_the_fast_unit_run(self):
        m = re.search(r"mods\+=\(([^)]*)\)", self.text)
        self.assertTrue(m, "no fixed module list in fast_unit_tests")
        fixed = m.group(1).split()
        for cls, module in FAST_CLASSES.items():
            self.assertIn(module, fixed, cls)
            self.assertTrue(os.path.exists(os.path.join(ROOT, "scripts", module + ".py")), module)

    def test_fast_unit_tests_run_under_the_39_interpreter(self):
        # `stage` runs it inside `if` (set -e off), so the 3.9 run must return on
        # failure itself; otherwise the later tomllib run decides the verdict.
        self.assertIn('"$PY39" scripts/prepush_unittest.py run "${uniq[@]}" || return 1', self.text)
        self.assertIn("fast_unit_tests", self.fast)

    def test_tomllib_gated_tests_run_under_a_found_311_or_report_skip(self):
        # Hosts' default python3 is the 3.9: the second run must not depend on it.
        self.assertIn("prepush_unittest.py pick-tomllib-python", self.text)
        self.assertIn('"$tomllib_py" scripts/prepush_unittest.py run "${chosen[@]}" || return 1', self.text)
        self.assertIn("SKIP: no Python 3.11+ with tomllib found", self.text)
        self.assertNotIn("python3 -m unittest \"${chosen[@]}\"", self.text)

    def test_serial_gates_cover_lint_interpreter_and_compile(self):
        self.assertIn('stage lint "repo lint" ./scripts/lint.sh', self.serial)
        self.assertIn('stage python-39-tests "interpreter is a tomllib-less 3.9" require_py39', self.serial)
        self.assertIn("py_compile_39", self.serial)
        self.assertIn('"$PY39" -m py_compile', self.text)
        self.assertIn('"$py311" -m py_compile', self.text)

    def test_every_test_module_is_import_checked_under_39(self):
        # An unguarded `import tomllib` in a test module fails CI's whole
        # discover run; fast mode loads (not runs) every test module under 3.9.
        self.assertIn('stage python-39-tests "every test module imports under $PY39" '
                      "test_modules_import_under_39", self.fast)
        self.assertIn("unittest.defaultTestLoader.discover", self.text)
        self.assertIn("_FailedTest", self.text)
        self.assertIn('PYTHONPATH=scripts "$PY39" -c', self.text)

    def test_a_selector_failure_fails_the_run(self):
        self.assertIn('selected="$(python3 scripts/prepush_select.py "$BASE")" || {', self.text)

    def test_changed_modules_and_their_tests_are_selected(self):
        # Unguarded tomllib import and a stale assertion in a changed module's
        # own tests are caught by running that module's tests under 3.9: the
        # selector must be consulted, against the base ref, in fast mode.
        self.assertIn('prepush_select.py "$BASE"', self.text)
        self.assertIn('BASE="${TARTCI_PREPUSH_BASE:-origin/main}"', self.text)
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import prepush_select
        self.assertIn("test_prepush_select", prepush_select.select(["scripts/test_prepush_select.py"]))
        self.assertIn("test_prepush_select", prepush_select.select(["scripts/prepush_select.py"]))


if __name__ == "__main__":
    unittest.main()
