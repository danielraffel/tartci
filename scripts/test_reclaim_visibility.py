#!/usr/bin/env python3
"""A dead or shadowed reclaim agent is visible, not silent.

Both faults are exercised with their healthy control in the same test, because
a check that always reports "fine" passes every test that only feeds it a
healthy host.
"""

from __future__ import annotations

import testing_support  # noqa: E402
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fleet_doctor as fd  # noqa: E402
import reclaim_status as rs  # noqa: E402

HOME = pathlib.Path("/Users/someone")


def launchctl(jobs: dict[str, str | None], list_rc: int = 0):
    """A launchctl double: `jobs` maps label -> plist path launchd holds."""
    def run(argv: list[str]) -> tuple[int, str, str]:
        if argv[1:] == ["list"]:
            if list_rc:
                return list_rc, "", "boom"
            rows = ["PID\tStatus\tLabel", "-\t0\tcom.apple.Finder"]
            rows += [f"-\t0\t{label}" for label in jobs]
            return 0, "\n".join(rows) + "\n", ""
        label = argv[2].rsplit("/", 1)[1]
        path = jobs.get(label)
        body = f"{label} = {{\n\tactive count = 0\n" + (f"\tpath = {path}\n" if path else "") + "}\n"
        return 0, body, ""
    return run


class LeakedRegistration(unittest.TestCase):
    real = str(HOME / "Library/LaunchAgents/com.danielraffel.tartci.reclaim.plist")

    def test_a_label_loaded_from_a_temp_home_is_a_problem_and_named(self):
        leaked = "/private/var/folders/x/T/tmpabc/home/Library/LaunchAgents/com.danielraffel.tartci.reclaim.plist"
        rows, _ = fd.launchd_registrations(launchctl({
            "com.danielraffel.tartci.reclaim": leaked,
            "com.danielraffel.tartci.reap": str(HOME / "Library/LaunchAgents/com.danielraffel.tartci.reap.plist"),
        }))
        finding = fd.check_launchd_registrations(rows, home=HOME)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "launchd_registration_leaked"))
        self.assertIn(leaked, finding.detail)
        self.assertEqual([r["label"] for r in finding.facts["leaked"]],
                         ["com.danielraffel.tartci.reclaim"])

    def test_the_same_labels_from_the_real_home_are_ok(self):
        rows, _ = fd.launchd_registrations(launchctl({
            "com.danielraffel.tartci.reclaim": self.real,
            "com.danielraffel.pulp.tart-runner": str(HOME / "Library/LaunchAgents/com.danielraffel.pulp.tart-runner.plist"),
        }))
        finding = fd.check_launchd_registrations(rows, home=HOME)
        self.assertEqual((finding.state, finding.facts["loaded"]), (fd.OK, 2))

    def test_a_nested_or_pathless_registration_is_leaked_too(self):
        rows, _ = fd.launchd_registrations(launchctl({
            "com.danielraffel.tartci.reclaim": str(HOME / "Library/LaunchAgents/sub/x.plist"),
            "com.danielraffel.tartci.reap": None,
        }))
        finding = fd.check_launchd_registrations(rows, home=HOME)
        self.assertEqual(len(finding.facts["leaked"]), 2)

    def test_non_tartci_labels_are_not_judged(self):
        rows, _ = fd.launchd_registrations(launchctl({"homebrew.mxcl.postgresql": "/tmp/x.plist"}))
        self.assertEqual(rows, [])

    def test_unreadable_launchd_is_unknown_not_clean(self):
        rows, error = fd.launchd_registrations(launchctl({}, list_rc=1))
        finding = fd.check_launchd_registrations(rows, home=HOME, error=error)
        self.assertEqual(finding.state, fd.UNKNOWN)

    def test_every_new_code_has_a_reason_row(self):
        reasons = json.loads((HERE / "fleet_reasons.json").read_text())["reasons"]
        for code in ("launchd_registration_leaked", "launchd_registrations_ok",
                     "launchd_registrations_unreadable", "reclaim_ok", "reclaim_failed",
                     "reclaim_stale", "reclaim_never_recorded", "reclaim_unreadable"):
            self.assertIn(code, fd.CODES)
            self.assertIn(code, reasons)


