#!/usr/bin/env python3
"""The hourly check that the host's CURRENT interpreters can still use the VM
store from launchd (scripts/interpreter_grant_check.py)."""
from __future__ import annotations

import os
import plistlib
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import interpreter_grant_check as grant  # noqa: E402
import vm_boot_alert  # noqa: E402

NOW = 1792050000.0


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.user = self.root / "user"
        self.home = self.user / ".tartci"
        self.agents = self.user / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        self.store = self.root / "Atelier" / "VMs"
        self.store.mkdir(parents=True)
        self.opened: list = []
        self.closed: list = []
        self.probed: list = []
        env = {"HOME": str(self.user), "TARTCI_HOME": str(self.home),
               "TART_HOME": str(self.store)}
        for key in ("TARTCI_LAUNCH_AGENTS_DIR", "TARTCI_PYTHON_PIN",
                    "TARTCI_GRANT_CHECK_ENABLE_FILE"):
            env[key] = ""
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("TARTCI_LAUNCH_AGENTS_DIR", "TARTCI_PYTHON_PIN",
                    "TARTCI_GRANT_CHECK_ENABLE_FILE"):
            os.environ.pop(key, None)
        host = mock.patch.object(vm_boot_alert, "_alert_host", return_value=("m5studio", "m5s"))
        host.start()
        self.addCleanup(host.stop)
        self.enable_file = self.user / ".config" / "tartci" / "interpreter-grant-check.enabled"
        self.enable_file.parent.mkdir(parents=True, exist_ok=True)
        self.enable_file.write_text("")

    def lane(self, python: str, name: str = "pulp-gate") -> None:
        plist = self.agents / f"com.danielraffel.tartci.tart-runner-macos-fleet.m5s.{name}.plist"
        plist.write_bytes(plistlib.dumps({"Label": plist.stem,
                                          "EnvironmentVariables": {"TARTCI_PYTHON": python}}))

    def pin(self, python: str) -> None:
        pin = self.user / ".config" / "tartci" / "python.pin"
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text(python + "\n")

    def probe(self, failing: set[str] | None = None):
        failing = failing or set()

        def run(python: str, store: str):
            self.probed.append((python, store))
            return f"{python} did not finish a write to {store} within 30s" \
                if python in failing else None
        return run

    def alert(self, now: float, probe):
        return grant.alert_pass(now, probe=probe,
                                issue=lambda t, b: self.opened.append((t, b)) or (0, "77"),
                                close=lambda n: self.closed.append(n) or (0, "closed"))


class WhatIsChecked(Base):
    def test_every_lane_interpreter_and_the_pin_are_checked_once_each(self) -> None:
        self.lane("/opt/homebrew/Cellar/python@3.12/3.12.15/bin/python3.12", "pulp-gate")
        self.lane("/opt/homebrew/Cellar/python@3.12/3.12.15/bin/python3.12", "forge-gate")
        self.pin("/opt/homebrew/Cellar/python@3.12/3.12.13_2/bin/python3.12")
        self.assertEqual(grant.current_interpreters(self.user), [
            "/opt/homebrew/Cellar/python@3.12/3.12.15/bin/python3.12",
            "/opt/homebrew/Cellar/python@3.12/3.12.13_2/bin/python3.12"])

    def test_a_mise_provided_lane_interpreter_is_checked_as_the_lanes_run_it(self) -> None:
        # Once lanes carry a mise-installed Python, that exact path is probed.
        mise = str(self.user / ".local/share/mise/installs/python/3.12.13/bin/python3")
        self.lane(mise)
        grant.check(NOW, probe=self.probe(), home=self.home, user_home=self.user)
        self.assertEqual(self.probed, [(mise, str(self.store))])

    def test_a_host_with_no_lanes_and_no_pin_probes_nothing(self) -> None:
        out = grant.check(NOW, probe=self.probe(), home=self.home, user_home=self.user)
        self.assertEqual((out["ran"], self.probed), (True, []))
        self.assertIsNone(self.alert(NOW, self.probe()))
        self.assertEqual(self.opened, [])


class OptIn(Base):
    def test_without_the_enable_file_it_probes_nothing(self) -> None:
        self.lane("/x/python3.12")
        self.enable_file.unlink()
        self.assertIsNone(self.alert(NOW, self.probe({"/x/python3.12"})))
        self.assertEqual((self.probed, self.opened), ([], []))
        self.assertFalse((self.home / "state" / "interpreter-grant-check.json").exists())

    def test_turning_it_off_closes_an_open_episode(self) -> None:
        self.lane("/x/python3.12")
        self.alert(NOW, self.probe({"/x/python3.12"}))
        self.assertEqual(len(self.opened), 1)
        self.enable_file.unlink()
        self.assertIsNone(self.alert(NOW + 3600, self.probe({"/x/python3.12"})))
        self.assertEqual(self.closed, ["77"])


