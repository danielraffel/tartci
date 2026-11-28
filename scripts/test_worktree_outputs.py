#!/usr/bin/env python3
"""worktree_outputs: which lane-worktree build output the reclaim pass reaps.

Real directories under a temp dir stand in for the worktree root; volume usage
is injected so each gate and the stop conditions are exercised directly.
"""

from __future__ import annotations

import testing_support  # noqa: E402
import io
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import disk_reclaim as dr  # noqa: E402
import pulp_reapers as pr  # noqa: E402
import worktree_outputs as wo  # noqa: E402

DAY = 86400


def backdate(path: pathlib.Path, days: float) -> None:
    stamp = time.time() - days * DAY
    for directory, subdirs, files in os.walk(path, topdown=False):
        for name in [*subdirs, *files]:
            os.utime(os.path.join(directory, name), (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp), follow_symlinks=False)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / "agent-worktrees"
        self.root.mkdir()
        self.usage = 97.0
        self.logs: list[str] = []

    def settings(self, **over):
        values = {"roots": [self.root], **wo.DEFAULTS}
        values.update(over)
        return values

    def worktree(self, name: str, git: bool = True) -> pathlib.Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        if git:
            (path / ".git").write_text("gitdir: elsewhere\n")
        return path

    def output(self, name: str, kind: str = "build", age: float = 10, git: bool = True
               ) -> pathlib.Path:
        out = self.worktree(name, git) / kind
        (out / "obj").mkdir(parents=True)
        (out / "obj" / "a.o").write_text("x" * 2048)
        backdate(out, age)
        return out

    def scan(self, fix: bool = True, **over):
        # Each removal drops usage by 5 points so stop conditions are observable.
        def measure(_root):
            return self.usage

        def log(line):
            self.logs.append(line)
            if fix and line.startswith("worktree_outputs: removed"):
                self.usage -= 5

        return wo.scan(self.settings(**over), fix=fix, log=log, measure=measure)


class Selection(Fixture):
    def test_eligible_output_is_removed_and_logged(self):
        out = self.output("lane-a")
        report = self.scan()
        self.assertFalse(out.exists())
        self.assertEqual(report["removed"], 1)
        self.assertGreater(report["removed_bytes"], 0)
        self.assertEqual(report["removed_paths"], [str(out)])
        self.assertTrue(any(str(out) in line for line in self.logs))

    def test_every_output_kind_is_taken(self):
        outs = [self.output(f"lane-{k}", k) for k in ("target", "build", ".build", "node_modules")]
        self.scan(pressure_used_pct=90)
        self.assertEqual([o.exists() for o in outs], [False] * 4)

    def test_recent_output_is_kept(self):
        out = self.output("lane-a", age=1)
        report = self.scan()
        self.assertTrue(out.exists())
        self.assertEqual(report["kept"], {"recent": 1})

    def test_one_recent_file_deep_inside_keeps_the_whole_dir(self):
        out = self.output("lane-a", age=10)
        (out / "obj" / "fresh.o").write_text("new")
        self.scan()
        self.assertTrue(out.exists())

    def test_output_outside_a_git_worktree_is_kept(self):
        out = self.output("plain", git=False)
        report = self.scan()
        self.assertTrue(out.exists())
        self.assertEqual(report["candidates"], 0)

    def test_w_lanes_and_fleet_pass_dirs_need_seven_days(self):
        young = [self.output("w-123", age=5), self.output(".fleet-pass-9", age=5)]
        old = self.output("w-old", age=9)
        report = self.scan()
        self.assertTrue(all(p.exists() for p in young))
        self.assertFalse(old.exists())
        self.assertEqual(report["kept"], {"protected_lane_recent": 2})

    def test_nested_lane_under_w_dir_is_protected_too(self):
        out = self.root / "w-77" / "repo" / "target"
        (out / "x").mkdir(parents=True)
        (self.root / "w-77" / "repo" / ".git").mkdir()
        backdate(out, 5)
        self.scan()
        self.assertTrue(out.exists())

    def test_live_pid_file_keeps_output(self):
        out = self.output("lane-a")
        (out.parent / "build.pid").write_text(str(os.getpid()))
        self.scan()
        self.assertTrue(out.exists())

    def test_dead_pid_file_does_not_protect(self):
        out = self.output("lane-a")
        (out.parent / "build.pid").write_text("2147483646")
        self.scan()
        self.assertFalse(out.exists())

    def test_symlinked_output_is_kept(self):
        real = self.base / "real"
        real.mkdir()
        backdate(real, 10)
        lane = self.worktree("lane-a")
        (lane / "build").symlink_to(real)
        self.scan()
        self.assertTrue(real.exists())

    def test_depth_is_bounded(self):
        deep = self.root / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / ".git").write_text("")
        (deep / "build").mkdir()
        backdate(deep / "build", 10)
        self.assertEqual(wo.find_candidates(self.root), [])

    def test_unknown_idle_check_keeps_output(self):
        out = self.output("lane-a")

        def broken(*args, **kwargs):
            if args[0][0] == "ps":
                return type("P", (), {"returncode": 0, "stdout": ""})()
            raise OSError("no find")
        wo.scan(self.settings(), fix=True, runner=broken, log=lambda _l: None,
                measure=lambda _r: 97.0)
        self.assertTrue(out.exists())

    def test_dry_run_removes_nothing_but_reports(self):
        out = self.output("lane-a")
        report = self.scan(fix=False)
        self.assertTrue(out.exists())
        self.assertEqual((report["mode"], report["removed"]), ("dry-run", 1))


