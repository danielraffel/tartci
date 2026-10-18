#!/usr/bin/env python3
"""tmp_checkouts: which /tmp git checkouts the reclaim lane removes, and why not.

Every fixture is a real git repository under a temp dir standing in for
/private/tmp, so the gates are exercised against git's own answers.
"""

from __future__ import annotations

import testing_support  # noqa: E402
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import disk_reclaim as dr  # noqa: E402
import pulp_reapers as pr  # noqa: E402
import tmp_checkouts as tc  # noqa: E402

DAY = 86400


def git(cwd: pathlib.Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                          text=True, env=env).stdout


def age(path: pathlib.Path, days: float) -> None:
    """Backdate a checkout and everything the idle gate reads."""
    stamp = time.time() - days * DAY
    targets = [path, *path.iterdir()]
    dot_git = path / ".git"
    gitdir = dot_git if dot_git.is_dir() else None
    if gitdir is None:
        gitdir = pathlib.Path(dot_git.read_text().split("gitdir:", 1)[1].strip())
    targets += [gitdir / name for name in ("index", "HEAD", "logs/HEAD")
                if (gitdir / name).exists()]
    for target in targets:
        os.utime(target, (stamp, stamp), follow_symlinks=False)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.base = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.tmp = self.base / "tmp"
        self.tmp.mkdir()
        self.origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True)
        seed = self.base / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        (seed / "a.txt").write_text("a\n")
        git(seed, "add", "a.txt")
        git(seed, "commit", "-qm", "seed")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-q", "origin", "main")
        # The parent repository of the /tmp worktrees lives OUTSIDE the root.
        self.parent = self.base / "parent"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.parent)], check=True)

    def clone(self, name: str, days: float = 3) -> pathlib.Path:
        path = self.tmp / name
        subprocess.run(["git", "clone", "-q", str(self.origin), str(path)], check=True)
        age(path, days)
        return path

    def worktree(self, name: str, days: float = 3) -> pathlib.Path:
        path = self.tmp / name
        git(self.parent, "worktree", "add", "-q", "--detach", str(path), "origin/main")
        age(path, days)
        return path

    def scan(self, fix: bool = True, **kw) -> dict:
        kw.setdefault("in_use", lambda path: False)
        kw.setdefault("cwds", ["/"])
        return tc.scan(self.tmp, fix=fix, idle_hours=48, **kw)


