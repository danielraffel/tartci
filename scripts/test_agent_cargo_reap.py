#!/usr/bin/env python3
"""Agents' stale Cargo target dirs (scripts/agent_cargo_reap.py)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_cargo_reap as acr  # noqa: E402

NOW = 1792120000.0
DAY = 86400


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(os.path.realpath(tmp.name))
        self.scratch = self.home / "agent-scratch"
        self.scratch.mkdir()
        (self.home / ".config" / "tartci").mkdir(parents=True)
        self.flag = self.home / ".config" / "tartci" / "reap-agent-cargo.enabled"
        patcher = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci")})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("TARTCI_AGENT_SCRATCH", "TARTCI_REAP_AGENT_CARGO_ENABLE_FILE"):
            os.environ.pop(key, None)
        self.removed: list[Path] = []

    def target(self, rel: str, built_days_ago: float, cargo: bool = True) -> Path:
        path = self.scratch / rel
        (path / "debug" / ".fingerprint").mkdir(parents=True)
        if cargo:
            (path / "CACHEDIR.TAG").write_text(acr.CARGO_SIGNATURE + "\n# created by cargo.\n")
        stamp = NOW - built_days_ago * DAY
        for p in (path / "debug" / ".fingerprint", path / "debug", path):
            os.utime(p, (stamp, stamp))
        return path

    def run_pass(self, opened=(), now: float = NOW) -> list[str]:
        return acr.watchdog_pass(
            now, user_home=self.home, opened=lambda: list(opened) if opened is not None else None,
            measure=lambda path, timeout=0: 50 * acr.GB,
            remove=lambda path: self.removed.append(path))


class Plan(Base):
    def test_stale_and_unopened_is_deleted_when_the_host_opted_in(self) -> None:
        stale = self.target("sy-impl3/target-rba", 3)
        self.flag.write_text("")
        lines = self.run_pass()
        self.assertEqual(self.removed, [stale])
        self.assertIn(f"WARN agent-cargo: 1 stale Cargo target dir(s), 50 GB: {stale} 50 GB "
                      "idle 3.0 d", lines[0])
        self.assertIn(f"agent-cargo: deleted {stale} (50 GB)", lines[1])

    def test_fresh_open_rcg_and_non_cargo_dirs_are_kept(self) -> None:
        self.flag.write_text("")
        fresh = self.target("sy-impl4/target", 1)
        busy = self.target("infra-review4/target-x", 5)
        rcg = self.target("rcg/target-one", 30)
        plain = self.target("pr10388/target", 30, cargo=False)
        lines = self.run_pass(opened=[f"{busy}/debug/deps/libfoo.rlib"])
        self.assertEqual(self.removed, [])
        self.assertEqual(lines, [])
        rows = {r["path"]: r for r in acr.plan(self.scratch, NOW, [str(busy)],
                                               lambda p, timeout=0: 1)}
        self.assertEqual(rows[str(fresh)]["verdict"], "keep")
        self.assertEqual(rows[str(busy)]["reason"], "in use")
        self.assertNotIn(str(rcg), rows)
        self.assertNotIn(str(plain), rows)

    def test_a_pytest_cache_with_the_same_signature_is_kept(self) -> None:
        # m1, 2026-10-16: the only matches were two .pytest_cache dirs.
        self.flag.write_text("")
        cache = self.scratch / "runner-heal-tartci" / ".pytest_cache"
        (cache / "debug").mkdir(parents=True)
        (cache / "CACHEDIR.TAG").write_text(
            acr.CARGO_SIGNATURE + "\n# This file is a cache directory tag created by pytest.\n")
        os.utime(cache, (NOW - 30 * DAY, NOW - 30 * DAY))
        self.run_pass()
        self.assertEqual(self.removed, [])
        self.assertEqual(acr.cargo_targets(self.scratch), [])

    def test_a_rebuild_an_hour_ago_keeps_an_old_target(self) -> None:
        # .fingerprint keeps its first-build mtime; a rebuild shows in its
        # entries or in deps/, and a no-op build moves only the profile dir
        # (measured on m1: debug/ changed while deps/ and .fingerprint did
        # not). Any one alone must keep the target.
        self.flag.write_text("")
        for recent in ("fingerprint entry", "deps", "debug dir", "release dir"):
            with self.subTest(recent=recent):
                target = self.target(f"sy-{recent.replace(' ', '-')}/target", 30)
                entry = target / "debug" / ".fingerprint" / "tartci-1234"
                entry.mkdir()
                (target / "debug" / "deps").mkdir()
                (target / "release").mkdir()
                old = NOW - 30 * DAY
                for path in (entry, target / "debug" / "deps", target / "debug" / ".fingerprint",
                             target / "debug", target / "release", target):
                    os.utime(path, (old, old))
                fresh = {"fingerprint entry": entry, "deps": target / "debug" / "deps",
                         "debug dir": target / "debug",
                         "release dir": target / "release"}[recent]
                os.utime(fresh, (NOW - 3600, NOW - 3600))
                self.assertGreater(acr.last_built(target), NOW - 2 * 3600)
                # Control, same target with everything old: stale.
                os.utime(fresh, (old, old))
                self.assertEqual(acr.last_built(target), old)
                os.utime(fresh, (NOW - 3600, NOW - 3600))
        self.run_pass()
        self.assertEqual(self.removed, [])

    def test_a_cwd_inside_the_target_keeps_it(self) -> None:
        self.flag.write_text("")
        busy = self.target("sy-impl3/target", 9)
        self.run_pass(opened=[str(busy)])
        self.assertEqual(self.removed, [])

    def test_unreadable_open_files_delete_nothing(self) -> None:
        self.flag.write_text("")
        self.target("sy-impl3/target", 9)
        self.assertEqual(self.run_pass(opened=None), [])
        self.assertEqual(self.removed, [])

    def test_opt_in_off_reports_only(self) -> None:
        stale = self.target("sy-impl3/target-rba", 3)
        lines = self.run_pass()
        self.assertEqual(self.removed, [])
        self.assertEqual(len(lines), 1)
        self.assertIn("report only: ~/.config/tartci/reap-agent-cargo.enabled is absent", lines[0])
        self.assertIn(str(stale), lines[0])

    def test_a_symlink_into_scratch_is_not_followed(self) -> None:
        self.flag.write_text("")
        outside = self.home / "elsewhere"
        real = self.target("../elsewhere/target", 9)
        (self.scratch / "link").symlink_to(outside, target_is_directory=True)
        self.run_pass()
        self.assertEqual(self.removed, [])
        self.assertTrue(real.exists())

    def test_it_runs_hourly(self) -> None:
        self.target("sy-impl3/target", 9)
        self.assertEqual(len(self.run_pass()), 1)
        self.assertEqual(self.run_pass(now=NOW + 600), [])
        self.assertEqual(len(self.run_pass(now=NOW + 3600)), 1)


class Wiring(Base):
    def test_the_watchdog_pass_returns_the_lines(self) -> None:
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        stale = self.target("sy-impl3/target", 9)
        line = watchdog.agent_cargo_pass(
            NOW, user_home=self.home, opened=lambda: [],
            measure=lambda path, timeout=0: acr.GB, remove=lambda path: None)
        self.assertIn(str(stale), line)
        source = Path(watchdog.__file__).read_text()
        self.assertIn("cargo_line = agent_cargo_pass()", source)


if __name__ == "__main__":
    unittest.main()
