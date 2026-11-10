#!/usr/bin/env python3
"""The checkout tier of disk_reclaim (checkout_outputs.py).

Build output in agent checkouts filled m1's data volume (2026-11-06..08) and
every VM lease failed its disk axis. The tier reclaims it after hours instead
of weeks, but only under pressure and only past the checkout gates: inside a
git checkout, no tracked file under it, and a clean or stale checkout.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

import testing_support  # noqa: F401
import checkout_outputs as co
import disk_reclaim as dr
import pulp_reapers

HOUR = 3600.0


def git(cwd: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def backdate(path: pathlib.Path, hours: float) -> None:
    stamp = time.time() - hours * HOUR
    entries = sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for entry in entries + [path]:
        os.utime(entry, (stamp, stamp), follow_symlinks=False)


def make_checkout(root: pathlib.Path, name: str = "wt") -> pathlib.Path:
    checkout = root / name
    checkout.mkdir(parents=True)
    git(checkout, "init", "-q")
    (checkout / "README").write_text("x")
    (checkout / ".gitignore").write_text("target/\nnode_modules/\nbuild/\nDerivedData/\n")
    git(checkout, "add", "README", ".gitignore")
    git(checkout, "commit", "-q", "-m", "init")
    return checkout


def make_target(checkout: pathlib.Path, hours: float = 10) -> pathlib.Path:
    target = checkout / "target"
    (target / "debug" / "deps").mkdir(parents=True)
    (target / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55")
    (target / "debug" / "deps" / "libx.rlib").write_text("y" * 64)
    backdate(target, hours)
    return target


class CheckoutGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name).resolve()

    def test_clean_checkout_with_ignored_output_passes(self) -> None:
        target = make_target(make_checkout(self.root))
        self.assertIsNone(co.checkout_gate(target))

    def test_tracked_files_keep_the_directory(self) -> None:
        checkout = make_checkout(self.root)
        target = make_target(checkout)
        (target / "keep.txt").write_text("tracked")
        git(checkout, "add", "-f", "target/keep.txt")
        git(checkout, "commit", "-q", "-m", "track")
        self.assertEqual(co.checkout_gate(target), "tracked_files")

    def test_dirty_recent_checkout_keeps_it_and_dirty_stale_does_not(self) -> None:
        checkout = make_checkout(self.root)
        target = make_target(checkout)
        (checkout / "README").write_text("edited")
        self.assertEqual(co.checkout_gate(target), "checkout_dirty_and_recent")
        backdate(checkout, co.STALE_CHECKOUT_HOURS + 1)
        self.assertIsNone(co.checkout_gate(target))

    def test_a_process_working_in_the_checkout_keeps_it(self) -> None:
        checkout = make_checkout(self.root)
        target = make_target(checkout)
        self.assertEqual(co.checkout_gate(target, cwds=[str(checkout / "src")]),
                         "checkout_in_use")
        self.assertEqual(co.checkout_gate(target, cwds=None), "process_scan_unavailable")
        self.assertIsNone(co.checkout_gate(target, cwds=[str(self.root / "elsewhere")]))

    def test_the_search_stops_at_the_scan_root(self) -> None:
        make_checkout(self.root, "home")
        loose = self.root / "home" / "Code" / "project" / "target"
        loose.mkdir(parents=True)
        self.assertEqual(co.checkout_gate(loose, root=self.root / "home" / "Code", cwds=[]),
                         "not_in_checkout")
        self.assertNotEqual(co.checkout_gate(loose, cwds=[]), "not_in_checkout",
                            "control: without the bound the home repository vouches for it")

    def test_tracked_files_under_a_bracketed_name_are_found(self) -> None:
        checkout = make_checkout(self.root)
        weird = checkout / "build-[x]"
        weird.mkdir()
        (weird / "keep.txt").write_text("tracked")
        git(checkout, "add", "-f", "build-[x]/keep.txt")
        git(checkout, "commit", "-q", "-m", "track")
        self.assertEqual(co.checkout_gate(weird, cwds=[]), "tracked_files")

    def test_outside_any_checkout_is_kept(self) -> None:
        loose = self.root / "loose" / "target"
        loose.mkdir(parents=True)
        self.assertEqual(co.checkout_gate(loose), "not_in_checkout")

    def test_git_that_cannot_answer_keeps_it(self) -> None:
        target = make_target(make_checkout(self.root))

        def broken(argv, **_kwargs):
            return subprocess.CompletedProcess(argv, 128, "", "fatal")

        self.assertEqual(co.checkout_gate(target, runner=broken, cwds=[]), "git_unanswered")

    def test_without_tomllib_the_tier_is_off(self) -> None:
        from unittest import mock
        profile = self.root / "profile.toml"
        profile.write_text("schema = 1\n[reclaim]\ncheckout_build_outputs = true\n")
        with mock.patch.dict(os.environ, {"TARTCI_FLEET_PROFILE": str(profile)}), \
                mock.patch.object(dr, "tomllib", None):
            self.assertIsNone(dr.checkout_output_settings())

    def test_settings_and_validation(self) -> None:
        self.assertIsNone(co.settings({}))
        self.assertEqual(co.settings({"checkout_build_outputs": True}), {"idle_hours": 4})
        self.assertIsNone(co.settings({"checkout_build_outputs": True,
                                       "checkout_build_output_idle_hours": 0}))
        self.assertIn("reclaim.checkout_build_output_idle_hours must be an integer from 1 through 168",
                      pulp_reapers.validate_table({"checkout_build_output_idle_hours": 500}))
        self.assertEqual(pulp_reapers.validate_table({"checkout_build_outputs": True}), [])


class ReclaimPassTests(unittest.TestCase):
    """End to end through disk_reclaim.main, against a profile that opts in."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name).resolve()
        self.root = base / "Code"
        self.root.mkdir()
        self.profile = base / "profile.toml"
        saved = {key: os.environ.get(key) for key in (
            "TARTCI_FLEET_PROFILE", "TARTCI_HOME", "TARTCI_RECLAIM_STATE_DIR",
            "TARTCI_RECLAIM_BOOT_FLOOR_GB", "TARTCI_BOOT_USAGE", "TART_HOME",
            "TARTCI_RECLAIM_LEASE_PATH")}

        def restore() -> None:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        os.environ.update({"TARTCI_FLEET_PROFILE": str(self.profile),
                           "TARTCI_HOME": str(base / "tartci"),
                           "TARTCI_RECLAIM_STATE_DIR": str(base / "tartci" / "state"),
                           "TARTCI_RECLAIM_BOOT_FLOOR_GB": "0", "TARTCI_BOOT_USAGE": "0"})
        os.environ.pop("TART_HOME", None)
        os.environ.pop("TARTCI_RECLAIM_LEASE_PATH", None)

    def opt_in(self, enabled: bool = True) -> None:
        if dr.tomllib is None:
            self.skipTest("the tier is read from the fleet profile with tomllib (3.11+), "
                          "as the reclaim agent runs it")
        self.profile.write_text(
            "schema = 1\n[reclaim]\n"
            f"checkout_build_outputs = {'true' if enabled else 'false'}\n")

    def run_pass(self, *, pressure: bool, fix: bool = True) -> dict:
        argv = ["--roots", str(self.root), "--json", "--min-age-days", "30",
                "--pressure-min-age-days", "7",
                "--pressure-free-gb", "1000000000" if pressure else "0"]
        if fix:
            argv.append("--fix")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            dr.main(argv)
        return json.loads(out.getvalue())

    def cmake_build(self, checkout: pathlib.Path, hours: float) -> pathlib.Path:
        build = checkout / "build"
        (build / "CMakeFiles").mkdir(parents=True)
        (build / "CMakeCache.txt").write_text("x")
        backdate(build, hours)
        return build

    def test_pressure_takes_hours_old_output_in_a_clean_checkout(self) -> None:
        self.opt_in()
        checkout = make_checkout(self.root)
        target = make_target(checkout, hours=10)
        build = self.cmake_build(checkout, hours=10)
        report = self.run_pass(pressure=True)
        self.assertFalse(target.exists(), report)
        self.assertFalse(build.exists(), report)
        self.assertEqual(report["checkout_build_outputs"]["selected"], 2)
        self.assertTrue((checkout / "README").is_file(), "source is never touched")

    def test_output_written_inside_the_gate_is_kept(self) -> None:
        self.opt_in()
        target = make_target(make_checkout(self.root), hours=1)
        report = self.run_pass(pressure=True)
        self.assertTrue(target.is_dir())
        self.assertIn("too_recent", [k["reason"] for k in report["kept"]])

    def test_without_pressure_nothing_moves(self) -> None:
        self.opt_in()
        checkout = make_checkout(self.root)
        target = make_target(checkout, hours=10)
        build = self.cmake_build(checkout, hours=10)
        self.run_pass(pressure=False)
        self.assertTrue(target.is_dir())
        self.assertTrue(build.is_dir())

    def test_off_by_default_keeps_the_day_gates(self) -> None:
        self.opt_in(enabled=False)
        checkout = make_checkout(self.root)
        target = make_target(checkout, hours=10)
        build = self.cmake_build(checkout, hours=10)
        report = self.run_pass(pressure=True)
        self.assertTrue(target.is_dir(), "target/ is not a candidate without the tier")
        self.assertTrue(build.is_dir(), "a 10 h tree is inside the 7-day pressure gate")
        self.assertEqual(report["checkout_build_outputs"], {"enabled": False})

    def test_dirty_recent_checkout_keeps_its_output(self) -> None:
        self.opt_in()
        checkout = make_checkout(self.root)
        target = make_target(checkout, hours=10)
        (checkout / "README").write_text("work in progress")
        report = self.run_pass(pressure=True)
        self.assertTrue(target.is_dir())
        self.assertEqual(report["checkout_build_outputs"]["kept"],
                         {"checkout_dirty_and_recent": 1})

    def test_node_modules_and_derived_data_follow_their_markers(self) -> None:
        self.opt_in()
        checkout = make_checkout(self.root)
        modules = checkout / "node_modules"
        (modules / "left-pad").mkdir(parents=True)
        (modules / "left-pad" / "package.json").write_text("{}")
        (modules / ".package-lock.json").write_text("{}")
        derived = checkout / "DerivedData"
        clone = derived / "SourcePackages" / "checkouts" / "swift-log"
        (clone / ".git").mkdir(parents=True)
        (derived / "info.plist").write_text("<plist/>")
        unmarked = checkout / "other" / "node_modules"
        unmarked.mkdir(parents=True)
        for path in (modules, derived, unmarked):
            backdate(path, 10)
        self.run_pass(pressure=True)
        self.assertFalse(modules.exists(), "package.json inside node_modules is not a source tree")
        self.assertFalse(derived.exists(), "SwiftPM's clones are regenerable")
        self.assertTrue(unmarked.is_dir(), "no lockfile marker, not proven generated")

    def test_a_process_naming_a_file_in_the_checkout_keeps_its_output(self) -> None:
        from unittest import mock
        self.opt_in()
        checkout = make_checkout(self.root)
        target = make_target(checkout, hours=10)
        server = f"123 node {checkout}/dist/index.js\n"
        with mock.patch.object(dr, "active_command_lines", return_value=server):
            report = self.run_pass(pressure=True)
        self.assertTrue(target.is_dir())
        self.assertEqual(report["checkout_build_outputs"]["kept"],
                         {"checkout_named_by_process": 1})

    def test_a_git_checkout_inside_node_modules_is_kept(self) -> None:
        self.opt_in()
        checkout = make_checkout(self.root)
        modules = checkout / "node_modules"
        (modules / "linked" / ".git").mkdir(parents=True)
        (modules / ".package-lock.json").write_text("{}")
        backdate(modules, 10)
        self.run_pass(pressure=True)
        self.assertTrue(modules.is_dir())


if __name__ == "__main__":
    unittest.main()