class LastPass(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, **fields):
        receipt = {"finished_ts": time.time(), "exit_code": 0, "reclaimed_bytes": 5 * rs.GIB,
                   "pulp_reapers": {"enabled": True, "reclaimed_bytes": 4 * rs.GIB}}
        receipt.update(fields)
        (self.dir / "last-run.json").write_text(json.dumps(receipt))

    def test_recent_ok_pass_is_ok(self):
        self.write()
        value = rs.status(self.dir, log_path=self.dir / "none.log")
        self.assertEqual(value["state"], "ok")
        self.assertEqual(fd.check_reclaim(value).state, fd.OK)
        self.assertIn("pulp reapers 4.0 GiB", rs.describe(value))

    def test_a_receipt_older_than_two_missed_passes_is_stale_and_a_problem(self):
        # The m3 shape: the agent exited 127 hourly for a day and wrote nothing.
        self.write(finished_ts=time.time() - 26 * 3600)
        value = rs.status(self.dir, log_path=self.dir / "none.log")
        self.assertEqual(value["state"], "stale")
        finding = fd.check_reclaim(value)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "reclaim_stale"))
        self.assertIn("STALE", finding.detail)

    def test_stale_wins_over_an_old_success(self):
        self.write(finished_ts=time.time() - rs.STALE_AFTER_S - 60, exit_code=0)
        self.assertEqual(rs.status(self.dir)["state"], "stale")

    def test_recent_failed_pass_is_a_problem(self):
        self.write(exit_code=2)
        finding = fd.check_reclaim(rs.status(self.dir))
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "reclaim_failed"))

    def test_exit_3_names_the_full_volume_instead_of_a_failed_pass(self):
        # m3, 2026-09-29: "LAST PASS FAILED; exit 3" while the pass had run
        # and the boot data volume sat at 55 GiB under a 60 GiB floor.
        self.write(exit_code=3, free_bytes_after=55 * rs.GIB, fail_below_gb=60,
                   tightest_root="/Users/x/Code")
        value = rs.status(self.dir)
        self.assertEqual(value["state"], "low_space")
        finding = fd.check_reclaim(value)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "reclaim_low_space"))
        self.assertIn("FREE SPACE STILL LOW after the pass: 55.0 GiB on /Users/x/Code < 60 GiB floor",
                      finding.detail)
        self.assertNotIn("LAST PASS FAILED", finding.detail)

    def test_exit_5_names_the_low_boot_volume(self):
        # m3, 2026-10-01: the Tart store on Workshop was healthy, the boot data
        # volume was at 2 GiB, and nothing said so.
        boot = {"path": "/System/Volumes/Data", "free_bytes_after": 2 * rs.GIB,
                "floor_gb": 30, "judged_by": "own_floor", "below_floor": True}
        self.write(exit_code=5, boot_volume=boot,
                   scratch_dirs={"enabled": True, "removed_bytes": 3 * rs.GIB})
        value = rs.status(self.dir)
        self.assertEqual(value["state"], "boot_low")
        self.assertEqual(value["scratch_removed_bytes"], 3 * rs.GIB)
        finding = fd.check_reclaim(value)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "reclaim_boot_low"))
        self.assertIn("BOOT VOLUME LOW after the pass: 2.0 GiB on /System/Volumes/Data "
                      "< 30 GiB floor", finding.detail)
        self.assertNotIn("LAST PASS FAILED", finding.detail)

    def test_reapers_that_never_ran_are_not_ok(self):
        # m5studio, 2026-09-30: the profile's worktrees_root did not exist, the
        # pass exited 0, and pool status printed "reclaim: ok" with the reason
        # in a parenthesis.
        root = "/Users/x/Code/agent-worktrees"
        self.write(pulp_reapers={"enabled": True, "reclaimed_bytes": 0,
                                 "error": f"worktrees_root {root} is not a directory",
                                 "host_vitals_sensor": {"state": "current"}})
        value = rs.status(self.dir, log_path=self.dir / "none.log")
        line = rs.describe(value)
        self.assertTrue(line.startswith("reclaim: WARN pulp reapers: NOT RUNNING "
                                        "(worktrees_root missing"), line)
        finding = fd.check_reclaim(value)
        self.assertEqual((finding.state, finding.code), (fd.PROBLEM, "reclaim_pass_degraded"))

    def test_a_failed_sensor_reinstall_is_not_ok(self):
        self.write(pulp_reapers={"enabled": True, "reclaimed_bytes": 0,
                                 "host_vitals_sensor": {"state": "refresh_failed",
                                                        "detail": "installer exit 3: denied"}})
        value = rs.status(self.dir, log_path=self.dir / "none.log")
        self.assertIn("host-vitals sensor: REFRESH FAILED (installer exit 3: denied)",
                      rs.describe(value))
        self.assertEqual(fd.check_reclaim(value).code, "reclaim_pass_degraded")

    def test_reapers_off_by_profile_and_a_current_sensor_stay_ok(self):
        # The controls: reapers not enabled is a choice, not a fault, and a
        # sensor that is current or was refreshed is not a warning.
        for pulp in ({"enabled": False, "reason": "[pulp_reapers] not enabled"},
                     {"enabled": True, "reclaimed_bytes": 0,
                      "host_vitals_sensor": {"state": "refreshed"}}):
            self.write(pulp_reapers=pulp)
            value = rs.status(self.dir, log_path=self.dir / "none.log")
            self.assertTrue(rs.describe(value).startswith("reclaim: ok;"), rs.describe(value))
            self.assertEqual(fd.check_reclaim(value).code, "reclaim_ok")

    def test_no_receipt_is_unknown_and_garbage_is_unreadable(self):
        self.assertEqual(fd.check_reclaim(rs.status(self.dir)).code, "reclaim_never_recorded")
        (self.dir / "last-run.json").write_text("{nope")
        self.assertEqual(fd.check_reclaim(rs.status(self.dir)).code, "reclaim_unreadable")

    def test_pool_status_carries_the_reclaim_state(self):
        # `tartci pool status` must print it in both forms; a grep is the whole
        # assertion because the shell composes the JSON by hand.
        body = (HERE.parent / "tartci").read_text()
        start = body.index("cmd_pool()")
        section = body[start:body.index("\n}\n", start)]
        self.assertIn('"reclaim":%s', section)
        self.assertIn('scripts/reclaim_status.py" --json', section)
        self.assertIn('scripts/reclaim_status.py" 2>/dev/null', section)

    def test_cli_reads_the_receipt(self):
        self.write(exit_code=4)
        out = subprocess.run([sys.executable, str(HERE / "reclaim_status.py"), "--json",
                              "--state-dir", str(self.dir)], capture_output=True, text=True,
                             check=True, env={**os.environ})
        self.assertEqual(json.loads(out.stdout)["state"], "failed")