class Bounds(Base):
    def test_it_probes_at_most_the_cap_per_pass_and_records_the_time(self) -> None:
        for n in range(grant.MAX_PROBES_PER_PASS + 2):
            self.lane(f"/x/python3.{n}", f"lane{n}")
        out = grant.check(NOW, probe=self.probe(), home=self.home, user_home=self.user)
        self.assertEqual(len(self.probed), grant.MAX_PROBES_PER_PASS)
        self.assertEqual(len(out["pythons"]), grant.MAX_PROBES_PER_PASS + 2)
        import json  # noqa: PLC0415
        state = json.loads((self.home / "state" / "interpreter-grant-check.json").read_text())
        self.assertIn("probe_seconds", state)
        self.assertEqual(len(state["probed"]), grant.MAX_PROBES_PER_PASS)


class Label(unittest.TestCase):
    def test_the_check_never_uses_the_install_preflights_label(self) -> None:
        # run() boots out whatever holds its label first: sharing the install
        # preflight's would kill an install's probe mid-write.
        import interpreter_preflight  # noqa: PLC0415
        self.assertNotEqual(grant.LABEL, interpreter_preflight.LABEL)
        with mock.patch.object(interpreter_preflight, "run", return_value=None) as run:
            grant._probe("/x/python3.12", "/Volumes/Store/VMs")
        self.assertEqual(run.call_args.kwargs["label"], grant.LABEL)
        self.assertEqual(run.call_args.kwargs["timeout"], grant.TIMEOUT_SECONDS)


class Cadence(Base):
    def test_it_runs_about_hourly_not_every_watchdog_pass(self) -> None:
        self.lane("/x/python3.12")
        self.assertTrue(grant.check(NOW, probe=self.probe(), home=self.home,
                                    user_home=self.user)["ran"])
        self.assertFalse(grant.check(NOW + 300, probe=self.probe(), home=self.home,
                                     user_home=self.user)["ran"])
        self.assertTrue(grant.check(NOW + 3600, probe=self.probe(), home=self.home,
                                    user_home=self.user)["ran"])
        self.assertEqual(len(self.probed), 2)


class Alerting(Base):
    def test_a_lost_grant_opens_one_issue_naming_host_binary_and_grant(self) -> None:
        real = self.root / "Cellar" / "python3.12"
        real.parent.mkdir()
        real.write_text("")
        link = self.root / "bin" / "python3"
        link.parent.mkdir()
        link.symlink_to(real)
        self.lane(str(link))
        failing = self.probe({str(link)})
        with mock.patch.object(grant, "store_path", return_value="/Volumes/Store/VMs"):
            line = self.alert(NOW, failing)
            self.alert(NOW + 300, failing)          # not due: the record stands
            self.alert(NOW + 3600, failing)         # due again, still failing
        self.assertIn("WARN interpreter-grant", line)
        self.assertIn("(issue #77)", line)
        self.assertEqual(len(self.opened), 1)
        title, body = self.opened[0]
        self.assertEqual(title, "[tartci] m5studio lane interpreter can no longer use the "
                                f"VM store: {real}")
        self.assertIn("Removable Volumes", body)
        self.assertIn(f"add or re-enable {real}", body)
        self.assertIn("has NOT been taken off", body)
        import tartci_incident  # noqa: PLC0415
        self.assertEqual(tartci_incident.parse(body)["class"], "interpreter_grant_lost")
        self.assertEqual(self.closed, [])

    def test_a_restored_grant_closes_the_issue_and_clears_the_record(self) -> None:
        self.lane("/x/python3.12")
        self.alert(NOW, self.probe({"/x/python3.12"}))
        record = self.home / "state" / "interpreter-grant-lost"
        self.assertTrue(record.exists())
        self.assertIsNone(self.alert(NOW + 3600, self.probe()))
        self.assertFalse(record.exists())
        self.assertEqual(self.closed, ["77"])

    def test_the_episode_keeps_its_first_time_while_it_lasts(self) -> None:
        self.lane("/x/python3.12")
        self.alert(NOW, self.probe({"/x/python3.12"}))
        self.alert(NOW + 3600, self.probe({"/x/python3.12"}))
        self.assertEqual(grant.read_record(self.home)[0], grant._iso(NOW))
        self.assertEqual(len(self.opened), 1)

    def test_it_never_takes_the_host_off(self) -> None:
        # Only the probe and the issue calls run; no pool command is issued.
        self.lane("/x/python3.12")
        with mock.patch("subprocess.run", side_effect=AssertionError("ran a command")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("ran a command")):
            line = self.alert(NOW, self.probe({"/x/python3.12"}))
        self.assertIn("WARN interpreter-grant", line)
        source = Path(grant.__file__).read_text()
        for verb in ('"off"', "pool_off", "pool off", "tartci_pool"):
            self.assertNotIn(verb, source, f"the grant check must not take the host off ({verb})")


