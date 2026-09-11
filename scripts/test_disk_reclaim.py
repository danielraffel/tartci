#!/usr/bin/env python3
"""Behavioral tests for scripts/disk_reclaim.py.

The janitor deletes directories, so every negative assertion here is paired
with a control in the same tree that MUST be deleted. A test that only proves
"the source tree survived" passes just as happily when the scan found nothing
at all, and that is the failure mode worth catching.

Run:  python3 -m unittest scripts.test_disk_reclaim   (or via discover)
"""

from __future__ import annotations

import errno
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import disk_reclaim as dr  # noqa: E402

DAY = 86400.0


def make_build_tree(path: pathlib.Path, *, age_days: float = 0.0,
                    marker: str = "CMakeCache.txt") -> pathlib.Path:
    """A directory that looks like generator output, aged `age_days` back."""
    path.mkdir(parents=True, exist_ok=True)
    (path / marker).write_text("x")
    (path / "CMakeFiles").mkdir(exist_ok=True)
    (path / "CMakeFiles" / "obj.o").write_text("y" * 32)
    age(path, age_days)
    return path


def age(path: pathlib.Path, age_days: float) -> None:
    """Backdate everything at or below `path`, deepest first."""
    stamp = time.time() - age_days * DAY
    paths = sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for entry in paths + [path]:
        os.utime(entry, (stamp, stamp))


class BuildDirNameTests(unittest.TestCase):
    def test_accepts_build_and_suffixed_variants(self):
        for name in ("build", "build-cov", "build-coverage",
                     "build-cov-phase6-gpu", "build-arm64"):
            self.assertTrue(dr.is_build_dir_name(name), name)

    def test_rejects_names_that_merely_start_with_build(self):
        for name in ("buildkite", "builder", "buildsrc", "build-", "rebuild"):
            self.assertFalse(dr.is_build_dir_name(name), name)


class FindCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # macOS puts TMPDIR under /var, a symlink to /private/var, and the
        # janitor resolves its roots. Resolve here so paths compare equal.
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def test_finds_build_dirs_at_worktree_depth(self):
        make_build_tree(self.root / "pulp" / "build")
        make_build_tree(self.root / "pulp-topic" / "build-cov")
        make_build_tree(self.root / "nested" / "deeper" / "build")
        found = set(dr.find_candidates([self.root], maxdepth=3))
        self.assertEqual(found, {
            self.root / "pulp" / "build",
            self.root / "pulp-topic" / "build-cov",
            self.root / "nested" / "deeper" / "build",
        })

    def test_maxdepth_bounds_the_scan(self):
        make_build_tree(self.root / "a" / "b" / "c" / "build")
        self.assertEqual(dr.find_candidates([self.root], maxdepth=3), [])
        # Control: the same tree IS found one level deeper, so the empty result
        # above is the depth bound and not a broken scan.
        self.assertEqual(len(dr.find_candidates([self.root], maxdepth=4)), 1)

    def test_does_not_descend_into_a_candidate(self):
        outer = make_build_tree(self.root / "wt" / "build")
        make_build_tree(outer / "build")
        found = dr.find_candidates([self.root], maxdepth=4)
        self.assertEqual(found, [outer])

    def test_missing_root_is_not_an_error(self):
        self.assertEqual(dr.find_candidates([self.root / "absent"], maxdepth=3), [])


class _RefusingEntry:
    """A scandir entry whose stat fails with a chosen errno.

    Real ENOENT races need a live build running under the scan, which no unit
    test can stage deterministically, so the errno is supplied directly. The
    distinction under test is errno-level, not filesystem-level.
    """

    def __init__(self, path: pathlib.Path, err: int) -> None:
        self.path = str(path)
        self.name = path.name
        self._err = err

    def stat(self, follow_symlinks: bool = True):
        raise OSError(self._err, os.strerror(self._err))

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return False


class NewestMtimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def test_an_unreadable_subtree_reports_unmeasured_not_maximally_idle(self):
        """0.0 would read as decades idle, which deletes a tree written seconds ago."""
        tree = make_build_tree(self.root / "wt" / "build", age_days=0)
        # Control first, on the same instrument and the same tree: while it is
        # readable the walk returns a real, recent figure.
        readable = dr.newest_mtime(tree)
        self.assertIsNotNone(readable, "control: a readable tree must measure")
        self.assertLess(time.time() - readable, 120)

        os.chmod(tree, 0o300)
        self.addCleanup(os.chmod, tree, 0o700)
        self.assertIsNone(dr.newest_mtime(tree),
                          "a refused subtree must be unmeasured, not idle")

    def test_a_vanishing_entry_stays_benign_but_a_refused_one_does_not(self):
        """ninja unlinks temp files under the scan; that must not read as unknown."""
        tree = make_build_tree(self.root / "busy" / "build", age_days=0)
        for err, expect_none in ((errno.ENOENT, False), (errno.EACCES, True)):
            with self.subTest(errno=errno.errorcode[err]):
                entries = [_RefusingEntry(tree / "gone.tmp", err)]
                with unittest.mock.patch.object(dr.os, "scandir",
                                                return_value=entries):
                    result = dr.newest_mtime(tree)
                if expect_none:
                    self.assertIsNone(result)
                else:
                    self.assertIsNotNone(
                        result, "a vanishing entry is evidence of a BUSY tree")


