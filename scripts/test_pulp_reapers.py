#!/usr/bin/env python3
"""The reclaim pass drives Pulp's own reapers, opt-in, from a fresh origin/main.

The end-to-end cases run the REAL reapers (verbatim copies under
tests/fixtures/pulp-reapers, see its SOURCE file) against a throwaway Pulp-like
repository: a bare origin, a primary clone, and worktrees under a scratch root.
Every "kept" assertion is paired with a "removed" control in the same run, so a
pass that silently did nothing cannot satisfy the test.

Run:  python3 -m unittest scripts.test_pulp_reapers
"""

from __future__ import annotations

import testing_support  # noqa: E402
import io
import json
import re
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from typing import Optional
from contextlib import redirect_stderr, redirect_stdout

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import disk_reclaim as dr  # noqa: E402
import pulp_reapers as pr  # noqa: E402

FIXTURE = HERE.parent / "tests" / "fixtures" / "pulp-reapers"
DAY = 86400.0


def git(cwd: pathlib.Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def backdate(path: pathlib.Path, days: float) -> None:
    stamp = time.time() - days * DAY
    for entry in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        os.utime(entry, (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp))


def fill(path: pathlib.Path, days: float) -> pathlib.Path:
    (path / "CMakeFiles").mkdir(parents=True, exist_ok=True)
    (path / "CMakeCache.txt").write_text("x")
    (path / "CMakeFiles" / "obj.o").write_bytes(b"y" * 4096)
    backdate(path, days)
    return path


class PulpRepo:
    """origin.git + a primary clone, the shape `[reclaim] repo` points at."""

    def __init__(self, root: pathlib.Path, *, with_ci: bool = True):
        self.root = root
        self.origin = root / "origin.git"
        seed = root / "seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        shutil.copytree(FIXTURE / "tools" / "scripts", seed / "tools" / "scripts")
        if with_ci:
            shutil.copytree(FIXTURE / "tools" / "ci", seed / "tools" / "ci")
        (seed / ".gitignore").write_text("build/\nbuild-*/\n")
        (seed / "README.md").write_text("pulp\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "a")
        self.first = git(seed, "rev-parse", "HEAD")
        (seed / "README.md").write_text("pulp 2\n")
        git(seed, "commit", "-q", "-am", "b")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-q", "origin", "main")
        self.seed = seed
        self.primary = root / "pulp"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.primary)], check=True)
        self.worktrees = root / "wts"
        self.worktrees.mkdir()

    def advance(self, name: str = "later") -> str:
        (self.seed / f"{name}.txt").write_text(name)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", name)
        git(self.seed, "push", "-q", "origin", "main")
        return git(self.seed, "rev-parse", "HEAD")

    def worktree(self, name: str, branch: str) -> pathlib.Path:
        path = self.worktrees / name
        git(self.primary, "worktree", "add", "-q", "-b", branch, str(path), self.first)
        return path

    def profile(self, path: pathlib.Path, **overrides) -> pathlib.Path:
        table = {"pulp_worktree_builds": True, "repo": str(self.primary),
                 "worktrees_root": str(self.worktrees), "pressure_free_gb": 10000}
        table.update(overrides)
        lines = ["[reclaim]"]
        for key, value in table.items():
            lines.append(f"{key} = {json.dumps(value)}")
        path.write_text("\n".join(lines) + "\n")
        return path


class Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        self.state = self.tmp / "state"
        self._env = {k: os.environ.get(k) for k in (
            "PULP_BUILD_DIR_LOCK_ROOT", "TARTCI_FLEET_PROFILE", "TARTCI_HOME",
            "TARTCI_BOOT_USAGE")}
        os.environ["PULP_BUILD_DIR_LOCK_ROOT"] = str(self.tmp / "locks")
        # A full reclaim pass would otherwise measure this host's real home.
        os.environ["TARTCI_BOOT_USAGE"] = "0"
        os.environ["TARTCI_HOME"] = str(self.tmp / "tartci")
        os.environ["TARTCI_FLEET_PROFILE"] = str(self.tmp / "absent.toml")
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for proc in self.procs:
            proc.kill()
            proc.wait()
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def hold(self, path: pathlib.Path) -> None:
        """A live process whose command line names `path`, like a build does."""
        # Not `sh -c "sleep N"`: sh execs the last command in place, so the
        # path would vanish from the process table before the reaper looks.
        self.procs.append(subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", str(path)],
            cwd=str(path)))

    def quiet(self, fn, *args, **kwargs):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            result = fn(*args, **kwargs)
        return result, err.getvalue()


class OffByDefault(Isolated):
    def recorder(self):
        calls = []

        def reaper(script, **kwargs):
            calls.append(script.name)
            return {"reaper": script.stem, "exit_code": 0, "reclaimed_bytes": 0}
        return calls, reaper

    @testing_support.requires_tomllib
    def test_no_profile_runs_nothing(self):
        calls, reaper = self.recorder()
        out = pr.run(fix=True, profile=self.tmp / "absent.toml", state_dir=self.state,
                     reaper=reaper)
        self.assertFalse(out["enabled"])
        self.assertIn("no installed fleet profile", out["reason"])
        self.assertEqual(calls, [])

    @testing_support.requires_tomllib
    def test_profile_without_the_table_or_with_it_false_runs_nothing(self):
        repo = PulpRepo(self.tmp)
        for body in ("schema = 1\n", repo.profile(self.tmp / "p.toml",
                                                  pulp_worktree_builds=False).read_text()):
            profile = self.tmp / "p.toml"
            profile.write_text(body)
            calls, reaper = self.recorder()
            out = pr.run(fix=True, profile=profile, state_dir=self.state, reaper=reaper)
            self.assertFalse(out["enabled"], body)
            self.assertEqual(calls, [], body)
        # Control: the same repo with the switch on DOES run a reaper, so the
        # two empty call lists above are the switch, not a broken fixture.
        calls, reaper = self.recorder()
        out = pr.run(fix=True, profile=repo.profile(self.tmp / "p.toml"),
                     state_dir=self.state, reaper=reaper)
        self.assertTrue(out["enabled"])
        self.assertIn("clean_build_cov.sh", calls)


