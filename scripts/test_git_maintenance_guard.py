#!/usr/bin/env python3
"""The git maintenance stampede guard (scripts/git_maintenance_guard.py)."""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import git_maintenance_guard as gmg  # noqa: E402

NOW = 1792120000.0
REPO = "/Volumes/Example/Code/pulp"
# m3, 2026-10-16 ~07:15Z: one store, many concurrent background maintenances.
STAMPEDE = "\n".join(
    [f"{1000 + i} 02:41:{i:02d} /opt/homebrew/bin/git maintenance run --auto" for i in range(3)]
    + [f"{2000 + i} 01:10:00 /opt/homebrew/bin/git repack -d -l --cruft" for i in range(2)]
    + ["3000 00:00:05 /opt/homebrew/bin/git status", "3001 00:10:00 bash -c git log"])


class FakeGit:
    """A `git` whose config lives in a dict and whose commands are recorded."""

    def __init__(self) -> None:
        self.config: dict[tuple[str, str], str] = {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list, **kwargs) -> "subprocess.CompletedProcess[str]":
        self.calls.append(list(argv))
        if "git" in argv:
            git = argv[argv.index("git"):]
            if git[1] == "-C" and git[3] == "config":
                repo, rest = git[2], git[4:]
                if rest[0] == "--get":
                    value = self.config.get((repo, rest[1]))
                    return subprocess.CompletedProcess(argv, 0 if value is not None else 1,
                                                       (value or "") + "\n", "")
                self.config[(repo, rest[0])] = rest[1]
                return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def maintenance_runs(self) -> list[list[str]]:
        return [c for c in self.calls if "maintenance" in c and "run" in c]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / ".config" / "tartci").mkdir(parents=True)
        self.repos = self.home / ".config" / "tartci" / "git-maintenance.repos"
        self.enable = self.home / ".config" / "tartci" / "git-maintenance.enabled"
        patcher = mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home / ".tartci")})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("TARTCI_GIT_MAINTENANCE_REPOS", "TARTCI_GIT_MAINTENANCE_ENABLE_FILE"):
            os.environ.pop(key, None)
        self.git = FakeGit()


class Check(Base):
    def test_more_than_three_maintenance_processes_warn_naming_the_repo(self) -> None:
        line = gmg.check(STAMPEDE, cwd=lambda pid: REPO)
        self.assertIn("5 git maintenance/repack/gc processes (more than 3)", line)
        self.assertIn(f"{REPO} x5", line)
        self.assertIn("oldest pid 1002 running 02:41:02", line)

    def test_three_or_fewer_and_ordinary_git_commands_do_not_warn(self) -> None:
        few = "\n".join(STAMPEDE.splitlines()[:3] + STAMPEDE.splitlines()[5:])
        self.assertIsNone(gmg.check(few, cwd=lambda pid: REPO))
        self.assertEqual(len(gmg.maintenance_processes(STAMPEDE)), 5)

    def test_a_repo_named_on_the_command_line_wins_over_the_cwd(self) -> None:
        rows = gmg.maintenance_processes("7 00:01 git -C /x/repo gc --auto")
        self.assertEqual(rows[0]["repo"], "/x/repo")


class Enforce(Base):
    def test_sets_the_three_settings_once(self) -> None:
        lines = gmg.enforce([Path(REPO)], self.git)
        self.assertEqual(self.git.config, {(REPO, "gc.auto"): "0",
                                           (REPO, "maintenance.auto"): "false",
                                           (REPO, "gc.autoDetach"): "false"})
        self.assertEqual(len(lines), 3)
        self.assertEqual(gmg.enforce([Path(REPO)], self.git), [])   # already set

    def test_installer_runs_it_and_no_list_is_a_no_op(self) -> None:
        source = (Path(gmg.__file__).parent / "install_macos_fleet.sh").read_text()
        self.assertIn('"$ROOT/scripts/git_maintenance_guard.py" enforce', source)
        self.assertEqual(gmg.repos(self.home), [])
        self.repos.write_text(f"# shared stores\n{REPO}\nrelative/ignored\n")
        self.assertEqual(gmg.repos(self.home), [Path(REPO)])


