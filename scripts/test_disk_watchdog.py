#!/usr/bin/env python3
"""The disk write-latency watchdog, driven through `tartci disk watch`.

A fake probe stands in for the filesystem: it reads what to do (`fast`,
`slow <seconds>` or `hang`) from a control file and logs every invocation, so
each test shapes the volume's latency and counts the probes started. Issue
writes are never tried from a scratch TARTCI_HOME; the alert state shows the
episode went through the alert path. The issue text and its close are checked
in-process with a fake opener.

Python 3.9-safe.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import disk_watchdog as dw  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]

FAKE_PROBE = """
import pathlib, sys, time
control = pathlib.Path(__file__).with_name("control")
log = pathlib.Path(__file__).with_name("probes.log")
with log.open("a") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
mode = control.read_text().split() if control.exists() else ["fast"]
if mode[0] == "slow":
    time.sleep(float(mode[1]))
elif mode[0] == "hang":
    time.sleep(3600)
print("0.001")
"""


class Harness:
    def __init__(self, test: unittest.TestCase) -> None:
        self.temp = tempfile.TemporaryDirectory()
        test.addCleanup(self.temp.cleanup)
        base = pathlib.Path(self.temp.name)
        self.home = base / "tartci-home"
        self.volume = base / "volume"
        self.volume.mkdir()
        self.fake = base / "fake"
        self.fake.mkdir()
        (self.fake / "probe.py").write_text(FAKE_PROBE)
        self.state = self.home / "state" / "disk-watchdog"

    def mode(self, text: str) -> None:
        (self.fake / "control").write_text(text)

    def probes(self) -> int:
        log = self.fake / "probes.log"
        return len(log.read_text().splitlines()) if log.exists() else 0

    def env(self) -> dict:
        env = dict(os.environ)
        env.update({
            "TARTCI_HOME": str(self.home),
            "TARTCI_DISK_WATCH_PATHS": str(self.volume),
            "TARTCI_DISK_PROBE_COMMAND": json.dumps([sys.executable, str(self.fake / "probe.py")]),
            "TARTCI_DISK_PROBE_TIMEOUT_S": "2",
            "TARTCI_DISK_EVIDENCE_TIMEOUT_S": "5",
        })
        env.pop("TARTCI_DISK_WATCH_ISSUE", None)
        return env

    def watch(self) -> dict:
        proc = subprocess.run([str(ROOT / "tartci"), "disk", "watch", "--json"],
                              env=self.env(), capture_output=True, text=True, timeout=120,
                              check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)

    def alert_state(self) -> dict:
        found = list(self.state.glob("alert-*.json"))
        return json.loads(found[0].read_text()) if found else {}

    def events(self) -> list:
        path = self.state / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["event"] for line in path.read_text().splitlines()]


class ThroughTheEntryPoint(unittest.TestCase):
    def test_a_fast_volume_is_quiet(self) -> None:
        h = Harness(self)
        h.mode("fast")
        result = h.watch()
        row = result["paths"][0]
        self.assertEqual(row["path"], str(h.volume))
        self.assertEqual(row["probe"]["status"], "ok")
        self.assertEqual(h.probes(), 1)
        self.assertEqual(h.events(), [])
        self.assertFalse((h.state / "evidence").exists())

    def test_a_slow_write_opens_one_episode_with_evidence_once(self) -> None:
        h = Harness(self)
        h.mode("slow 1.5")
        first = h.watch()["paths"][0]
        self.assertEqual(first["probe"]["status"], "slow")
        self.assertGreater(first["probe"]["elapsed_s"], 1.0)
        self.assertTrue(first["episode_since"])
        evidence = pathlib.Path(first["evidence"])
        for name in ("log-show.txt", "ps-blocked.txt", "evidence.json"):
            self.assertTrue((evidence / name).exists(), name)
        summary = json.loads((evidence / "evidence.json").read_text())
        self.assertEqual(summary["path"], str(h.volume))
        self.assertEqual(summary["probe"]["status"], "slow")
        self.assertEqual(h.events(), ["disk_latency_slow"])
        # The episode went through the alert path. A state dir under the temp
        # dir never writes an issue, so none was tried.
        alert = h.alert_state()
        self.assertEqual(alert.get("since"), first["episode_since"])
        self.assertTrue(alert.get("evented"))
        self.assertNotIn("issue", alert)
        self.assertNotIn("issue_error", alert)

        mtime = (evidence / "evidence.json").stat().st_mtime
        second = h.watch()["paths"][0]
        self.assertEqual(second["episode_since"], first["episode_since"])
        self.assertEqual(second["evidence"], first["evidence"])
        self.assertEqual((evidence / "evidence.json").stat().st_mtime, mtime)
        self.assertEqual(h.events(), ["disk_latency_slow"])
        self.assertEqual(len(list((h.state / "evidence").iterdir())), 1)

    def test_a_write_that_never_finishes_times_out_without_waiting(self) -> None:
        h = Harness(self)
        h.mode("hang")
        start = time.monotonic()
        row = h.watch()["paths"][0]
        self.assertLess(time.monotonic() - start, 60)
        self.assertEqual(row["probe"]["status"], "timeout")
        self.assertFalse(row["probe"]["stuck"], row["probe"])  # SIGKILL took it
        self.assertFalse(dw._alive(row["probe"]["pid"]))
        self.assertTrue(row["episode_since"])
        self.assertEqual(h.events(), ["disk_latency_slow"])
        evidence = pathlib.Path(row["evidence"])
        self.assertTrue((evidence / f"sample-{row['probe']['pid']}.txt").exists())
        state = json.loads(next(h.state.glob("path-*.json")).read_text())
        self.assertNotIn("outstanding", state)

    def test_three_fast_probes_end_the_episode(self) -> None:
        h = Harness(self)
        h.mode("slow 1.5")
        h.watch()
        h.mode("fast")
        # Three fast probes in a row end it, not fewer.
        for _ in range(2):
            row = h.watch()["paths"][0]
            self.assertTrue(row["episode_since"], "closed too early")
        row = h.watch()["paths"][0]
        self.assertIsNone(row["episode_since"])
        self.assertTrue(row.get("closed"))
        self.assertEqual(h.events(), ["disk_latency_slow", "disk_latency_recovered"])
        self.assertEqual(h.alert_state(), {})

    def test_a_probe_still_in_the_kernel_is_not_joined_by_another(self) -> None:
        # A probe that outlived SIGKILL is recorded as outstanding. Stand one up
        # (alive, carrying the probe marker) and point the state at it.
        h = Harness(self)
        h.mode("fast")
        stuck = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                  dw.PROBE_MARKER, str(h.volume)])
        self.addCleanup(stuck.wait)
        self.addCleanup(stuck.kill)
        h.state.mkdir(parents=True)
        (h.state / f"path-{dw._key(str(h.volume))}.json").write_text(json.dumps(
            {"outstanding": {"pid": stuck.pid, "started": time.time() - 300}}))
        row = h.watch()["paths"][0]
        self.assertEqual(h.probes(), 0, "a second probe was started beside a stuck one")
        self.assertEqual(row["probe"]["status"], "timeout")
        self.assertTrue(row["probe"]["stuck"])
        self.assertGreaterEqual(row["probe"]["elapsed_s"], 300)
        self.assertTrue(row["episode_since"])
        # Once it is gone, probing resumes.
        stuck.kill()
        stuck.wait()
        row = h.watch()["paths"][0]
        self.assertEqual(h.probes(), 1)
        self.assertEqual(row["probe"]["status"], "ok")

    def test_a_scratch_pass_never_touches_the_real_device_filesystem(self) -> None:
        # The host running the tests may have a real devicefs mount; a test
        # pass must neither probe it nor kill its helper.
        h = Harness(self)
        h.mode("fast")
        result = h.watch()
        self.assertEqual(result["devicefs"]["mounts"], [])
        self.assertEqual(h.probes(), 1)  # only the volume probe ran
        # Whether or not this host has a devicefs mount right now, a scratch
        # pass runs no host command at all: not mount, ps, pkill or umount.
        calls = []
        original = dw.bounded
        dw.bounded = lambda command, *rest: calls.append(command) or ("ok", "")
        self.addCleanup(setattr, dw, "bounded", original)
        h.state.mkdir(parents=True, exist_ok=True)
        dw.watch_devicefs(h.state, time.time())
        self.assertEqual(calls, [])

    def test_status_marks_a_reading_the_agent_stopped_refreshing(self) -> None:
        h = Harness(self)
        h.mode("fast")
        h.watch()
        state_path = next(h.state.glob("path-*.json"))
        state = json.loads(state_path.read_text())
        state["last"]["at"] = "2026-10-17T23:29:24Z"
        state_path.write_text(json.dumps(state))
        proc = subprocess.run([str(ROOT / "tartci"), "disk", "status"], env=h.env(),
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = next(l for l in proc.stdout.splitlines() if str(h.volume) in l)
        # Stamped with the probe's own time, not now, and called stale.
        self.assertTrue(line.startswith("2026-10-17T23:29:24Z"), line)
        self.assertIn("STALE", line)
        self.addCleanup(os.environ.pop, "TARTCI_DISK_WATCH_PATHS", None)
        os.environ["TARTCI_DISK_WATCH_PATHS"] = str(h.volume)
        fresh = dw.status(h.state, now=dw.dt.datetime(
            2026, 10, 17, 23, 30, tzinfo=dw.dt.timezone.utc).timestamp())
        self.assertFalse(fresh["paths"][0]["stale"])

    def test_status_reads_the_last_probe(self) -> None:
        h = Harness(self)
        h.mode("slow 1.5")
        h.watch()
        proc = subprocess.run([str(ROOT / "tartci"), "disk", "status", "--json"], env=h.env(),
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        row = json.loads(proc.stdout)["paths"][0]
        self.assertEqual(row["last"]["status"], "slow")
        self.assertTrue(row["since"])
        self.assertTrue(row["evidence"])


class Episodes(unittest.TestCase):
    """The issue itself, with a fake opener, in-process."""

    def test_one_issue_per_episode_closed_on_recovery(self) -> None:
        h = Harness(self)
        env = h.env()
        for key in ("TARTCI_HOME", "TARTCI_DISK_WATCH_PATHS", "TARTCI_DISK_PROBE_COMMAND",
                    "TARTCI_DISK_PROBE_TIMEOUT_S", "TARTCI_DISK_EVIDENCE_TIMEOUT_S"):
            self.addCleanup(os.environ.pop, key, None)
            os.environ[key] = env[key]
        opened, closed = [], []

        def issue(title: str, body: str):
            opened.append((title, body))
            return 0, "41"

        def close(number: str):
            closed.append(number)
            return 0, "closed"

        def run(command, timeout, out):
            out.write_text("fake\n")
            return "ok", "PID PPID STAT ELAPSED WCHAN COMMAND\n"

        h.mode("slow 1.5")
        kw = dict(issue=issue, close=close, run=run, host="m5studio", target="tartci-m5studio")
        for _ in range(2):
            dw.watch_path(str(h.volume), h.state, time.time(), **kw)
        self.assertEqual(len(opened), 1)
        title, body = opened[0]
        self.assertIn("m5studio", title)
        self.assertIn(str(h.volume), title)
        self.assertIn("tartci disk status", body)
        self.assertIn("stuck_in_open", body)
        h.mode("fast")
        for _ in range(2):
            dw.watch_path(str(h.volume), h.state, time.time(), **kw)
        self.assertEqual(closed, [], "closed before the third fast probe")
        dw.watch_path(str(h.volume), h.state, time.time(), **kw)
        self.assertEqual(closed, ["41"])


class DeviceFS(unittest.TestCase):
    """A wedged iPhone mount, with fake mounts, probe and host commands."""

    def setUp(self) -> None:
        self.h = Harness(self)
        self.mount = self.h.volume / "DeviceFS"
        self.mount.mkdir()
        env = self.h.env()
        env["TARTCI_DEVICEFS_MOUNTS"] = str(self.mount)
        env["TARTCI_DEVICEFS_PROBE_COMMAND"] = env["TARTCI_DISK_PROBE_COMMAND"]
        for key in ("TARTCI_HOME", "TARTCI_DEVICEFS_MOUNTS", "TARTCI_DEVICEFS_PROBE_COMMAND",
                    "TARTCI_DISK_PROBE_TIMEOUT_S", "TARTCI_DISK_EVIDENCE_TIMEOUT_S"):
            self.addCleanup(os.environ.pop, key, None)
            os.environ[key] = env[key]
        self.addCleanup(os.environ.pop, "TARTCI_DEVICEFS_REMEDIATE", None)
        self.commands = []
        self.opened, self.closed = [], []
        # A stand-in for the DeviceFS helper: a real process, reported by the
        # fake ps, so the kill is a real one against a known pid.
        self.helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                        "/x/" + dw.DEVICEFS_HELPER])
        self.addCleanup(self.helper.wait)
        self.addCleanup(self.helper.kill)

    helper_stat = "Us"
    mount_outcome = "ok"
    umount_outcome = "ok"

    def run_fake(self, command, timeout, out):
        self.commands.append(list(command))
        if command[0] == "/usr/bin/log":
            # Evidence capture must see the stuck helper before it is killed.
            self.helper_alive_at_capture = self.helper.poll() is None
        text = ""
        if command[0] == "/bin/ps":
            text = (f"PID STAT COMMAND\n{self.helper.pid} {self.helper_stat} "
                    f"/x/{dw.DEVICEFS_HELPER}\n{os.getpid()} S python3 test_disk_watchdog.py\n")
        out.write_text(text)
        if command[0] == "/sbin/mount":
            return self.mount_outcome, text
        if command[0] == "/sbin/umount":
            return self.umount_outcome, text
        return "ok", text

    def helper_killed(self) -> bool:
        try:
            self.helper.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return False
        return self.helper.returncode == -9

    def watch(self):
        def issue(title, body):
            self.opened.append((title, body))
            return 0, "77"

        def close(number):
            self.closed.append(number)
            return 0, "closed"

        return dw.watch_devicefs(self.h.state, time.time(), issue=issue, close=close,
                                 run=self.run_fake, host="m5studio", target="m5s")

    def acted(self, tool):
        return [c for c in self.commands if c[0] == tool]

    def test_a_healthy_mount_is_left_alone(self) -> None:
        self.h.state.mkdir(parents=True)
        self.h.mode("fast")
        row = self.watch()
        self.assertEqual(row["mounts"], [str(self.mount)])
        self.assertEqual(row["probes"][0]["status"], "ok")
        self.assertEqual(self.acted("/bin/ps"), [])
        self.assertIsNone(self.helper.poll())
        self.assertEqual(self.opened, [])

    def test_one_unanswered_pass_is_not_acted_on(self) -> None:
        # A probe SIGKILL could still take (not in the kernel) is a slow
        # answer until the next pass confirms it.
        self.h.state.mkdir(parents=True)
        self.h.mode("hang")
        row = self.watch()
        self.assertEqual(row["probes"][0]["status"], "timeout")
        self.assertFalse(row["probes"][0]["stuck"])
        self.assertIsNone(self.helper.poll(), "a single slow pass killed the helper")
        self.assertEqual(self.acted("/sbin/umount"), [])
        self.assertEqual(self.opened, [])
        self.assertIsNone(row["episode_since"])

    def test_a_confirmed_wedge_is_cleared_reported_once_and_closed_on_recovery(self) -> None:
        self.h.state.mkdir(parents=True)
        self.h.mode("hang")
        self.watch()
        row = self.watch()  # the second pass with no answer confirms it
        # The helper stuck in the kernel, and only it, was killed by pid,
        # after the evidence was taken.
        self.assertTrue(self.helper_alive_at_capture)
        self.assertTrue(self.helper_killed())
        self.assertEqual(row["actions"][0], f"DeviceFS helper pid {self.helper.pid} (Us): killed")
        self.assertEqual(self.acted("/sbin/umount"), [["/sbin/umount", "-f", str(self.mount)]])
        self.assertEqual(len(self.opened), 1)
        title, body = self.opened[0]
        self.assertIn("DeviceFS", title)
        self.assertIn("m5studio", title)
        self.assertIn("Connect via network", body)
        self.assertIn("umount -f", body)
        self.assertTrue(row["episode_since"])
        events = [json.loads(l)["event"] for l in
                  (self.h.state / "events.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["devicefs_wedged"])
        # Healthy again: the issue closes on the third healthy pass, not before.
        self.h.mode("fast")
        for _ in range(2):
            self.watch()
        self.assertEqual(self.closed, [])
        self.watch()
        self.assertEqual(self.closed, ["77"])
        self.assertEqual(len(self.opened), 1)

    def test_a_helper_not_stuck_in_the_kernel_is_spared(self) -> None:
        self.h.state.mkdir(parents=True)
        self.helper_stat = "Ss"
        self.h.mode("hang")
        self.watch()
        row = self.watch()
        self.assertIsNone(self.helper.poll(), "a healthy helper was killed")
        self.assertEqual(row["actions"][0], "no DeviceFS helper is stuck in the kernel; none killed")

    def test_a_stuck_umount_is_not_joined_by_another(self) -> None:
        self.h.state.mkdir(parents=True)
        stand_in = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(stand_in.wait)
        self.addCleanup(stand_in.kill)
        self.umount_outcome = f"abandoned pid {stand_in.pid} after 15 s"
        self.h.mode("hang")
        self.watch()
        self.watch()
        self.assertEqual(len(self.acted("/sbin/umount")), 1)
        row = self.watch()
        self.assertEqual(len(self.acted("/sbin/umount")), 1, "a second umount was started")
        self.assertIn(f"pid {stand_in.pid} from an earlier pass is still stuck", row["actions"][-1])

    def test_a_failed_mount_listing_is_unknown_not_healthy(self) -> None:
        self.h.state.mkdir(parents=True)
        os.environ.pop("TARTCI_DEVICEFS_MOUNTS")
        self.mount_outcome = "timed out after 10 s"
        (self.h.state / "devicefs.json").write_text(json.dumps(
            {"since": "2026-10-22T00:00:00Z", "ok_streak": 2}))
        row = self.watch()
        self.assertEqual(row["probes"][0]["status"], "unprobed")
        state = json.loads((self.h.state / "devicefs.json").read_text())
        self.assertEqual(state["ok_streak"], 0, "an unanswered mount listing counted as healthy")
        self.assertEqual(self.closed, [])

    def test_report_only_mode_does_not_act(self) -> None:
        self.h.state.mkdir(parents=True)
        os.environ["TARTCI_DEVICEFS_REMEDIATE"] = "0"
        self.h.mode("hang")
        self.watch()
        row = self.watch()
        self.assertEqual(row["probes"][0]["status"], "timeout")
        self.assertIsNone(self.helper.poll(), "report-only mode killed the helper")
        self.assertEqual(self.acted("/sbin/umount"), [])
        self.assertIn("TARTCI_DEVICEFS_REMEDIATE=0", self.opened[0][1])

    def test_capped_stuck_probes_take_no_action_and_do_not_resolve(self) -> None:
        # Past the cap the pass cannot see the mount, so it must neither act
        # on a healthy remount nor close the episode.
        self.h.state.mkdir(parents=True)
        self.h.mode("fast")
        stuck = []
        for _ in range(dw.DEVICEFS_MAX_STUCK):
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)",
                                     dw.PROBE_MARKER, str(self.mount)])
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
            stuck.append(proc.pid)
        (self.h.state / "devicefs.json").write_text(json.dumps(
            {"stuck": stuck, "since": "2026-10-22T00:00:00Z", "ok_streak": 0}))
        for _ in range(dw.RECOVER_PASSES + 1):
            row = self.watch()
        self.assertEqual(self.h.probes(), 0, "a probe started beside the stuck ones")
        self.assertEqual(row["probes"][0]["status"], "unprobed")
        self.assertIsNone(self.helper.poll(), "a capped pass killed the helper")
        self.assertEqual(self.acted("/sbin/umount"), [])
        self.assertEqual(self.acted("/bin/ps"), [])
        self.assertEqual(self.closed, [])
        self.assertTrue(row["episode_since"])
        self.assertEqual(row["stuck_probes"], stuck)


class Pieces(unittest.TestCase):
    def test_devicefs_mounts_are_read_from_mount_output(self) -> None:
        text = "\n".join([
            "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)",
            "/dev/disk7s1 on /Volumes/Example (apfs, local, nodev, nosuid, journaled, noowners)",
            "devices -- file:///Users/x/Library/Containers/com.apple.CoreDevice.CoreDeviceService"
            "/Data/ on /Users/x/Library/Developer/CoreDevice/DeviceFS (devicefs, local, nodev, "
            "nosuid, noowners, noatime, quarantine, nobrowse, fskit, mounted by x)",
        ])
        self.assertEqual(dw.devicefs_mounts(text), ["/Users/x/Library/Developer/CoreDevice/DeviceFS"])
        self.assertEqual(dw.devicefs_mounts("/dev/disk7s1 on /Volumes/A (apfs, local)"), [])

    def test_devicefs_helpers_are_found_with_their_state(self) -> None:
        ps = "\n".join([
            "  PID STAT COMMAND",
            " 8824 Ss   /Library/Developer/PrivateFrameworks/CoreDevice.framework/Versions/A/"
            "PrivateApplications/CoreDeviceExtensions.app/Contents/Extensions/"
            "DeviceFS.appex/Contents/MacOS/DeviceFS -BSServiceDomains {}",
            " 8923 Us   /Library/Developer/.../DeviceFS.appex/Contents/MacOS/DeviceFS",
            "  900 S    /usr/bin/rg DeviceFS",
            "  901 S    /usr/sbin/lsof /Library/Developer/.../DeviceFS.appex/Contents/MacOS/DeviceFS",
        ])
        self.assertEqual(dw.devicefs_helpers(ps), [{"pid": 8824, "stat": "Ss"},
                                                   {"pid": 8923, "stat": "Us"}])

    def test_blocked_processes_are_d_or_u_state(self) -> None:
        ps = "\n".join([
            "  PID  PPID STAT ELAPSED WCHAN  COMMAND",
            "  101     1 Ss   01:00   -      /sbin/launchd",
            "  202     1 U    07:00:00 -     python3 -c probe /Volumes/Store",
            "  303     1 D+   00:05   -      cp a b",
            "  404     1 R    00:01   -      ps",
        ])
        self.assertEqual(dw.blocked_pids(ps), [202, 303])

    def test_a_stack_in_open_is_recognised(self) -> None:
        self.assertTrue(dw.in_open("    + 2381 __open  (in libsystem_kernel.dylib) + 8"))
        self.assertTrue(dw.in_open("open$NOCANCEL  (in libsystem_kernel.dylib)"))
        self.assertFalse(dw.in_open("    + 2381 __read_nocancel  (in libsystem_kernel.dylib)"))

    def test_evidence_samples_the_stuck_probe_and_the_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def run(command, timeout, out):
                calls.append(list(command))
                if command[0] == "/bin/ps":
                    text = "PID PPID STAT\n  77 1 U\n  88 1 S\n"
                elif command[0] == "/usr/bin/sample":
                    text = "__open" if command[1] == "55" else "__read"
                else:
                    text = "log"
                out.write_text(text)
                return "ok", text

            summary = dw.capture_evidence(pathlib.Path(tmp) / "e", "/Volumes/X",
                                          {"status": "timeout", "pid": 55}, run=run)
            self.assertEqual(summary["sampled"], [55, 77])
            self.assertEqual(summary["stuck_in_open"], [55])
            self.assertEqual(summary["blocked_pids"], [77])
            self.assertEqual(calls[0][0], "/usr/bin/log")
            self.assertIn("--last", calls[0])
            blocked = (pathlib.Path(tmp) / "e" / "ps-blocked.txt").read_text()
            self.assertIn("77", blocked)
            self.assertNotIn(" 88 ", blocked)

    def test_a_capture_that_hangs_is_abandoned_within_its_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            start = time.monotonic()
            outcome, _ = dw.bounded([sys.executable, "-c", "import time; time.sleep(60)"],
                                    1.0, pathlib.Path(tmp) / "out.txt")
            self.assertLess(time.monotonic() - start, 10)
            # Killed and reaped, not left running: only a process stuck in the
            # kernel is abandoned.
            self.assertEqual(outcome, "timed out after 1 s")

    def test_placeholder_paths_are_dropped(self) -> None:
        os.environ["TARTCI_DISK_WATCH_PATHS"] = os.pathsep.join(
            ["/Volumes/A", "$DISK_WATCH_PATHS", "/Volumes/A", "/Users/x/.cache"])
        self.addCleanup(os.environ.pop, "TARTCI_DISK_WATCH_PATHS", None)
        self.assertEqual(dw.watch_paths(), ["/Volumes/A", "/Users/x/.cache"])

    def test_the_agent_template_runs_the_pass_every_minute(self) -> None:
        import plistlib
        import re
        source = (ROOT / "launchd" / "com.danielraffel.tartci.disk-watchdog.plist.template").read_bytes()
        value = plistlib.loads(re.sub(rb"<!--.*?-->", b"", source, flags=re.DOTALL))
        self.assertEqual(value["ProgramArguments"][-2:], ["disk", "watch"])
        self.assertEqual(value["StartInterval"], 60)
        self.assertEqual(value["EnvironmentVariables"]["TARTCI_DISK_WATCH_PATHS"],
                         "$DISK_WATCH_PATHS")


if __name__ == "__main__":
    unittest.main()
