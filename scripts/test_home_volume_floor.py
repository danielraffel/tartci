#!/usr/bin/env python3
"""The home-volume admission floor: computed per host, refuses only new clones.

m5studio, 2026-10-04: the Tart store was on /Volumes/Store, so the lease disk
axis judged that volume while the boot Data volume filled to 99% (21 GiB free)
with coverage build dirs. ENOSPC killed a merge-group runner and every lane
supervisor, and VM leases kept being granted. These tests replay that, prove
the floor arithmetic (minimum, fill rate, cap), the fail-open read, and the
doctor findings that keep a wrong floor from looking like a full disk.
"""

from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_doctor as fd  # noqa: E402
import home_volume_floor as hvf  # noqa: E402
import leases  # noqa: E402

try:
    import tomllib  # noqa: F401  (macos_fleet_lanes needs it; /usr/bin/python3 3.9 lacks it)
    HAVE_TOMLLIB = True
except ModuleNotFoundError:
    HAVE_TOMLLIB = False

ROOT = Path(__file__).resolve().parents[1]
GIB = hvf.GIB
NOW = 1_790_000_000.0
Usage = collections.namedtuple("Usage", "total used free")


def usage(total_gib: float, free_gib: float):
    return lambda path: Usage(int(total_gib * GIB), 0, int(free_gib * GIB))


class FloorTests(unittest.TestCase):
    def test_minimum_rate_and_cap(self) -> None:
        self.assertEqual(hvf.floor_bytes(1000 * GIB, 0.0, 1.0), 30 * GIB)
        # 20 GiB/h of fill with a pass every 2 h needs 40 GiB.
        self.assertEqual(hvf.floor_bytes(1000 * GIB, 20 * GIB / 3600, 2.0), 40 * GIB)
        # A runaway rate is capped at 20% of the volume.
        self.assertEqual(hvf.floor_bytes(1000 * GIB, 10_000 * GIB / 3600, 1.0), 200 * GIB)
        # A small volume gets 20% of its size, even below the 30 GiB minimum.
        self.assertEqual(hvf.floor_bytes(100 * GIB, 0.0, 1.0), 20 * GIB)

    def test_fill_rate_needs_a_six_hour_window(self) -> None:
        recent = [(NOW - 3600, 500 * GIB)]
        self.assertIsNone(hvf.fill_rate(recent, NOW, 100 * GIB))
        window = [(NOW - 7 * 3600, 170 * GIB), (NOW - 3600, 500 * GIB)]
        # Measured from the newest sample at least 6 h old: 70 GiB over 7 h.
        self.assertAlmostEqual(hvf.fill_rate(window, NOW, 100 * GIB), 10 * GIB / 3600)
        self.assertEqual(hvf.fill_rate([(NOW - 7 * 3600, 50 * GIB)], NOW, 100 * GIB), 0.0)


class JudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name)

    def judge(self, free_gib: float, total_gib: float = 994, device: str = "home",
              now: float = NOW) -> dict:
        return hvf.judge("/Users/x", self.store, now, store_device="atelier",
                         usage=usage(total_gib, free_gib), device=lambda p: device)

    def test_m5studio_boot_volume_at_99_percent_is_below(self) -> None:
        result = self.judge(21)
        self.assertEqual((result["state"], result["floor_bytes"]), ("below", 30 * GIB))
        self.assertEqual(result["consecutive_denials"], 1)
        self.assertEqual(self.judge(21)["consecutive_denials"], 2)
        # Control: the same host with room clears the streak.
        healthy = self.judge(300)
        self.assertEqual((healthy["state"], healthy["consecutive_denials"]), ("ok", 0))

    def test_the_store_volume_itself_is_left_to_the_disk_axis(self) -> None:
        self.assertEqual(self.judge(1, device="atelier")["state"], "same_device")
        self.assertFalse((self.store / hvf.STATE_FILE).exists())

    def test_an_unreadable_volume_admits_and_remembers_since_when(self) -> None:
        def boom(path):
            raise OSError("Operation not permitted")
        first = hvf.judge("/Users/x", self.store, NOW, store_device="atelier",
                          usage=boom, device=lambda p: "home")
        later = hvf.judge("/Users/x", self.store, NOW + 600, store_device="atelier",
                          usage=boom, device=lambda p: "home")
        self.assertEqual((first["state"], later["unread_since"]), ("unread", NOW))

    def test_a_measured_fill_rate_raises_the_floor(self) -> None:
        self.judge(400, now=NOW - 7 * 3600)
        result = self.judge(260)  # lost 140 GiB in 7 h: 20 GiB/h
        self.assertEqual(result["floor_bytes"], 30 * GIB)  # 20 GiB/h x 1 h < 30 GiB
        result = hvf.judge("/Users/x", self.store, NOW, store_device="atelier",
                           usage=usage(994, 260), device=lambda p: "home",
                           hours_to_next_pass=3.0)
        self.assertEqual(result["floor_bytes"], 60 * GIB)