class DiskPressure(unittest.TestCase):
    """The VM store volume's fill level is a readiness fact (pool status)."""

    def run_with(self, used_percent: float, home_percent: float = 50.0):
        import shutil
        from unittest import mock
        import macos_fleet_lanes as fleet
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        store, home = tmp / "vms", tmp / "home"
        store.mkdir()
        home.mkdir()
        config = tmp / "profile.toml"
        config.write_text(f'[host]\ntart_home = "{store}"\n')
        total = 1000 * rs.GIB

        def usage(path):
            percent = used_percent if pathlib.Path(path) == store else home_percent
            free = int(total * (100 - percent) / 100)
            return shutil._ntuple_diskusage(total, total - free, free)
        # Distinct devices, as on m3 (Workshop and the boot volume).
        real_stat = pathlib.Path.stat

        def fake_stat(self, *args, **kwargs):
            value = real_stat(self, *args, **kwargs)
            if self == store:
                return os.stat_result((value.st_mode, value.st_ino, 4242, *tuple(value)[3:]))
            return value
        with mock.patch.object(fleet.shutil, "disk_usage", side_effect=usage), \
                mock.patch.object(pathlib.Path, "stat", fake_stat):
            return fleet.disk_pressure(config, home)

    @testing_support.requires_tomllib
    def test_thresholds(self):
        rows = {row["role"]: row for row in self.run_with(84.0)}
        self.assertEqual(rows["vm_store"]["state"], "ok")
        self.assertEqual({row["role"]: row["state"] for row in self.run_with(86.0)}["vm_store"],
                         "warn")
        self.assertEqual({row["role"]: row["state"] for row in self.run_with(93.0)}["vm_store"],
                         "problem")

    @testing_support.requires_tomllib
    def test_a_full_home_volume_warns_but_is_not_a_readiness_problem(self):
        rows = {row["role"]: row for row in self.run_with(50.0, home_percent=94.0)}
        self.assertEqual(rows["home"]["state"], "warn")
        self.assertEqual(rows["vm_store"]["state"], "ok")

    @testing_support.requires_tomllib
    def test_a_home_volume_full_enough_to_break_builds_is_a_problem(self):
        # m3, 2026-10-01: its internal data volume at 99% failed a Shipyard
        # release build with ENOSPC while pool status only warned.
        rows = {row["role"]: row for row in self.run_with(50.0, home_percent=99.0)}
        self.assertEqual(rows["home"]["state"], "problem")
        self.assertEqual({r["role"]: r for r in self.run_with(50.0, home_percent=96.0)}
                         ["home"]["state"], "warn")

    @testing_support.requires_tomllib
    def test_readiness_turns_a_full_store_into_a_problem_and_pool_status_prints_it(self):
        import macos_fleet_lanes as fleet
        source = (HERE / "macos_fleet_lanes.py").read_text()
        body = source[source.index("def fleet_readiness("):]
        self.assertIn('if disk["state"] == "problem":', body[:body.index("\ndef ")])
        self.assertIn('"code": "disk_pressure"', body[:body.index("\ndef ")])
        self.assertIn('for disk in fleet.get("disk") or []:', (HERE.parent / "tartci").read_text())
        self.assertEqual((fleet.DISK_WARN_PERCENT, fleet.DISK_PROBLEM_PERCENT,
                          fleet.HOME_DISK_PROBLEM_PERCENT), (85, 92, 97))


if __name__ == "__main__":
    unittest.main()