class ActiveCommandLinesTests(unittest.TestCase):
    """The fail-open branches, driven through the real function.

    Patching `active_command_lines` itself (as the surrounding suite does for
    classify) leaves these two `return None` lines unreachable, so replacing
    either with `return ""` keeps the whole suite green while the guard that
    stops the janitor deleting a live build is switched off.
    """

    def test_an_unexecutable_pgrep_is_a_refusal_not_an_idle_host(self):
        self.assertIsInstance(dr.active_command_lines("python"), str,
                              "control: a working pgrep returns a string")
        with unittest.mock.patch.object(dr.subprocess, "run",
                                        side_effect=OSError("no pgrep")):
            self.assertIsNone(dr.active_command_lines())

    def test_a_pgrep_error_exit_is_a_refusal_not_an_idle_host(self):
        self.assertIsInstance(dr.active_command_lines("python"), str,
                              "control: a working pgrep returns a string")
        broken = subprocess.CompletedProcess(
            args=["pgrep"], returncode=2, stdout="", stderr="boom")
        with unittest.mock.patch.object(dr.subprocess, "run",
                                        return_value=broken):
            self.assertIsNone(dr.active_command_lines())


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # macOS puts TMPDIR under /var, a symlink to /private/var, and the
        # janitor resolves its roots. Resolve here so paths compare equal.
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()

    def classify(self, path, *, min_age_days=7.0, active=""):
        return dr.classify(path, now=self.now, min_age_days=min_age_days,
                           active=active)

    def test_old_generated_tree_is_reclaimable(self):
        path = make_build_tree(self.root / "wt" / "build", age_days=30)
        delete, reason, age_days = self.classify(path)
        self.assertTrue(delete, reason)
        self.assertGreater(age_days, 29)

    def test_recent_tree_is_kept(self):
        path = make_build_tree(self.root / "wt" / "build", age_days=1)
        delete, reason, _ = self.classify(path)
        self.assertFalse(delete)
        self.assertEqual(reason, "too_recent")

    def test_directory_without_a_generated_marker_is_kept(self):
        path = self.root / "wt" / "build"
        path.mkdir(parents=True)
        (path / "notes.txt").write_text("hand-made")
        age(path, 400)
        delete, reason, _ = self.classify(path)
        self.assertFalse(delete)
        self.assertEqual(reason, "not_a_build_tree")

    def test_source_marker_wins_over_generated_marker(self):
        path = make_build_tree(self.root / "wt" / "build", age_days=400)
        (path / ".git").mkdir()
        age(path, 400)
        delete, reason, _ = self.classify(path)
        self.assertFalse(delete)
        self.assertEqual(reason, "source_tree")

    def test_live_build_command_line_protects_the_tree(self):
        path = make_build_tree(self.root / "wt" / "build", age_days=400)
        cmdline = f"66665 cmake --build {path} --target all\n"
        delete, reason, _ = self.classify(path, active=cmdline)
        self.assertFalse(delete)
        self.assertEqual(reason, "active_build")
        # Control: identical tree, identical age, no matching command line.
        delete_ctl, reason_ctl, _ = self.classify(path, active="66665 cmake --build /elsewhere\n")
        self.assertTrue(delete_ctl, reason_ctl)

    def test_unreadable_process_table_blocks_every_deletion(self):
        """None means "we could not look", which must never license a delete.

        An empty string and None both mean "no matching command line was
        returned", so the only thing separating a quiet host from a broken
        `pgrep` is this distinction. Getting it wrong deletes a live build.
        """
        path = make_build_tree(self.root / "wt" / "build", age_days=400)
        delete, reason, _ = self.classify(path, active=None)
        self.assertFalse(delete)
        self.assertEqual(reason, "process_scan_unavailable")
        # Control: same ancient tree, a process table that was READ and was
        # empty. This must delete, or the test above proves nothing.
        delete_ctl, reason_ctl, _ = self.classify(path, active="")
        self.assertTrue(delete_ctl, reason_ctl)

    def test_empty_pgrep_result_is_an_answer_not_a_failure(self):
        """`pgrep` exits 1 with no output when nothing matches."""
        result = dr.active_command_lines("zzz-no-process-matches-this-zzz")
        self.assertEqual(result, "")
        # Control: a pattern that must match this very test process.
        self.assertNotEqual(dr.active_command_lines("python"), "")

    def test_an_unreadable_build_tree_is_never_reclaimable(self):
        """An age we could not measure must not be spent as an old age."""
        path = make_build_tree(self.root / "wt" / "build", age_days=40)
        # Control first: while readable, this exact tree is reclaimable.
        delete_ctl, reason_ctl, age_ctl = self.classify(path)
        self.assertTrue(delete_ctl, reason_ctl)
        self.assertGreater(age_ctl, 39)

        os.chmod(path, 0o300)
        self.addCleanup(os.chmod, path, 0o700)
        delete, reason, age_days = self.classify(path)
        self.assertFalse(delete)
        self.assertEqual(reason, "unmeasured")
        self.assertEqual(age_days, 0.0)

    def test_a_command_line_naming_the_other_spelling_of_the_path_protects_it(self):
        """A checkout behind a symlink has two absolute spellings.

        `cmake --build` records whichever one the shell handed it, so a guard
        that compares only the resolved spelling silently does not fire on the
        unresolved one (and vice versa).
        """
        real = make_build_tree(self.root / "real" / "pulp" / "build", age_days=400)
        (self.root / "link").symlink_to(self.root / "real")
        unresolved = self.root / "link" / "pulp" / "build"
        self.assertNotEqual(str(unresolved), str(real),
                            "control: the two spellings must differ")

        for candidate, named in ((unresolved, real), (real, unresolved)):
            with self.subTest(candidate=str(candidate)):
                cmdline = f"66665 cmake --build {named} --target all\n"
                delete, reason, _ = self.classify(candidate, active=cmdline)
                self.assertFalse(delete)
                self.assertEqual(reason, "active_build")
        # Control: an unrelated path in the same process table reclaims.
        delete_ctl, reason_ctl, _ = self.classify(
            unresolved, active="66665 cmake --build /elsewhere\n")
        self.assertTrue(delete_ctl, reason_ctl)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # macOS puts TMPDIR under /var, a symlink to /private/var, and the
        # janitor resolves its roots. Resolve here so paths compare equal.
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def run_main(self, *argv):
        buffer = io.StringIO()
        # stderr too: the progress heartbeat writes there, and letting it reach
        # the real stream interleaves it with the runner's own dots.
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = dr.main(["--roots", str(self.root), *argv])
        return code, buffer.getvalue()

    def run_json(self, *argv):
        code, out = self.run_main("--json", *argv)
        return code, json.loads(out)

    def test_dry_run_deletes_nothing(self):
        stale = make_build_tree(self.root / "wt" / "build", age_days=400)
        code, out = self.run_main("--min-age-days", "7", "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        self.assertTrue(stale.is_dir(), "dry run must not delete")
        self.assertIn("would remove", out)

    def test_fix_deletes_only_the_reclaimable_tree(self):
        stale = make_build_tree(self.root / "old" / "build", age_days=400)
        fresh = make_build_tree(self.root / "new" / "build", age_days=1)
        source = self.root / "src" / "build"
        source.mkdir(parents=True)
        (source / "CMakeLists.txt").write_text("project(x)")
        age(source, 400)

        code, _ = self.run_main("--fix", "--min-age-days", "7",
                                "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        self.assertFalse(stale.exists(), "control: the stale tree must be deleted")
        self.assertTrue(fresh.is_dir())
        self.assertTrue(source.is_dir())

    def test_pressure_tier_applies_the_shorter_age_gate(self):
        path = make_build_tree(self.root / "wt" / "build", age_days=10)
        # No pressure: the 30-day gate keeps a 10-day-old tree.
        code, report = self.run_json("--min-age-days", "30",
                                     "--pressure-min-age-days", "7",
                                     "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        self.assertFalse(report["pressure"])
        self.assertEqual(report["deleted"], [])
        self.assertEqual([k["reason"] for k in report["kept"]], ["too_recent"])
        # Under pressure the 7-day gate reclaims the same tree. A free-space
        # threshold this large is always above the real free space.
        code, report = self.run_json("--min-age-days", "30",
                                     "--pressure-min-age-days", "7",
                                     "--pressure-free-gb", "1000000000")
        self.assertEqual(code, 0)
        self.assertTrue(report["pressure"])
        self.assertEqual([d["path"] for d in report["deleted"]], [str(path)])
        self.assertTrue(path.is_dir(), "still a dry run")

    def test_fail_below_reports_a_still_full_host(self):
        make_build_tree(self.root / "wt" / "build", age_days=400)
        code, _ = self.run_main("--fix", "--min-age-days", "7",
                                "--pressure-free-gb", "0",
                                "--fail-below-gb", "1000000000")
        self.assertEqual(code, 3)
        # Control: the same pass with the floor disabled exits 0, so the 3 is
        # the floor and not an unrelated failure.
        make_build_tree(self.root / "wt2" / "build", age_days=400)
        code, _ = self.run_main("--fix", "--min-age-days", "7",
                                "--pressure-free-gb", "0")
        self.assertEqual(code, 0)

    def test_json_report_is_machine_readable(self):
        make_build_tree(self.root / "wt" / "build", age_days=400)
        code, report = self.run_json("--min-age-days", "7",
                                     "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["candidates"], 1)
        self.assertEqual(len(report["deleted"]), 1)

    def test_dry_run_reports_the_bytes_a_fix_pass_would_free(self):
        """A dry run that totals 0.0 GiB defeats the report-only first step.

        The accumulator used to sit inside the --fix branch, so every dry run
        listed real per-directory sizes under a zero total.
        """
        make_build_tree(self.root / "wt" / "build", age_days=400)
        code, dry = self.run_json("--min-age-days", "7",
                                  "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        per_record = sum(d["size_bytes"] for d in dry["deleted"])
        self.assertGreater(per_record, 0, "control: the tree must have a size")
        self.assertEqual(dry["reclaimed_bytes"], per_record)

        # Control: the same tree under --fix reports the same total, so the
        # dry-run figure is the fix figure rather than an independent guess.
        code, fixed = self.run_json("--fix", "--min-age-days", "7",
                                    "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        self.assertEqual(fixed["reclaimed_bytes"], per_record)

    def test_summary_line_says_would_reclaim_in_a_dry_run(self):
        make_build_tree(self.root / "wt" / "build", age_days=400)
        code, out = self.run_main("--min-age-days", "7",
                                  "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        self.assertIn("would reclaim", out)
        # Control: the same phrase must NOT survive a --fix pass, which
        # reports what it actually freed.
        code, out = self.run_main("--fix", "--min-age-days", "7",
                                  "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        self.assertNotIn("would reclaim", out)
        self.assertIn("reclaimed", out)

    def test_a_missing_scan_root_is_a_configuration_error(self):
        """A rendered plist pointing at the wrong volume must not exit 0.

        `find_candidates` skips a root it cannot enter, which is right for a
        host that simply has no builds. At the top level that same silence is
        how a misconfigured agent reports a clean pass forever while the disk
        it was installed to protect fills up.
        """
        absent = self.root / "no-such-volume"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = dr.main(["--roots", str(absent)])
        self.assertEqual(code, 2)
        # Control: the same invocation against the root that does exist.
        code_ctl, _ = self.run_main()
        self.assertEqual(code_ctl, 0)

    def test_a_tree_touched_during_the_pass_is_not_deleted(self):
        """Liveness is sampled once and the pass can run for minutes.

        `du -sk` over a large tree is the long pole, so a build can start after
        the process scan and before the rmtree. The age is re-read immediately
        before the irreversible act; here the sizer stands in for that delay.
        """
        stale = make_build_tree(self.root / "wt" / "build", age_days=400)
        real_sizer = dr.dir_size_bytes

        def touching_sizer(path):
            (path / "CMakeCache.txt").write_text("a build just started")
            return real_sizer(path)

        with unittest.mock.patch.object(dr, "dir_size_bytes",
                                        side_effect=touching_sizer):
            code, report = self.run_json("--fix", "--min-age-days", "7",
                                         "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        self.assertTrue(stale.is_dir(), "a tree written mid-pass must survive")
        self.assertEqual(report["deleted"], [])
        self.assertEqual([k["reason"] for k in report["kept"]],
                         ["touched_during_pass"])

        # Control: the identical run with the real sizer deletes it, so the
        # keep above is the re-check and not an unrelated refusal. The sizer
        # above touched the tree, so backdate it again first.
        age(stale, 400)
        code_ctl, report_ctl = self.run_json("--fix", "--min-age-days", "7",
                                             "--pressure-free-gb", "0")
        self.assertEqual(code_ctl, 0)
        self.assertEqual(len(report_ctl["deleted"]), 1)
        self.assertFalse(stale.exists())


class FailClosedTests(unittest.TestCase):
    """The janitor must treat "could not measure" as a reason to do less.

    Both helpers used to fold their failure case into an ordinary value, and
    both folded it toward deleting MORE: an unreadable process table read as
    an idle host, and an unreadable volume read as a full one, which selects
    the shorter age gate. These tests pin the direction.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def run_json(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = dr.main(["--roots", str(self.root), "--json", *argv])
        return code, json.loads(buffer.getvalue())

    def test_unreadable_process_table_deletes_nothing_and_reports_it(self):
        stale = make_build_tree(self.root / "wt" / "build", age_days=400)
        with unittest.mock.patch.object(dr, "active_command_lines",
                                        return_value=None):
            code, report = self.run_json("--fix", "--min-age-days", "7",
                                         "--pressure-free-gb", "0")
        self.assertEqual(code, 4)
        self.assertFalse(report["process_scan_ok"])
        self.assertEqual(report["deleted"], [])
        self.assertTrue(stale.is_dir(), "a failed scan must delete nothing")
        self.assertEqual(report["kept"][0]["reason"], "process_scan_unavailable")
        # Control: the identical run with a readable, empty process table.
        code_ctl, report_ctl = self.run_json("--fix", "--min-age-days", "7",
                                             "--pressure-free-gb", "0")
        self.assertEqual(code_ctl, 0)
        self.assertTrue(report_ctl["process_scan_ok"])
        self.assertEqual(len(report_ctl["deleted"]), 1)
        self.assertFalse(stale.is_dir())

    def test_unknown_free_space_selects_the_longer_age_gate(self):
        make_build_tree(self.root / "wt" / "build", age_days=400)
        # A pressure threshold no real volume can satisfy: if free space were
        # readable this run would be in the pressure tier.
        argv = ("--min-age-days", "30", "--pressure-free-gb", "999999999",
                "--pressure-min-age-days", "7")
        with unittest.mock.patch.object(dr, "free_bytes", return_value=None):
            code, report = self.run_json(*argv)
        self.assertEqual(code, 0)
        self.assertFalse(report["pressure"])
        self.assertEqual(report["min_age_days"], 30)
        # Control: the same argv with free space readable takes the short gate.
        code_ctl, report_ctl = self.run_json(*argv)
        self.assertEqual(code_ctl, 0)
        self.assertTrue(report_ctl["pressure"])
        self.assertEqual(report_ctl["min_age_days"], 7)

    def test_unknown_free_space_cannot_certify_the_floor(self):
        with unittest.mock.patch.object(dr, "free_bytes", return_value=None):
            code, _ = self.run_json("--fail-below-gb", "60")
        self.assertEqual(code, 4)


class ProgressTests(unittest.TestCase):
    """The heartbeat that keeps a working pass distinguishable from a wedged one."""

    def test_rate_limit_suppresses_then_releases(self):
        stream = io.StringIO()
        progress = dr.Progress(interval_s=1000.0, stream=stream)
        self.assertFalse(progress.emit("first"),
                         "a line inside the interval must be suppressed")
        self.assertEqual(stream.getvalue(), "")
        # Control, same instrument and same stream: past the interval it fires.
        progress._last -= 1001.0
        self.assertTrue(progress.emit("second"))
        self.assertIn("second", stream.getvalue())

    def test_force_ignores_the_rate_limit(self):
        stream = io.StringIO()
        progress = dr.Progress(interval_s=1000.0, stream=stream)
        self.assertTrue(progress.emit("urgent", force=True))
        self.assertIn("urgent", stream.getvalue())
        # Control: without force, the very next call is still suppressed.
        self.assertFalse(progress.emit("routine"))
        self.assertNotIn("routine", stream.getvalue())

    def test_write_is_flushed_so_the_mtime_moves(self):
        """A buffered line leaves the log exactly as stale as it was."""
        with tempfile.TemporaryDirectory() as td:
            log = pathlib.Path(td) / "reclaim.log"
            with log.open("w") as handle:
                dr.Progress(stream=handle).emit("beat", force=True)
                # Read from a SECOND descriptor while the writer is still open:
                # unflushed bytes are invisible here.
                self.assertIn("beat", log.read_text())


class ProgressWiringTests(MainTests):
    """The heartbeat has to reach a real pass, not just exist."""

    def run_with_stderr(self, *argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dr.main(["--roots", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_heartbeat_never_contaminates_the_json_document(self):
        make_build_tree(self.root / "old" / "build", age_days=400)
        code, out, err = self.run_with_stderr(
            "--json", "--fix", "--min-age-days", "7", "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        # The whole point: stdout still parses, and the lines went somewhere.
        report = json.loads(out)
        self.assertEqual(len(report["deleted"]), 1)
        self.assertIn("disk_reclaim:", err)
        self.assertNotIn("disk_reclaim:", out)

    def test_pass_start_and_candidate_count_are_announced(self):
        make_build_tree(self.root / "old" / "build", age_days=400)
        _, _, err = self.run_with_stderr(
            "--json", "--min-age-days", "7", "--pressure-free-gb", "0")
        self.assertIn("pass starting", err)
        self.assertIn("candidate(s)", err)

    def test_the_tree_being_deleted_is_named_before_the_rmtree(self):
        """The sole record of which tree was half deleted if the pass is killed."""
        stale = make_build_tree(self.root / "old" / "build", age_days=400)
        _, _, err = self.run_with_stderr(
            "--json", "--fix", "--min-age-days", "7", "--pressure-free-gb", "0")
        self.assertIn(f"removing {stale}", err)
        # Control: a dry run reclaims nothing, so it must announce no removal.
        fresh = make_build_tree(self.root / "other" / "build", age_days=400)
        _, _, err_dry = self.run_with_stderr(
            "--json", "--min-age-days", "7", "--pressure-free-gb", "0")
        self.assertTrue(fresh.is_dir())
        self.assertNotIn("removing ", err_dry)


class MultiVolumeTests(unittest.TestCase):
    """Every volume the scan spans must be measured, not just the first.

    The janitor used to read free space from roots[0] alone, so a host that
    scans a boot disk and an external volume judged pressure and the
    --fail-below-gb floor on whichever happened to be listed first. On m3 that
    is the boot disk, while the volume that actually fills is Workshop.
    """

    def setUp(self):
        self.first = tempfile.TemporaryDirectory()
        self.second = tempfile.TemporaryDirectory()
        self.addCleanup(self.first.cleanup)
        self.addCleanup(self.second.cleanup)
        self.a = pathlib.Path(self.first.name).resolve()
        self.b = pathlib.Path(self.second.name).resolve()

    def run_json(self, *argv, free=None, devices=None):
        """Run over both roots, optionally faking per-root volume facts."""
        stack = []
        if devices is not None:
            # Keyed by root, resolved by prefix: a candidate inside a root
            # really does live on that root's volume, and main() asks for the
            # device of every candidate, not just of the roots.
            def device_of(path, table=devices):
                text = str(path)
                for root, device in table.items():
                    if text == root or text.startswith(root + "/"):
                        return device
                return None
            stack.append(unittest.mock.patch.object(
                dr, "device_id", side_effect=device_of))
        if free is not None:
            stack.append(unittest.mock.patch.object(
                dr, "free_bytes", side_effect=lambda p: free[str(p)]))
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            for patcher in stack:
                patcher.start()
            try:
                code = dr.main(["--roots", f"{self.a}:{self.b}", "--json",
                                *argv])
            finally:
                for patcher in reversed(stack):
                    patcher.stop()
        return code, json.loads(buffer.getvalue())

    def separate_volumes(self):
        return {str(self.a): 101, str(self.b): 202}

    def test_the_short_gate_applies_only_to_the_pressured_volume(self):
        """A low boot disk must not shorten the gate on a healthy volume.

        Pressure selects the aggressive direction, so scoping it per volume is
        the difference between reclaiming the disk that is actually full and
        deleting a week-old build tree off a volume with terabytes free.
        """
        make_build_tree(self.a / "wt" / "build", age_days=10)
        make_build_tree(self.b / "wt" / "build", age_days=10)
        argv = ("--min-age-days", "30", "--pressure-free-gb", "200",
                "--pressure-min-age-days", "7")
        code, report = self.run_json(
            *argv,
            free={str(self.a): 10 * dr.GIB, str(self.b): 900 * dr.GIB},
            devices=self.separate_volumes())
        self.assertEqual(code, 0)
        self.assertTrue(report["pressure"])
        taken = {entry["path"] for entry in report["deleted"]}
        self.assertIn(str(self.a / "wt" / "build"), taken)
        self.assertNotIn(str(self.b / "wt" / "build"), taken)
        # Control: move the pressure to the other volume and the selection must
        # flip. Without it this passes on an implementation that simply never
        # reclaims anything found under the second root.
        _, control = self.run_json(
            *argv,
            free={str(self.a): 900 * dr.GIB, str(self.b): 10 * dr.GIB},
            devices=self.separate_volumes())
        taken_ctl = {entry["path"] for entry in control["deleted"]}
        self.assertIn(str(self.b / "wt" / "build"), taken_ctl)
        self.assertNotIn(str(self.a / "wt" / "build"), taken_ctl)

    def test_a_second_volume_below_the_floor_fails_the_pass(self):
        plenty = 900 * dr.GIB
        starved = 3 * dr.GIB
        code, report = self.run_json(
            "--fail-below-gb", "60",
            free={str(self.a): plenty, str(self.b): starved},
            devices=self.separate_volumes())
        self.assertEqual(code, 3)
        self.assertEqual(report["free_bytes_after"], starved)
        self.assertEqual(
            [v["root"] for v in report["free_bytes_by_volume_after"]],
            [str(self.a), str(self.b)])
        # Control: the identical run with the second volume healthy must pass.
        # Without it, exit 3 could just mean the floor rejects every host.
        code_ctl, report_ctl = self.run_json(
            "--fail-below-gb", "60",
            free={str(self.a): plenty, str(self.b): plenty},
            devices=self.separate_volumes())
        self.assertEqual(code_ctl, 0)
        self.assertEqual(report_ctl["free_bytes_after"], plenty)

    def test_pressure_fires_when_any_volume_is_low(self):
        make_build_tree(self.b / "wt" / "build", age_days=400)
        argv = ("--min-age-days", "30", "--pressure-free-gb", "200",
                "--pressure-min-age-days", "7")
        code, report = self.run_json(
            *argv,
            free={str(self.a): 900 * dr.GIB, str(self.b): 10 * dr.GIB},
            devices=self.separate_volumes())
        self.assertEqual(code, 0)
        self.assertTrue(report["pressure"])
        self.assertEqual(report["min_age_days"], 7)
        # Control: both volumes above the threshold stay on the long gate.
        code_ctl, report_ctl = self.run_json(
            *argv,
            free={str(self.a): 900 * dr.GIB, str(self.b): 900 * dr.GIB},
            devices=self.separate_volumes())
        self.assertEqual(code_ctl, 0)
        self.assertFalse(report_ctl["pressure"])
        self.assertEqual(report_ctl["min_age_days"], 30)

    def test_one_unreadable_volume_cannot_certify_the_floor(self):
        code, _ = self.run_json(
            "--fail-below-gb", "60",
            free={str(self.a): 900 * dr.GIB, str(self.b): None},
            devices=self.separate_volumes())
        self.assertEqual(code, 4)
        # Control: the same roots with both figures readable certify fine.
        code_ctl, _ = self.run_json(
            "--fail-below-gb", "60",
            free={str(self.a): 900 * dr.GIB, str(self.b): 900 * dr.GIB},
            devices=self.separate_volumes())
        self.assertEqual(code_ctl, 0)

    def test_two_roots_on_one_volume_are_measured_once(self):
        same = {str(self.a): 101, str(self.b): 101}
        code, report = self.run_json(
            free={str(self.a): 900 * dr.GIB, str(self.b): 900 * dr.GIB},
            devices=same)
        self.assertEqual(code, 0)
        self.assertEqual(
            [v["root"] for v in report["free_bytes_by_volume_before"]],
            [str(self.a)])
        # Control: the same two roots on distinct volumes report both.
        _, report_ctl = self.run_json(
            free={str(self.a): 900 * dr.GIB, str(self.b): 900 * dr.GIB},
            devices=self.separate_volumes())
        self.assertEqual(
            [v["root"] for v in report_ctl["free_bytes_by_volume_before"]],
            [str(self.a), str(self.b)])


class RootDiscoveryTests(unittest.TestCase):
    """The default roots must not depend on per-host hand tuning.

    A rendered plist that names one root which EXISTS but sits on the wrong
    volume passes every guard in main() and reports exit 0 forever. Discovery
    removes the tuning step that nobody performs.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = pathlib.Path(self.tmp.name).resolve()
        self.present_a = self.base / "boot-code"
        self.present_b = self.base / "workshop-code"
        self.present_a.mkdir()
        self.present_b.mkdir()
        self.absent = self.base / "not-on-this-host"

    def test_discovery_keeps_every_existing_candidate(self):
        with unittest.mock.patch.object(
                dr, "DEFAULT_ROOT_CANDIDATES",
                (str(self.present_a), str(self.absent), str(self.present_b))):
            self.assertEqual(dr.parse_roots(None),
                             [self.present_a, self.present_b])
        # Control: with no candidate present, discovery must report nothing
        # rather than invent a root. A test that only proves the present ones
        # survive would pass on a function that returns its input unfiltered.
        with unittest.mock.patch.object(
                dr, "DEFAULT_ROOT_CANDIDATES", (str(self.absent),)):
            self.assertEqual(dr.parse_roots(None), [])

    def test_an_explicitly_declared_missing_root_is_still_a_fault(self):
        """Discovery must not soften the declared-root contract."""
        buffer = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(buffer):
            code = dr.main(["--roots", str(self.absent), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("unusable scan root", buffer.getvalue())
        # Control: the same invocation against a root that exists passes.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code_ctl = dr.main(["--roots", str(self.present_a), "--json"])
        self.assertEqual(code_ctl, 0)

    def test_a_host_matching_no_candidate_exits_two(self):
        with unittest.mock.patch.object(
                dr, "DEFAULT_ROOT_CANDIDATES", (str(self.absent),)):
            buffer = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(buffer):
                code = dr.main(["--json"])
        self.assertEqual(code, 2)
        self.assertIn("no scan roots", buffer.getvalue())
        # Control: one existing candidate is enough to run a pass.
        with unittest.mock.patch.object(
                dr, "DEFAULT_ROOT_CANDIDATES", (str(self.present_a),)):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code_ctl = dr.main(["--json"])
        self.assertEqual(code_ctl, 0)


if __name__ == "__main__":
    unittest.main()
