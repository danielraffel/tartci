#!/usr/bin/env python3
"""Retention deletes only old per-boot debris and unreferenced generations."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state_retention as r  # noqa: E402

NOW = 1_800_000_000.0
DAY = 86400
GEN_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-1111111111111111"


def gen_name(i: int) -> str:
    return f"{i:040x}-{i:016x}"


class Home:
    def __init__(self, test: unittest.TestCase) -> None:
        self.td = tempfile.TemporaryDirectory()
        test.addCleanup(self._cleanup)
        self.home = Path(self.td.name)
        self.state = self.home / ".tartci/state"
        self.gens = self.home / ".local/share/tartci-generations"
        self.gens.mkdir(parents=True)
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        (self.home / ".config/tartci").mkdir(parents=True)
        os.environ["TARTCI_HOME"] = str(self.home / ".tartci")
        test.addCleanup(os.environ.pop, "TARTCI_HOME", None)

    def _cleanup(self) -> None:
        for root, dirs, _files in os.walk(self.td.name):
            for name in dirs:
                os.chmod(os.path.join(root, name), 0o755)
        self.td.cleanup()

    def file(self, rel: str, age_days: float) -> Path:
        path = self.state / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * 10)
        os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
        return path

    def generation(self, name: str, age_days: float) -> Path:
        path = self.gens / name
        (path / "scripts").mkdir(parents=True)
        (path / "scripts/x.py").write_text("print(1)\n")
        # Installed generations are read-only.
        os.chmod(path / "scripts/x.py", 0o444)
        os.chmod(path / "scripts", 0o555)
        os.utime(path, (NOW - age_days * DAY, NOW - age_days * DAY))
        os.chmod(path, 0o555)
        return path

    def plan(self, commands=(), cwds=("/",), keep_per_dir=2, keep_generations=3,
             table=True) -> r.Plan:
        return r.build_plan(self.home, now=NOW, max_age_days=30, keep_per_dir=keep_per_dir,
                            keep_generations=keep_generations,
                            table=(list(commands), list(cwds)) if table else None,
                            running_from=None, generation_grace_days=2)


class StateFileTests(unittest.TestCase):
    def test_only_old_per_boot_files_beyond_the_newest_few_go(self) -> None:
        h = Home(self)
        lane = "macos-fleet/pulp-gate"
        old = [h.file(f"{lane}/m1-pulp-gate-01-{100 + i}-1.actions-runner.log", 40 + i)
               for i in range(4)]
        recent = h.file(f"{lane}/m1-pulp-gate-01-99-1.admission-clean.json", 5)
        keepers = [h.file(f"{lane}/m1-pulp-gate-01.state.json", 90),
                   h.file(f"{lane}/events.jsonl", 90),
                   h.file(f"{lane}/m1-pulp-gate-01.disk-admission.json", 90),
                   h.file("self-update/attempts/20260101T000000Z-aaaa.json", 90)]
        plan = h.plan(keep_per_dir=2)
        # Newest two per-boot files are the recent one and the youngest old one.
        self.assertEqual(sorted(plan.files), sorted(old[1:]))
        self.assertNotIn(recent, plan.files)
        for path in keepers:
            self.assertNotIn(path, plan.files)

    def test_a_boot_still_named_by_a_live_process_is_kept(self) -> None:
        h = Home(self)
        live = h.file("macos/m1-pulp-gate-01-777-3.actions-runner.log", 60)
        for i in range(3):
            h.file(f"macos/m1-pulp-gate-01-{i}-9.repository-access.json", 1)
        command = "tart run --no-graphics m1-pulp-gate-01-777-3"
        plan = h.plan(commands=[command], keep_per_dir=0)
        self.assertNotIn(live, plan.files)
        self.assertEqual(plan.files_kept_busy, 1)
        # Control, same instrument: the runner name alone (every supervisor's
        # argv carries it) does not protect the file.
        plan = h.plan(commands=["runner.sh --loop --runner m1-pulp-gate-01"], keep_per_dir=0)
        self.assertIn(live, plan.files)

    def test_an_unreadable_process_table_deletes_nothing(self) -> None:
        h = Home(self)
        h.file("macos/m1-x-01-1-1.actions-runner.log", 90)
        h.generation(gen_name(1), 90)
        plan = h.plan(table=False, keep_per_dir=0)
        self.assertEqual((plan.files, plan.generations), ([], []))
        self.assertIn("nothing will be deleted", plan.notes[0])


class GenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Home(self)
        # Ten generations, one per day, newest first: gen 0 is today.
        self.paths = [self.h.generation(gen_name(i), age_days=i) for i in range(10)]

    def names(self, plan: r.Plan) -> list[str]:
        return sorted(p.name for p in plan.generations)

    def test_count_and_grace_bound_the_generations(self) -> None:
        plan = self.h.plan(keep_generations=3)
        self.assertEqual(self.names(plan), sorted(gen_name(i) for i in range(3, 10)))
        self.assertEqual(plan.generations_kept[gen_name(0)], "among the newest 3")

    def test_every_reference_keeps_its_generation(self) -> None:
        h = self.h
        (h.home / ".local/bin/tartci").write_text(
            f"exec /bin/bash {h.gens}/{gen_name(9)}/tartci \"$@\"\n")
        (h.home / "Library/LaunchAgents/com.x.lane.plist").write_text(
            f"<string>{h.gens}/{gen_name(8)}/providers/tart-macos/runner.sh</string>")
        (h.home / ".config/tartci/macos-fleet-install.json").write_text(
            json.dumps({"root": f"{h.gens}/{gen_name(7)}"}))
        snap = h.home / ".tartci/state/self-update/rollback/20261009T000000Z-x"
        snap.mkdir(parents=True)
        (snap / "snapshot.json").write_text(json.dumps({"previous": f"{6:040x}"}))
        plan = h.plan(commands=[f"python3 {h.gens}/{gen_name(5)}/scripts/x.py"],
                      cwds=["/", f"{h.gens}/{gen_name(4)}/scripts"], keep_generations=3)
        kept = plan.generations_kept
        self.assertEqual(kept[gen_name(9)], "named by the tartci wrapper")
        self.assertEqual(kept[gen_name(8)], "named by com.x.lane.plist")
        self.assertEqual(kept[gen_name(7)], "named by macos-fleet-install.json")
        self.assertEqual(kept[gen_name(6)], "a rollback snapshot's previous commit")
        self.assertEqual(kept[gen_name(5)], "in use by a running process")
        self.assertEqual(kept[gen_name(4)], "in use by a running process")
        self.assertEqual(self.names(plan), [gen_name(3)])

    def test_a_live_self_update_leaves_every_generation_alone(self) -> None:
        marker = self.h.home / ".tartci/state/self-update/active.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")
        plan = self.h.plan(keep_generations=3)
        self.assertEqual(plan.generations, [])
        self.assertTrue(any("self-update marker" in n for n in plan.notes))

    def test_staging_directories_are_never_candidates(self) -> None:
        staging = self.h.gens / f".{gen_name(20)}.abc123"
        staging.mkdir()
        os.utime(staging, (NOW - 90 * DAY, NOW - 90 * DAY))
        plan = self.h.plan(keep_generations=3)
        self.assertNotIn(staging, plan.generations)


class ApplyTests(unittest.TestCase):
    def test_plan_writes_nothing_and_apply_removes_read_only_generations(self) -> None:
        h = Home(self)
        doomed = h.generation(gen_name(1), 40)
        keep = [h.generation(gen_name(i), 0) for i in range(2, 5)]
        old_file = h.file("macos/m1-x-01-1-1.jit-error", 90)
        plan = h.plan(keep_generations=3, keep_per_dir=0)
        self.assertEqual(plan.generations, [doomed])
        self.assertTrue(doomed.exists() and old_file.exists(), "a plan must not delete")
        self.assertFalse(os.access(doomed, os.W_OK))
        self.assertEqual(r.apply(plan), [])
        self.assertFalse(doomed.exists())
        self.assertFalse(old_file.exists())
        for path in keep:
            self.assertTrue(path.exists())


class CliTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(Path(r.__file__)), *args],
                              capture_output=True, text=True, check=False)

    def test_bounds_below_the_rollback_floor_are_refused(self) -> None:
        for args in (["--keep-generations", "2"], ["--max-age-days", "1"],
                     ["--generation-grace-days", "0"]):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("bounds too tight", result.stderr)

    def test_the_default_is_a_plan_against_an_empty_home(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, TARTCI_HOME=str(Path(td) / ".tartci"))
            result = subprocess.run([sys.executable, str(Path(r.__file__)), "--json",
                                     "--home", td], capture_output=True, text=True,
                                    env=env, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["mode"], "plan")


if __name__ == "__main__":
    unittest.main()