class Bounds(Fixture):
    def test_roots_unset_means_nothing(self):
        out = self.output("lane-a")
        self.assertEqual(wo.settings_from_table({"pulp_worktree_builds": False})[0], None)
        self.assertEqual(wo.settings_from_table({wo.ROOTS_KEY: []})[0], None)
        self.assertTrue(out.exists())
        profile = self.base / "profile.toml"
        profile.write_text("[reclaim]\nscratch_dirs = true\n")
        self.assertFalse(wo.run(fix=True, profile=profile)["enabled"])

    @testing_support.requires_tomllib
    def test_profile_with_roots_enables_it(self):
        out = self.output("lane-a")
        profile = self.base / "profile.toml"
        profile.write_text(f'[reclaim]\nworktree_roots = ["{self.root}"]\n')
        report = wo.run(fix=True, profile=profile, measure=lambda _r: 97.0)
        self.assertTrue(report["enabled"])
        self.assertFalse(out.exists())

    def test_per_pass_dir_cap(self):
        outs = [self.output(f"lane-{i}") for i in range(5)]
        report = self.scan(max_dirs=2, pressure_used_pct=99)
        self.assertEqual(report["removed"], 2)
        self.assertEqual(report["capped"], "max_dirs")
        self.assertEqual(sum(o.exists() for o in outs), 3)

    def test_per_pass_gib_cap(self):
        outs = [self.output(f"lane-{i}") for i in range(4)]
        orig = wo.scratch_dirs.size_bytes
        wo.scratch_dirs.size_bytes = lambda *a, **k: 60 * wo.GIB
        self.addCleanup(setattr, wo.scratch_dirs, "size_bytes", orig)
        report = self.scan(max_gib=100, pressure_used_pct=99)
        self.assertEqual(report["removed"], 2)
        self.assertEqual(report["capped"], "max_gib")
        self.assertEqual(sum(o.exists() for o in outs), 2)

    def test_time_budget(self):
        self.output("lane-a")
        ticks = iter([0, 1000, 2000, 3000, 4000, 5000])
        report = wo.scan(self.settings(), fix=True, log=lambda _l: None,
                         measure=lambda _r: 97.0, clock=lambda: next(ticks))
        self.assertEqual((report["removed"], report["capped"]), (0, "budget_seconds"))

    def test_stops_at_target_oldest_first(self):
        outs = {age: self.output(f"lane-{age}", age=age) for age in (4, 30, 10, 20)}
        self.usage = 94.0  # over target 85, under pressure 92? no: 94 >= 92
        self.scan(pressure_used_pct=95)  # now below pressure; each removal frees 5 points
        # 94 -> 89 -> 84: two oldest go, then the target is reached.
        self.assertFalse(outs[30].exists())
        self.assertFalse(outs[20].exists())
        self.assertTrue(outs[10].exists())
        self.assertTrue(outs[4].exists())

    def test_under_target_removes_nothing(self):
        out = self.output("lane-a")
        self.usage = 80.0
        report = self.scan()
        self.assertTrue(out.exists())
        self.assertEqual(report["roots"][0]["state"], "under_target")

    def test_pressure_removes_every_eligible(self):
        outs = [self.output(f"lane-{i}") for i in range(4)]
        self.usage = 97.0
        report = self.scan(max_dirs=25)
        self.assertEqual(report["removed"], 4)
        self.assertFalse(any(o.exists() for o in outs))


