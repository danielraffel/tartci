#!/usr/bin/env python3
"""Tests for persistent_runner_heal.py and runner_host_health.sh."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import persistent_runner_heal as prh  # noqa: E402
TOKEN = "AREGTOKEN0123456789"
SPEC = {
    "name": "pulp-intel-macmini",
    "repo": "Example/repo",
    "host": "runnerhost",
    "dir": "~/actions-runner",
    "service": "actions.runner.Example-repo.pulp-intel-macmini",
    "labels": "self-hosted,macOS,X64,pulp-intel-native,pulp-host-runnerhost",
}


class FakeFleet(prh.Fleet):
    """Records calls instead of reaching GitHub or a host."""

    def __init__(self, runners=None, probe=None, census_error=False) -> None:
        super().__init__(runner=self._refuse)
        self.runners = runners or []
        self.probe_result = probe
        self.census_error = census_error
        self.calls: list[tuple] = []

    @staticmethod
    def _refuse(*_a, **_k):  # pragma: no cover - a fake must never shell out
        raise AssertionError("FakeFleet shelled out")

    def census(self, repo):
        self.calls.append(("census", repo))
        if self.census_error:
            raise RuntimeError("HTTP 403")
        return self.runners

    def probe(self, spec):
        self.calls.append(("probe", spec["host"]))
        return self.probe_result

    def restart(self, spec):
        self.calls.append(("restart", spec["service"]))
        return True

    def mint_token(self, repo):
        self.calls.append(("mint_token", repo))
        return TOKEN

    def reregister(self, spec, token):
        self.calls.append(("reregister", spec["name"], token))
        return True, "ok"


def online():
    return [{"name": SPEC["name"], "status": "online"}]


def offline():
    return [{"name": SPEC["name"], "status": "offline"}]


class HealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = pathlib.Path(self.tmp.name)

    def heal(self, fleet, fix=True, now=10_000.0, stuck=1800, cooldown=3600):
        return prh.heal([dict(SPEC)], fleet, self.state, fix, now, stuck, cooldown)

    def actions(self, fleet):
        return [c[0] for c in fleet.calls if c[0] not in ("census", "probe")]

    def test_incident_registration_deleted_and_runner_deconfigured_is_reregistered(self) -> None:
        fleet = FakeFleet(runners=[], probe={"configured": "0", "listening": "0"})
        rows, rc = self.heal(fleet)
        self.assertEqual(rows[0]["action"], "reregister")
        self.assertEqual(rows[0]["result"], "reregistered")
        self.assertEqual(self.actions(fleet), ["mint_token", "reregister"])
        self.assertEqual(rc, 0)

    def test_registration_gone_while_host_still_configured_is_reregistered(self) -> None:
        # The window before the runner notices: its stale identity is replaced.
        fleet = FakeFleet(runners=[], probe={"configured": "1", "listening": "1"})
        rows, _ = self.heal(fleet)
        self.assertEqual(rows[0]["action"], "reregister")
        self.assertEqual(self.actions(fleet), ["mint_token", "reregister"])

    def test_online_runner_is_left_alone_without_touching_the_host(self) -> None:
        # Negative control: a healthy runner is neither probed nor repaired.
        fleet = FakeFleet(runners=online())
        rows, rc = self.heal(fleet)
        self.assertEqual((rows[0]["action"], rc), ("healthy", 0))
        self.assertEqual(fleet.calls, [("census", "Example/repo")])

    def test_offline_with_listener_down_is_restarted(self) -> None:
        fleet = FakeFleet(runners=offline(), probe={"configured": "1", "listening": "0"})
        rows, _ = self.heal(fleet)
        self.assertEqual(rows[0]["action"], "restart")
        self.assertEqual(self.actions(fleet), ["restart"])

    def test_offline_with_listener_up_waits_then_restarts_after_grace(self) -> None:
        probe = {"configured": "1", "listening": "1"}
        rows, rc = self.heal(FakeFleet(runners=offline(), probe=probe), now=1000.0)
        self.assertEqual((rows[0]["action"], rc), ("wait", 0))
        fleet = FakeFleet(runners=offline(), probe=probe)
        rows, _ = self.heal(fleet, now=1000.0 + 1800)
        self.assertEqual(rows[0]["action"], "restart")

    def test_recovery_clears_the_offline_clock(self) -> None:
        probe = {"configured": "1", "listening": "1"}
        self.heal(FakeFleet(runners=offline(), probe=probe), now=1000.0)
        self.heal(FakeFleet(runners=online()), now=1500.0)
        rows, _ = self.heal(FakeFleet(runners=offline(), probe=probe), now=1000.0 + 1800)
        self.assertEqual(rows[0]["action"], "wait")

    def test_unreachable_host_is_reported_never_repaired(self) -> None:
        fleet = FakeFleet(runners=[], probe=None)
        rows, rc = self.heal(fleet)
        self.assertEqual((rows[0]["action"], rc), ("host_unreachable", 1))
        self.assertEqual(self.actions(fleet), [])

    def test_report_mode_never_acts(self) -> None:
        fleet = FakeFleet(runners=[], probe={"configured": "0", "listening": "0"})
        rows, rc = self.heal(fleet, fix=False)
        self.assertEqual((rows[0]["action"], rc), ("reregister", 1))
        self.assertEqual(self.actions(fleet), [])

    def test_reregister_is_rate_limited(self) -> None:
        deconfigured = {"configured": "0", "listening": "0"}
        self.heal(FakeFleet(runners=[], probe=deconfigured), now=1000.0)
        fleet = FakeFleet(runners=[], probe=deconfigured)
        rows, rc = self.heal(fleet, now=1000.0 + 600)
        self.assertEqual((rows[0]["action"], rc), ("reregister_cooldown", 1))
        self.assertEqual(self.actions(fleet), [])

    def test_unreadable_census_is_an_error_not_a_missing_runner(self) -> None:
        fleet = FakeFleet(census_error=True, probe={"configured": "0", "listening": "0"})
        rows, rc = self.heal(fleet)
        self.assertEqual((rows[0]["action"], rc), ("census_unreadable", 2))
        self.assertEqual(self.actions(fleet), [])


class RemoteCommandTests(unittest.TestCase):
    def test_token_travels_on_stdin_never_on_the_ssh_command_line(self) -> None:
        seen = []

        def fake_run(argv, stdin=None, timeout=0):
            seen.append((argv, stdin))
            return subprocess.CompletedProcess(argv, 0, "", "")

        ok, _ = prh.Fleet(runner=fake_run).reregister(dict(SPEC), TOKEN)
        self.assertTrue(ok)
        argv, stdin = seen[0]
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertEqual(stdin, TOKEN + "\n")
        script = argv[-1]
        self.assertIn("IFS= read -r RUNNER_TOKEN", script)
        self.assertIn("--replace", script)
        self.assertIn('mv "$f" "$aside/"', script)
        self.assertNotIn("rm ", script)

    def test_runner_dir_quoting_keeps_home_expansion_and_blocks_injection(self) -> None:
        self.assertEqual(prh.remote_path("~/actions-runner"), '"$HOME"/actions-runner')
        self.assertEqual(prh.remote_path("/x/a b;rm -rf ~"), "'/x/a b;rm -rf ~'")

    def test_probe_parses_the_host_view_and_treats_garbage_as_unreachable(self) -> None:
        for stdout, rc, want in (
            ("configured=0\nlistening=0\n", 0, {"configured": "0", "listening": "0"}),
            ("", 255, None),
            ("configured=1\n", 0, None),
        ):
            fleet = prh.Fleet(runner=lambda argv, stdin=None, timeout=0, o=stdout, r=rc:
                              subprocess.CompletedProcess(argv, r, o, ""))
            self.assertEqual(fleet.probe(dict(SPEC)), want)


class ConfigTests(unittest.TestCase):
    def test_incomplete_runner_entry_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "c.json"
            path.write_text(json.dumps({"runners": [{"name": "x", "repo": "a/b"}]}))
            with self.assertRaises(ValueError):
                prh.load_specs(path)
            self.assertEqual(prh.main(["--config", str(path), "--state-dir", td]), 2)


class HostHealthScriptTests(unittest.TestCase):
    """runner_host_health.sh against fake launchctl/pgrep binaries."""

    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.runner = self.tmp / "runner"
        (self.runner / "bin").mkdir(parents=True)
        self.log = self.tmp / "calls.log"
        self.listening = self.tmp / "listening"
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "launchctl").write_text(
            f'#!/bin/bash\necho "launchctl $*" >> {self.log}\n'
            f'[ "$1" = kickstart ] && touch {self.listening}\nexit 0\n')
        (bindir / "pgrep").write_text(f'#!/bin/bash\n[ -e {self.listening} ]\n')
        for tool in bindir.iterdir():
            tool.chmod(0o755)
        self.env = dict(os.environ,
                        RUNNER_HEALTH_LAUNCHCTL=str(bindir / "launchctl"),
                        RUNNER_HEALTH_PGREP=str(bindir / "pgrep"),
                        RUNNER_HEALTH_SETTLE_SECS="0")

    def check(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/bash", str(HERE / "runner_host_health.sh"), str(self.runner), "svc.label",
             str(self.tmp / "state")],
            capture_output=True, text=True, env=self.env, timeout=30)

    def calls(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def test_deconfigured_runner_is_marked_and_not_restarted(self) -> None:
        proc = self.check()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("status=deconfigured", proc.stdout)
        self.assertTrue((self.tmp / "state" / "svc.label.deconfigured").exists())
        self.assertEqual(self.calls(), "")

    def test_configured_runner_with_listener_down_is_kickstarted(self) -> None:
        (self.runner / ".runner").write_text("{}")
        proc = self.check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("status=restarted", proc.stdout)
        self.assertIn("kickstart -k gui/", self.calls())

    def test_healthy_runner_is_left_alone(self) -> None:
        (self.runner / ".runner").write_text("{}")
        self.listening.touch()
        proc = self.check()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("status=healthy", proc.stdout)
        self.assertEqual(self.calls(), "")


if __name__ == "__main__":
    unittest.main()
