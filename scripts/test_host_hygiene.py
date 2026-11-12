#!/usr/bin/env python3
"""Disk floor and foreign-VM reports (scripts/host_hygiene.py)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_hygiene as hy  # noqa: E402
import tartci_incident  # noqa: E402
import vm_boot_alert  # noqa: E402

NOW = 1792120000.0
GB = hy.GB


def vol(path: str, free_gb: float, total_gb: float) -> dict:
    return {"paths": [path], "free": free_gb * GB, "total": total_gb * GB,
            "free_pct": 100.0 * free_gb / total_gb}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.user = self.root / "user"
        self.store = self.root / "VMs"
        (self.store / "vms").mkdir(parents=True)
        (self.user / ".tart" / "vms").mkdir(parents=True)
        (self.user / ".config" / "tartci").mkdir(parents=True)
        (self.user / "Library" / "LaunchAgents").mkdir(parents=True)
        self.enable = self.user / ".config" / "tartci" / "host-hygiene.enabled"
        self.enable.write_text("")
        self.profile = self.user / ".config" / "tartci" / "macos-fleet-profile.toml"
        self.profile.write_text(
            f'[host]\nid = "m1"\ntart_home = "{self.store}"\n'
            '[[lane]]\nid = "pulp-gate"\ngolden = "pulp-build-runner:latest"\n')
        env = {"HOME": str(self.user), "TARTCI_HOME": str(self.user / ".tartci"),
               "TART_HOME": str(self.store),
               "TARTCI_MACOS_FLEET_PROFILE": str(self.profile)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("TARTCI_LAUNCH_AGENTS_DIR", "TARTCI_HOST_HYGIENE_ENABLE_FILE"):
            os.environ.pop(key, None)
        host = mock.patch.object(vm_boot_alert, "_alert_host", return_value=("m1", "m1"))
        host.start()
        self.addCleanup(host.stop)
        self.opened: list = []
        self.closed: list = []
        self.measured: list = []
        self.listings: dict = {self.store: [], self.user / ".tart": []}

    def vm(self, store: Path, name: str, state: str = "stopped", age_days: float = 0,
           size: int = 20) -> None:
        self.listings.setdefault(store, []).append(
            {"Name": name, "State": state, "Size": size, "Source": "local",
             "Running": state == "running",
             "Accessed": hy._iso(NOW - age_days * 86400)})
        bundle = store / "vms" / name
        bundle.mkdir(parents=True, exist_ok=True)
        os.utime(bundle, (NOW - age_days * 86400, NOW - age_days * 86400))

    def run_pass(self, now: float = NOW, free_gb: float = 900, ps: str = "") -> list[str]:
        def measure(path: Path, timeout: float = 0):
            self.measured.append(path)
            return {"agent-scratch": 432, "agent-worktrees": 138}.get(path.name, 1) * GB

        return hy.alert_pass(
            now, user_home=self.user,
            vols=lambda paths: [vol(str(paths[0]), free_gb, 2000)],
            measure=measure, listing=lambda store: self.listings.get(store, []),
            ps=lambda: ps, lsof=lambda: [],
            issue=lambda t, b: self.opened.append((t, b)) or (0, str(70 + len(self.opened))),
            close=lambda n: self.closed.append(n) or (0, "closed"))


class DiskFloor(Base):
    def test_below_twenty_percent_opens_one_issue_with_consumers_and_reclaim(self) -> None:
        lines = self.run_pass(free_gb=150)
        self.assertTrue(any("WARN host-disk" in line and "150 GB free (8%)" in line
                            for line in lines), lines)
        self.run_pass(NOW + 300, free_gb=150)
        disk = [(t, b) for t, b in self.opened if "disk floor" in t]
        self.assertEqual(len(disk), 1)
        title, body = disk[0]
        self.assertIn("[tartci] m1 is below its disk floor: 150 GB free (8%)", title)
        self.assertLess(body.index("agent-scratch`: 432 GB"), body.index("agent-worktrees`: 138 GB"))
        self.assertIn("never rm -rf", body)
        self.assertIn("tartci reclaim", body)
        self.assertEqual(tartci_incident.parse(body)["class"], "host_disk_floor")
        # The sizes are measured once and reused inside their TTL.
        self.assertEqual(len([p for p in self.measured if p.name == "agent-scratch"]), 1)

    def test_under_two_hundred_gb_breaches_even_above_twenty_percent(self) -> None:
        self.assertEqual(len(hy.breaches([vol("/x", 190, 500)])), 1)
        self.assertEqual(hy.breaches([vol("/x", 400, 1000)]), [])   # control: 40%, 400 GB
        self.assertEqual(len(hy.breaches([vol("/x", 300, 2000)])), 1)  # 300 GB but 15%

    def test_recovery_closes_the_issue_and_nothing_is_deleted(self) -> None:
        scratch = self.user / "agent-scratch" / "old-run"
        scratch.mkdir(parents=True)
        self.run_pass(free_gb=150)
        self.assertEqual([x for x in self.run_pass(NOW + 300, free_gb=900) if "host-disk" in x], [])
        self.assertEqual(self.closed, ["71"])
        self.assertTrue(scratch.exists())

    def test_measuring_stops_at_one_overall_deadline(self) -> None:
        clock = [0.0]
        timeouts = []

        def slow(path: Path, timeout: float = 0):
            timeouts.append(timeout)
            clock[0] += 200
            return 1

        rows = [(Path(f"/p{i}"), "x") for i in range(4)]
        out = hy.consumers(rows, slow, self.root / "c.json", NOW, deadline_seconds=300,
                           clock=lambda: clock[0])
        self.assertEqual(len(out), 2)
        self.assertEqual(timeouts, [hy.DU_TIMEOUT_SECONDS, 100.0])
        cached = hy._read_json(self.root / "c.json")
        self.assertEqual(cached["unmeasured"], ["/p2", "/p3"])

    def test_du_runs_as_a_background_task(self) -> None:
        seen = []
        (self.root / "d").mkdir()
        with mock.patch.object(hy.os.path, "exists", side_effect=lambda p: True), \
                mock.patch.object(hy.subprocess, "run",
                                  side_effect=lambda argv, **kw: seen.append(argv) or
                                  mock.Mock(stdout="8\t/d\n")):
            self.assertEqual(hy.du_bytes(self.root / "d", timeout=5), 8 * 1024)
        self.assertEqual(seen[0][:3], ["/usr/sbin/taskpolicy", "-b", "du"])

    def test_volumes_are_counted_once_per_device(self) -> None:
        stat = mock.Mock(return_value=mock.Mock(f_blocks=100, f_frsize=GB, f_bavail=10))
        rows = hy.volumes([Path("/a"), Path("/b")], stat=stat, device=lambda p: 7)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["paths"], ["/a", "/b"])


NEEDS_TOMLLIB = unittest.skipIf(sys.version_info < (3, 11),
                                "ownership reads the fleet profile (tomllib, 3.11+)")


@NEEDS_TOMLLIB
class ForeignVms(Base):
    def foreign_line(self, lines: list[str]) -> str:
        return next((x for x in lines if "foreign-vms" in x), "")

    def test_real_fleet_images_are_tartci_s_and_only_prunable_ones_are_reported(self) -> None:
        # The names infra-review2 saw on m1, m5 and m3 (2026-10-16): goldens,
        # every :latest bake tier and the newest rollback are tartci's.
        for name in ("pulp-build-runner:latest", "pulp-linux-build:latest",
                     "macos-build-base:latest", "macos-apple-xcode:latest",
                     "pulp-build-base:latest"):
            self.vm(self.store, name, age_days=90)
        self.vm(self.store, "pulp-build-runner:m1-rollback-20260826-pre-m3", age_days=50)
        self.vm(self.store, "pulp-build-runner:m1-rollback-20260701", age_days=100)
        lines = self.run_pass()
        line = self.foreign_line(lines)
        self.assertIn("pulp-build-runner:m1-rollback-20260701 stopped", line)
        for kept in ("pulp-build-runner:latest", "pulp-linux-build:latest",
                     "macos-build-base:latest", "macos-apple-xcode:latest",
                     "pulp-build-base:latest", "pulp-build-runner:m1-rollback-20260826-pre-m3"):
            self.assertNotIn(kept, line)
        (title, body), = [(t, b) for t, b in self.opened if "nothing tartci uses" in t]
        self.assertIn("tart delete pulp-build-runner:m1-rollback-20260701", body)
        self.assertNotIn("tart delete pulp-linux-build:latest", body)
        self.assertEqual(tartci_incident.parse(body)["class"], "host_foreign_vms")

    def test_manual_vms_are_reported_by_age_and_never_touched(self) -> None:
        self.vm(self.user / ".tart", "vellum-pr19-linux-20260811", age_days=60, size=6)
        self.vm(self.store, "lv2repro-pulpimpl", state="running", size=20)
        self.vm(self.store, "fresh-manual", age_days=3)                 # under 14 d
        self.vm(self.store, "m1-pulp-gate-01-23177-1", state="running")  # a lane clone
        ps = ("11:02:03 /opt/homebrew/bin/tart run --no-graphics lv2repro-pulpimpl\n"
              "12:00:00 /opt/homebrew/bin/tart run --no-graphics m1-pulp-gate-01-23177-1\n")
        line = self.foreign_line(self.run_pass(ps=ps))
        self.assertIn("lv2repro-pulpimpl running 11.0 h", line)
        self.assertIn("vellum-pr19-linux-20260811 stopped 60 d", line)
        self.assertNotIn("fresh-manual", line)
        self.assertNotIn("m1-pulp-gate", line)
        (title, body), = [(t, b) for t, b in self.opened if "nothing tartci uses" in t]
        self.assertEqual(title, "[tartci] m1 has 2 VM(s) nothing tartci uses")
        self.assertIn("tart stop lv2repro-pulpimpl", body)
        self.assertTrue((self.user / ".tart" / "vms" / "vellum-pr19-linux-20260811").exists())

    def test_a_recorded_scratch_vm_is_tartci_s_until_its_ttl(self) -> None:
        # Its record under ~/.tartci/state/scratch-vms names it, and the
        # ownership text reads that state, so it is never reported foreign;
        # the scratch-vm pass deletes it at its TTL.
        import scratch_vm_ttl  # noqa: PLC0415
        self.vm(self.store, "scratch-pulp-impl-repro", state="running")
        self.vm(self.store, "scratch-unrecorded", state="running")
        scratch_vm_ttl.write({"name": "scratch-pulp-impl-repro", "owner": "pulp-impl",
                              "store": str(self.store), "expires_at": NOW + 3600,
                              "warned_at": None})
        ps = ("05:00:00 /opt/homebrew/bin/tart run scratch-pulp-impl-repro\n"
              "05:00:00 /opt/homebrew/bin/tart run scratch-unrecorded\n")
        line = self.foreign_line(self.run_pass(ps=ps))
        self.assertNotIn("scratch-pulp-impl-repro", line)
        # Control: the same shape without a record is reported.
        self.assertIn("scratch-unrecorded running 5.0 h", line)

    def test_a_short_foreign_run_is_not_reported(self) -> None:
        self.vm(self.store, "lv2repro-pulpimpl", state="running")
        ps = "03:59:00 /opt/homebrew/bin/tart run lv2repro-pulpimpl\n"
        self.assertEqual(self.foreign_line(self.run_pass(ps=ps)), "")

    def test_a_vm_a_tartci_agent_names_is_tartci_s(self) -> None:
        self.vm(self.store, "linux-lane-golden", age_days=60)
        plist = self.user / "Library" / "LaunchAgents" / "com.danielraffel.pulp.tart-runner-linux.plist"
        plist.write_text("<plist><dict><key>TARTCI_GOLDEN</key>"
                         "<string>linux-lane-golden</string></dict></plist>")
        self.assertEqual(self.foreign_line(self.run_pass()), "")
        # Control: without the agent naming it, it is reported.
        plist.unlink()
        os.unlink(self.user / ".tartci" / "state" / "host-hygiene" / "foreign-vms.json")
        self.assertIn("linux-lane-golden", self.foreign_line(self.run_pass()))

    def test_an_unreadable_profile_judges_nothing_foreign(self) -> None:
        self.vm(self.store, "pulp-build-runner:m1-rollback-20260701", age_days=100)
        self.profile.write_text("not = [toml")
        lines = self.run_pass()
        self.assertNotIn("rollback", self.foreign_line(lines))
        self.assertIn("fleet profile unreadable", "".join(lines))

    def test_it_scans_hourly_and_reports_a_store_it_cannot_list(self) -> None:
        self.listings[self.store] = None
        lines = self.run_pass()
        self.assertIn(f"could not list {self.store}", "".join(lines))
        self.listings[self.store] = []
        self.assertIn("could not list", "".join(self.run_pass(NOW + 600)))   # not rescanned
        self.assertNotIn("could not list", "".join(self.run_pass(NOW + 3600)))


class NoTomllib(Base):
    def test_without_tomllib_nothing_is_judged_foreign(self) -> None:
        # The 3.9 fallback: ownership needs the fleet profile (tomllib). Without
        # it no VM is reported, and the log says why.
        import tart_image_prune  # noqa: PLC0415
        self.vm(self.store, "pulp-build-runner:m1-rollback-20260701", age_days=100)
        self.vm(self.store, "lv2repro-pulpimpl", age_days=60)
        with mock.patch.object(tart_image_prune, "profile_strings",
                               side_effect=RuntimeError("no tomllib (needs Python 3.11+)")):
            lines = self.run_pass()
        self.assertNotIn("WARN foreign-vms: pulp", "".join(lines))
        self.assertNotIn("lv2repro", "".join(lines))
        self.assertIn("fleet profile unreadable", "".join(lines))
        self.assertEqual([t for t, _ in self.opened if "nothing tartci uses" in t], [])


class OptIn(Base):
    def test_off_without_the_enable_file(self) -> None:
        self.enable.unlink()
        self.vm(self.store, "old-thing", age_days=60)
        self.assertEqual(self.run_pass(free_gb=10), [])
        self.assertEqual((self.opened, self.measured), ([], []))


class WatchdogPass(Base):
    def test_the_watchdog_pass_returns_the_lines_host_hygiene_produced(self) -> None:
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        line = watchdog.host_hygiene_pass(
            NOW, user_home=self.user,
            vols=lambda paths: [vol(str(paths[0]), 150, 2000)],
            measure=lambda path, timeout=0: GB, listing=lambda store: [],
            ps=lambda: "", lsof=lambda: [],
            issue=lambda t, b: (0, "71"), close=lambda n: (0, "closed"))
        self.assertIn("WARN host-disk", line)
        # Control: with the floor holding, the pass reports no disk line.
        os.unlink(self.user / ".tartci" / "state" / "host-hygiene" / "foreign-vms.json")
        self.assertNotIn("host-disk", watchdog.host_hygiene_pass(
            NOW + 3600, user_home=self.user,
            vols=lambda paths: [vol(str(paths[0]), 900, 2000)],
            measure=lambda path, timeout=0: GB, listing=lambda store: [],
            ps=lambda: "", lsof=lambda: [],
            issue=lambda t, b: (0, "71"), close=lambda n: (0, "closed")) or "")


class Wiring(unittest.TestCase):
    def test_the_heal_pass_runs_it(self) -> None:
        source = (Path(hy.__file__).parent / "tartci_launchd_watchdog.py").read_text()
        self.assertIn("hygiene_line = host_hygiene_pass()", source)


if __name__ == "__main__":
    unittest.main()
