#!/usr/bin/env python3
"""Behavioral tests for scripts/dr.py.

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

_ISOLATION: tempfile.TemporaryDirectory | None = None
_SAVED_ENV: dict[str, str | None] = {}


def setUpModule():
    """Keep every pass in this module off the host it runs on.

    dr.main now writes a receipt under $TARTCI_HOME and reads the INSTALLED
    fleet profile to decide whether to run Pulp's reapers. On a fleet host
    with `[reclaim] pulp_worktree_builds = true`, a test pass that reached
    the real profile would run the real reapers against the real worktrees.
    """
    global _ISOLATION
    _ISOLATION = tempfile.TemporaryDirectory()
    iso = pathlib.Path(_ISOLATION.name)
    # The floor reads the lease volume from these; the host's own Tart store
    # must not decide which volume a test pass judges.
    for key in ("TART_HOME", "TARTCI_RECLAIM_LEASE_PATH"):
        _SAVED_ENV[key] = os.environ.pop(key, None)
    for key, value in (("TARTCI_HOME", str(iso / "tartci")),
                       ("TARTCI_RECLAIM_STATE_DIR", str(iso / "tartci" / "state" / "reclaim")),
                       ("TARTCI_FLEET_PROFILE", str(iso / "no-such-profile.toml")),
                       # The boot-volume watch would judge the real boot disk;
                       # test_scratch_dirs.BootVolumeWatch covers it hermetically.
                       ("TARTCI_RECLAIM_BOOT_FLOOR_GB", "0")):
        _SAVED_ENV[key] = os.environ.get(key)
        os.environ[key] = value


def tearDownModule():
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    if _ISOLATION is not None:
        _ISOLATION.cleanup()


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


class ScanDepthTests(unittest.TestCase):
    """Depth 5 is the shipped default, and it must reach the worktree nest.

    The janitor shipped at depth 3, which reached <root>/<repo>/build and
    <root>/agent-worktrees/<worktree>/build-cov but stopped one level short of
    <root>/<repo>/.claude/worktrees/<worktree>/build. On m3 that nest held the
    largest single reclaimable tree on the volume: 14.64 GiB of the 14.86 GiB
    depth 3 could not see, so the miss was most of the deep bytes rather than a
    rounding error.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # macOS puts TMPDIR under /var, a symlink to /private/var, and the
        # janitor resolves its roots. Resolve here so paths compare equal.
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def nest(self) -> pathlib.Path:
        """The shape depth 3 missed, five levels below the scan root."""
        return self.root / "pulp" / ".claude" / "worktrees" / "agent-x" / "build"

    @staticmethod
    def env_without_depth() -> dict[str, str]:
        """The real environment minus the depth override.

        Cleared outright it would take PATH with it, and the pass shells out
        to `ps` to sample live builds.
        """
        return {key: value for key, value in os.environ.items()
                if key != "TARTCI_RECLAIM_MAXDEPTH"}

    def run_json(self, *argv):
        buffer = io.StringIO()
        with unittest.mock.patch.dict(os.environ, self.env_without_depth(),
                                      clear=True):
            with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
                code = dr.main(["--roots", str(self.root), "--json", *argv])
        return code, json.loads(buffer.getvalue())

    def test_default_maxdepth_is_five(self):
        with unittest.mock.patch.dict(os.environ, self.env_without_depth(),
                                      clear=True):
            self.assertEqual(dr.build_parser().parse_args([]).maxdepth, 5)
        # Control: the environment still overrides. Without it, the 5 above
        # would pass just as happily on a default nothing can reach.
        with unittest.mock.patch.dict(
                os.environ, {"TARTCI_RECLAIM_MAXDEPTH": "7"}, clear=False):
            self.assertEqual(dr.build_parser().parse_args([]).maxdepth, 7)

    def test_the_worktree_nest_is_reached_at_five_and_missed_at_three(self):
        deep = make_build_tree(self.nest())
        shallow = make_build_tree(self.root / "pulp" / "build")
        self.assertNotIn(deep, dr.find_candidates([self.root], maxdepth=3))
        # Control: depth 3 is not simply blind here. It finds the shallow tree
        # in the same fixture, so the miss above is the depth bound and not a
        # scan that returned nothing at all.
        self.assertIn(shallow, dr.find_candidates([self.root], maxdepth=3))
        found = dr.find_candidates([self.root], maxdepth=5)
        self.assertIn(deep, found)
        self.assertIn(shallow, found)

    def test_a_default_pass_reclaims_the_nest(self):
        """The default must reach the nest, not merely an explicit --maxdepth.

        find_candidates has always accepted a deeper bound; what was wrong was
        the number every hourly pass actually ran with.
        """
        deep = make_build_tree(self.nest(), age_days=400)
        code, report = self.run_json("--fix")
        self.assertEqual(code, 0)
        self.assertEqual([record["path"] for record in report["deleted"]],
                         [str(deep)])
        self.assertFalse(deep.exists())

    def test_depth_five_exposes_vendored_payloads_and_still_refuses_them(self):
        """Past depth 3 the marker gate is what protects a vendored tree.

        Depth 3 kept external/skia-build/build out of reach by an accident of
        geometry rather than by judging it. Depth 5 sees it, so the refusal has
        to come from the absent generated-tree marker. Measured on m3, depth 5
        newly exposed 94 build-named directories and refused 88 of them exactly
        this way.
        """
        vendored = self.root / "pulp" / "external" / "skia-build" / "build"
        (vendored / "lib").mkdir(parents=True)
        (vendored / "lib" / "libskia.a").write_text("prebuilt")
        age(vendored, 400)
        code, report = self.run_json("--maxdepth", "5", "--fix")
        self.assertEqual(code, 0)
        self.assertIn(str(vendored),
                      [record["path"] for record in report["kept"]])
        self.assertTrue(vendored.exists())
        # Control: the identical path carrying a generated marker IS reclaimed,
        # so the survival above is the marker gate doing the work and not the
        # scan quietly failing to reach five levels down.
        make_build_tree(vendored, age_days=400)
        code_ctl, report_ctl = self.run_json("--maxdepth", "5", "--fix")
        self.assertEqual(code_ctl, 0)
        self.assertEqual([record["path"] for record in report_ctl["deleted"]],
                         [str(vendored)])
        self.assertFalse(vendored.exists())


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

    def test_every_source_marker_refuses_the_tree_on_its_own(self):
        """Each spelling in SOURCE_MARKERS has to stop a delete by itself.

        Only .git carried any test weight, and the other three are exactly the
        spellings that tell a checkout apart from generator output. The scan
        reaches <repo>/.claude/worktrees/<worktree>/ now, so a typo in one of
        them costs a source tree rather than a build directory, and that is the
        single irreversible decision this script makes.
        """
        # Spelled out rather than read from dr.SOURCE_MARKERS. Iterating the
        # constant under test makes the loop shrink with it, so deleting three
        # of the four spellings left this green -- confirmed by breaking it.
        spellings = (".git", "CMakeLists.txt", "Cargo.toml", "package.json")
        self.assertEqual(set(dr.SOURCE_MARKERS), set(spellings),
                         "a marker changed without a case here to cover it")
        for index, marker in enumerate(spellings):
            with self.subTest(marker=marker):
                path = make_build_tree(self.root / f"wt{index}" / "build",
                                       age_days=400)
                # Written as a file for every marker, .git included: a linked
                # worktree's .git is a regular file holding a gitdir: line, and
                # a linked worktree is the shape living in that nest.
                (path / marker).write_text("{}")
                age(path, 400)
                delete, reason, _ = self.classify(path)
                self.assertFalse(delete, marker)
                self.assertEqual(reason, "source_tree", marker)
        # Control: the same tree at the same age carrying no source marker must
        # be taken. Without it every assertion above passes just as well on an
        # implementation that never deletes anything in this fixture.
        control = make_build_tree(self.root / "unmarked" / "build",
                                  age_days=400)
        delete, reason, _ = self.classify(control)
        self.assertTrue(delete, reason)

    def test_a_fetched_dependency_checkout_stops_the_delete(self):
        """A build tree is deleted whole, so what is INSIDE it is at stake.

        The depth-0 marker check answers "is this directory a source tree" and
        cannot answer this: a build tree that fetched its dependencies holds
        real checkouts at `_deps/<name>-src/.git`, and a dependency carrying
        local edits, or one with no `.git` of its own, does not come back.
        """
        for depth, parts in enumerate((
            ("mywork",),                       # a worktree parked inside
            ("_deps", "dawn-src"),             # the FetchContent shape
            ("_deps", "dawn-src", "third"),    # one level deeper again
        )):
            with self.subTest(parts=parts):
                path = make_build_tree(self.root / f"d{depth}" / "build",
                                       age_days=400)
                nested = path.joinpath(*parts)
                nested.mkdir(parents=True)
                (nested / "CMakeLists.txt").write_text("project(dep)")
                (nested / ".git").write_text("gitdir: elsewhere")
                age(path, 400)
                delete, reason, _ = self.classify(path)
                self.assertFalse(delete, reason)
                self.assertTrue(reason.startswith("nested_source_tree"), reason)
        # Control. Every refusal above is also satisfied by a classifier that
        # has stopped deleting anything at all, so the same tree carrying the
        # same nested directory WITHOUT a source marker must still be taken.
        control = make_build_tree(self.root / "control" / "build",
                                  age_days=400)
        plain = control / "_deps" / "dawn-src"
        plain.mkdir(parents=True)
        (plain / "dawn.cpp").write_text("int main(){}")
        age(control, 400)
        delete, reason, _ = self.classify(control)
        self.assertTrue(delete, reason)

    def test_a_nested_scan_that_was_refused_is_not_an_absence(self):
        # Same refusal-versus-answer rule the age and process scans follow. A
        # subtree we could not read could hold a checkout.
        path = make_build_tree(self.root / "locked" / "build", age_days=400)
        (path / "_deps").mkdir()
        (path / "_deps" / "dep-src").mkdir()
        age(path, 400)
        real_scandir = dr.os.scandir

        def refusing(target):
            # Refused at depth two, which the age scan's shallower walk never
            # enters. Refusing higher would make `newest_mtime` answer
            # "unmeasured" first and this test would never reach the scan it
            # names -- which is how it was first written, and how it failed.
            if str(target).endswith("dep-src"):
                raise PermissionError(errno.EACCES, "refused", str(target))
            return real_scandir(target)

        with unittest.mock.patch.object(dr.os, "scandir", refusing):
            delete, reason, _ = self.classify(path)
        self.assertFalse(delete)
        self.assertEqual(reason, "nested_scan_unreadable")
        # Control: the identical tree, readable, is taken.
        delete, reason, _ = self.classify(path)
        self.assertTrue(delete, reason)

    def test_the_nested_scan_does_not_mistake_the_tree_for_its_own_content(self):
        # The candidate's OWN markers are guard-0's job. If this scan counted
        # them the generated-marker path would never delete anything, and the
        # control in every test above would be the thing that broke.
        path = make_build_tree(self.root / "plain" / "build", age_days=400)
        found, readable = dr.nested_source_marker(path)
        self.assertTrue(readable)
        self.assertIsNone(found)

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

    def test_the_report_records_the_depth_the_scan_actually_used(self):
        """A receipt that cannot name its own depth cannot be read.

        The scan depth was wrong on the whole fleet and invisible in every
        report it wrote: a pass that "found nothing deep" looked identical
        whether it had walked three levels or five, so the blind spot was only
        found by measuring by hand. Recording the depth beside the candidate
        count it produced is what makes a later receipt answerable.
        """
        # Nested so the build tree sits four levels below the root: reachable
        # at depth 5, out of reach at depth 3. That is the same shape as the
        # agent worktree nest, <repo>/.claude/worktrees/<worktree>/build.
        deep = make_build_tree(self.root / "repo" / "nest" / "wt" / "build",
                               age_days=400)

        code_deep, deep_report = self.run_json("--maxdepth", "5",
                                               "--min-age-days", "7",
                                               "--pressure-free-gb", "0")
        self.assertEqual(code_deep, 0)
        self.assertEqual(deep_report["maxdepth"], 5)
        self.assertEqual(len(deep_report["deleted"]), 1,
                         "control: depth 5 has to reach the nested tree")

        code_shallow, shallow_report = self.run_json("--maxdepth", "3",
                                                     "--min-age-days", "7",
                                                     "--pressure-free-gb", "0")
        self.assertEqual(code_shallow, 0)
        # Two different values from two runs, so a hardcoded constant in place
        # of the real argument fails here rather than reading plausibly.
        self.assertEqual(shallow_report["maxdepth"], 3)
        self.assertEqual(shallow_report["candidates"], 0,
                         "control: depth 3 must not reach it")
        self.assertTrue(deep.is_dir(), "both passes are dry runs")


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

    def test_a_zero_age_gate_is_refused_rather_than_obeyed(self):
        """The two knobs that decide whether anything is examined at all.

        A zero age gate deletes every generated tree the scan reaches the
        moment it reaches it, and a depth below one makes the scan return
        nothing while still exiting 0 - a janitor reporting success for having
        looked nowhere. Both are refusals, not defaults to clamp, because a
        clamped value would run a pass the operator did not ask for.
        """
        stale = make_build_tree(self.root / "wt" / "build", age_days=400)
        for argv in (("--min-age-days", "0"),
                     ("--min-age-days", "-1"),
                     ("--pressure-min-age-days", "0"),
                     ("--maxdepth", "0")):
            with self.subTest(argv=argv):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = dr.main(["--roots", str(self.root), "--json",
                                    "--fix", *argv])
                self.assertEqual(code, 2)
                self.assertEqual(buffer.getvalue(), "",
                                 "a refused run must not emit a report")
                self.assertTrue(stale.is_dir(), argv)
        # Control: the same --fix run with both knobs positive does delete,
        # so the refusals above are the flags and not the fixture.
        code, report = self.run_json("--fix", "--min-age-days", "7",
                                     "--pressure-free-gb", "0")
        self.assertEqual(code, 0)
        self.assertEqual(len(report["deleted"]), 1)
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