class Nightly(Base):
    def run_nightly(self, now: float = NOW, busy: bool = False, ps_out: str = "",
                    loaded=None) -> list[str]:
        return gmg.nightly(now, self.home, run=self.git, busy=lambda home: busy,
                           ps_out=ps_out, loaded=loaded or (lambda: None))

    def test_off_without_the_enable_file(self) -> None:
        self.repos.write_text(REPO + "\n")
        self.assertEqual(self.run_nightly(), [])
        self.assertEqual(self.git.calls, [])

    def test_one_niced_background_gc_per_repo_and_settings_enforced(self) -> None:
        self.repos.write_text(f"{REPO}\n/Volumes/Example/Code/tartci\n")
        self.enable.write_text("")
        lines = self.run_nightly()
        runs = self.git.maintenance_runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0][:3], ["nice", "-n", "19"])
        self.assertIn("--task=gc", runs[0])
        self.assertIn("--task=loose-objects", runs[0])
        self.assertNotIn("--auto", runs[0])
        if os.path.exists(gmg.TASKPOLICY):
            self.assertEqual(runs[0][3:5], [gmg.TASKPOLICY, "-b"])
        self.assertEqual(self.git.config[(REPO, "gc.auto")], "0")
        self.assertTrue(any("rc=0" in line for line in lines))
        # Once a day: a second pass soon after runs nothing.
        self.git.calls.clear()
        self.assertEqual(self.run_nightly(NOW + 3600), [])
        self.assertEqual(self.git.maintenance_runs(), [])

    def test_deferred_while_a_gate_lane_is_mid_job(self) -> None:
        self.repos.write_text(REPO + "\n")
        self.enable.write_text("")
        lines = self.run_nightly(busy=True)
        self.assertIn("deferred, a gate lane is mid-job", lines[0])
        self.assertEqual(self.git.maintenance_runs(), [])
        # Not recorded as run: the next pass tries again.
        self.assertEqual(len(self.run_nightly(NOW + 600) and self.git.maintenance_runs()), 1)

    def test_pruning_is_pinned_whatever_the_repo_config_says(self) -> None:
        self.repos.write_text(REPO + "\n")
        self.enable.write_text("")
        self.run_nightly()
        (argv,) = self.git.maintenance_runs()
        at = argv.index("maintenance")
        self.assertEqual(argv[at - 4:at], ["-c", "gc.pruneExpire=2.weeks.ago",
                                           "-c", "gc.worktreePruneExpire=3.months.ago"])
        self.assertNotIn("now", " ".join(argv))

    def test_deferred_while_other_git_maintenance_runs(self) -> None:
        # m3's storm came from agents' host-side git, not from gate lanes.
        self.repos.write_text(REPO + "\n")
        self.enable.write_text("")
        lines = self.run_nightly(ps_out="1 00:05 /opt/homebrew/bin/git maintenance run --auto")
        self.assertIn("deferred, 1 git maintenance/repack/gc process(es) already running", lines[0])
        self.assertEqual(self.git.maintenance_runs(), [])
        # Control: an ordinary git command does not defer it.
        self.run_nightly(ps_out="2 00:05 /opt/homebrew/bin/git status")
        self.assertEqual(len(self.git.maintenance_runs()), 1)

    def test_deferred_while_the_host_is_loaded(self) -> None:
        self.repos.write_text(REPO + "\n")
        self.enable.write_text("")
        lines = self.run_nightly(loaded=lambda: "load 207 is above 2 x 24 cores")
        self.assertIn("deferred, load 207 is above 2 x 24 cores", lines[0])
        self.assertEqual(self.git.maintenance_runs(), [])
        with mock.patch.object(gmg.os, "getloadavg", return_value=(207.0, 150.0, 90.0)), \
                mock.patch.object(gmg.os, "cpu_count", return_value=24):
            self.assertEqual(gmg.host_loaded(), "load 207 is above 2 x 24 cores")
        with mock.patch.object(gmg.os, "getloadavg", return_value=(40.0, 30.0, 20.0)), \
                mock.patch.object(gmg.os, "cpu_count", return_value=24):
            self.assertIsNone(gmg.host_loaded())

    def test_an_unknown_lane_state_defers_the_run(self) -> None:
        # Fail closed: a lane whose busy state cannot be read counts as busy.
        import lane_busy  # noqa: PLC0415
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        (agents / "com.danielraffel.tartci.tart-runner-macos-fleet.m3.pulp-gate.plist").write_text("x")
        self.repos.write_text(REPO + "\n")
        self.enable.write_text("")
        unknown = [lane_busy.LaneBusy("com.danielraffel.tartci.tart-runner-macos-fleet.m3.pulp-gate",
                                      lane_busy.UNKNOWN, "host lease store unreadable", 4242)]
        idle = [lane_busy.LaneBusy(unknown[0].label, lane_busy.IDLE, "no work", 4242)]
        with mock.patch.object(lane_busy, "probe", return_value=unknown):
            self.assertTrue(gmg.lanes_busy(self.home))
            lines = gmg.nightly(NOW, self.home, run=self.git, ps_out="",
                                loaded=lambda: None)
        self.assertIn("deferred, a gate lane is mid-job", lines[0])
        self.assertEqual(self.git.maintenance_runs(), [])
        with mock.patch.object(lane_busy, "probe", return_value=idle):
            self.assertFalse(gmg.lanes_busy(self.home))

    def test_two_runs_never_overlap(self) -> None:
        self.repos.write_text(REPO + "\n")
        self.enable.write_text("")
        state = self.home / ".tartci" / "state"
        state.mkdir(parents=True)
        holder = (state / "git-maintenance.lock").open("w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lines = self.run_nightly()
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        self.assertIn("another run holds the lock", lines[0])
        self.assertEqual(self.git.maintenance_runs(), [])


class Wiring(Base):
    def test_the_watchdog_pass_returns_the_lines(self) -> None:
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        line = watchdog.git_maintenance_pass(NOW, user_home=self.home, run=self.git,
                                             ps=lambda: STAMPEDE, cwd=lambda pid: REPO,
                                             busy=lambda home: False, loaded=lambda: None)
        self.assertIn("WARN git-maintenance", line)
        source = Path(watchdog.__file__).read_text()
        self.assertIn("git_line = git_maintenance_pass()", source)


if __name__ == "__main__":
    unittest.main()