class GateTests(Fixture):
    def test_git_timeout_kills_process_group_without_waiting_forever(self) -> None:
        child = mock.Mock(pid=1234)
        child.communicate.side_effect = subprocess.TimeoutExpired(["git"], 1)
        child.wait.side_effect = subprocess.TimeoutExpired(["git"], 0.2)
        with mock.patch.object(tc.subprocess, "Popen", return_value=child), \
                mock.patch.object(tc.os, "killpg") as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                tc._bounded_git(["git", "status"], timeout=1)
        killpg.assert_called_once_with(1234, tc.signal.SIGKILL)
        child.wait.assert_called_once_with(timeout=0.2)

    def test_a_finished_clone_and_worktree_are_removed(self) -> None:
        clone = self.clone("done-clone")
        wt = self.worktree("done-wt")
        report = self.scan()
        self.assertEqual((report["checkouts"], report["removed"]), (2, 2), report)
        self.assertFalse(clone.exists())
        self.assertFalse(wt.exists())
        # The worktree went through git, so the parent forgot it.
        self.assertNotIn(str(wt), git(self.parent, "worktree", "list"))
        self.assertGreater(report["removed_bytes"], 0)

    def test_dry_run_removes_nothing_but_counts(self) -> None:
        clone = self.clone("done-clone")
        report = self.scan(fix=False)
        self.assertEqual(report["removed"], 1)
        self.assertTrue(clone.exists())

    def test_each_gate_keeps_its_checkout(self) -> None:
        self.clone("recent", days=1)
        dirty = self.clone("dirty")
        (dirty / "new.txt").write_text("x")
        age(dirty, 3)
        unpushed = self.clone("unpushed")
        (unpushed / "a.txt").write_text("b\n")
        git(unpushed, "commit", "-qam", "local")
        age(unpushed, 3)
        branch = self.clone("branch")
        git(branch, "checkout", "-qb", "side")
        (branch / "a.txt").write_text("c\n")
        git(branch, "commit", "-qam", "side")
        git(branch, "checkout", "-q", "main")
        age(branch, 3)
        stash = self.clone("stash")
        (stash / "a.txt").write_text("d\n")
        git(stash, "stash", "-q")
        age(stash, 3)
        busy = self.clone("busy")
        report = self.scan(cwds=["/", str(busy / "sub")])
        self.assertEqual(report["removed"], 0, report)
        self.assertEqual(report["kept"], {"recent": 1, "dirty": 1, "unpushed": 2,
                                          "stash": 1, "in_use": 1})
        for name in ("recent", "dirty", "unpushed", "branch", "stash", "busy"):
            self.assertTrue((self.tmp / name).exists(), name)

    def test_a_live_build_naming_it_keeps_it(self) -> None:
        path = self.clone("building")
        report = self.scan(in_use=lambda p: p == path)
        self.assertEqual(report["kept"], {"in_use": 1})

    def test_an_unreadable_process_table_removes_nothing(self) -> None:
        self.clone("done")
        for kw in ({"in_use": None}, {"cwds": None}):
            report = self.scan(**kw)
            self.assertEqual(report["removed"], 0)
            self.assertEqual(report["kept"], {"process_scan_unavailable": 1})
            self.assertIn("removed nothing", report["error"])

    def test_orphaned_worktrees_are_reported_not_removed(self) -> None:
        wt = self.worktree("orphan")
        shutil.rmtree(self.parent)
        report = self.scan()
        self.assertEqual(report["orphaned_worktrees"], 1)
        self.assertEqual(report["orphaned_paths"], [str(wt)])
        self.assertEqual(report["removed"], 0)
        self.assertTrue(wt.exists())

    def test_a_git_dir_without_head_is_reported_broken_not_removed(self) -> None:
        husk = self.tmp / "husk"
        (husk / ".git" / "refs").mkdir(parents=True)
        report = self.scan()
        self.assertEqual((report["broken_checkouts"], report["broken_paths"]), (1, [str(husk)]))
        self.assertEqual((report["removed"], report["kept"]), (0, {}))
        self.assertTrue(husk.exists())

    def test_git_refusing_worktree_remove_is_kept(self) -> None:
        wt = self.worktree("locked")
        git(self.parent, "worktree", "lock", str(wt))
        report = self.scan()
        self.assertEqual(report["kept"], {"worktree_remove_refused": 1})
        self.assertTrue(wt.exists())

    def test_a_worktree_with_an_unpushed_detached_commit_is_kept(self) -> None:
        wt = self.worktree("detached")
        (wt / "a.txt").write_text("e\n")
        git(wt, "commit", "-qam", "detached work")
        age(wt, 3)
        report = self.scan()
        self.assertEqual(report["kept"], {"unpushed": 1})
        self.assertTrue(wt.exists())

    def test_remove_never_forces_a_worktree(self) -> None:
        # The gates above stop a dirty worktree first; remove() must still not
        # override git if it is ever reached with one.
        wt = self.worktree("dirty-wt")
        (wt / "untracked.txt").write_text("x")
        kind, gitdir = tc.gitdir_of(wt)
        self.assertEqual(tc.remove(wt, kind, gitdir, subprocess.run), "worktree_remove_refused")
        self.assertTrue((wt / "untracked.txt").exists())

    def test_non_checkouts_and_symlinks_are_ignored(self) -> None:
        (self.tmp / "plain").mkdir()
        target = self.clone("real", days=1)
        (self.tmp / "link").symlink_to(target)
        report = self.scan()
        self.assertEqual(report["checkouts"], 1)

    def test_the_time_budget_defers_rather_than_keeps(self) -> None:
        self.clone("one")
        self.clone("two")
        report = self.scan(budget_s=-1)
        self.assertEqual((report["deferred"], report["removed"], report["kept"]), (2, 0, {}))


