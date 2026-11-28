#!/usr/bin/env python3
"""The disk watchdog's free-space check against the fleet manifest's floors.

Free space is injected (`free_of`) and the issue opener is a fake, so each test
shapes a volume's free GiB pass by pass. Floors come from a temporary manifest,
as in production; nothing here is a default.

Python 3.9-safe: the classes that parse a manifest skip without tomllib, and
the no-tomllib behaviour is tested on every interpreter.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disk_watchdog as dw  # noqa: E402

try:
    import tomllib  # noqa: F401
except ModuleNotFoundError:
    tomllib = None  # type: ignore[assignment]

MANIFEST = """
[hosts.box]
[[hosts.box.keys]]
name = "data_free_gb"
kind = "disk_floor_gb"
path = "/Volumes/Data"
value = 200
[[hosts.box.keys]]
name = "root_free_gb"
kind = "disk_floor_gb"
path = "/"
value = 40
[[hosts.box.keys]]
name = "role"
kind = "file"
path = "/etc/role"
value = "x"
[hosts.other]
[[hosts.other.keys]]
name = "root_free_gb"
kind = "disk_floor_gb"
path = "/"
value = 999
"""
PROFILE_FLOORS = ("/Volumes/Data", "/")  # one probe per floored volume
PROFILE = {"host": {"id": "box", "tart_home": "/Volumes/Data/VMs", "cache_root": "/cache"},
           "reclaim": {"worktrees_root": "/Volumes/Data/trees"}}


class Fakes:
    def __init__(self, test: unittest.TestCase, manifest: bool = True) -> None:
        tmp = tempfile.TemporaryDirectory()
        test.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.state = self.dir / "state"
        self.manifest = self.dir / "manifest.toml"
        if manifest:
            self.manifest.write_text(MANIFEST)
        patch = mock.patch.dict(os.environ, {"TARTCI_FLEET_MANIFEST": str(self.manifest)})
        patch.start()
        test.addCleanup(patch.stop)
        self.free = {"/Volumes/Data": 500.0, "/": 100.0}
        self.opened: list = []
        self.closed: list = []
        self.du_calls: list = []
        self.du_outcome = "ok"
        self.now = 1_700_000_000.0

    def issue(self, title: str, body: str):
        self.opened.append((title, body))
        return 0, str(100 + len(self.opened))

    def close(self, number: str):
        self.closed.append(number)
        return 0, ""

    def run(self, command, timeout, out):
        self.du_calls.append((list(command), timeout))
        root = command[-1]
        text = (f"900\t{root}/small\n{3 * 1024 ** 2}\t{root}/big\n"
                f"{2 * 1024 ** 2}\t{root}/mid\n{4 * 1024 ** 2 + 900}\t{root}\n")
        out.write_text(text)
        return self.du_outcome, text

    def pass_(self, **free: float) -> dict:
        self.free.update(free)
        self.now += 60
        return dw.watch_free(self.state, self.now, issue=self.issue, close=self.close,
                             run=self.run, host="box", target="ssh-box",
                             free_of=lambda path: self.free.get(path), profile=PROFILE)

    def data(self, gib: float) -> dict:
        self.free["/Volumes/Data"] = gib
        return self.pass_()

    def events(self) -> list:
        path = self.state / "events.jsonl"
        return [json.loads(line)["event"] for line in path.read_text().splitlines()] \
            if path.exists() else []


@unittest.skipIf(tomllib is None, "reads the manifest with tomllib")
class FreeFloorTests(unittest.TestCase):
    def test_below_the_floor_opens_one_issue_naming_the_floor_and_top_dirs(self) -> None:
        f = Fakes(self)
        self.assertEqual(f.data(500)["volumes"][0]["status"], "ok")
        self.assertEqual(f.opened, [])
        row = f.data(96)["volumes"][0]
        self.assertEqual(row["status"], "low")
        self.assertEqual(row["path"], "/Volumes/Data")
        self.assertEqual(len(f.opened), 1)
        title, body = f.opened[0]
        self.assertIn("/Volumes/Data", title)
        self.assertIn("96 GiB", title)
        self.assertIn("200 GiB floor", title)
        self.assertIn("96.0 GiB free", body)
        self.assertIn("floor (data_free_gb) is 200 GiB", body)
        self.assertIn("3.0 GiB  /Volumes/Data/VMs/big", body)
        self.assertIn("Nothing is deleted", body)
        self.assertEqual(f.events(), ["disk_free_low"])
        # The root floor, a different volume, stayed quiet; the manifest's
        # other hosts and non-floor keys were ignored.
        self.assertEqual(len(f.opened), 1)

    def test_still_low_is_deduped_to_one_issue_and_one_du_per_root(self) -> None:
        f = Fakes(self)
        for gib in (96, 90, 120):
            f.data(gib)
        self.assertEqual(len(f.opened), 1)
        self.assertEqual(f.events(), ["disk_free_low"])
        roots = [call[0][-1] for call in f.du_calls]
        self.assertEqual(sorted(roots), ["/Volumes/Data/VMs", "/Volumes/Data/trees"])

    def test_top_five_are_bounded_depth_one_and_only_this_volumes_roots(self) -> None:
        f = Fakes(self)
        f.data(10)
        for command, timeout in f.du_calls:
            self.assertEqual(command[:4], ["/usr/bin/du", "-k", "-d", "1"])
            self.assertLessEqual(timeout, dw.DU_TIMEOUT_S)
        # /cache is on the root volume, not this one.
        self.assertNotIn("/cache", [c[0][-1] for c in f.du_calls])
        top = dw.top_dirs(["/r"], f.state, run=lambda c, t, o: (
            "ok", "".join(f"{(n + 1) * 1024}\t/r/d{n}\n" for n in range(8)) + "99999\t/r\n"))
        self.assertEqual([row["path"] for row in top["top"]],
                         ["/r/d7", "/r/d6", "/r/d5", "/r/d4", "/r/d3"])

    def test_closes_only_after_three_passes_above_floor_plus_margin(self) -> None:
        f = Fakes(self)
        f.data(96)
        # 210 is above the 200 floor but inside the 10% margin (220): no progress.
        for _ in range(4):
            f.data(210)
        self.assertEqual(f.closed, [])
        f.data(230)
        f.data(230)
        self.assertEqual(f.closed, [])
        f.data(210)  # inside the margin again: the streak restarts
        f.data(230)
        f.data(230)
        self.assertEqual(f.closed, [])
        out = f.data(230)
        self.assertEqual(f.closed, ["101"])
        self.assertTrue(out["volumes"][0]["closed"])
        self.assertEqual(f.events(), ["disk_free_low", "disk_free_recovered"])
        # A second drop is a new episode with its own issue.
        f.data(50)
        self.assertEqual(len(f.opened), 2)

    def test_an_unread_volume_changes_nothing(self) -> None:
        f = Fakes(self)
        f.data(96)
        f.free.pop("/Volumes/Data")
        for _ in range(5):
            row = f.pass_()["volumes"][0]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(f.closed, [])
        self.assertEqual(len(f.opened), 1)

    def test_an_unread_pass_resets_the_recovery_streak(self) -> None:
        f = Fakes(self)
        f.data(96)
        f.data(230)
        f.data(230)
        f.free.pop("/Volumes/Data")
        self.assertEqual(f.pass_()["volumes"][0]["status"], "unknown")
        f.data(230)
        self.assertEqual(f.closed, [])
        f.data(230)
        f.data(230)
        self.assertEqual(f.closed, ["101"])

    def test_a_stuck_free_probe_is_the_only_one_across_many_passes(self) -> None:
        f = Fakes(self)
        log = f.dir / "starts.log"
        script = ("import sys,time\nopen(sys.argv[1],'a').write('x')\ntime.sleep(300)\n")
        env = {"TARTCI_DISK_FREE_COMMAND": json.dumps([sys.executable, "-c", script, str(log)]),
               "TARTCI_DISK_FREE_TIMEOUT_S": "0.5"}
        killed = []
        self.addCleanup(lambda: [os.kill(p, 9) for p in killed if dw._alive(p)])
        with mock.patch.dict(os.environ, env), mock.patch.object(dw.os, "killpg"):
            outs = []
            for _ in range(4):
                f.now += 60
                outs.append(dw.watch_free(f.state, f.now, profile=PROFILE, issue=f.issue,
                                          close=f.close, run=f.run, host="box", target="t"))
            for state in f.state.glob("free-*.json"):
                pid = (json.loads(state.read_text()).get("outstanding") or {}).get("pid")
                if pid:
                    killed.append(pid)
        self.assertEqual(len(log.read_text()), len(PROFILE_FLOORS))
        self.assertTrue(all(v["status"] == "unknown" for v in outs[-1]["volumes"]))
        self.assertTrue(all(v["probe_stuck_since"] for v in outs[-1]["volumes"]))

    def test_a_du_that_times_out_still_reports_what_it_read(self) -> None:
        f = Fakes(self)
        f.du_outcome = "timed out after 20 s"
        f.data(96)
        body = f.opened[0][1]
        self.assertIn("du timed out after 20 s", body)
        self.assertIn("/Volumes/Data/VMs/big", body)

    def test_a_du_that_never_prints_does_not_block_the_alert(self) -> None:
        f = Fakes(self)
        f.run = lambda command, timeout, out: ("abandoned pid 7 after 20 s", "")
        f.data(96)
        self.assertEqual(len(f.opened), 1)
        self.assertIn("No work root on this volume reported a size", f.opened[0][1])
        self.assertIn("abandoned pid 7", f.opened[0][1])

    def test_a_real_du_is_killed_at_its_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "du"
            fake.write_text("#!/bin/sh\nprintf '5\\t/r/a\\n'\nsleep 60\n")
            fake.chmod(0o755)
            with mock.patch.dict(os.environ, {"TARTCI_DISK_DU_TIMEOUT_S": "1"}):
                def run(command, timeout, out):
                    return dw.bounded([str(fake), *command[1:]], timeout, out)
                top = dw.top_dirs(["/r"], pathlib.Path(tmp) / "e", run=run)
            self.assertEqual([r["path"] for r in top["top"]], ["/r/a"])
            self.assertIn("timed out", top["notes"][0])

    def test_a_host_without_floors_or_a_manifest_entry_is_off_and_says_why(self) -> None:
        f = Fakes(self)
        f.manifest.write_text("[hosts.other]\n")
        out = dw.watch_free(f.state, f.now, profile=PROFILE, free_of=lambda p: 1.0)
        self.assertIn("no host box", out["disabled"])
        f.manifest.write_text("[hosts.box]\n")
        out = dw.watch_free(f.state, f.now, profile=PROFILE, free_of=lambda p: 1.0)
        self.assertIn("no disk_floor_gb", out["disabled"])
        self.assertEqual(out["volumes"], [])


class FloorConfigTests(unittest.TestCase):
    """Run on every interpreter."""

    def test_a_missing_manifest_config_disables_the_check_and_logs_once(self) -> None:
        f = Fakes(self, manifest=False)
        with mock.patch.dict(os.environ):
            os.environ.pop("TARTCI_FLEET_MANIFEST")
            with mock.patch("sys.stderr") as err:
                first = dw.watch_free(f.state, f.now, profile=PROFILE, issue=f.issue,
                                      free_of=lambda p: 1.0)
                second = dw.watch_free(f.state, f.now, profile=PROFILE, issue=f.issue,
                                       free_of=lambda p: 1.0)
        self.assertIn("no fleet manifest configured", first["disabled"])
        self.assertEqual(second["disabled"], first["disabled"])
        logged = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertEqual(logged.count("free-space check off"), 1)
        self.assertEqual(f.opened, [])
        self.assertEqual(f.events(), [])

    def test_an_unreadable_manifest_disables_the_check(self) -> None:
        f = Fakes(self, manifest=False)  # TARTCI_FLEET_MANIFEST names a file that is absent
        floors, why = dw.load_floors(PROFILE)
        self.assertEqual(floors, [])
        self.assertTrue(why)
        self.assertTrue("unreadable" in why or "tomllib" in why)

    def test_without_tomllib_the_check_is_off(self) -> None:
        f = Fakes(self)
        with mock.patch.object(dw, "tomllib", None):
            floors, why = dw.load_floors(PROFILE)
        self.assertEqual((floors, why), ([], "no tomllib (needs Python 3.11+)"))

    def test_the_manifest_path_can_come_from_the_profile(self) -> None:
        f = Fakes(self)
        with mock.patch.dict(os.environ):
            os.environ.pop("TARTCI_FLEET_MANIFEST")
            floors, why = dw.load_floors({**PROFILE, "disk_watch": {"fleet_manifest": "x"}})
        self.assertEqual(floors, [])
        self.assertNotIn("no fleet manifest configured", why or "")

    def test_roots_belong_to_the_longest_matching_floor_path(self) -> None:
        floors = [{"path": "/"}, {"path": "/Volumes/Data"}]
        roots = ["/Volumes/Data/VMs", "/cache", "/Volumes/Data"]
        self.assertEqual(dw.roots_on("/Volumes/Data", floors, roots),
                         ["/Volumes/Data/VMs", "/Volumes/Data"])
        self.assertEqual(dw.roots_on("/", floors, roots), ["/cache"])

    def test_the_free_reader_is_bounded_and_never_waits_on_a_hung_child(self) -> None:
        env = {"TARTCI_DISK_FREE_COMMAND": json.dumps(
            [sys.executable, "-c", "import time; time.sleep(60)"]),
            "TARTCI_DISK_FREE_TIMEOUT_S": "1"}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(dw.read_free_gib("/x"))
        env["TARTCI_DISK_FREE_COMMAND"] = json.dumps(
            [sys.executable, "-c", "import sys; print(3 * 1024**3)"])
        with mock.patch.dict(os.environ, env):
            self.assertEqual(dw.read_free_gib("/x"), 3.0)
        self.assertIsNotNone(dw.read_free_gib(tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main()
