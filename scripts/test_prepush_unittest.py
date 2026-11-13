"""scripts/prepush_unittest.py: module-level skips report, never crash; tomllib pick.

Runs under Python 3.9.
"""
import io
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prepush_unittest as pu

HERE = os.path.dirname(os.path.abspath(__file__))
SKIPPING = textwrap.dedent('''
    import unittest
    raise unittest.SkipTest("needs tomllib (Python 3.11+)")
''')
PASSING = textwrap.dedent('''
    import unittest
    class T(unittest.TestCase):
        def test_ok(self):
            pass
''')
FAILING = textwrap.dedent('''
    import unittest
    class T(unittest.TestCase):
        def test_bad(self):
            self.fail("boom")
''')


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        for name, body in (("test_zz_skipping", SKIPPING), ("test_zz_passing", PASSING),
                           ("test_zz_failing", FAILING)):
            with open(os.path.join(self.dir, name + ".py"), "w") as fh:
                fh.write(body)

    def tearDown(self):
        for name in ("test_zz_skipping", "test_zz_passing", "test_zz_failing"):
            sys.modules.pop(name, None)
        self.tmp.cleanup()

    def _run(self, *modules):
        out = io.StringIO()
        try:
            rc = pu.run(list(modules), start=self.dir, stream=out)
        except unittest.SkipTest as exc:
            # Escaping here would mark this test skipped, hiding the very crash.
            self.fail("module-level SkipTest escaped run(): %s" % exc)
        return rc, out.getvalue()

    def test_module_level_skip_is_reported_not_raised(self):
        # The crash this exists for: `python3 -m unittest <module>` raised the
        # module's SkipTest as a traceback under 3.9.
        rc, out = self._run("test_zz_skipping", "test_zz_passing")
        self.assertEqual(rc, 0, out)
        self.assertIn("module skipped: test_zz_skipping", out)
        self.assertIn("skipped=1", out)
        self.assertIn("Ran 2 tests", out)

    def test_plain_unittest_crashes_on_the_same_module(self):
        # Control: the unwrapped invocation does not survive a module-level skip,
        # so the test above is not passing for an unrelated reason.
        proc = subprocess.run([sys.executable, "-m", "unittest", "test_zz_skipping"],
                              cwd=self.dir, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("SkipTest", proc.stderr)

    def test_a_failing_module_fails_the_run(self):
        rc, out = self._run("test_zz_skipping", "test_zz_failing")
        self.assertEqual(rc, 1, out)
        self.assertIn("boom", out)

    def test_a_missing_module_fails_rather_than_running_nothing(self):
        rc, out = self._run("test_zz_passing", "test_zz_nonexistent")
        self.assertEqual(rc, 2, out)
        self.assertIn("no such test module: test_zz_nonexistent", out)

    def test_cli_runs_a_real_tomllib_gated_module(self):
        # test_advertised_labels skips itself at import without tomllib: the CLI
        # exits 0 under any interpreter (one module skip on 3.9, real tests on 3.11+).
        proc = subprocess.run([sys.executable, os.path.join(HERE, "prepush_unittest.py"),
                               "run", "test_advertised_labels"],
                              capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=HERE))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if sys.version_info < (3, 11):
            self.assertIn("module skipped: test_advertised_labels", proc.stderr)
        else:
            self.assertNotIn("module skipped", proc.stderr)


def _fake_python(directory, name, has_tomllib):
    path = os.path.join(directory, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\nexit %d\n" % (0 if has_tomllib else 1))
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


class PickTomllibPythonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_skips_a_tomllib_less_python3_and_finds_a_newer_one(self):
        _fake_python(self.dir, "python3", False)          # the hosts' 3.9
        want = _fake_python(self.dir, "python3.12", True)
        got = pu.pick_tomllib_python(("python3", "python3.12"), env={}, path=self.dir)
        self.assertEqual(got, want)

    def test_none_when_no_candidate_has_tomllib(self):
        _fake_python(self.dir, "python3", False)
        self.assertIsNone(pu.pick_tomllib_python(("python3", "python3.11"), env={}, path=self.dir))

    def test_override_is_checked_first(self):
        _fake_python(self.dir, "python3.12", True)
        want = _fake_python(self.dir, "mypy311", True)
        got = pu.pick_tomllib_python(("python3.12",), env={"TARTCI_PREPUSH_TOMLLIB_PY": "mypy311"},
                                     path=self.dir)
        self.assertEqual(got, want)

    def test_real_pick_has_tomllib_or_is_none(self):
        found = pu.pick_tomllib_python()
        if found is not None:
            subprocess.run([found, "-c", "import tomllib"], check=True)


if __name__ == "__main__":
    unittest.main()