class WorktreeRootGateTests(Fixture):
    """Gates added for the host worktree root; they apply to every root."""

    def test_a_live_build_marker_keeps_it(self) -> None:
        wt = self.worktree("building")
        (wt / ".pulp-build-active").write_text(f"pid={os.getpid()}\nstarted_at=x\n")
        age(wt, 3)
        self.assertEqual(self.scan()["kept"], {"in_use": 1})
        (wt / ".pulp-build-active").write_text("pid=999999\n")   # a dead build
        age(wt, 3)
        self.assertEqual(self.scan()["kept"], {"dirty": 1})   # the stale marker is untracked

    def test_a_clone_that_other_worktrees_point_into_is_kept(self) -> None:
        clone = self.clone("hub")
        git(clone, "worktree", "add", "-q", "--detach", str(self.base / "elsewhere"), "HEAD")
        age(clone, 3)
        report = self.scan()
        self.assertEqual(report["kept"], {"has_worktrees": 1})
        self.assertTrue(clone.exists())

    def test_an_active_lineage_branch_is_kept(self) -> None:
        wt = self.tmp / "owned"
        git(self.parent, "worktree", "add", "-q", "-b", "feature/owned", str(wt), "origin/main")
        git(self.parent, "push", "-q", "origin", "feature/owned")
        git(self.parent, "config", "branch.feature/owned.pulpWorktreeStatus", "active")
        age(wt, 3)
        self.assertEqual(self.scan()["kept"], {"lineage_active": 1})
        git(self.parent, "config", "branch.feature/owned.pulpWorktreeStatus", "merged")
        self.assertEqual(self.scan()["removed"], 1)   # the control

    def test_keep_verdicts_are_cached_until_the_checkout_changes(self) -> None:
        dirty = self.clone("dirty")
        (dirty / "new.txt").write_text("x")
        age(dirty, 3)
        cache: dict = {}
        first = self.scan(next_cache=cache)
        self.assertEqual(first["kept"], {"dirty": 1})
        # Asking git did not make the checkout look used.
        self.assertEqual(self.scan()["kept"], {"dirty": 1})
        self.assertEqual(cache[str(dirty)]["reason"], "dirty")
        calls = []

        def counting(argv, **kw):
            calls.append(argv)
            return subprocess.run(argv, **kw)
        self.scan(cache=cache, runner=counting, next_cache={})
        self.assertFalse([a for a in calls if "status" in a], calls)
        (dirty / "new.txt").unlink()        # cleaned: the top level changed
        age(dirty, 3)
        os.utime(dirty, (time.time() - 3 * DAY + 60, time.time() - 3 * DAY + 60))
        self.assertEqual(self.scan(cache=cache)["removed"], 1)


class MultiRootTests(Fixture):
    def profile(self, body: str) -> pathlib.Path:
        path = self.base / "profile.toml"
        path.write_text(body)
        return path

    @testing_support.requires_tomllib
    def test_both_roots_are_swept_and_reported_per_root(self) -> None:
        worktrees = self.base / "agent-worktrees"
        worktrees.mkdir()
        self.clone("done-tmp")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(worktrees / "done-wt")],
                       check=True)
        age(worktrees / "done-wt", 3)
        profile = self.profile(
            "[reclaim]\ntmp_checkouts = true\nworktree_root_checkouts = true\n"
            f'worktrees_root = "{worktrees}"\n')
        with mock.patch.dict(os.environ, {"TARTCI_TMP_CHECKOUT_ROOT": str(self.tmp)}), \
                mock.patch.object(tc, "process_cwds", return_value=["/"]):
            report = tc.run(fix=True, profile=profile, in_use=lambda p: False,
                            verdicts=self.base / "verdicts.json")
        self.assertEqual(report["removed"], 2, report)
        self.assertEqual(sorted(report["by_root"]), sorted([str(self.tmp), str(worktrees)]))
        self.assertEqual(report["by_root"][str(worktrees)]["removed"], 1)

    @testing_support.requires_tomllib
    def test_worktree_root_is_opt_in_and_needs_a_root(self) -> None:
        self.assertTrue(pr.validate_table({"worktree_root_checkouts": True}))
        self.assertEqual(pr.validate_table({"worktree_root_checkouts": True,
                                            "worktrees_root": "/Volumes/W/agent-worktrees"}), [])
        settings, _ = tc.load_settings(self.profile("[reclaim]\ntmp_checkouts = true\n"))
        self.assertEqual(len(settings["roots"]), 1)

    @testing_support.requires_tomllib
    def test_an_extra_root_reaps_worktrees_and_keeps_every_clone(self) -> None:
        # m5s's internal ~/Code: agents' worktrees of an old primary checkout
        # sit beside that checkout and other real clones.
        code = self.base / "Code"
        code.mkdir()
        git(self.parent, "worktree", "add", "-q", "--detach", str(code / "pulp-done-wt"),
            "origin/main")
        age(code / "pulp-done-wt", 3)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(code / "Shipyard")],
                       check=True)
        age(code / "Shipyard", 30)
        profile = self.profile(f'[reclaim]\nextra_worktree_roots = ["{code}"]\n')
        with mock.patch.object(tc, "process_cwds", return_value=["/"]):
            report = tc.run(fix=True, profile=profile, in_use=lambda p: False,
                            verdicts=self.base / "verdicts.json")
        self.assertEqual(report["removed"], 1, report)
        self.assertFalse((code / "pulp-done-wt").exists())
        self.assertTrue((code / "Shipyard").exists())
        self.assertEqual(report["by_root"][str(code)]["kept"], {"clone_in_shared_root": 1})
        self.assertNotIn(str(code / "pulp-done-wt"), git(self.parent, "worktree", "list"))

    def test_extra_roots_validate(self) -> None:
        self.assertEqual(pr.validate_table({"extra_worktree_roots": ["/Users/x/Code"]}), [])
        for bad in ("/Users/x/Code", ["Code"], ["/"], ["/Users/../etc"], [1]):
            self.assertTrue(pr.validate_table({"extra_worktree_roots": bad}), bad)

    def test_only_m3_sweeps_its_worktree_root(self) -> None:
        # m1 and m5 keep worktrees beside their primary checkouts in ~/Code,
        # where idle clean clones are the operator's own repositories.
        root = pathlib.Path(__file__).resolve().parents[1] / "profiles"
        enabled = sorted(path.name for path in root.glob("*-macos-fleet.toml")
                         if "worktree_root_checkouts = true" in path.read_text())
        self.assertEqual(enabled, ["m3-macos-fleet.toml"])


