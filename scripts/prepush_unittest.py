#!/usr/bin/env python3
"""Run named unit-test modules for scripts/prepush.sh, and pick a tomllib Python.

    prepush_unittest.py run test_a test_b ...   run the modules, as CI's discover would
    prepush_unittest.py pick-tomllib-python     print a Python 3.11+ (has tomllib), or
                                                nothing and exit 1 when none exists

Why `run` instead of `python3 -m unittest test_a`: a module that calls
testing_support.skip_module_without_tomllib() raises unittest.SkipTest at import.
`unittest discover` (what CI runs) turns that into one skipped test, but naming
the module on the command line does not catch it, so the 3.9 run crashed with a
SkipTest traceback instead of reporting the skip. Loading each module through
discover gives the same result CI gives. A name that matches no module fails the
run, so a typo cannot pass by running nothing.

`pick-tomllib-python` lets prepush.sh run the tomllib-gated tests on hosts whose
default python3 is the tomllib-less 3.9.

Runs under Python 3.9; no third-party imports.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest

SCRIPTS = os.path.dirname(os.path.abspath(__file__))

# Checked in order; TARTCI_PREPUSH_TOMLLIB_PY, when set, is checked first.
TOMLLIB_CANDIDATES = ("python3", "python3.15", "python3.14", "python3.13",
                      "python3.12", "python3.11", "/opt/homebrew/bin/python3",
                      "/usr/local/bin/python3")


def has_tomllib(python):
    try:
        proc = subprocess.run([python, "-c", "import tomllib"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def pick_tomllib_python(candidates=TOMLLIB_CANDIDATES, env=None, path=None):
    """First candidate on PATH (or absolute) that imports tomllib, else None."""
    env = os.environ if env is None else env
    order = [env["TARTCI_PREPUSH_TOMLLIB_PY"]] if env.get("TARTCI_PREPUSH_TOMLLIB_PY") else []
    order += list(candidates)
    for name in order:
        found = shutil.which(name, path=path)
        if found and has_tomllib(found):
            return found
    return None


def _skips(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            for item in _skips(test):
                yield item
        elif type(test).__name__ == "ModuleSkipped":
            yield test


def load(modules, start=SCRIPTS):
    """(suite, missing): one discover per module, so module-level skips load as skips."""
    loader = unittest.TestLoader()
    suite, missing = unittest.TestSuite(), []
    for module in modules:
        if not os.path.exists(os.path.join(start, module + ".py")):
            missing.append(module)
            continue
        suite.addTests(loader.discover(start, pattern=module + ".py", top_level_dir=start))
    return suite, missing


def run(modules, start=SCRIPTS, stream=None):
    stream = sys.stderr if stream is None else stream
    suite, missing = load(modules, start)
    if missing:
        stream.write("no such test module: %s\n" % ", ".join(missing))
        return 2
    for test in _skips(suite):
        # Name each whole-module skip, so it is visible in the log, not buried in a count.
        stream.write("module skipped: %s\n" % test.id().rsplit(".", 1)[-1])
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


def main(argv):
    if argv[:1] == ["run"] and len(argv) > 1:
        return run(argv[1:])
    if argv == ["pick-tomllib-python"]:
        found = pick_tomllib_python()
        if found:
            print(found)
            return 0
        return 1
    sys.stderr.write("usage: prepush_unittest.py run MODULE... | pick-tomllib-python\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