class Validation(unittest.TestCase):
    def good(self, **overrides):
        table = {"pulp_worktree_builds": True, "repo": "/r", "worktrees_root": "/w"}
        table.update(overrides)
        return table

    def test_accepts_the_minimal_enabled_table_and_a_one_day_idle_window(self):
        self.assertEqual(pr.validate_table(self.good()), [])
        self.assertEqual(pr.validate_table(self.good(worktree_build_idle_hours=24)), [])

    def test_idle_window_can_neither_loosen_the_reaper_nor_exceed_a_day(self):
        # 1 would loosen the reaper's own 2 h gate; 25 would let a merged
        # 40 GB tree outlive the ~48 h it takes to eat a lease's headroom.
        for hours in (0, 1, 25, 48, 2.5, True):
            self.assertTrue(pr.validate_table(self.good(worktree_build_idle_hours=hours)),
                            hours)

    def test_rejects_unknown_keys_relative_paths_and_missing_paths(self):
        self.assertTrue(pr.validate_table(self.good(age_days=1)))
        self.assertTrue(pr.validate_table(self.good(repo="Code/pulp")))
        self.assertTrue(pr.validate_table({"pulp_worktree_builds": True}))
        self.assertTrue(pr.validate_table(self.good(pulp_worktree_builds="yes")))

    @testing_support.requires_tomllib
    def test_fleet_profile_loader_uses_the_same_validator(self):
        import macos_fleet_lanes as fleet
        base = (HERE.parent / "profiles" / "m3-macos-fleet.toml").read_text()
        # The checked-in profile may already carry the table; drop it so the
        # cases below decide what the loader sees.
        base = re.sub(r"(?ms)^\[reclaim\]\n.*?(?=^\[)", "", base)
        with tempfile.TemporaryDirectory() as td:
            ok = pathlib.Path(td) / "ok.toml"
            ok.write_text(base + '\n[reclaim]\npulp_worktree_builds = true\n'
                          'repo = "/Volumes/Example/Code/pulp"\n'
                          'worktrees_root = "/Volumes/Example/Code/agent-worktrees"\n')
            self.assertIs(fleet.load(ok)["reclaim"]["pulp_worktree_builds"], True)
            bad = pathlib.Path(td) / "bad.toml"
            bad.write_text(base + '\n[reclaim]\npulp_worktree_builds = true\n')
            with self.assertRaisesRegex(ValueError, "reclaim.repo is required"):
                fleet.load(bad)


class VolumeStallGuards(Isolated):
    """A wedged scan root must not start another potentially wedged Git walk."""

    @testing_support.requires_tomllib
    def test_scan_timeout_overlapping_configured_root_returns_a_skip_reason(self):
        repo = PulpRepo(self.tmp)
        profile = repo.profile(self.tmp / "p.toml")
        reason = pr.skip_reason_for_scan_timeouts(
            [str(repo.worktrees / "nested"), str(self.tmp / "unrelated")], profile)
        self.assertIn("configured Pulp root overlaps timed-out scan path", reason)
        self.assertIn(str(repo.worktrees / "nested"), reason)

    @testing_support.requires_tomllib
    def test_timeout_on_an_unrelated_root_does_not_skip_reapers(self):
        repo = PulpRepo(self.tmp)
        profile = repo.profile(self.tmp / "p.toml")
        self.assertIsNone(pr.skip_reason_for_scan_timeouts([str(self.tmp / "other")], profile))

    @testing_support.requires_tomllib
    def test_explicit_skip_does_not_materialize_or_invoke_a_reaper(self):
        repo = PulpRepo(self.tmp)
        calls = []

        def reaper(script, **kwargs):
            calls.append(script)
            return {"reaper": script.stem, "exit_code": 0, "reclaimed_bytes": 0}

        out = pr.run(fix=True, profile=repo.profile(self.tmp / "p.toml"),
                     state_dir=self.state, reaper=reaper,
                     skip_reason="skipped: external volume scan timed out")
        self.assertTrue(out["enabled"])
        self.assertEqual(out["runs"], [])
        self.assertEqual(calls, [])
        self.assertEqual(out["skipped"]["all"],
                         "skipped: external volume scan timed out")

    def test_git_timeout_is_clamped_to_the_remaining_pass_budget(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 0, "origin", "")

        pr._git(pathlib.Path("/repo"), "rev-parse", runner=runner,
                deadline=time.monotonic() + 0.05)
        self.assertEqual(len(calls), 1)
        self.assertLess(calls[0], pr.GIT_TIMEOUT_S)

    @testing_support.requires_tomllib
    def test_expired_pass_deadline_skips_reapers_fail_closed(self):
        repo = PulpRepo(self.tmp)
        calls = []

        def reaper(script, **kwargs):
            calls.append(script)
            return {"reaper": script.stem, "exit_code": 0, "reclaimed_bytes": 0}

        out = pr.run(fix=True, profile=repo.profile(self.tmp / "p.toml"),
                     state_dir=self.state, reaper=reaper,
                     deadline=time.monotonic() - 1)
        self.assertTrue(out["enabled"])
        self.assertEqual(out["runs"], [])
        self.assertEqual(calls, [])
        self.assertIn("deadline", out["reason"])


