#!/usr/bin/env python3
"""Behavioral tests for scripts/disk_reclaim.py.

The janitor deletes directories, so every negative assertion here is paired
with a control in the same tree that MUST be deleted. A test that only proves
"the source tree survived" passes just as happily when the scan found nothing
at all, and that is the failure mode worth catching.

Run:  python3 -m unittest scripts.test_disk_reclaim   (or via discover)
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout

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


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # macOS puts TMPDIR under /var, a symlink to /private/var, and the
        # janitor resolves its roots. Resolve here so paths compare equal.
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def run_main(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
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


if __name__ == "__main__":
    unittest.main()