FAKE_LAUNCHCTL = textwrap.dedent("""\
    #!{python}
    # A launchd stand-in: runs the one-shot job for real on kickstart.
    import json, os, plistlib, subprocess, sys
    from pathlib import Path
    state = Path({state!r})
    jobs = json.loads(state.read_text()) if state.exists() else {{}}
    cmd = sys.argv[1]
    label = sys.argv[2].rsplit("/", 1)[-1] if len(sys.argv) > 2 else ""
    if cmd == "bootstrap":
        path = sys.argv[3]
        jobs[Path(path).stem] = {{"plist": path}}
    elif cmd == "kickstart" and label in jobs:
        job = plistlib.loads(Path(jobs[label]["plist"]).read_bytes())
        with open(job["StandardOutPath"], "ab") as log:
            jobs[label]["exit"] = subprocess.run(job["ProgramArguments"], stdout=log,
                                                 stderr=subprocess.STDOUT).returncode
    elif cmd == "bootout":
        jobs.pop(label, None)
    elif cmd == "print":
        if label not in jobs:
            raise SystemExit(113)
        if "exit" in jobs[label]:
            print(f"\\tlast exit code = {{jobs[label]['exit']}}")
    state.write_text(json.dumps(jobs))
""")


class RealProbe(Base):
    """The real launchd probe (interpreter_preflight.run) under a fake launchctl
    that starts the job's program for real."""

    def setUp(self) -> None:
        super().setUp()
        fakebin = self.root / "fakebin"
        fakebin.mkdir()
        launchctl = fakebin / "launchctl"
        launchctl.write_text(FAKE_LAUNCHCTL.format(python=sys.executable,
                                                   state=str(self.root / "launchctl.json")))
        launchctl.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{fakebin}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)
        # A mise-shaped install whose bin/python3 is a symlink, as mise lays it out.
        self.mise = self.user / ".local/share/mise/installs/python/3.12.13/bin/python3"
        self.mise.parent.mkdir(parents=True)
        self.mise.symlink_to(sys.executable)
        self.lane(str(self.mise))

    def test_a_store_the_interpreter_cannot_write_is_reported(self) -> None:
        self.store.chmod(0o500)
        try:
            line = self.alert(NOW, grant._probe)
        finally:
            self.store.chmod(0o755)
        self.assertIn(f"WARN interpreter-grant: {self.mise} cannot use {self.store}", line)
        self.assertEqual(grant.read_record(self.home)[2], os.path.realpath(sys.executable))
        self.assertEqual(len(self.opened), 1)

    def test_a_writable_store_passes_and_is_left_clean(self) -> None:
        # Control, same instrument.
        self.assertIsNone(self.alert(NOW, grant._probe))
        self.assertEqual(self.opened, [])
        self.assertEqual(list(self.store.iterdir()), [])
        state = (self.home / "state" / "interpreter-grant-check.json").read_text()
        self.assertIn(str(self.mise), state)


class WatchdogWiring(Base):
    def test_the_watchdog_pass_reports_and_never_raises(self) -> None:
        try:
            import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        except ImportError as exc:  # the watchdog runs the pinned 3.11+
            self.skipTest(str(exc))
        self.lane("/x/python3.12")
        line = watchdog.interpreter_grant_pass(
            now=NOW, probe=self.probe({"/x/python3.12"}),
            issue=lambda t, b: self.opened.append((t, b)) or (0, "77"),
            close=lambda n: (0, ""))
        self.assertIn("WARN interpreter-grant", line)

        def boom(python, store):
            raise RuntimeError("probe broke")
        os.unlink(self.home / "state" / "interpreter-grant-check.json")
        line = watchdog.interpreter_grant_pass(now=NOW, probe=boom)
        self.assertIn("WARN interpreter-grant check FAILED", line)

    def test_the_heal_pass_runs_it(self) -> None:
        source = (Path(grant.__file__).parent / "tartci_launchd_watchdog.py").read_text()
        self.assertIn("grant_line = interpreter_grant_pass()", source)


if __name__ == "__main__":
    unittest.main()