class AdmissionTests(unittest.TestCase):
    """leases.acquire refuses a NEW VM lease below the floor and admits otherwise."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "store"
        self.vms = self.dir / "vms"
        self.vms.mkdir()

    def acquire(self, free_gib: float, home_device: str = "home",
                mode: str = "refuse") -> tuple:
        args = leases.parse_args([
            "acquire", "--store-dir", str(self.store), "--id", "vm-1", "--cores", "4",
            "--capacity", "16", "--capacity-mem-mb", "0", "--priority", "gate",
            "--pid", str(os.getpid()), "--kind", "macos-vm", "--disk-path", str(self.vms),
            "--home-floor-path", "/Users/x", "--home-floor-mode", mode, "--json"])
        real_device = str(os.stat(self.vms).st_dev)
        device = real_device if home_device == "store" else home_device

        def fake_usage(path):
            if str(path) == "/Users/x":
                return Usage(994 * GIB, 0, int(free_gib * GIB))
            return Usage(4000 * GIB, 0, 2000 * GIB)

        with mock.patch.object(hvf, "volume_usage", side_effect=fake_usage), \
                mock.patch.object(hvf, "device_of", side_effect=lambda p: device):
            return leases.acquire(args)

    def test_m5studio_replay_refuses_the_new_clone(self) -> None:
        result, rc = self.acquire(21)
        self.assertEqual((result["ok"], result["reason"], rc),
                         (False, "home_volume_below_floor", 75))
        self.assertEqual(result["exceeded_axis"]["disk"], True)
        self.assertEqual(result["home_volume"]["free_bytes"], 21 * GIB)
        self.assertEqual(result["home_volume"]["floor_bytes"], 30 * GIB)

    def test_report_mode_never_refuses_and_says_what_refuse_would_do(self) -> None:
        # The shipped default: the m5studio replay is admitted, marked would_refuse.
        result, rc = self.acquire(21, mode="report")
        self.assertEqual((result["ok"], rc), (True, 0))
        self.assertEqual((result["home_volume"]["state"], result["home_volume"]["mode"],
                          result["home_volume"]["would_refuse"]), ("below", "report", True))
        # The streak still counts, so the day of report data is readable.
        self.assertEqual(hvf.status(self.store)["consecutive_denials"], 1)

    def test_the_cli_default_is_report(self) -> None:
        args = leases.parse_args(["acquire", "--id", "x", "--cores", "1"])
        self.assertEqual(args.home_floor_mode, "report")

    def test_a_home_volume_with_room_is_admitted(self) -> None:
        result, rc = self.acquire(300)
        self.assertEqual((result["ok"], rc, result["home_volume"]["state"]), (True, 0, "ok"))

    def test_the_store_volume_is_not_judged_twice(self) -> None:
        result, rc = self.acquire(1, home_device="store")
        self.assertEqual((result["ok"], result["home_volume"]["state"]), (True, "same_device"))


class DoctorTests(unittest.TestCase):
    def test_a_refusal_streak_as_long_as_the_lanes_is_a_problem(self) -> None:
        value = {"consecutive_denials": 2, "last": {"free_bytes": 21 * GIB,
                                                     "floor_bytes": 30 * GIB}}
        self.assertEqual(fd.check_home_volume(value, lanes=3, now=NOW).code,
                         "home_volume_floor_ok")
        refusing = fd.check_home_volume(dict(value, consecutive_denials=3), lanes=3, now=NOW)
        self.assertEqual((refusing.state, refusing.code), (fd.PROBLEM, "disk_floor_refusing"))

    def test_an_axis_unread_for_a_reclaim_cadence_is_a_problem(self) -> None:
        value = {"unread_since": NOW - 1800, "unread_reason": "EPERM"}
        self.assertNotEqual(fd.check_home_volume(value, lanes=2, now=NOW).code,
                            "disk_axis_unread")
        late = fd.check_home_volume(dict(value, unread_since=NOW - 4000), lanes=2, now=NOW)
        self.assertEqual((late.state, late.code), (fd.PROBLEM, "disk_axis_unread"))
        self.assertEqual(fd.check_home_volume({}, lanes=2, now=NOW).state, fd.NOT_APPLICABLE)
        for code in ("disk_axis_unread", "disk_floor_refusing", "home_volume_floor_ok",
                     "home_volume_floor_not_judged"):
            self.assertIn(code, fd.CODES)


class DenialEventTests(unittest.TestCase):
    """The supervisor's lease_denied event names the volume, free and floor."""

    def test_the_event_carries_volume_free_and_floor(self) -> None:
        denial = json.dumps({"ok": False, "reason": "home_volume_below_floor",
                             "exceeded_axis": {"cores": False, "memory": False, "disk": True},
                             "requested_cores": 4,
                             "home_volume": {"state": "below", "free_bytes": 21 * GIB,
                                             "floor_bytes": 30 * GIB}})
        script = (f'event(){{ printf "%s\\n" "$*"; }}; TARTCI_ROOT={ROOT}; '
                  f'. {ROOT}/providers/common/vm-lease.lib.sh; '
                  f"tartci_vm_lease_denied_event '{denial}' 75 macos-vm 4 '' gate")
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             timeout=30).stdout
        self.assertIn("axis=disk reason=home_volume_below_floor", out)
        self.assertIn(f"volume=home free={21 * GIB} floor={30 * GIB}", out)