class Materialize(Isolated):
    def test_checkout_is_origin_main_and_carries_tools_ci(self):
        repo = PulpRepo(self.tmp)
        checkout, detail, sha = pr.materialize(repo.primary, self.state / "pulp-reapers")
        self.assertIsNotNone(checkout, detail)
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), git(repo.origin, "rev-parse", "main"))
        # The worktree reaper imports this; a copy without it dies with
        # ModuleNotFoundError on its first candidate.
        self.assertTrue((checkout / "tools" / "ci" / "build_dir_lock.py").is_file())
        # It is a worktree of the configured repository, which is how both
        # reapers find the worktrees they police.
        self.assertIn(str(checkout), git(repo.primary, "worktree", "list"))

    def test_follows_origin_main_on_the_next_pass(self):
        repo = PulpRepo(self.tmp)
        checkout, _, first = pr.materialize(repo.primary, self.state / "pulp-reapers")
        newer = repo.advance()
        self.assertNotEqual(first, newer)
        checkout, detail, sha = pr.materialize(repo.primary, self.state / "pulp-reapers")
        self.assertEqual(sha, newer, detail)
        self.assertEqual(git(checkout, "rev-parse", "HEAD"), newer)

    @testing_support.requires_tomllib
    def test_origin_without_tools_ci_is_refused_and_nothing_runs(self):
        repo = PulpRepo(self.tmp, with_ci=False)
        profile = repo.profile(self.tmp / "p.toml")
        calls = []
        out = pr.run(fix=True, profile=profile, state_dir=self.state,
                     reaper=lambda script, **kw: calls.append(script) or {})
        self.assertIn("build_dir_lock.py", out["error"])
        self.assertEqual(calls, [])


class EndToEnd(Isolated):
    """The real reapers, under pressure and not."""

    def run_real(self, repo: PulpRepo, **overrides):
        profile = repo.profile(self.tmp / "p.toml", **overrides)
        return self.quiet(pr.run, fix=True, profile=profile, state_dir=self.state)

    @testing_support.requires_tomllib
    def test_two_day_old_build_cov_idle_goes_active_stays(self):
        # m3, 2026-09-27: a 40 GB build-cov two days old, on a volume with
        # 21 GiB free. tartci's 7-day pressure gate kept it; Pulp's reaper
        # must take it unless something is using it.
        repo = PulpRepo(self.tmp)
        idle = fill(repo.worktrees / "wt-idle" / "build-cov", days=2)
        active = fill(repo.worktrees / "wt-active" / "build-cov", days=2)
        self.hold(active)
        (out, log) = self.run_real(repo)
        self.assertTrue(out["pressure"], out)
        self.assertFalse(idle.exists(), log)
        self.assertTrue(active.is_dir(), log)
        runs = {r["reaper"]: r for r in out["runs"]}
        self.assertEqual(runs["clean_build_cov"]["exit_code"], 0, log)

    @testing_support.requires_tomllib
    def test_build_cov_runs_even_without_pressure_but_worktree_reaper_does_not(self):
        repo = PulpRepo(self.tmp)
        cov = fill(repo.worktrees / "wt" / "build-cov", days=2)
        (out, log) = self.run_real(repo, pressure_free_gb=1)
        free_gib = shutil.disk_usage(repo.worktrees).free / pr.GIB
        if free_gib < 1:  # pragma: no cover - a host this full has other problems
            self.skipTest("host has under 1 GiB free, so 'no pressure' is not constructible")
        self.assertFalse(out["pressure"])
        self.assertEqual([r["reaper"] for r in out["runs"]], ["clean_build_cov"])
        self.assertIn("clean_worktree_builds", out["skipped"])
        self.assertFalse(cov.exists(), log)

    @testing_support.requires_tomllib
    def test_a_volume_past_the_used_percent_is_pressure_with_free_gib_to_spare(self):
        # m3 and m5, 2026-10-24/25: 91% and 92.5% used with 364 and 281 GiB
        # free. Judged by free GiB alone it was "no pressure", so the worktree
        # reaper never ran while readiness took both hosts out of the fleet.
        repo = PulpRepo(self.tmp)
        profile = repo.profile(self.tmp / "p.toml", pressure_free_gb=1)
        usage = shutil.disk_usage(repo.worktrees)
        if usage.free / pr.GIB < 1 or not usage.used:  # pragma: no cover
            self.skipTest("the free-GiB control isn't constructible on this host")
        (out, log) = self.quiet(pr.run, fix=True, profile=profile, state_dir=self.state,
                                pressure_used_percent=0.01)
        self.assertTrue(out["pressure"], out)
        self.assertIn("clean_worktree_builds", [r["reaper"] for r in out["runs"]], log)
        self.assertNotIn("clean_worktree_builds", out.get("skipped") or {})

    @testing_support.requires_tomllib
    def test_used_percent_off_or_not_reached_keeps_the_free_gib_rule(self):
        repo = PulpRepo(self.tmp)
        profile = repo.profile(self.tmp / "p.toml", pressure_free_gb=1)
        if shutil.disk_usage(repo.worktrees).free / pr.GIB < 1:  # pragma: no cover
            self.skipTest("the free-GiB control isn't constructible on this host")
        for percent in (0, 100):
            with self.subTest(pressure_used_percent=percent):
                (out, _log) = self.quiet(pr.run, fix=True, profile=profile, state_dir=self.state,
                                         pressure_used_percent=percent)
                self.assertFalse(out["pressure"], out)
                self.assertIn("clean_worktree_builds", out["skipped"])

    @testing_support.requires_tomllib
    def test_merged_idle_worktree_build_goes_under_pressure_and_the_guarded_ones_stay(self):
        repo = PulpRepo(self.tmp)
        merged = repo.worktree("merged", "feat/merged")
        build = fill(merged / "build", days=2)
        active_lineage = repo.worktree("lineage-active", "feat/busy")
        git(repo.primary, "config", "branch.feat/busy.pulpWorktreeStatus", "active")
        kept_lineage = fill(active_lineage / "build", days=2)
        in_use = repo.worktree("in-use", "feat/inuse")
        kept_in_use = fill(in_use / "build", days=2)
        self.hold(in_use)
        recent = repo.worktree("recent", "feat/recent")
        kept_recent = fill(recent / "build", days=0)
        (out, log) = self.run_real(repo)
        runs = {r["reaper"]: r for r in out["runs"]}
        self.assertIn("clean_worktree_builds", runs, out)
        record = runs["clean_worktree_builds"]
        if record["exit_code"] == 3 and "could not read process" in log:
            self.skipTest("this host's lsof/ps cannot give the reaper a clean process "
                          "snapshot, so it refuses (exit 3) by design; not a pass")
        self.assertEqual(record["exit_code"], 0, log)
        self.assertFalse(build.exists(), log)
        self.assertTrue(kept_lineage.is_dir(), log)
        self.assertTrue(kept_in_use.is_dir(), log)
        self.assertTrue(kept_recent.is_dir(), log)
        self.assertIn("removed 1 build dir(s)", record.get("summary", ""), log)