class Validation(unittest.TestCase):
    def test_bad_values_are_refused(self):
        for table in ({wo.ROOTS_KEY: "/x"}, {wo.ROOTS_KEY: ["rel"]},
                      {wo.ROOTS_KEY: ["/a/../b"]},
                      {wo.ROOTS_KEY: ["/a"], "worktree_output_idle_days": 0},
                      {wo.ROOTS_KEY: ["/a"], "worktree_output_target_used_pct": 99,
                       "worktree_output_pressure_used_pct": 50}):
            self.assertTrue(wo.validate(table), table)

    def test_the_whole_table_validates(self):
        self.assertEqual(pr.validate_table({"pulp_worktree_builds": False,
                                            wo.ROOTS_KEY: ["/Volumes/Example/x"]}), [])


class PassSummary(unittest.TestCase):
    def test_summary_and_event_detail_carry_the_removals(self):
        worktree = {"enabled": True, "mode": "fix", "removed": 3, "removed_bytes": 5 * wo.GIB,
                    "kept": {"recent": 2}, "capped": "max_dirs",
                    "roots": [{"root": "/r", "used_pct_before": 97.0, "used_pct_after": 90.0}]}
        summary = dr.pass_summary({"mode": "fix", "worktree_outputs": worktree}, 0)
        self.assertEqual(summary["worktree_outputs"]["removed"], 3)
        self.assertEqual(summary["reclaimed_bytes"], 5 * wo.GIB)
        text = dr.worktree_outputs_detail(summary["worktree_outputs"])
        self.assertIn("removed 3 (5.0 GiB)", text)
        self.assertIn("capped max_dirs", text)
        self.assertIn("97.0% -> 90.0%", text)


class MainReport(unittest.TestCase):
    """The report and human line of a whole pass count the worktree removals."""

    def _run(self, *argv):
        import contextlib
        import json
        import unittest.mock
        worktree = {"enabled": True, "mode": "fix", "removed": 3,
                    "removed_bytes": 5 * wo.GIB, "kept": {}}
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as td, \
                unittest.mock.patch.dict(os.environ, {
                    "TARTCI_HOME": td, "TARTCI_RECLAIM_STATE_DIR": td,
                    "TARTCI_FLEET_PROFILE": td + "/none.toml",
                    "TARTCI_RECLAIM_BOOT_FLOOR_GB": "0", "TARTCI_BOOT_USAGE": "0",
                    "TARTCI_RECLAIM_RETENTION": "0"}), \
                unittest.mock.patch.object(dr.worktree_outputs, "run", return_value=worktree), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            dr.main(["--roots", td, *argv])
        return out.getvalue()

    def test_json_report_counts_worktree_removals(self):
        import json
        report = json.loads(self._run("--json", "--fix"))
        self.assertEqual(report["reclaimed_bytes"], 5 * wo.GIB)
        self.assertEqual(report["worktree_outputs_removed"], 3)
        self.assertEqual(report["worktree_outputs_removed_bytes"], 5 * wo.GIB)
        self.assertEqual(report["candidate_reclaimed_bytes"], 0)

    def test_human_summary_counts_worktree_removals(self):
        text = self._run("--fix")
        self.assertIn("worktree outputs removed 3", text)
        self.assertIn("reclaimed 5.0 GiB", text)

    def test_receipt_summary_does_not_double_count(self):
        summary = dr.pass_summary({"mode": "fix", "worktree_outputs": {"removed_bytes": 5 * wo.GIB},
                                   "report": {"reclaimed_bytes": 5 * wo.GIB,
                                              "candidate_reclaimed_bytes": 0}}, 0)
        self.assertEqual(summary["reclaimed_bytes"], 5 * wo.GIB)


if __name__ == "__main__":
    unittest.main()