@unittest.skipUnless(HAVE_TOMLLIB, "validates profiles through macos_fleet_lanes (tomllib)")
class ProfileModeTests(unittest.TestCase):
    def test_every_shipped_profile_ships_report_and_renders_it(self) -> None:
        import tomllib
        import macos_fleet_lanes as lanes
        for path in sorted((ROOT / "profiles").glob("*-macos-fleet.toml")):
            with self.subTest(profile=path.name):
                data = tomllib.loads(path.read_text())
                self.assertEqual(data["host"].get("home_volume_floor_mode"), "report")
                res = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                                      str(path)], cwd=ROOT, capture_output=True, text=True)
                self.assertEqual(res.returncode, 0, res.stderr)

    def test_an_unknown_mode_is_rejected(self) -> None:
        source = (ROOT / "profiles" / "m5-macos-fleet.toml").read_text()
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.toml"
            bad.write_text(source.replace('home_volume_floor_mode = "report"',
                                          'home_volume_floor_mode = "enforce"'))
            res = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                                  str(bad)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("home_volume_floor_mode", res.stderr)


class UnreadEventTests(unittest.TestCase):
    def run_lib(self, payload: dict) -> str:
        script = (f'event(){{ printf "%s\\n" "$*"; }}; TARTCI_ROOT={ROOT}; '
                  f'. {ROOT}/providers/common/vm-lease.lib.sh; '
                  f"tartci_vm_lease_home_unread_event '{json.dumps(payload)}'")
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              timeout=30).stdout

    def test_a_grant_that_skipped_the_axis_says_so(self) -> None:
        out = self.run_lib({"ok": True, "home_volume": {"state": "unread",
                                                        "reason": "OSError: EPERM"}})
        self.assertIn("disk_axis_unread volume=home reason=OSError:_EPERM", out)
        self.assertEqual(self.run_lib({"ok": True, "home_volume": {"state": "ok"}}), "")

    def test_a_report_mode_grant_below_the_floor_logs_would_refuse(self) -> None:
        out = self.run_lib({"ok": True, "home_volume": {
            "state": "below", "mode": "report", "would_refuse": True,
            "free_bytes": 21 * GIB, "floor_bytes": 30 * GIB}})
        self.assertIn(f"home_volume_would_refuse volume=home free={21 * GIB} "
                      f"floor={30 * GIB}", out)

    def test_an_unset_mode_env_passes_report(self) -> None:
        # The shell default must be report: a lane rendered before the profile
        # key existed has no TARTCI_HOME_VOLUME_FLOOR_MODE in its environment.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            (root / "scripts").mkdir(parents=True)
            argv_file = Path(td) / "argv.json"
            (root / "scripts" / "leases.py").write_text(
                "import json, sys\n"
                f"open({str(argv_file)!r}, 'w').write(json.dumps(sys.argv))\n"
                "print(json.dumps({'ok': True, 'disk': {'free_bytes': 1, 'reserved_bytes': 0, "
                "'requested_bytes': 0, 'required_bytes': 0, 'device_id': '1'}}))\n")
            vms = Path(td) / "vms"
            vms.mkdir()
            script = f"""
                unset TARTCI_HOME_VOLUME_FLOOR_MODE
                TARTCI_ROOT={ROOT}; . {ROOT}/providers/common/vm-lease.lib.sh
                TARTCI_ROOT={root}
                tartci_vm_leases_enabled(){{ return 0; }}
                tartci_vm_lease_granted_cores(){{ printf '%s' "$1"; }}
                tartci_vm_lease_derived_mem_mb(){{ printf 4096; }}
                tartci_observe_disk_admission(){{ :; }}
                tartci_start_vm_lease_heartbeat(){{ :; }}
                tartci_vm_lease_disk_expected_device_id(){{ :; }}
                tartci_vm_lease_disk_expected_mount_path(){{ :; }}
                tartci_acquire_vm_lease vm-1 4 macos-vm gate pulp 4096 {vms} tart-macos lane runner
            """
            subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                           env={k: v for k, v in os.environ.items()
                                if k != "TARTCI_HOME_VOLUME_FLOOR_MODE"})
            argv = json.loads(argv_file.read_text())
        self.assertEqual(argv[argv.index("--home-floor-mode") + 1], "report")

    def test_the_acquire_path_emits_it_on_a_grant(self) -> None:
        # End to end through tartci_acquire_vm_lease: a granted lease whose
        # home volume could not be read must reach the event log.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            (root / "scripts").mkdir(parents=True)
            (root / "scripts" / "leases.py").write_text(
                "import json\nprint(json.dumps({'ok': True, 'disk': {'free_bytes': 1, "
                "'reserved_bytes': 0, 'requested_bytes': 0, 'required_bytes': 0, "
                "'device_id': '1'}, 'home_volume': {'state': 'unread', "
                "'reason': 'OSError: EPERM'}}))\n")
            vms = Path(td) / "vms"
            vms.mkdir()
            script = f"""
                event(){{ printf 'EVENT %s\\n' "$*"; }}
                TARTCI_ROOT={ROOT}; . {ROOT}/providers/common/vm-lease.lib.sh
                TARTCI_ROOT={root}
                tartci_vm_leases_enabled(){{ return 0; }}
                tartci_vm_lease_granted_cores(){{ printf '%s' "$1"; }}
                tartci_vm_lease_derived_mem_mb(){{ printf 4096; }}
                tartci_observe_disk_admission(){{ :; }}
                tartci_start_vm_lease_heartbeat(){{ :; }}
                tartci_vm_lease_disk_expected_device_id(){{ :; }}
                tartci_vm_lease_disk_expected_mount_path(){{ :; }}
                tartci_acquire_vm_lease vm-1 4 macos-vm gate pulp 4096 {vms} tart-macos lane runner
            """
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                                 timeout=60).stdout
        self.assertIn("EVENT disk_axis_unread volume=home reason=OSError:_EPERM", out)


if __name__ == "__main__":
    unittest.main()