class WorktreesInTmp(Isolated):
    """REPORT ONLY: Pulp worktrees under /tmp are counted, never touched."""

    def setUp(self):
        super().setUp()
        # The control worktree must live OUTSIDE /tmp. $TMPDIR is /var/folders
        # on macOS but /tmp on Linux CI, so move the fixture root when needed.
        if pr._is_under_tmp(os.path.realpath(str(self.tmp)) + "/"):
            base = pathlib.Path.home() / ".cache" / "tartci-tests"
            base.mkdir(parents=True, exist_ok=True)
            self.tmp = pathlib.Path(tempfile.mkdtemp(dir=base)).resolve()
            self.addCleanup(shutil.rmtree, self.tmp, True)
        self._slash_tmp = tempfile.mkdtemp(prefix="tartci-tmp-wt-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self._slash_tmp, True)
        self.assertFalse(pr._is_under_tmp(os.path.realpath(str(self.tmp)) + "/"),
                         "fixture root must not itself be under /tmp")

    def test_counts_tmp_worktrees_and_not_the_ones_elsewhere(self):
        repo = PulpRepo(self.tmp)
        in_tmp = pathlib.Path(self._slash_tmp) / "wt"
        git(repo.primary, "worktree", "add", "-q", "-b", "feat/tmp", str(in_tmp), repo.first)
        (in_tmp / "blob").write_bytes(b"x" * 200_000)
        outside = repo.worktree("outside", "feat/outside")  # the control
        value = pr.tmp_worktrees(repo.primary)
        self.assertEqual(value["count"], 1, value)
        self.assertTrue(value["paths"][0].endswith("/wt"), value)
        self.assertFalse(any(str(outside.name) in p for p in value["paths"]))
        self.assertGreater(value["total_bytes"], 200_000)
        self.assertIsNotNone(value["oldest_mtime"])
        self.assertIn("never /tmp", value["rule"])
        # Never touched.
        self.assertTrue((in_tmp / "blob").is_file())
        self.assertTrue(outside.is_dir())

    def test_du_timeout_reads_unknown_not_zero(self):
        repo = PulpRepo(self.tmp)
        in_tmp = pathlib.Path(self._slash_tmp) / "wt"
        git(repo.primary, "worktree", "add", "-q", "-b", "feat/tmp", str(in_tmp), repo.first)

        def runner(argv, **kwargs):
            if argv[0] == "du":
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return subprocess.run(argv, **kwargs)
        value = pr.tmp_worktrees(repo.primary, runner=runner, du_timeout_s=1)
        self.assertEqual(value["count"], 1)
        self.assertIsNone(value["total_bytes"])
        self.assertIn("unknown", value["size"])

    @testing_support.requires_tomllib
    def test_reclaim_pass_reports_it_as_an_event_field(self):
        repo = PulpRepo(self.tmp)
        in_tmp = pathlib.Path(self._slash_tmp) / "wt"
        git(repo.primary, "worktree", "add", "-q", "-b", "feat/tmp", str(in_tmp), repo.first)
        os.environ["TARTCI_FLEET_PROFILE"] = str(repo.profile(self.tmp / "p.toml", pressure_free_gb=1))
        scan = self.tmp / "scan"
        scan.mkdir()
        state = self.tmp / "reclaim-state"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            dr.main(["--roots", str(scan), "--fix", "--state-dir", str(state)])
        event = json.loads((state / "events.jsonl").read_text().splitlines()[0])
        self.assertEqual(event["fields"]["pulp_reapers"]["worktrees_in_tmp"]["count"], 1)
        self.assertTrue(in_tmp.is_dir(), "a reclaim pass must never remove a /tmp worktree")

    def test_doctor_finding_names_the_rule_and_count(self):
        import fleet_doctor as fd
        finding = fd.check_worktrees_in_tmp({"count": 3, "size": "4.0 GiB",
                                            "oldest_mtime": time.time() - 86400})
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "worktrees_in_tmp"))
        self.assertIn("3 Pulp worktree(s)", finding.detail)
        self.assertIn(pr.TMP_WORKTREE_RULE, finding.detail)
        ok = fd.check_worktrees_in_tmp({"count": 0})
        self.assertEqual((ok.state, ok.code), (fd.OK, "worktrees_in_tmp_none"))
        off = fd.check_worktrees_in_tmp(None, reason="not enabled")
        self.assertEqual(off.code, "worktrees_in_tmp_not_checked")
        reasons = json.loads((HERE / "fleet_reasons.json").read_text())["reasons"]
        for code in ("worktrees_in_tmp", "worktrees_in_tmp_none", "worktrees_in_tmp_not_checked"):
            self.assertIn(code, fd.CODES)
            self.assertIn(code, reasons)