class WiringTests(Fixture):
    def profile(self, body: str) -> pathlib.Path:
        path = self.base / "profile.toml"
        path.write_text(body)
        return path

    def test_off_unless_the_profile_opts_in(self) -> None:
        self.clone("done")
        off = tc.run(fix=True, profile=self.profile("[reclaim]\n"), in_use=lambda p: False,
                     root=self.tmp)
        self.assertFalse(off["enabled"])
        self.assertTrue((self.tmp / "done").exists())

    def test_validation_is_shared_with_the_profile_validator(self) -> None:
        self.assertEqual(pr.validate_table({"tmp_checkouts": True}), [])
        self.assertTrue(pr.validate_table({"tmp_checkouts": "yes"}))
        self.assertTrue(pr.validate_table({"tmp_checkouts": True,
                                           "tmp_checkout_idle_hours": 2}))

    def test_the_pass_summary_and_event_carry_the_counts(self) -> None:
        receipt = {"mode": "fix", "report": {"reclaimed_bytes": 0},
                   "tmp_checkouts": {"enabled": True, "mode": "fix", "checkouts": 5,
                                     "removed": 2, "removed_bytes": 3 * dr.GIB,
                                     "kept": {"dirty": 2}, "deferred": 0,
                                     "orphaned_worktrees": 1, "orphaned_paths": ["/tmp/x"],
                                     "broken_checkouts": 4, "broken_paths": []}}
        summary = dr.pass_summary(receipt, 0)
        self.assertEqual(summary["tmp_checkouts"]["removed"], 2)
        self.assertEqual(summary["tmp_checkouts"]["kept"], {"dirty": 2})
        self.assertEqual(summary["reclaimed_bytes"], 3 * dr.GIB)
        detail = dr.tmp_checkout_detail(summary["tmp_checkouts"])
        self.assertIn("stale checkouts removed 2 (3.0 GiB), kept dirty=2", detail)
        self.assertIn("1 orphaned worktrees (not removed)", detail)
        self.assertIn("4 broken .git dirs (not removed)", detail)
        # A dry run's would-remove bytes are not reclaimed bytes.
        dry = dr.pass_summary({**receipt, "mode": "dry-run"}, 0)
        self.assertEqual(dry["reclaimed_bytes"], 0)
        json.dumps(summary)


if __name__ == "__main__":
    unittest.main()