class TwoRootHarness(unittest.TestCase):
    """Two scan roots, with per-root volume facts that can be faked."""

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


class MultiVolumeTests(TwoRootHarness):
    """Every volume the scan spans must be measured, not just the first.

    The janitor used to read free space from roots[0] alone, so a host that
    scans a boot disk and an external volume judged pressure and, when no
    Tart store is declared, the --fail-below-gb floor on whichever happened
    to be listed first.
    """

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


class LeaseVolumeFloorTests(TwoRootHarness):
    """The floor is judged on the volume that holds the Tart store.

    Lease admission probes $TART_HOME, so "this host will refuse leases" is a
    statement about that volume alone. m3 keeps its VMs on Workshop and scans
    a boot-disk ~/Code whose volume is mostly personal data the janitor must
    never touch; judging the floor there failed every pass on a host that was
    leasing fine.
    """

    def setUp(self):
        super().setUp()
        self.vms = self.b / "VMs"
        self.vms.mkdir()

    def free_table(self, a, b):
        return {str(self.a): a, str(self.b): b, str(self.vms): b}

    def test_a_low_scan_volume_off_the_lease_volume_does_not_fail(self):
        code, report = self.run_json(
            "--fail-below-gb", "60", "--lease-path", str(self.vms),
            free=self.free_table(3 * dr.GIB, 900 * dr.GIB),
            devices=self.separate_volumes())
        self.assertEqual(code, 0)
        self.assertEqual(report["floor_scope"], "lease_volume")
        self.assertEqual(report["free_bytes_after"], 900 * dr.GIB)
        self.assertEqual(report["scan_volumes_below_floor"], [str(self.a)])
        # Both scanned volumes are still reported, separately.
        self.assertEqual(
            [v["root"] for v in report["free_bytes_by_volume_after"]],
            [str(self.a), str(self.b)])
        # Control: starve the lease volume instead and the floor must fire.
        code_ctl, report_ctl = self.run_json(
            "--fail-below-gb", "60", "--lease-path", str(self.vms),
            free=self.free_table(900 * dr.GIB, 3 * dr.GIB),
            devices=self.separate_volumes())
        self.assertEqual(code_ctl, 3)
        self.assertEqual(report_ctl["free_bytes_after"], 3 * dr.GIB)
        self.assertEqual(report_ctl["scan_volumes_below_floor"], [])

    def test_a_lease_volume_shared_with_the_scan_still_fails(self):
        """m5's shape: Tart store and ~/Code on one internal disk."""
        same = {str(self.a): 101, str(self.b): 101}
        code, report = self.run_json(
            "--fail-below-gb", "60", "--lease-path", str(self.vms),
            free=self.free_table(3 * dr.GIB, 3 * dr.GIB), devices=same)
        self.assertEqual(code, 3)
        self.assertEqual(report["scan_volumes_below_floor"], [])

    def test_tart_home_names_the_lease_volume(self):
        with unittest.mock.patch.dict(os.environ, {"TART_HOME": str(self.vms)}):
            code, report = self.run_json(
                "--fail-below-gb", "60",
                free=self.free_table(3 * dr.GIB, 900 * dr.GIB),
                devices=self.separate_volumes())
        self.assertEqual(code, 0)
        self.assertEqual(report["lease_path_source"], "TART_HOME")
        # Control: nothing declared keeps the legacy every-volume floor.
        code_ctl, report_ctl = self.run_json(
            "--fail-below-gb", "60",
            free=self.free_table(3 * dr.GIB, 900 * dr.GIB),
            devices=self.separate_volumes())
        self.assertEqual(code_ctl, 3)
        self.assertEqual(report_ctl["floor_scope"], "scan_volumes")

    def test_the_fleet_profile_names_the_lease_volume(self):
        profile = self.a / "profile.toml"
        profile.write_text(f'[host]\ntart_home = "{self.vms}"\n')
        with unittest.mock.patch.dict(
                os.environ, {"TARTCI_FLEET_PROFILE": str(profile)}):
            code, report = self.run_json(
                "--fail-below-gb", "60",
                free=self.free_table(3 * dr.GIB, 900 * dr.GIB),
                devices=self.separate_volumes())
        self.assertEqual(code, 0)
        self.assertEqual(report["lease_path"], str(self.vms))
        self.assertIn("[host].tart_home", report["lease_path_source"])

    def test_an_unavailable_lease_volume_cannot_certify_the_floor(self):
        missing = str(self.b / "unmounted" / "VMs")
        free = self.free_table(900 * dr.GIB, 900 * dr.GIB)
        free[missing] = None
        code, report = self.run_json(
            "--fail-below-gb", "60", "--lease-path", missing,
            free=free, devices=self.separate_volumes())
        self.assertEqual(code, 4)
        self.assertIsNone(report["free_bytes_after"])


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

    def test_profile_reclaim_paths_add_the_external_volume_root(self):
        """A host whose external volume is not named Workshop is still scanned.

        m5studio keeps code on /Volumes/Atelier; the root comes from the
        installed profile's [reclaim] repo/worktrees_root, not a volume name.
        """
        code = self.base / "atelier" / "Code"
        (code / "pulp").mkdir(parents=True)
        profile = self.base / "profile.toml"
        profile.write_text(
            "[reclaim]\n"
            f'repo = "{code}/pulp"\n'
            f'worktrees_root = "{code}/agent-worktrees"\n')
        with unittest.mock.patch.dict(os.environ, {"TARTCI_FLEET_PROFILE": str(profile)}), \
                unittest.mock.patch.object(
                    dr, "DEFAULT_ROOT_CANDIDATES", (str(self.present_a),)):
            self.assertEqual(dr.parse_roots(None), [self.present_a, code])
        # Control: without the profile only the built-in candidate remains.
        with unittest.mock.patch.dict(
                os.environ, {"TARTCI_FLEET_PROFILE": str(self.absent)}), \
                unittest.mock.patch.object(
                    dr, "DEFAULT_ROOT_CANDIDATES", (str(self.present_a),)):
            self.assertEqual(dr.parse_roots(None), [self.present_a])

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