class DiscoveredPulpRoots(Isolated):
    """Coverage dirs are reaped in every Code root that holds this repo's worktrees."""

    def recorder(self):
        calls = []

        def reaper(script, **kwargs):
            calls.append((script.stem, kwargs["worktrees_root"], kwargs.get("timeout_s")))
            return {"reaper": script.stem, "exit_code": 0, "reclaimed_bytes": 0,
                    "worktrees_root": kwargs["worktrees_root"]}
        return calls, reaper

    def other_root_worktree(self, repo: PulpRepo, root: pathlib.Path, name: str) -> pathlib.Path:
        root.mkdir(exist_ok=True)
        path = root / name
        git(repo.primary, "worktree", "add", "-q", "-b", f"feat/{name}", str(path), repo.first)
        return path

    def run_with(self, repo: PulpRepo, roots, *, fix: bool = True, runner=subprocess.run,
                 **overrides):
        calls, reaper = self.recorder()
        profile = repo.profile(self.tmp / "p.toml", **overrides)
        (out, log) = self.quiet(pr.run, fix=fix, profile=profile, state_dir=self.state,
                                reaper=reaper, discovered_roots=roots, runner=runner)
        return calls, out, log

    def extra_runs(self, calls, roots):
        wanted = {str(os.path.realpath(r)) for r in roots}
        return [c for c in calls if c[1] in wanted and c[0] == "clean_build_cov"]

    @testing_support.requires_tomllib
    def test_the_m5s_boot_volume_root_gets_its_own_coverage_run(self):
        # m5s, 2026-10-04: worktrees in ~/Code on the boot volume, the profile's
        # worktrees_root on another volume. The boot root was never reaped.
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        self.other_root_worktree(repo, boot_code, "pulp-wave2e")
        calls, out, log = self.run_with(repo, [boot_code])
        extra = self.extra_runs(calls, [boot_code])
        self.assertEqual(len(extra), 1, calls)
        record = [r for r in out["runs"] if r.get("reason") == "discovered_pulp_root"]
        self.assertEqual(len(record), 1, out)
        self.assertEqual(out["outside_profile_roots"],
                         [{"root": str(os.path.realpath(boot_code)), "count": 1}])
        self.assertEqual(log.count("worktrees_outside_profile_root"), 1, log)
        # The heavier reaper never follows a discovered root.
        self.assertFalse([c for c in calls if c[0] == "clean_worktree_builds"
                          and c[1] == str(os.path.realpath(boot_code))], calls)

    def second_clone_worktree(self, repo: PulpRepo, root: pathlib.Path, name: str,
                              *, url: Optional[str] = None) -> pathlib.Path:
        """A worktree of a SECOND clone of the same origin, as in m5s's ~/Code."""
        clone = self.tmp / f"clone-{name}"
        subprocess.run(["git", "clone", "-q", str(repo.origin), str(clone)], check=True)
        if url is not None:
            git(clone, "remote", "set-url", "origin", url)
        root.mkdir(exist_ok=True)
        path = root / name
        git(clone, "worktree", "add", "-q", "-b", f"feat/{name}", str(path), repo.first)
        return path

    @testing_support.requires_tomllib
    def test_a_second_clone_of_the_same_origin_is_reaped(self):
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        self.second_clone_worktree(repo, boot_code, "wt-a")
        self.second_clone_worktree(repo, boot_code, "wt-b")
        calls, out, log = self.run_with(repo, [boot_code])
        resolved = str(os.path.realpath(boot_code))
        self.assertEqual(len(self.extra_runs(calls, [boot_code])), 1, calls)
        self.assertEqual(out["outside_profile_roots"], [{"root": resolved, "count": 2}])
        self.assertEqual(out["discovery"]["children_seen"], 2)
        self.assertIn(resolved, out["discovery"]["roots"])
        self.assertEqual(log.count(f"worktrees_outside_profile_root root={resolved} count=2"), 1)

    @testing_support.requires_tomllib
    def test_a_clone_of_another_origin_is_not_counted(self):
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        self.second_clone_worktree(repo, boot_code, "wt", url="git@github.com:Someone/else.git")
        calls, out, log = self.run_with(repo, [boot_code])
        self.assertEqual(self.extra_runs(calls, [boot_code]), [], calls)
        self.assertEqual(out["outside_profile_roots"], [])
        self.assertEqual(out["discovery"]["children_seen"], 1, "scanned, matched nothing")
        self.assertNotIn("worktrees_outside_profile_root", log)

    @testing_support.requires_tomllib
    def test_a_child_whose_url_cannot_be_read_is_not_counted_and_is_recorded(self):
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        broken = boot_code / "broken"
        broken.mkdir(parents=True)
        (broken / ".git").write_text("gitdir: /nonexistent/worktrees/broken\n")
        calls, out, _ = self.run_with(repo, [boot_code])
        self.assertEqual(self.extra_runs(calls, [boot_code]), [], calls)
        self.assertEqual(out["discovery"]["unreadable"], [str(broken)])
        self.assertEqual(out["discovery"]["unreadable_children"], 1)
        no_origin = self.second_clone_worktree(repo, boot_code, "no-origin")
        git(self.tmp / "clone-no-origin", "remote", "remove", "origin")
        calls, out, _ = self.run_with(repo, [boot_code])
        self.assertEqual(self.extra_runs(calls, [boot_code]), [], calls)
        self.assertEqual(sorted(out["discovery"]["unreadable"]), sorted([str(broken), str(no_origin)]))
        self.assertEqual(out["discovery"]["unreadable_children"], 2)

    @testing_support.requires_tomllib
    def test_an_unreadable_configured_origin_runs_nothing_outside_and_says_so(self):
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        self.second_clone_worktree(repo, boot_code, "wt")
        primary = str(repo.primary)

        def runner(argv, **kwargs):
            # The configured clone's origin cannot be read; everything else can.
            if argv[:4] == ["git", "-C", primary, "config"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.run(argv, **kwargs)
        calls, out, _ = self.run_with(repo, [boot_code], runner=runner)
        self.assertEqual(self.extra_runs(calls, [boot_code]), [], calls)
        self.assertEqual(out["discovery"]["reason"], "configured_origin_unreadable")
        self.assertEqual((out["discovery"]["roots"], out["discovery"]["children_seen"]), ([], 0),
                         "with no identity nothing outside the profile root is even scanned")
        self.assertEqual(out["outside_profile_roots"], [])
        self.assertTrue([c for c in calls if c[0] == "clean_build_cov"], "the profile root still runs")

    @testing_support.requires_tomllib
    def test_discovery_that_did_not_run_reads_differently_from_a_zero(self):
        repo = PulpRepo(self.tmp)
        calls, out, _ = self.run_with(repo, [], worktrees_root=str(self.tmp / "absent"))
        self.assertEqual(out["discovery"]["reason"], "not_reached")
        calls, out, _ = self.run_with(repo, [])
        self.assertIsNone(out["discovery"]["reason"])
        self.assertEqual(out["discovery"]["children_seen"], 0)

    def test_origin_urls_normalize_across_forms(self):
        same = ["git@github.com:Generous-Corp/pulp.git", "ssh://git@github.com/Generous-Corp/pulp",
                "ssh://git@github.com:22/Generous-Corp/pulp.git/",
                "https://github.com/Generous-Corp/pulp.git", "https://GitHub.com/generous-corp/pulp/",
                "http://github.com/Generous-Corp/pulp"]
        self.assertEqual({pr.normalize_origin(u) for u in same}, {"github.com/generous-corp/pulp"})
        self.assertNotEqual(pr.normalize_origin("git@github.com:Generous-Corp/pulp-planning.git"),
                            pr.normalize_origin(same[0]))

    @testing_support.requires_tomllib
    def test_the_receipt_carries_every_field_a_control_reads(self):
        # Structural, not a second literal list: every key the REAL reaper puts
        # in a run record, and the discovery evidence, must survive the
        # projection into last-run.json. A key run() adds without being
        # projected fails here.
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        self.second_clone_worktree(repo, boot_code, "wt")
        profile = repo.profile(self.tmp / "p.toml")
        out, _ = self.quiet(pr.run, fix=False, profile=profile, state_dir=self.state,
                            discovered_roots=[boot_code])
        self.assertTrue([r for r in out["runs"] if r.get("reason") == "discovered_pulp_root"])
        summary = dr.pass_summary({"pulp_reapers": out, "mode": "dry-run", "report": {}}, 0)
        projected = summary["pulp_reapers"]
        self.assertEqual(len(projected["runs"]), len(out["runs"]))
        for record, shown in zip(out["runs"], projected["runs"]):
            self.assertLessEqual(set(record), set(shown), record)
        self.assertEqual(projected["outside_profile_roots"], out["outside_profile_roots"])
        self.assertLessEqual(set(out["discovery"]), set(projected["discovery"]))

    @testing_support.requires_tomllib
    def test_each_clones_agent_worktrees_get_one_run_when_present(self):
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        self.second_clone_worktree(repo, boot_code, "wt")
        calls, out, _ = self.run_with(repo, [boot_code])
        self.assertFalse([r for r in out["runs"] if "agent_worktrees" in str(r.get("reason"))],
                         "absent .claude/worktrees: nothing recorded")
        configured = repo.primary / ".claude" / "worktrees"
        second = self.tmp / "clone-wt" / ".claude" / "worktrees"
        configured.mkdir(parents=True)
        second.mkdir(parents=True)
        calls, out, _ = self.run_with(repo, [boot_code])
        reasons = {(r["reason"], r["worktrees_root"]) for r in out["runs"] if r.get("reason")}
        self.assertIn(("configured_clone_agent_worktrees", str(configured)), reasons)
        self.assertIn(("discovered_clone_agent_worktrees", str(second)), reasons)
        heavy = [c for c in calls if c[0] == "clean_worktree_builds"
                 and c[1] in (str(configured), str(second))]
        self.assertEqual(heavy, [], "the heavier reaper never follows these")

    @testing_support.requires_tomllib
    def test_the_configured_clones_agent_worktrees_run_once_when_also_discovered(self):
        # m3: every matching child of /Volumes/Example/Code is a worktree of
        # the CONFIGURED clone, so discovery finds that clone too. Its
        # .claude/worktrees must still be reaped exactly once, as configured.
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        self.other_root_worktree(repo, boot_code, "wt")
        agent = repo.primary / ".claude" / "worktrees"
        agent.mkdir(parents=True)
        calls, out, _ = self.run_with(repo, [boot_code])
        runs = [r for r in out["runs"] if r.get("worktrees_root") == str(agent)]
        self.assertEqual([r["reason"] for r in runs], ["configured_clone_agent_worktrees"], runs)
        self.assertEqual(len([c for c in calls if c[1] == str(agent)]), 1, calls)

    @testing_support.requires_tomllib
    def test_two_discovered_roots_get_one_run_each(self):
        repo = PulpRepo(self.tmp)
        first, second = self.tmp / "code-a", self.tmp / "code-b"
        self.other_root_worktree(repo, first, "wt-a")
        self.other_root_worktree(repo, second, "wt-b")
        calls, out, _ = self.run_with(repo, [first, second])
        self.assertEqual(len(self.extra_runs(calls, [first])), 1, calls)
        self.assertEqual(len(self.extra_runs(calls, [second])), 1, calls)

    @testing_support.requires_tomllib
    def test_the_profile_root_is_not_run_twice(self):
        repo = PulpRepo(self.tmp)
        repo.worktree("wt", "feat/wt")
        calls, out, _ = self.run_with(repo, [repo.worktrees])
        runs = [c for c in calls if c[0] == "clean_build_cov"]
        self.assertEqual(len(runs), 1, calls)
        self.assertEqual(out["outside_profile_roots"], [])

    def test_a_root_holding_only_a_foreign_repo_is_left_alone(self):
        repo = PulpRepo(self.tmp)
        foreign_root = self.tmp / "foreign-code"
        foreign_root.mkdir()
        other = self.tmp / "other-repo"
        subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
        (other / "f").write_text("x")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "o")
        git(other, "worktree", "add", "-q", "-b", "x", str(foreign_root / "wt"))
        calls, out, log = self.run_with(repo, [foreign_root])
        self.assertEqual(self.extra_runs(calls, [foreign_root]), [], calls)
        self.assertNotIn("worktrees_outside_profile_root", log)

    def test_the_primary_checkout_is_not_a_worktree_outside_the_profile(self):
        # m3 keeps its primary checkout in /Volumes/Example/Code beside
        # agent-worktrees; that root holds the repository itself, not worktrees.
        repo = PulpRepo(self.tmp)
        calls, out, log = self.run_with(repo, [repo.root])
        self.assertEqual(self.extra_runs(calls, [repo.root]), [], calls)
        self.assertNotIn("worktrees_outside_profile_root", log)

    def test_a_root_of_plain_dirs_is_left_alone(self):
        repo = PulpRepo(self.tmp)
        plain = self.tmp / "plain-code"
        (plain / "project" / "build-cov").mkdir(parents=True)
        calls, out, _ = self.run_with(repo, [plain])
        self.assertEqual(self.extra_runs(calls, [plain]), [], calls)

    def test_disabled_pulp_worktree_builds_runs_nothing_anywhere(self):
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        self.other_root_worktree(repo, boot_code, "wt")
        calls, out, _ = self.run_with(repo, [boot_code], pulp_worktree_builds=False)
        self.assertEqual(calls, [])
        self.assertFalse(out["enabled"])

    @testing_support.requires_tomllib
    def test_the_discovered_root_run_follows_the_pass_mode(self):
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        self.other_root_worktree(repo, boot_code, "wt")
        modes = []

        def reaper(script, **kwargs):
            modes.append((kwargs["worktrees_root"], kwargs["fix"]))
            return {"reaper": script.stem, "exit_code": 0, "reclaimed_bytes": 0}
        profile = repo.profile(self.tmp / "p.toml")
        for fix in (False, True):
            modes.clear()
            self.quiet(pr.run, fix=fix, profile=profile, state_dir=self.state,
                       reaper=reaper, discovered_roots=[boot_code])
            extra = [m for m in modes if m[0] == str(os.path.realpath(boot_code))]
            self.assertEqual(extra, [(str(os.path.realpath(boot_code)), fix)])

    @testing_support.requires_tomllib
    def test_the_real_reaper_clears_coverage_in_the_discovered_root(self):
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        wt = self.other_root_worktree(repo, boot_code, "pulp-wave2f")
        cov = fill(wt / "build-cov", days=2)
        kept = fill(repo.worktrees / "wt" / "build-cov", days=2)
        profile = repo.profile(self.tmp / "p.toml", pressure_free_gb=1)
        (out, log) = self.quiet(pr.run, fix=True, profile=profile, state_dir=self.state,
                                discovered_roots=[boot_code])
        self.assertFalse(cov.exists(), log)
        self.assertFalse(kept.exists(), log)
        extra = [r for r in out["runs"] if r.get("reason") == "discovered_pulp_root"]
        self.assertEqual(extra[0]["exit_code"], 0, log)


