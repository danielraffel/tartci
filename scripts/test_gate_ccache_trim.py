#!/usr/bin/env python3
"""gate_ccache_trim: when the reclaim pass evicts old gate ccache entries.

Most tests drive `evict()` and `run()` through a stub `ccache` that records its
arguments, so every gate is exercised without a host cache. RealCcacheTests use
the real ccache and a C compiler when both are installed: entries backdated
past the window are evicted, recent ones stay, and the counters ccache keeps
are recounted from disk even after they were zeroed (the undercount the trim
also repairs).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ccache_guard  # noqa: E402
import disk_reclaim  # noqa: E402
import gate_ccache_trim as trim  # noqa: E402
import pulp_reapers  # noqa: E402

HERE = Path(__file__).resolve().parent
DAY = 86400

STUB = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["STUB_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
if "--print-stats" in sys.argv:
    evicted = any("--evict-older-than" in line for line in open(log))
    print("files_in_cache\t%d" % (150 if evicted else 500))
    print("cache_size_kibibyte\t%d" % (1500 if evicted else 5000))
sys.exit(int(os.environ.get("STUB_RC", "0")))
'''


class StubFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cache = self.tmp / "ccache"
        (self.cache / "0" / "1").mkdir(parents=True)
        for name, size in (("a", 100), ("b", 250)):
            (self.cache / "0" / "1" / name).write_bytes(b"x" * size)
        (self.cache / "0" / "stats").write_text("0\n")
        self.state = self.tmp / "state"
        self.log = self.tmp / "calls.jsonl"
        self.stub = self.tmp / "ccache-stub"
        self.stub.write_text(STUB)
        self.stub.chmod(0o755)
        os.environ["STUB_LOG"] = str(self.log)
        os.environ.pop("STUB_RC", None)

    def tearDown(self) -> None:
        os.environ.pop("STUB_LOG", None)
        os.environ.pop("STUB_RC", None)
        self._tmp.cleanup()

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def evict(self, *, fix: bool = True, busy: str | None = None, age: int = 14) -> dict:
        return trim.evict(cache=self.cache, max_age_days=age, fix=fix, ccache=str(self.stub),
                          busy_probe=lambda: busy, runner=subprocess.run)


class Evict(StubFixture):
    def test_evicts_by_age_through_ccache_and_reports_recounted_totals(self):
        report = self.evict(age=14)
        self.assertEqual(report["status"], "evicted")
        evictions = [call for call in self.calls() if "--evict-older-than" in call]
        self.assertEqual(evictions, [["-d", str(self.cache), "--evict-older-than", "14d"]])
        # Entries and bytes come from the files, not ccache's counters.
        self.assertEqual(report["before"], {"entries": 2, "bytes": 350})
        self.assertEqual(report["after"], {"entries": 2, "bytes": 350})
        self.assertEqual(report["counters_after"]["files_in_cache"], 150)

    def test_a_running_or_leased_vm_blocks_the_trim(self):
        report = self.evict(busy="a Tart VM is running on this host")
        self.assertEqual(report["status"], "skipped")
        self.assertIn("Tart VM", report["reason"])
        self.assertEqual(self.calls(), [])

    def test_a_held_vm_lease_blocks_the_trim(self):
        report = self.evict(busy="1 VM lease(s) held on this host")
        self.assertEqual(report["status"], "skipped")
        self.assertIn("VM lease", report["reason"])
        self.assertEqual(self.calls(), [])

    def test_a_held_guard_lock_blocks_the_trim(self):
        lock = ccache_guard.Lock(ccache_guard.default_quarantine_root(self.cache) / ".guard.lock")
        self.assertTrue(lock.acquire())
        try:
            report = self.evict()
        finally:
            lock.release()
        self.assertEqual(report["status"], "skipped")
        self.assertIn("guard", report["reason"])
        self.assertEqual(self.calls(), [])

    def test_the_guard_lock_is_held_during_and_released_after(self):
        seen = {}
        lock_path = ccache_guard.default_quarantine_root(self.cache) / ".guard.lock"

        def runner(command, **kwargs):
            if "--evict-older-than" in command:
                probe = ccache_guard.Lock(lock_path)
                seen["held"] = not probe.acquire(0.0)
                probe.release()
            return subprocess.run(command, **kwargs)

        report = trim.evict(cache=self.cache, max_age_days=14, fix=True, ccache=str(self.stub),
                            busy_probe=lambda: None, runner=runner)
        self.assertEqual(report["status"], "evicted")
        self.assertTrue(seen["held"], "a guard must not run while ccache evicts")
        after = ccache_guard.Lock(lock_path)
        self.assertTrue(after.acquire(0.0))
        after.release()

    def test_dry_run_plans_and_runs_nothing(self):
        report = self.evict(fix=False)
        self.assertEqual(report["status"], "planned")
        self.assertEqual(self.calls(), [])

    def test_a_failed_eviction_is_an_error_not_a_completion(self):
        os.environ["STUB_RC"] = "1"
        report = self.evict()
        self.assertEqual(report["status"], "error")

    def test_a_missing_cache_or_ccache_skips(self):
        shutil.rmtree(self.cache)
        self.assertEqual(self.evict()["status"], "skipped")
        report = trim.evict(cache=self.tmp, max_age_days=14, fix=True, ccache=None,
                            busy_probe=lambda: None, runner=subprocess.run)
        self.assertEqual(report["status"], "skipped")


class CacheDir(unittest.TestCase):
    def test_the_runners_variable_wins_then_the_legacy_one_then_the_profile(self):
        both = {"TARTCI_CI_CACHE": "/Volumes/Workshop/ci-cache", "PULP_CI_CACHE": "/legacy"}
        self.assertEqual(trim.cache_dir("/profile/root", env=both),
                         Path("/Volumes/Workshop/ci-cache/ccache"))
        self.assertEqual(trim.cache_dir("/profile/root", env={"PULP_CI_CACHE": "/legacy"}),
                         Path("/legacy/ccache"))
        self.assertEqual(trim.cache_dir("/profile/root", env={}), Path("/profile/root/ccache"))
        self.assertEqual(trim.cache_dir(None, env={}),
                         Path("~/.cache/pulp-ci").expanduser() / "ccache")


class Settings(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(trim.validate({"gate_ccache_trim": True}), [])
        for key, value in (("gate_ccache_max_age_days", 2), ("gate_ccache_max_age_days", 91),
                           ("gate_ccache_max_age_days", "14"), ("gate_ccache_max_age_days", 14.0),
                           ("gate_ccache_trim_interval_hours", 0),
                           ("gate_ccache_trim_interval_hours", 169),
                           ("gate_ccache_trim", "yes")):
            problems = trim.validate({"gate_ccache_trim": True, key: value})
            self.assertTrue(problems, (key, value))

    def test_the_whole_reclaim_table_accepts_the_trim_keys(self):
        table = {"pulp_worktree_builds": False, "gate_ccache_trim": True,
                 "gate_ccache_max_age_days": 14, "gate_ccache_trim_interval_hours": 24}
        self.assertEqual(pulp_reapers.validate_table(table), [])
        self.assertTrue(pulp_reapers.validate_table({"gate_ccache_max_age_days": 1}))

    @unittest.skipIf(trim.tomllib is None, "needs tomllib (Python 3.11+)")
    def test_every_macos_fleet_profile_opts_in_with_the_default_window(self):
        # The m5 canary held the per-job hit rate (99.49% against 99.51%) while
        # the pre-boot guard's ok-run median fell from 89 s to 11 s, so every
        # macOS fleet host runs it at the defaults.
        profiles = sorted((HERE.parent / "profiles").glob("*-macos-fleet.toml"))
        self.assertEqual([p.name.split("-macos")[0] for p in profiles],
                         ["m1", "m3", "m5", "m5studio"])
        for profile in profiles:
            settings, why = trim.load_settings(profile)
            self.assertEqual(why, "enabled", profile.name)
            self.assertEqual((settings["max_age_days"], settings["interval_hours"]), (14, 24),
                             profile.name)


class ReclaimEvent(unittest.TestCase):
    def test_the_pass_event_names_the_eviction_and_hides_a_pass_that_was_not_due(self):
        receipt = {"mode": "fix", "report": {}, "pulp_reapers": {},
                   "gate_ccache_trim": {"enabled": True, "status": "evicted", "max_age_days": 14,
                                        "before": {"entries": 506745, "bytes": 14_300_000_000},
                                        "after": {"entries": 149655, "bytes": 5_500_000_000}}}
        summary = disk_reclaim.pass_summary(receipt, 0)
        self.assertEqual(summary["gate_ccache_trim"]["after"]["entries"], 149655)
        self.assertEqual(disk_reclaim.gate_ccache_detail(summary["gate_ccache_trim"]),
                         "; gate ccache evicted >14d: 506745 -> 149655 entries (14.3 -> 5.5 GB)")
        self.assertEqual(disk_reclaim.gate_ccache_detail({"enabled": True, "status": "not_due"}), "")
        self.assertIn("Tart VM", disk_reclaim.gate_ccache_detail(
            {"enabled": True, "status": "skipped", "reason": "a Tart VM is running on this host"}))


@unittest.skipIf(trim.tomllib is None, "profile reading needs tomllib (Python 3.11+)")
class Run(StubFixture):
    def profile(self, body: str) -> Path:
        path = self.tmp / "profile.toml"
        path.write_text(body)
        return path

    def run_pass(self, profile: Path, now: float, busy: str | None = None) -> dict:
        return trim.run(fix=True, profile=profile, state_dir=self.state, now=now,
                        ccache=str(self.stub), busy_probe=lambda: busy)

    def setUp(self) -> None:
        super().setUp()
        for name in ("TARTCI_CI_CACHE", "PULP_CI_CACHE"):
            self.addCleanup(os.environ.__setitem__, name, os.environ[name]) \
                if name in os.environ else self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)

    def opted_in(self, extra: str = "") -> Path:
        return self.profile(f'[host]\ncache_root = "{self.tmp}"\n'
                            f"[reclaim]\ngate_ccache_trim = true\n{extra}")

    def test_off_unless_the_profile_opts_in(self):
        report = self.run_pass(self.profile("[reclaim]\nscratch_dirs = true\n"), now=1e9)
        self.assertFalse(report["enabled"])
        self.assertEqual(self.calls(), [])

    def test_the_cache_is_the_profile_cache_root_and_the_age_is_configurable(self):
        report = self.run_pass(self.opted_in("gate_ccache_max_age_days = 21\n"), now=1e9)
        self.assertEqual(report["status"], "evicted")
        self.assertIn(["-d", str(self.cache), "--evict-older-than", "21d"], self.calls())

    def test_tartci_ci_cache_overrides_the_profile_cache_root(self):
        os.environ["TARTCI_CI_CACHE"] = str(self.tmp)
        profile = self.profile('[host]\ncache_root = "/nonexistent/elsewhere"\n'
                               "[reclaim]\ngate_ccache_trim = true\n")
        report = self.run_pass(profile, now=1e9)
        self.assertEqual(report["status"], "evicted")
        self.assertIn(["-d", str(self.cache), "--evict-older-than", "14d"], self.calls())

    def test_disabled_is_a_no_op(self):
        report = self.run_pass(self.profile('[host]\ncache_root = "%s"\n[reclaim]\n'
                                            "gate_ccache_trim = false\n" % self.tmp), now=1e9)
        self.assertFalse(report["enabled"])
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.state / trim.STAMP_FILE).exists())

    def test_at_most_once_per_interval_and_a_skip_retries_next_pass(self):
        profile = self.opted_in("gate_ccache_trim_interval_hours = 24\n")
        busy = self.run_pass(profile, now=1e9, busy="1 VM lease(s) held on this host")
        self.assertEqual(busy["status"], "skipped")
        first = self.run_pass(profile, now=1e9 + 3600)
        self.assertEqual(first["status"], "evicted")
        again = self.run_pass(profile, now=1e9 + 3600 + 23 * 3600)
        self.assertEqual(again["status"], "not_due")
        later = self.run_pass(profile, now=1e9 + 3600 + 25 * 3600)
        self.assertEqual(later["status"], "evicted")
        evictions = [c for c in self.calls() if "--evict-older-than" in c]
        self.assertEqual(len(evictions), 2)


def _real_tools() -> tuple[str, str] | None:
    ccache = ccache_guard.resolve_ccache()
    cc = shutil.which("cc") or shutil.which("clang")
    if not ccache or not cc:
        return None
    probe = subprocess.run([ccache, "--help"], capture_output=True, text=True)
    if "--evict-older-than" not in probe.stdout:
        return None
    return ccache, cc


@unittest.skipIf(_real_tools() is None, "needs ccache with --evict-older-than and a C compiler")
class RealCcacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cache = self.tmp / "ccache"
        self.ccache, self.cc = _real_tools()  # type: ignore[misc]
        self.env = dict(os.environ, CCACHE_DIR=str(self.cache), CCACHE_NODEPEND="true",
                        CCACHE_TEMPDIR=str(self.tmp / "cctmp"))
        for name in ("CCACHE_DISABLE", "CCACHE_RECACHE", "CCACHE_READONLY"):
            self.env.pop(name, None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def compile(self, name: str, body: str) -> None:
        src = self.tmp / f"{name}.c"
        src.write_text(body)
        subprocess.run([self.ccache, self.cc, "-c", str(src), "-o", str(self.tmp / f"{name}.o")],
                       env=self.env, check=True, cwd=self.tmp)

    def entries(self) -> set[Path]:
        return set(ccache_guard.iter_entries(self.cache))

    def test_old_entries_go_recent_stay_and_zeroed_counters_are_recounted(self):
        self.compile("old", "int old_unit(void) { return 1; }\n")
        old = self.entries()
        self.assertTrue(old)
        stamp = time.time() - 30 * DAY
        for path in old:
            os.utime(path, (stamp, stamp))
        self.compile("recent", "int recent_unit(void) { return 2; }\n")
        recent = self.entries() - old
        self.assertTrue(recent)
        # The undercount this also repairs: every level-1 counter file reset.
        for stats in self.cache.glob("?/stats"):
            stats.write_text("")
        report = trim.evict(cache=self.cache, max_age_days=14, fix=True, ccache=self.ccache,
                            busy_probe=lambda: None, runner=subprocess.run)
        self.assertEqual(report["status"], "evicted")
        remaining = self.entries()
        self.assertFalse(old & remaining, "entries unused for 30 days must be evicted")
        self.assertEqual(recent, remaining, "recent entries must stay")
        sizes = sum(path.stat().st_size for path in remaining)
        self.assertEqual(report["after"], {"entries": len(remaining), "bytes": sizes})
        self.assertGreater(report["before"]["entries"], report["after"]["entries"])
        self.assertEqual(report["counters_after"]["files_in_cache"], len(remaining))


if __name__ == "__main__":
    unittest.main()