class RotateLogTests(unittest.TestCase):
    """The janitor's own receipt must not be the thing that fills the disk.

    launchd appends every hourly pass to `StandardOutPath` and nothing has
    ever bounded it, so these cover the rotation that does. Every negative
    assertion here is paired with a control that MUST rotate, because a
    rotation that silently does nothing passes a "the file survived" test
    exactly as happily as one that correctly refused.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name).resolve()
        self.log = self.dir / "tartci-reclaim.log"
        self.addCleanup(self.tmp.cleanup)

    def write_log(self, size: int, content: str = "a") -> None:
        self.log.write_text(content * size)

    def test_an_oversized_log_is_renamed_aside_and_a_fresh_one_takes_over(self):
        self.write_log(64, "o")
        self.assertTrue(dr.rotate_log(self.log, max_bytes=64, generations=3))
        # Renamed, not truncated: launchd's inherited fd still points at the
        # old inode, so this pass's own output has to keep landing somewhere.
        self.assertEqual((self.dir / "tartci-reclaim.log.1").read_text(),
                         "o" * 64)
        self.assertTrue(self.log.exists())
        self.assertEqual(self.log.read_text(), "")

    def test_a_log_under_the_bound_is_left_alone(self):
        self.write_log(63, "o")
        self.assertFalse(dr.rotate_log(self.log, max_bytes=64, generations=3))
        self.assertEqual(self.log.read_text(), "o" * 63)
        self.assertFalse((self.dir / "tartci-reclaim.log.1").exists())
        # Control: one more byte on the SAME instrument must rotate, so a
        # rotation that can never fire cannot pass the assertions above.
        self.write_log(64, "o")
        self.assertTrue(dr.rotate_log(self.log, max_bytes=64, generations=3))
        self.assertTrue((self.dir / "tartci-reclaim.log.1").exists())

    def test_generations_shuffle_down_and_the_oldest_is_dropped(self):
        for index in (1, 2):
            (self.dir / f"tartci-reclaim.log.{index}").write_text(f"gen{index}")
        self.write_log(64, "n")
        self.assertTrue(dr.rotate_log(self.log, max_bytes=64, generations=2))
        self.assertEqual((self.dir / "tartci-reclaim.log.1").read_text(),
                         "n" * 64)
        self.assertEqual((self.dir / "tartci-reclaim.log.2").read_text(), "gen1")
        # gen2's content is gone: what proves the drop is generation 2 now
        # carrying gen1, not the absence below. The ceiling assertion is a
        # guard against a future refactor, and it is deliberately not the
        # evidence -- removing either the unlink or the shuffle's upper bound
        # leaves it green, because the two enforce the ceiling jointly.
        self.assertFalse((self.dir / "tartci-reclaim.log.3").exists())

    def test_a_symlink_is_refused_rather_than_followed(self):
        target = self.dir / "elsewhere.log"
        target.write_text("t" * 64)
        link = self.dir / "linked.log"
        link.symlink_to(target)
        stream = io.StringIO()
        self.assertFalse(dr.rotate_log(link, max_bytes=64, generations=3,
                                       stream=stream))
        self.assertIn("not a user-owned regular file", stream.getvalue())
        # Neither the link nor what it points at was renamed: following it
        # would let anything that can write this directory pick the victim.
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "t" * 64)
        self.assertFalse((self.dir / "linked.log.1").exists())
        # Control: the identical size through a real file does rotate.
        self.write_log(64, "t")
        self.assertTrue(dr.rotate_log(self.log, max_bytes=64, generations=3))

    def test_an_absent_log_is_not_an_error(self):
        self.assertFalse(dr.rotate_log(self.dir / "never-written.log",
                                       max_bytes=64, generations=3))
        self.assertFalse((self.dir / "never-written.log").exists())

    def test_a_zero_bound_disables_rotation(self):
        self.write_log(64, "z")
        self.assertFalse(dr.rotate_log(self.log, max_bytes=0, generations=3))
        self.assertFalse((self.dir / "tartci-reclaim.log.1").exists())
        # Control: the same file with a real bound rotates.
        self.assertTrue(dr.rotate_log(self.log, max_bytes=64, generations=3))
        self.assertTrue((self.dir / "tartci-reclaim.log.1").exists())


class LogRotationWiringTests(unittest.TestCase):
    """A rotation nothing calls bounds nothing.

    This is the half that actually ships: `rotate_log` passing its own unit
    tests while `main` never reaches it would leave the log growing exactly
    as it does today, and every test above would still be green.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.scan = self.root / "code"
        self.scan.mkdir()
        self.log = self.root / "tartci-reclaim.log"
        self.addCleanup(self.tmp.cleanup)

    def run_main(self, *argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return dr.main(["--roots", str(self.scan), *argv])

    def test_a_pass_rotates_an_oversized_log_before_it_writes(self):
        self.log.write_text("o" * 64)
        code = self.run_main("--json", "--log-path", str(self.log),
                             "--log-max-bytes", "64", "--log-generations", "3")
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "tartci-reclaim.log.1").read_text(),
                         "o" * 64)

    def test_a_pass_leaves_a_log_under_the_bound_alone(self):
        self.log.write_text("o" * 63)
        code = self.run_main("--json", "--log-path", str(self.log),
                             "--log-max-bytes", "64", "--log-generations", "3")
        self.assertEqual(code, 0)
        self.assertEqual(self.log.read_text(), "o" * 63)
        self.assertFalse((self.root / "tartci-reclaim.log.1").exists())

    def test_without_a_log_path_a_pass_rotates_nothing(self):
        self.log.write_text("o" * 64)
        code = self.run_main("--json", "--log-max-bytes", "64")
        self.assertEqual(code, 0)
        self.assertEqual(self.log.read_text(), "o" * 64)
        self.assertFalse((self.root / "tartci-reclaim.log.1").exists())

    def test_the_env_var_supplies_the_log_path_launchd_will_set(self):
        # The plist sets TARTCI_RECLAIM_LOG; the flag is for a human. Parsing
        # the env happens at parser construction, so this has to be patched
        # around the call rather than set once in setUp.
        self.log.write_text("o" * 64)
        with unittest.mock.patch.dict(
                os.environ,
                {"TARTCI_RECLAIM_LOG": str(self.log),
                 "TARTCI_RECLAIM_LOG_MAX_BYTES": "64",
                 "TARTCI_RECLAIM_LOG_GENERATIONS": "3"}):
            code = self.run_main("--json")
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "tartci-reclaim.log.1").read_text(),
                         "o" * 64)