class DiskReclaimIntegration(Isolated):
    @testing_support.requires_tomllib
    def test_scan_timeout_skips_pulp_reapers_and_is_recorded(self):
        repo = PulpRepo(self.tmp)
        os.environ["TARTCI_FLEET_PROFILE"] = str(repo.profile(self.tmp / "p.toml"))
        scan = self.tmp / "scan"
        scan.mkdir()
        state = self.tmp / "reclaim-state"
        out = io.StringIO()
        with unittest.mock.patch.object(dr, "SCAN_TIMEOUTS", [str(repo.worktrees)]), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = dr.main(["--roots", str(scan), "--json", "--state-dir", str(state),
                            "--boot-floor-gb", "0"])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        reason = report["pulp_reapers"]["reason"]
        self.assertIn("configured Pulp root overlaps timed-out scan path", reason)
        self.assertEqual(report["pulp_reapers"]["runs"], [])
        receipt = json.loads((state / "last-run.json").read_text())
        self.assertEqual(receipt["pulp_reapers"]["reason"], reason)

    @testing_support.requires_tomllib
    def test_receipt_event_and_log_line_carry_the_pulp_result(self):
        repo = PulpRepo(self.tmp)
        cov = fill(repo.worktrees / "wt" / "build-cov", days=2)
        os.environ["TARTCI_FLEET_PROFILE"] = str(repo.profile(self.tmp / "p.toml"))
        scan = self.tmp / "scan"
        scan.mkdir()
        state = self.tmp / "reclaim-state"
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()) as err:
            code = dr.main(["--roots", str(scan), "--json", "--fix",
                            "--state-dir", str(state), "--boot-floor-gb", "0"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertFalse(cov.exists())
        report = json.loads(out.getvalue())
        self.assertTrue(report["pulp_reapers"]["enabled"])
        receipt = json.loads((state / "last-run.json").read_text())
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["result"], "ok")
        self.assertTrue(receipt["pulp_reapers"]["enabled"])
        self.assertEqual(receipt["pulp_reapers"]["runs"][0]["reaper"], "clean_build_cov")
        for key in ("free_bytes_before", "free_bytes_after", "reclaimed_bytes"):
            self.assertIn(key, receipt["pulp_reapers"]["runs"][0])
        events = [json.loads(line) for line in
                  (state / "events.jsonl").read_text().splitlines()]
        # Under pressure (pressure_free_gb = 10000) both reapers run.
        self.assertEqual([e["event"] for e in events],
                         ["reclaim_pass", "pulp_reaper", "pulp_reaper"])
        self.assertEqual([e["fields"]["reaper"] for e in events[1:]],
                         ["clean_build_cov", "clean_worktree_builds"])
        self.assertIn('"event": "reclaim_pass"', err.getvalue())

    @testing_support.requires_tomllib
    def test_the_reclaim_pass_hands_its_scan_roots_to_the_reapers(self):
        repo = PulpRepo(self.tmp)
        boot_code = self.tmp / "boot-code"
        boot_code.mkdir()
        git(repo.primary, "worktree", "add", "-q", "-b", "feat/boot", str(boot_code / "wt"),
            repo.first)
        cov = fill(boot_code / "wt" / "build-cov", days=2)
        os.environ["TARTCI_FLEET_PROFILE"] = str(repo.profile(self.tmp / "p.toml"))
        state = self.tmp / "reclaim-state"
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()) as err:
            code = dr.main(["--roots", str(boot_code), "--json", "--fix",
                            "--state-dir", str(state), "--boot-floor-gb", "0"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertFalse(cov.exists(), err.getvalue())
        report = json.loads(out.getvalue())
        self.assertEqual(report["pulp_reapers"]["outside_profile_roots"],
                         [{"root": str(os.path.realpath(boot_code)), "count": 1}])
        # The landed control reads the receipt, not stdout.
        receipt = json.loads((state / "last-run.json").read_text())["pulp_reapers"]
        self.assertEqual(receipt["outside_profile_roots"],
                         [{"root": str(os.path.realpath(boot_code)), "count": 1}])
        self.assertEqual([(r["reason"], r["worktrees_root"]) for r in receipt["runs"]
                          if r.get("reason")],
                         [("discovered_pulp_root", str(os.path.realpath(boot_code)))])
        self.assertGreaterEqual(receipt["discovery"]["children_seen"], 1)

    def test_a_failed_pass_still_leaves_a_receipt(self):
        state = self.tmp / "reclaim-state"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = dr.main(["--roots", str(self.tmp / "absent"), "--state-dir", str(state)])
        self.assertEqual(code, 2)
        receipt = json.loads((state / "last-run.json").read_text())
        self.assertEqual((receipt["exit_code"], receipt["result"]), (2, "failed"))


if __name__ == "__main__":
    unittest.main()
