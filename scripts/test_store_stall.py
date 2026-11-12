#!/usr/bin/env python3
"""store_stall: a bounded probe that names a stalled store volume."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_doctor as fd
import store_stall as ss

ROOT = Path(__file__).resolve().parents[1]
STORE = "/Volumes/Example/VMs"
REPO = "/Volumes/Example2/Code/pulp"
PROFILE = {"host": {"tart_home": STORE},
           "reclaim": {"repo": REPO, "worktrees_root": "/Volumes/Example2/Code/worktrees"}}
NOW = 1_800_000_000.0


def item(volume: str, argv: list[str], what: str = "listing") -> dict:
    return {"volume": volume, "path": volume, "what": what, "argv": argv, "env": None}


def hangs(argv, timeout, env, started):
    started(424242)
    return None


def answers(argv, timeout, env, started):
    started(424243)
    return 0.01


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.state = Path(self.td.name) / "state"

    def probe(self, probes, **kwargs):
        kwargs.setdefault("ps_output", "")
        kwargs.setdefault("now", NOW)
        return ss.probe(probes, state=self.state, **kwargs)


class ProbeTests(Base):
    def test_a_hanging_command_is_reported_stalled_within_the_timeout(self) -> None:
        # A real child that hangs: the probe kills it and returns on time.
        started = time.monotonic()
        value = self.probe([item("/Volumes/Example", ["/bin/sleep", "30"], "VM store listing")],
                           timeout=1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual("stalled", value["state"])
        detail = value["detail"]
        self.assertIn("/Volumes/Example: still stalled since 2027-01-15T08:00:00Z: VM store "
                      "listing did not finish within 1s", detail)
        self.assertIn("sandbox or TCC stall", detail)
        self.assertIn("reboot", detail)
        self.assertIn("tartci pool drain", detail)

    def test_the_runner_kills_a_hanging_child_and_returns_on_time(self) -> None:
        seen = []
        started = time.monotonic()
        self.assertIsNone(ss.run_bounded(["/bin/sleep", "30"], 1, None, seen.append))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(1, len(seen), "the PID is reported as the child starts")
        self.assertIsNotNone(ss.run_bounded(["/bin/sleep", "0"], 5))

    def test_a_command_that_answers_is_ok_and_leaves_no_marker(self) -> None:
        # Control, same instrument: only the command's behaviour changed.
        value = self.probe([item("/Volumes/Example", ["/bin/sleep", "0"])], timeout=5)
        self.assertEqual("ok", value["state"])
        self.assertFalse(ss.marker_path(self.state, "/Volumes/Example").exists())

    def test_a_command_that_fails_fast_is_not_a_stall(self) -> None:
        value = self.probe([item("/", ["/bin/ls", "/nonexistent-store-path"])], timeout=5)
        self.assertEqual("ok", value["state"])

    def test_a_hanging_probe_leaves_its_pid_in_the_volume_marker(self) -> None:
        self.probe([item("/Volumes/Example", ["x"])], timeout=1, runner=hangs)
        marker = json.loads(ss.marker_path(self.state, "/Volumes/Example").read_text())
        self.assertEqual({"since": NOW, "pids": [424242], "identities": {"424242": None}}, marker)

    def test_while_an_earlier_probe_is_alive_no_new_child_is_spawned(self) -> None:
        # The earlier probe's child is a real live process.
        child = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        marker = ss.marker_path(self.state, "/Volumes/Example")
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"since": NOW - 7200, "pids": [child.pid],
                                      "identities": {str(child.pid): ss.pid_identity(child.pid)}}))
        calls = []

        def counting(argv, timeout, env, started):
            calls.append(argv)
            return 0.01
        value = self.probe([item("/Volumes/Example", ["x"]), item("/Volumes/Example", ["y"]),
                            item("/Volumes/Example2", ["z"])], timeout=1, runner=counting)
        self.assertEqual([["z"]], calls, "only the volume with no live probe is probed")
        self.assertEqual("stalled", value["state"])
        self.assertIn("/Volumes/Example: still stalled since 2027-01-15T06:00:00Z: an earlier "
                      f"probe (pid {child.pid}) is still hanging, so no new one was started",
                      value["detail"])
        self.assertNotIn("/Volumes/Example2:", value["detail"])
        self.assertEqual(NOW - 7200, value["since"]["/Volumes/Example"])

    def test_a_dead_earlier_probe_does_not_block_the_next_one(self) -> None:
        # Control: same marker, its PID gone, so the volume is probed again.
        marker = ss.marker_path(self.state, "/Volumes/Example")
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"since": NOW - 7200, "pids": [424242],
                                      "identities": {"424242": "gone"}}))
        calls = []

        def counting(argv, timeout, env, started):
            calls.append(argv)
            return 0.01
        value = self.probe([item("/Volumes/Example", ["x"])], timeout=1, runner=counting,
                           alive=lambda pid: False)
        self.assertEqual([["x"]], calls)
        self.assertEqual("ok", value["state"])
        self.assertFalse(marker.exists())

    def test_a_reused_pid_does_not_block_the_next_probe(self) -> None:
        marker = ss.marker_path(self.state, "/Volumes/Example")
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"since": NOW - 7200, "pids": [424242],
                                      "identities": {"424242": "old-start"}}))
        calls = []

        def counting(argv, timeout, env, started):
            calls.append(argv)
            return 0.01

        value = self.probe([item("/Volumes/Example", ["x"])], timeout=1, runner=counting,
                           alive=lambda pid: True, identity=lambda pid: "new-start")
        self.assertEqual([["x"]], calls)
        self.assertEqual("ok", value["state"])
        self.assertFalse(marker.exists())

    def test_a_live_unrelated_process_with_the_marker_pid_does_not_hold_a_probe(self) -> None:
        # The same as the reuse after a reboot, with a real process: alive,
        # but not started when the marker says, so it is not the probe.
        other = subprocess.Popen(["/bin/sleep", "30"])
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.assertIsNotNone(ss.pid_identity(other.pid), "ps reports a live process's start")
        marker = ss.marker_path(self.state, "/Volumes/Example")
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"since": NOW - 86400, "pids": [other.pid],
                                      "identities": {str(other.pid): "Mon Jan  1 00:00:00 2001"}}))
        calls = []

        def counting(argv, timeout, env, started):
            calls.append(argv)
            return 0.01
        value = self.probe([item("/Volumes/Example", ["x"])], timeout=1, runner=counting)
        self.assertEqual([["x"]], calls, "the volume is probed again")
        self.assertEqual("ok", value["state"])
        self.assertFalse(marker.exists())

    def test_since_is_kept_across_probes_until_the_volume_answers(self) -> None:
        self.probe([item("/Volumes/Example", ["x"])], timeout=1, runner=hangs, now=NOW)
        value = self.probe([item("/Volumes/Example", ["x"])], timeout=1, runner=hangs,
                           now=NOW + 900, alive=lambda pid: False)
        self.assertEqual(NOW, value["since"]["/Volumes/Example"])
        value = self.probe([item("/Volumes/Example", ["x"])], timeout=1, runner=answers,
                           now=NOW + 1800, alive=lambda pid: False)
        self.assertEqual("ok", value["state"])
        self.assertEqual({}, value["since"])

    def test_the_finding_says_how_long_an_older_process_has_been_hanging(self) -> None:
        ps = ("    05:00:12 /usr/bin/git -C /Volumes/Example2/Code/pulp rev-parse HEAD\n"
              "       00:40 /usr/bin/git -C /Volumes/Example2/Code/pulp status\n"
              " 1-02:00:00 /usr/bin/git -C /Users/example/elsewhere status\n"
              "    09:00:00 /bin/zsh /Volumes/Example2/script.sh\n")
        value = self.probe([item("/Volumes/Example2", ["/usr/bin/git", "-C", REPO, "rev-parse"],
                                 "git rev-parse")], timeout=1, runner=hangs, ps_output=ps)
        self.assertIn("git rev-parse did not finish within 1s; an older git on it has been "
                      "hanging for 5.0 h", value["detail"])
        self.assertEqual(5 * 3600 + 12, value["stalled"][0]["hanging_s"])

    def test_etime_parses_every_ps_form(self) -> None:
        for text, seconds in (("05", None), ("01:05", 65), ("02:01:05", 7265),
                              ("3-02:01:05", 3 * 86400 + 7265)):
            with self.subTest(text=text):
                self.assertEqual(seconds, ss.etime_seconds(text))

    def test_targets_cover_the_vm_store_and_the_repository_volumes(self) -> None:
        whats = {(row["volume"], row["what"]) for row in ss.targets(PROFILE)}
        self.assertIn(("/Volumes/Example", "VM store listing"), whats)
        self.assertIn(("/Volumes/Example2", "git rev-parse"), whats)
        self.assertIn(("/Volumes/Example2", "worktrees listing"), whats)
        self.assertEqual([], ss.targets({}))


class CacheTests(Base):
    def test_status_probes_writes_the_cache_and_cached_reads_it(self) -> None:
        # Any TOML parser will do here: the fixture profile is JSON, which a
        # stand-in parser reads, so this runs on the hosts' Python too.
        home = Path(self.td.name) / "home"
        with mock.patch.dict(os.environ, {"TARTCI_HOME": str(home / ".tartci")}):
            self.assertEqual("unprobed", ss.cached(home)["state"])
            self.assertEqual("unprobed", ss.status(home)["state"], "no profile yet")
            path = ss.installed_profile(home)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"host": {"tart_home": str(home / "store")}}))
            with mock.patch.object(ss, "tomllib", types.SimpleNamespace(loads=json.loads)):
                probed = ss.status(home, timeout=5, ps_output="")
            self.assertEqual("ok", probed["state"])
            self.assertEqual(probed, ss.cached(home))
            self.assertIn("store: ok (as of ", ss.describe(ss.cached(home)))
            with mock.patch.object(ss, "tomllib", None):
                self.assertEqual("unprobed", ss.status(home)["state"])

    def test_cached_and_the_pool_status_mode_never_spawn_anything(self) -> None:
        home = Path(self.td.name) / "home"
        with mock.patch.dict(os.environ, {"TARTCI_HOME": str(home / ".tartci")}):
            ss.probe([item("/Volumes/Example", ["x"])], timeout=1, runner=hangs,
                     state=ss.state_dir(home), now=NOW, ps_output="")
            refuse = mock.Mock(side_effect=AssertionError("spawned a process"))
            with mock.patch.object(subprocess, "Popen", refuse), \
                    mock.patch.object(subprocess, "run", refuse), \
                    mock.patch.object(ss, "run_bounded", refuse), \
                    mock.patch.object(Path, "home", return_value=home):
                value = ss.cached(home)
                with mock.patch("builtins.print"):
                    self.assertEqual(0, ss.main(["--cached", "--json"]))
        self.assertEqual("stalled", value["state"])
        line = ss.describe(value)
        self.assertIn("store: STALLED since 2027-01-15T08:00:00Z (as of 2027-01-15T08:00:00Z)", line)
        refuse.assert_not_called()

    def test_a_two_day_old_ok_reads_unprobed_naming_its_age_and_producer(self) -> None:
        home = Path(self.td.name) / "home"
        with mock.patch.dict(os.environ, {"TARTCI_HOME": str(home / ".tartci")}):
            ss._write(ss.state_dir(home) / "status.json",
                      {"state": "ok", "detail": "2 store probe(s) answered within 20s",
                       "stalled": [], "probed_at": NOW, "since": {}})
            # Control: within one missed watchdog run it is still the answer.
            self.assertEqual("ok", ss.cached(home, now=NOW + ss.STALE_AFTER_S - 1)["state"])
            old = ss.cached(home, now=NOW + 2 * 86400)
            self.assertEqual("unprobed", old["state"])
            self.assertEqual(("ok", 2 * 86400), (old["stale_state"], old["age_s"]))
            self.assertIn("last probe 48.0 h ago", old["detail"])
            self.assertIn("the launchd watchdog is not probing", old["detail"])
            self.assertIn("tartci doctor fleet", old["detail"])
            self.assertTrue(ss.describe(old).startswith("store: not probed (last probe 48.0 h"))
            with mock.patch.object(ss.time, "time", return_value=NOW + 2 * 86400), \
                    mock.patch("sys.stdout") as out:
                ss.main(["--cached", "--json"])
            printed = json.loads("".join(call.args[0] for call in out.write.call_args_list))
            self.assertEqual("unprobed", printed["state"])

    def test_an_old_ok_cache_is_unprobed(self) -> None:
        home = Path(self.td.name) / "home"
        state = ss.state_dir(home)
        state.mkdir(parents=True)
        (state / "status.json").write_text(json.dumps({"state": "ok",
                                                         "probed_at": NOW - 2 * ss.PROBE_INTERVAL_S - 1,
                                                         "detail": "old", "stalled": []}))
        with mock.patch.object(ss.time, "time", return_value=NOW):
            value = ss.cached(home)
        self.assertEqual("unprobed", value["state"])
        self.assertIn("watchdog", value["detail"])

    def test_the_watchdog_probes_at_most_every_interval(self) -> None:
        home = Path(self.td.name) / "home"
        with mock.patch.dict(os.environ, {"TARTCI_HOME": str(home / ".tartci")}):
            with mock.patch.object(ss, "status", return_value={"state": "ok"}) as probe:
                ss.state_dir(home).mkdir(parents=True)
                (ss.state_dir(home) / "status.json").write_text(json.dumps({"probed_at": NOW}))
                self.assertIsNone(ss.watchdog_pass(home, now=NOW + ss.PROBE_INTERVAL_S - 1))
                probe.assert_not_called()
                ss.watchdog_pass(home, now=NOW + ss.PROBE_INTERVAL_S)
                probe.assert_called_once()
            with mock.patch.object(ss, "status", return_value={"state": "stalled",
                                                                "detail": "x stalled"}):
                self.assertIn("store-stall: WARN x stalled",
                              ss.watchdog_pass(home, now=NOW + 10 * ss.PROBE_INTERVAL_S))


class DoctorTests(Base):
    def test_each_state_has_its_finding(self) -> None:
        stalled = self.probe([item("/Volumes/Example", ["x"])], timeout=1, runner=hangs)
        for value, state, code in ((stalled, fd.PROBLEM, "store_stalled"),
                                   ({"state": "ok", "detail": "1 probe", "stalled": []},
                                    fd.OK, "store_ok"),
                                   (None, fd.UNKNOWN, "store_unprobed")):
            with self.subTest(code=code):
                finding = fd.check_store_stall(value)
                self.assertEqual((state, code), (finding.state, finding.code))
                self.assertIn(code, fd.CODES)
        self.assertIn("/Volumes/Example", fd.check_store_stall(stalled).detail)

    def test_pool_status_reads_the_cache_and_never_probes(self) -> None:
        # The code path: pool status calls the module only with --cached.
        source = (ROOT / "tartci").read_text()
        calls = re.findall(r"scripts/store_stall\.py\"[^\n]*", source)
        self.assertTrue(calls)
        self.assertTrue(all("--cached" in call for call in calls), calls)
        # And the real command, on an empty home, reports the cache's absence.
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "TARTCI_HOME": f"{home}/.tartci"}
            run = lambda *a: subprocess.run(  # noqa: E731
                ["bash", str(ROOT / "tartci"), "pool", "status", *a], cwd=ROOT, env=env,
                text=True, capture_output=True, timeout=120, check=False)
            value = json.loads(run("--json").stdout)
            text = run().stdout
            self.assertFalse((Path(home) / ".tartci/state/store-stall").exists(),
                             "pool status wrote no probe result")
        self.assertEqual("unprobed", value["store"]["state"])
        self.assertIn("store: not probed (no probe result yet", text)

    def test_only_doctor_and_the_watchdog_probe_and_no_lane_runs_pool_status(self) -> None:
        users = sorted(str(path.relative_to(ROOT))
                       for path in [*ROOT.glob("providers/**/*.sh"), *ROOT.glob("scripts/*.py"),
                                    ROOT / "tartci"]
                       if path.is_file() and "store_stall" in path.read_text()
                       and not path.name.startswith("test_") and path.name != "store_stall.py")
        self.assertEqual(["scripts/fleet_doctor.py", "scripts/tartci_launchd_watchdog.py",
                          "tartci"], users)
        # No provider (the lane path) runs `tartci pool status`, so nothing on a
        # lane can reach even the cached read.
        callers = sorted(str(path.relative_to(ROOT)) for path in ROOT.glob("providers/**/*.sh")
                         if re.search(r"^[^#\n]*\bpool\s+status\b", path.read_text(), re.M))
        self.assertEqual([], callers)
        # Nor does any provider run a probe itself: neither the probe mode
        # nor `doctor fleet`, which probes. Quoted text is a message that may
        # point an operator at doctor; only code outside quotes runs it.
        def runs_a_probe(line: str) -> bool:
            code = re.sub(r'"[^"]*"|\'[^\']*\'', "", line.split("#", 1)[0])
            return bool(re.search(r"store_stall\.py\s+--probe|\bdoctor\s+fleet\b", code))
        self.assertTrue(runs_a_probe('tartci doctor fleet --json'))
        self.assertFalse(runs_a_probe('note "see tartci doctor fleet"'))
        probers = sorted(str(path.relative_to(ROOT)) for path in ROOT.glob("providers/**/*.sh")
                         if any(runs_a_probe(line) for line in path.read_text().splitlines()))
        self.assertEqual([], probers)


if __name__ == "__main__":
    unittest.main()