class BoundedScandirTests(unittest.TestCase):
    """A directory listing that never returns is abandoned, named, and not judged."""

    def setUp(self) -> None:
        import threading
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.blocked = self.root / "stuck"
        self.blocked.mkdir()
        real = os.scandir

        def scandir(path):
            if pathlib.Path(path) == self.blocked:
                self.release.wait(30)   # m1 on 2026-10-02: open() never returned
            return real(path)
        self.patch(dr.os, "scandir", scandir)
        self.patch(dr, "SCANDIR_TIMEOUT_S", 0.2)
        self.patch(dr, "SCAN_TIMEOUTS", [])

    def patch(self, owner, name, value) -> None:
        original = getattr(owner, name)
        setattr(owner, name, value)
        self.addCleanup(setattr, owner, name, original)

    def test_a_listing_that_never_returns_times_out_and_is_named(self) -> None:
        started = time.monotonic()
        with self.assertRaises(OSError) as raised:
            dr.bounded_scandir(self.blocked)
        self.assertEqual(raised.exception.errno, errno.ETIMEDOUT)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(dr.SCAN_TIMEOUTS, [str(self.blocked)])

    def test_a_normal_listing_returns_its_entries(self) -> None:
        # Control, same instrument: an unblocked directory lists normally.
        (self.root / "build").mkdir()
        names = sorted(e.name for e in dr.bounded_scandir(self.root))
        self.assertEqual(names, ["build", "stuck"])
        self.assertEqual(dr.SCAN_TIMEOUTS, [])

    def test_a_timed_out_tree_is_unmeasured_never_old(self) -> None:
        # newest_mtime None keeps the tree; reading the timeout as "empty"
        # would make it look maximally idle and delete it.
        self.assertIsNone(dr.newest_mtime(self.blocked))

    def test_the_candidate_scan_finishes_past_a_stuck_directory(self) -> None:
        (self.root / "other" / "build").mkdir(parents=True)
        found = dr.find_candidates([self.root], maxdepth=4)
        self.assertEqual(found, [self.root / "other" / "build"])
        self.assertEqual(dr.SCAN_TIMEOUTS, [str(self.blocked)])


