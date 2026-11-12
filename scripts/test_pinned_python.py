#!/usr/bin/env python3
"""The interpreter tartci's LaunchAgents run is pinned, never a PATH search.

On m5s (2026-10-15) the launchd watchdog had no TARTCI_PYTHON, so the picker
searched PATH and took /opt/homebrew/bin/python3, Homebrew 3.15. macOS denied
that binary Local Network, and every LAN probe failed with EHOSTUNREACH. The
installer now writes the pinned path to ~/.config/tartci/python.pin, the
picker uses it, and a tartci LaunchAgent with no pin refuses to run.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers" / "common" / "toml-python.lib.sh"
# The fake interpreters exec this one, and the picker accepts only a Python
# with tomllib (3.11+); the system Python 3.9 lane cannot stand in for them.
NEEDS_TOMLLIB = unittest.skipIf(sys.version_info < (3, 11), "needs tomllib (Python 3.11+)")


@NEEDS_TOMLLIB
class Picker(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / "home"
        (self.home / ".config" / "tartci").mkdir(parents=True)
        # The bait: a working python3.12 first on PATH, as Homebrew links one.
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.bait = self.interpreter(self.bin / "python3.12")
        self.pinned = self.interpreter(self.root / "pinned" / "python3.12")

    def interpreter(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        path.chmod(0o755)
        return path

    def pick(self, **env: str) -> subprocess.CompletedProcess:
        base = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin"}
        base.update(env)
        return subprocess.run(["/bin/bash", "-c", '. "$1"; tartci_toml_python_path', "_", str(LIB)],
                              capture_output=True, text=True, env=base, check=False)

    def pin(self, value: str) -> None:
        (self.home / ".config" / "tartci" / "python.pin").write_text(value + "\n")

    def test_the_pin_wins_over_a_path_search(self) -> None:
        self.pin(str(self.pinned))
        result = self.pick()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, str(self.pinned)))

    def test_an_explicit_tartci_python_wins_over_the_pin(self) -> None:
        self.pin(str(self.pinned))
        result = self.pick(TARTCI_PYTHON=str(self.bait))
        self.assertEqual(result.stdout.strip(), str(self.bait))

    def test_a_tartci_launchagent_without_a_pin_refuses_the_path_search(self) -> None:
        result = self.pick(XPC_SERVICE_NAME="com.danielraffel.tartci.launchd-watchdog")
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, "")
        self.assertIn("refusing an unpinned interpreter under launchd job "
                      "com.danielraffel.tartci.launchd-watchdog", result.stderr)

    def test_every_launchagent_tartci_ships_refuses_without_a_pin(self) -> None:
        # The labels come from the templates, so a new template is covered.
        import re  # noqa: PLC0415
        labels = sorted({re.search(r"<key>Label</key>\s*<string>([^<]+)</string>",
                                   path.read_text()).group(1)
                         for path in (ROOT / "launchd").glob("*.plist.template")})
        self.assertIn("com.danielraffel.network.http-connect-ssh-relay", labels)
        self.assertIn("com.danielraffel.pulp.tart-runner-linux", labels)
        labels.append("com.danielraffel.tartci.tart-runner-macos-fleet.m5studio.pulp-gate")
        for label in labels:
            with self.subTest(label=label):
                result = self.pick(XPC_SERVICE_NAME=label)
                self.assertEqual((result.returncode, result.stdout), (127, ""))
                self.assertIn("refusing an unpinned interpreter", result.stderr)

    def test_the_launchd_marker_refuses_even_when_the_label_is_reset(self) -> None:
        # Homebrew's GNU timeout resets XPC_SERVICE_NAME to 0 for its child.
        result = self.pick(TARTCI_LAUNCHD="1", XPC_SERVICE_NAME="0")
        self.assertEqual((result.returncode, result.stdout), (127, ""))
        self.assertIn("refusing an unpinned interpreter", result.stderr)

    def test_every_template_and_rendered_lane_sets_the_launchd_marker(self) -> None:
        import plistlib  # noqa: PLC0415
        import re  # noqa: PLC0415
        for path in sorted((ROOT / "launchd").glob("*.plist.template")):
            with self.subTest(template=path.name):
                text = re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.S)
                env = plistlib.loads(text.encode()).get("EnvironmentVariables") or {}
                self.assertEqual(env.get("TARTCI_LAUNCHD"), "1")
        lanes_source = (ROOT / "scripts" / "macos_fleet_lanes.py").read_text()
        self.assertIn('"TARTCI_LAUNCHD": "1",', lanes_source)

    def test_a_python3_fallback_helper_stays_refused_under_launchd(self) -> None:
        # The relay and the other *_or_python3 helpers fall back to
        # /usr/bin/python3 for an operator, never under a LaunchAgent.
        base = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        refused = subprocess.run(["/bin/bash", str(ROOT / "tartci"), "pre-mint-outcomes", "--help"],
                                 capture_output=True, text=True, check=False,
                                 env=dict(base, TARTCI_LAUNCHD="1"))
        self.assertEqual(refused.returncode, 127, refused.stdout + refused.stderr)
        self.assertIn("refusing an unpinned interpreter", refused.stderr)
        # Control: the same command from a shell falls back and runs.
        shell = subprocess.run(["/bin/bash", str(ROOT / "tartci"), "pre-mint-outcomes", "--help"],
                               capture_output=True, text=True, check=False, env=base)
        self.assertEqual(shell.returncode, 0, shell.stdout + shell.stderr)

    def lane(self, python: Path, lane: str = "pulp-gate") -> Path:
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist = agents / f"com.danielraffel.tartci.tart-runner-macos-fleet.m5studio.{lane}.plist"
        plist.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0">\n<dict>\n'
            "    <key>EnvironmentVariables</key>\n    <dict>\n"
            "        <key>TARTCI_PYTHON</key>\n"
            f"        <string>{python}</string>\n    </dict>\n</dict>\n</plist>\n")
        return plist

    def test_a_host_without_a_pin_bootstraps_from_its_lanes_under_launchd(self) -> None:
        # m3 and m5s, 2026-10-15: no python.pin had been written yet, and the
        # self-update agent (no TARTCI_PYTHON) handed off to the new picker,
        # which refused, so the install that writes the pin could never run.
        self.lane(self.pinned)
        result = self.pick(XPC_SERVICE_NAME="com.danielraffel.tartci.self-update")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, str(self.pinned)))
        # Never silent: the job's log names the borrowed interpreter.
        self.assertIn("com.danielraffel.tartci.self-update uses the fleet lanes' interpreter "
                      f"{self.pinned}", result.stderr)
        self.assertIn("python.pin", result.stderr)

    def test_the_pin_is_not_announced_as_a_bootstrap(self) -> None:
        # Control, same instrument: with a pin, no bootstrap line.
        self.lane(self.pinned)
        self.pin(str(self.pinned))
        result = self.pick(XPC_SERVICE_NAME="com.danielraffel.tartci.self-update")
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_the_newest_lane_wins_when_lanes_disagree(self) -> None:
        older = self.interpreter(self.root / "older" / "python3.12")
        old_plist = self.lane(older, "aaa-gate")
        new_plist = self.lane(self.pinned, "zzz-gate")
        os.utime(old_plist, (2_000_000_000, 2_000_000_000))
        os.utime(new_plist, (1_000_000_000, 1_000_000_000))
        result = self.pick(TARTCI_LAUNCHD="1")
        self.assertEqual(result.stdout.strip(), str(older))
        # The same lanes with the times swapped pick the other one.
        os.utime(old_plist, (1_000_000_000, 1_000_000_000))
        os.utime(new_plist, (2_000_000_000, 2_000_000_000))
        result = self.pick(TARTCI_LAUNCHD="1")
        self.assertEqual(result.stdout.strip(), str(self.pinned))

    def test_a_lane_pin_whose_binary_is_gone_still_refuses(self) -> None:
        self.lane(self.root / "gone" / "python3.12")
        result = self.pick(TARTCI_LAUNCHD="1")
        self.assertEqual((result.returncode, result.stdout), (127, ""))
        self.assertIn("no pinned fleet lane", result.stderr)

    def test_an_operator_shell_ignores_the_lane_pin(self) -> None:
        # Interactive commands keep the search, lane plist or not.
        self.lane(self.pinned)
        self.assertEqual(self.pick().stdout.strip(), str(self.bait))

    def test_the_self_update_handoff_is_not_refused_on_a_host_with_lanes(self) -> None:
        # The exact entry launchd runs; the control is the same entry on a
        # host with no lanes and no pin, which refuses.
        argv = ["/bin/bash", str(ROOT / "tartci"), "fleet-macos", "self-update", "--status"]
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
               "XPC_SERVICE_NAME": "com.danielraffel.tartci.self-update",
               "TARTCI_HOME": str(self.home / ".tartci")}
        control = subprocess.run(argv, capture_output=True, text=True, env=env, check=False,
                                 timeout=120)
        self.assertIn("refusing an unpinned interpreter", control.stderr)
        self.lane(self.pinned)
        fixed = subprocess.run(argv, capture_output=True, text=True, env=env, check=False,
                               timeout=120)
        self.assertNotIn("refusing an unpinned interpreter", fixed.stderr, fixed.stderr)

    def test_without_launchd_the_same_search_still_finds_the_bait(self) -> None:
        # Control, same instrument: an interactive shell keeps the search.
        result = self.pick()
        self.assertEqual((result.returncode, result.stdout.strip()), (0, str(self.bait)))
        other = self.pick(XPC_SERVICE_NAME="com.apple.Terminal")
        self.assertEqual(other.stdout.strip(), str(self.bait))

    def test_a_launchagent_with_a_pin_runs_the_pinned_binary(self) -> None:
        self.pin(str(self.pinned))
        result = self.pick(XPC_SERVICE_NAME="com.danielraffel.tartci.launchd-watchdog")
        self.assertEqual((result.returncode, result.stdout.strip()), (0, str(self.pinned)))

    def test_a_refusal_is_recorded_for_the_watchdog(self) -> None:
        marker = self.home / ".tartci" / "state" / "interpreter-refused"
        self.pick(XPC_SERVICE_NAME="com.danielraffel.tartci.reclaim")
        stamp, job, reason = marker.read_text().strip().split("\t")
        self.assertEqual(job, "com.danielraffel.tartci.reclaim")
        self.assertIn("no TARTCI_PYTHON", reason)
        # The first refusal is kept: a later one does not move its time.
        marker.write_text(f"2000-01-01T00:00:00Z\t{job}\t{reason}\n")
        self.pick(XPC_SERVICE_NAME="com.danielraffel.tartci.launchd-watchdog")
        self.assertEqual(marker.read_text().split("\t")[:2],
                         ["2000-01-01T00:00:00Z", "com.danielraffel.tartci.reclaim"])

    def test_an_interactive_search_records_nothing(self) -> None:
        # Control: the same picker outside launchd neither refuses nor records.
        self.pick()
        self.assertFalse((self.home / ".tartci" / "state" / "interpreter-refused").exists())

    def test_a_vanished_pin_refuses_rather_than_falling_back(self) -> None:
        # A Homebrew cleanup removed the pinned Cellar path: fail loudly.
        self.pin(str(self.root / "gone" / "python3.12"))
        result = self.pick()
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, "")
        self.assertIn("pinned interpreter", result.stderr)


class ProfilePython(unittest.TestCase):
    def read(self, text: str) -> subprocess.CompletedProcess:
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as handle:
            handle.write(text)
        self.addCleanup(os.unlink, handle.name)
        return subprocess.run(["/bin/bash", "-c", '. "$1"; tartci_profile_python "$2"', "_",
                               str(LIB), handle.name], capture_output=True, text=True, check=False)

    def test_host_python_is_read_and_other_tables_are_ignored(self) -> None:
        result = self.read('schema = 1\n[host]\nid = "m5studio"\npython = "/opt/py/python3.12"\n'
                           '[[lane]]\npython = "/lane/python"\n')
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "/opt/py/python3.12"))

    def test_a_profile_without_it_reads_nothing(self) -> None:
        result = self.read('[host]\nid = "m1"\n[[lane]]\npython = "/lane/python"\n')
        self.assertEqual((result.returncode, result.stdout), (1, ""))

    def test_the_validator_wants_an_absolute_path(self) -> None:
        try:
            import macos_fleet_lanes as lanes  # noqa: PLC0415 - tomllib (3.11+)
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))
        source = (ROOT / "profiles" / "m1-macos-fleet.toml").read_text()
        with tempfile.TemporaryDirectory() as tmp:
            for value, ok in (("/opt/py/python3.12", True), ("python3.12", False)):
                path = Path(tmp) / "p.toml"
                path.write_text(source.replace('ssh = "m1"\n', f'ssh = "m1"\npython = "{value}"\n', 1))
                if ok:
                    self.assertEqual(lanes.load(path)["host"]["python"], value)
                else:
                    with self.assertRaises(ValueError):
                        lanes.load(path)


@NEEDS_TOMLLIB
class WatchdogIssue(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.marker = self.home / "state" / "interpreter-refused"
        self.opened: list = []
        self.closed: list = []

    def run_pass(self):
        import tartci_launchd_watchdog as watchdog  # noqa: PLC0415
        import vm_boot_alert  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415
        with mock.patch.dict(os.environ, {"TARTCI_HOME": str(self.home)}), \
                mock.patch.object(vm_boot_alert, "_alert_host", return_value=("m5studio", "m5s")):
            return watchdog.interpreter_refusal_pass(
                now=1792050000.0,
                issue=lambda t, b: self.opened.append((t, b)) or (0, "61"),
                close=lambda n: self.closed.append(n) or (0, "closed"))

    def test_a_refusal_opens_one_issue_and_its_removal_closes_it(self) -> None:
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text("2026-10-15T09:00:00Z\tcom.danielraffel.tartci.reclaim\t"
                               "no TARTCI_PYTHON and no /x/python.pin\n")
        line = self.run_pass()
        self.assertIn("WARN interpreter: com.danielraffel.tartci.reclaim refused", line)
        self.run_pass()
        self.assertEqual([t for t, _ in self.opened],
                         ["[tartci] m5studio refused to run an unpinned interpreter"])
        import tartci_incident  # noqa: PLC0415
        self.assertEqual(tartci_incident.parse(self.opened[0][1])["class"], "interpreter_unpinned")
        self.marker.unlink()
        self.assertIsNone(self.run_pass())
        self.assertEqual(self.closed, ["61"])

    def test_a_watchdog_run_as_launchd_does_reports_its_own_refusal(self) -> None:
        # The pin names a binary a Homebrew cleanup removed. launchd runs the
        # watchdog as `/bin/bash ~/.local/bin/tartci launchd heal`; the heal is
        # refused, and the alert still runs, under /usr/bin/python3.
        if not Path("/usr/bin/python3").exists():
            self.skipTest("no /usr/bin/python3")
        (self.home / ".config" / "tartci").mkdir(parents=True)
        (self.home / ".config" / "tartci" / "python.pin").write_text(
            str(self.home / "gone" / "python3.12") + "\n")
        env = {"HOME": str(self.home), "TARTCI_HOME": str(self.home),
               "PATH": "/usr/bin:/bin", "XPC_SERVICE_NAME":
               "com.danielraffel.tartci.launchd-watchdog",
               "TARTCI_FLEET_PROFILE": str(self.home / "profile.toml")}
        (self.home / "profile.toml").write_text('[host]\nid = "m5studio"\n')
        result = subprocess.run(["/bin/bash", str(ROOT / "tartci"), "launchd", "heal"],
                                capture_output=True, text=True, env=env, check=False,
                                timeout=120)
        self.assertEqual(result.returncode, 127, result.stdout + result.stderr)
        self.assertIn("pinned interpreter", result.stderr)
        self.assertIn("WARN interpreter: com.danielraffel.tartci.launchd-watchdog refused",
                      result.stdout)
        events = (self.home / "state" / "events.jsonl").read_text()
        self.assertIn("interpreter_refused", events)
        state = (self.home / "state" / "interpreter-refused-alert.json").read_text()
        # The issue write itself is refused from a scratch home (a test run);
        # the alert reached it.
        self.assertIn("scratch", state)

    def test_a_refused_switch_opens_its_own_issue_and_closes_on_removal(self) -> None:
        record = self.home / "state" / "interpreter-switch-refused"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text("2026-10-15T13:59:30Z\t/new/python3.12\t/old/python3.15\t"
                          "did not finish a write to /Volumes/Store/VMs within 60s\n")
        line = self.run_pass()
        self.assertIn("the lanes kept /old/python3.15; /new/python3.12 failed the launchd "
                      "preflight", line)
        self.assertEqual([t for t, _ in self.opened],
                         ["[tartci] m5studio kept its lane interpreter: /new/python3.12 failed "
                          "the launchd preflight"])
        body = self.opened[0][1]
        self.assertIn("Full Disk Access", body)
        import tartci_incident  # noqa: PLC0415
        self.assertEqual(tartci_incident.parse(body)["class"], "interpreter_switch_refused")
        record.unlink()
        self.assertIsNone(self.run_pass())
        self.assertEqual(self.closed, ["61"])

    def test_no_refusal_is_quiet(self) -> None:
        self.assertIsNone(self.run_pass())
        self.assertEqual(self.opened, [])


class InterpreterPreflight(unittest.TestCase):
    """The launchd job runs the NEW binary; its exit code decides."""

    def run_with(self, prints, timeout=0.5):
        import interpreter_preflight as ip  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415
        calls = []

        def fake(*args, timeout=5):
            calls.append(args)
            if args[0] != "print":
                return subprocess.CompletedProcess(args, 0, "", "")
            text = prints(args)
            return subprocess.CompletedProcess(args, 0 if text else 1, text, "")
        profile = {"host": {"tart_home": "/Volumes/Store/VMs"}}
        with mock.patch.object(ip, "launchctl", side_effect=fake):
            return ip.run(profile, "/new/python3.12", timeout=timeout), calls

    def test_a_clean_exit_passes(self) -> None:
        state = {"n": 0}

        def prints(args):
            state["n"] += 1
            return "" if state["n"] == 1 else "\tlast exit code = 0\n"
        reason, calls = self.run_with(prints)
        self.assertIsNone(reason)
        self.assertIn(("bootout", "gui/%d/com.danielraffel.tartci.interpreter-preflight"
                       % os.getuid()), calls)

    def test_a_failure_or_a_hang_names_the_grant(self) -> None:
        state = {"n": 0}

        def failing(args):
            state["n"] += 1
            return "" if state["n"] == 1 else "\tlast exit code = 1\n"
        reason, _ = self.run_with(failing)
        self.assertIn("exit 1", reason)
        self.assertIn("Removable Volumes", reason)
        reason, _ = self.run_with(lambda args: "")
        self.assertIn("did not finish a write to /Volumes/Store/VMs", reason)
        self.assertIn("Full Disk Access", reason)


class DoctorLastExit(unittest.TestCase):
    def test_a_loaded_agent_that_keeps_failing_is_a_problem(self) -> None:
        try:
            import fleet_doctor as fd  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))
        rows = [{"label": label, "last_exit": 0} for label in fd.REQUIRED_HOST_AGENTS]
        self.assertEqual(fd.check_host_agents(rows, managed=True).code, "host_agents_ok")
        rows[0]["last_exit"] = 127
        finding = fd.check_host_agents(rows, managed=True)
        self.assertEqual(finding.code, "host_agents_failing")
        self.assertIn("last exit 127", finding.detail)
        self.assertIn("interpreter-refused", finding.detail)

    def test_last_exit_is_read_from_launchctl_print(self) -> None:
        try:
            import fleet_doctor as fd  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))
        label = "com.danielraffel.tartci.launchd-watchdog"

        def run(argv):
            if argv[1] == "list":
                return 0, f"PID\tStatus\tLabel\n-\t127\t{label}\n", ""
            return 0, f"{label} = {{\n\tpath = /x.plist\n\tlast exit code = 127\n}}\n", ""
        rows, _ = fd.launchd_registrations(run)
        self.assertEqual(rows, [{"label": label, "path": "/x.plist", "last_exit": 127}])


@NEEDS_TOMLLIB
class DoctorPythonPin(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import fleet_doctor  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))
        self.fd = fleet_doctor
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pin = Path(tmp.name) / "python.pin"

    def test_a_managed_host_without_a_pin_is_a_problem(self) -> None:
        finding = self.fd.check_python_pin(self.pin, managed=True)
        self.assertEqual((finding.code, finding.state), ("python_pin_missing", self.fd.PROBLEM))
        self.assertIn("reinstall", finding.detail)

    def test_a_pin_naming_an_executable_is_ok(self) -> None:
        self.pin.write_text(sys.executable + "\n")
        self.assertEqual(self.fd.check_python_pin(self.pin, managed=True).code, "python_pin_ok")

    def test_a_pin_naming_nothing_usable_is_a_problem(self) -> None:
        for value in ("python3", str(Path(self.pin.parent) / "gone"), str(self.pin.parent), ""):
            with self.subTest(value=value):
                self.pin.write_text(value + "\n")
                self.assertEqual(self.fd.check_python_pin(self.pin, managed=True).code,
                                 "python_pin_unusable")

    def test_an_unmanaged_host_is_not_judged(self) -> None:
        self.assertEqual(self.fd.check_python_pin(self.pin, managed=False).code,
                         "python_pin_not_applicable")

    def test_every_pin_code_and_the_failing_code_carry_a_reason(self) -> None:
        reasons = self.fd.load_reasons()
        for code in ("python_pin_missing", "python_pin_unusable", "python_pin_ok",
                     "python_pin_not_applicable", "host_agents_failing"):
            with self.subTest(code=code):
                self.assertIn(code, self.fd.CODES)
                self.assertIn(code, reasons)


@NEEDS_TOMLLIB
class SelfUpdateLauncher(unittest.TestCase):
    def test_the_launcher_is_built_with_the_profile_interpreter(self) -> None:
        try:
            import fleet_self_update as su  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "p.toml"
            profile.write_text('[host]\nid = "m5studio"\npython = "/opt/py/python3.12"\n')
            self.assertEqual(su.launch_python(profile), "/opt/py/python3.12")
            profile.write_text('[host]\nid = "m5studio"\n')
            os.environ["TARTCI_RENDER_PYTHON"] = "/elsewhere/python3"
            try:
                self.assertEqual(su.launch_python(profile), "/elsewhere/python3")
            finally:
                del os.environ["TARTCI_RENDER_PYTHON"]


if __name__ == "__main__":
    unittest.main()