class AppContainerSkipTests(unittest.TestCase):
    """Other apps' data containers are never listed: listing one prompts."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.listed: list[str] = []
        real = os.scandir

        def scandir(path):
            self.listed.append(str(path))
            return real(path)
        for owner, name, value in ((dr.os, "scandir", scandir), (dr, "APP_CONTAINERS_SKIPPED", [])):
            original = getattr(owner, name)
            setattr(owner, name, value)
            self.addCleanup(setattr, owner, name, original)

    def test_a_containers_tree_is_skipped_unlisted(self) -> None:
        # m1 on 2026-10-02: a listing under launchd waited forever on the
        # "access data from other apps" consent prompt.
        containers = self.root / "Library" / "Containers" / "com.example.app" / "build"
        containers.mkdir(parents=True)
        group = self.root / "Library" / "Group Containers" / "group.example" / "build"
        group.mkdir(parents=True)
        found = dr.find_candidates([self.root], maxdepth=6)
        self.assertEqual(found, [])
        self.assertFalse(any("Containers" in path for path in self.listed), self.listed)
        self.assertEqual(sorted(dr.APP_CONTAINERS_SKIPPED),
                         [str(self.root / "Library" / "Containers"),
                          str(self.root / "Library" / "Group Containers")])

    def test_a_skipped_container_is_unmeasured_never_old(self) -> None:
        path = self.root / "Library" / "Containers" / "com.example.app"
        path.mkdir(parents=True)
        self.assertIsNone(dr.newest_mtime(path))

    def test_ordinary_directories_are_still_scanned(self) -> None:
        # Control, same instrument: a build tree outside any container is found,
        # including one under a Library that is not a container directory.
        (self.root / "Library" / "Caches" / "build").mkdir(parents=True)
        (self.root / "proj" / "build").mkdir(parents=True)
        found = dr.find_candidates([self.root], maxdepth=6)
        self.assertEqual(found, [self.root / "Library" / "Caches" / "build",
                                 self.root / "proj" / "build"])
        self.assertEqual(dr.APP_CONTAINERS_SKIPPED, [])


class HomeRootGuardTests(unittest.TestCase):
    """No pass may walk the home directory: ~/Library prompts under launchd."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name).resolve() / "home"
        (self.home / "Code" / "pulp").mkdir(parents=True)
        (self.home / "Library" / "Mail").mkdir(parents=True)
        self.profile = pathlib.Path(self.tmp.name) / "profile.toml"
        for patcher in (unittest.mock.patch.dict(os.environ, {"HOME": str(self.home),
                                                              "TARTCI_FLEET_PROFILE": str(self.profile)}),
                        unittest.mock.patch.object(dr, "DEFAULT_ROOT_CANDIDATES", ())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def roots(self, repo: str, worktrees_root: str) -> tuple[list[pathlib.Path], str]:
        if dr.tomllib is None:
            self.skipTest("profile roots need tomllib (Python 3.11+), as on the fleet")
        self.profile.write_text(f'[reclaim]\nrepo = "{repo}"\nworktrees_root = "{worktrees_root}"\n')
        err = io.StringIO()
        with redirect_stderr(err):
            roots = dr.parse_roots(None)
        return roots, err.getvalue()

    def test_m1s_profile_scans_code_not_home(self) -> None:
        # m1 and m5 set worktrees_root = ~/Code; its parent is $HOME, and every
        # pass walked ~/Library and hung on the "data from other apps" prompt.
        code = self.home / "Code"
        roots, err = self.roots(f"{code}/pulp", str(code))
        self.assertEqual(roots, [code])
        # Not merely filtered by the guard: home is never a candidate at all.
        self.assertNotIn(str(self.home), dr.profile_root_candidates())
        self.assertNotIn("REFUSED", err)

    def test_a_profile_root_at_home_is_refused_loudly(self) -> None:
        roots, err = self.roots(f"{self.home}/pulp", str(self.home))
        self.assertEqual(roots, [])
        self.assertIn("REFUSED scan root", err)
        self.assertIn("home directory", err)

    def test_an_explicit_home_root_fails_the_pass(self) -> None:
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = dr.main(["--roots", str(self.home), "--json"])
        self.assertEqual(code, 2)
        self.assertIn("refused", err.getvalue())

    def test_every_fleet_profile_yields_its_code_directory(self) -> None:
        # The four [reclaim] tables as deployed on 2026-10-02 (home and the
        # external volumes stand in under this test's temporary directory).
        base = pathlib.Path(self.tmp.name).resolve()
        workshop, atelier = base / "Workshop" / "Code", base / "Atelier" / "Code"
        for code in (workshop, atelier):
            (code / "agent-worktrees").mkdir(parents=True)
        home_code = self.home / "Code"
        cases = {
            "m3": (f"{workshop}/pulp", f"{workshop}/agent-worktrees", [workshop]),
            "m5studio": (f"{atelier}/pulp", f"{atelier}/agent-worktrees", [atelier]),
            "m1": (f"{home_code}/pulp", str(home_code), [home_code]),
            "m5": (f"{home_code}/pulp", str(home_code), [home_code]),
        }
        for host, (repo, worktrees_root, expected) in cases.items():
            with self.subTest(host=host):
                roots, err = self.roots(repo, worktrees_root)
                self.assertEqual(roots, expected)
                self.assertNotIn("REFUSED", err)

    def test_a_whole_volume_root_is_refused(self) -> None:
        self.assertIn("whole volume", dr.refused_root(pathlib.Path("/Volumes/Workshop")))

    def test_a_refused_listing_is_named_never_silent(self) -> None:
        code = self.home / "Code"
        locked = code / "locked"
        locked.mkdir()
        (code / "proj" / "build").mkdir(parents=True)
        real = os.scandir

        def scandir(path):
            if pathlib.Path(path) == locked:
                raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
            return real(path)
        with unittest.mock.patch.object(dr.os, "scandir", scandir), \
                unittest.mock.patch.object(dr, "SCAN_REFUSED", []):
            err = io.StringIO()
            with redirect_stderr(err):
                found = dr.find_candidates([code], maxdepth=4)
            self.assertEqual(found, [code / "proj" / "build"])
            self.assertEqual(dr.SCAN_REFUSED, [str(locked)])
            self.assertIn(f"skipped {locked}", err.getvalue())

    def test_a_root_nested_in_another_is_walked_once(self) -> None:
        # m3 and m5studio: worktrees_root (…/Code/agent-worktrees) sits inside
        # repo's parent (…/Code).
        code = self.home / "Code"
        (code / "agent-worktrees").mkdir()
        roots, _ = self.roots(f"{code}/pulp", f"{code}/agent-worktrees")
        self.assertEqual(roots, [code])

    def test_roots_inside_or_above_library_are_refused(self) -> None:
        self.assertIsNotNone(dr.refused_root(self.home / "Library" / "Mail"))
        self.assertIsNotNone(dr.refused_root(pathlib.Path("/")))
        # An ordinary directory above home still contains ~/Library.
        self.assertIn("contains", dr.refused_root(self.home.parent))
        # Control: a code directory under home is a valid root.
        self.assertIsNone(dr.refused_root(self.home / "Code"))

    def test_the_walk_never_lists_home_library(self) -> None:
        with self.assertRaises(OSError) as raised:
            dr.bounded_scandir(self.home / "Library" / "Mail")
        self.assertEqual(raised.exception.errno, errno.EPERM)

if __name__ == "__main__":
    unittest.main()
