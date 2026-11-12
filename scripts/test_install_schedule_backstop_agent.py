#!/usr/bin/env python3
"""The schedule backstop is installed by setup, from the generation, as the profile says.

On 2026-10-03 m3's live backstop was a script copied out of a checkout plus a
PlistBuddy edit: no self-update refreshed it and nothing would reinstall it.
These tests pin what replaced that: `tartci setup` reaches the installer, the
installed agent runs the installed generation's script, only a profile that
says "live" dispatches, and the installer never reaches the real launchd domain
from a temporary HOME.

Run:  python3 scripts/test_install_schedule_backstop_agent.py
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import schedule_backstop_mode as sbm  # noqa: E402

INSTALLER = ROOT / "scripts" / "install_schedule_backstop_agent.sh"
LABEL = "com.danielraffel.pulp.schedule-backstop"
FLEET_PROFILES = sorted((ROOT / "profiles").glob("*-macos-fleet.toml"))


def run(argv, env=None):
    return subprocess.run([str(a) for a in argv], cwd=ROOT, text=True, capture_output=True,
                          env={**os.environ, **(env or {})}, check=False)


def launchctl_double(directory: Path):
    """A launchctl stand-in that records every call and holds no real domain."""
    calls = directory / "launchctl.calls"
    double = directory / "launchctl-double"
    loaded = directory / "loaded"
    double.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{calls}'\n"
        f"case \"$1\" in print) [ -f '{loaded}' ] && printf '\\tpath = %s\\n' \"$(cat '{loaded}')\" ;; "
        f"bootstrap) printf '%s' \"$3\" > '{loaded}' ;; bootout) rm -f '{loaded}' ;; "
        "esac\n")
    double.chmod(0o755)
    return double, calls


def profile(directory: Path, line: str = "") -> Path:
    path = directory / "profile.toml"
    path.write_text(f'schema = 1\nname = "t"\n{line}\n[host]\nid = "t"\n')
    return path


class Fixture:
    """A temp HOME, a launchctl double, and an installer env pointing at both."""

    def __init__(self, td: str, line: str = 'schedule_backstop = "live"'):
        self.root = Path(td)
        self.home = self.root / "home"
        self.home.mkdir()
        self.agents = self.home / "Library" / "LaunchAgents"
        self.double, self.calls = launchctl_double(self.root)
        self.profile = profile(self.root, line)
        self.target = self.agents / f"{LABEL}.plist"

    def env(self, **extra):
        return {"HOME": str(self.home), "TARTCI_AGENTS_DIR": str(self.agents),
                "TARTCI_LAUNCHCTL_BIN": str(self.double),
                "TARTCI_FLEET_PROFILE": str(self.profile), **extra}

    def install(self, *args):
        return run([INSTALLER, *(args or ("--install",))], self.env())

    def call_log(self) -> list:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def spec(self) -> dict:
        return plistlib.loads(self.target.read_bytes())


class SetupWiresTheBackstop(unittest.TestCase):
    def test_setup_invokes_the_installer(self):
        body = (ROOT / "tartci").read_text()
        start = body.index("cmd_setup()")
        end = body.index("cmd_bench()", start)
        self.assertIn("install_schedule_backstop_agent.sh\" --install ||", body[start:end],
                      "cmd_setup must install the schedule backstop, non-fatally")


class ProfileGate(unittest.TestCase):
    """Only the profile that says "live" dispatches; the rest stay off."""

    def test_repo_profiles_live_on_m3_only(self):
        if sbm.tomllib is None:
            self.skipTest("needs tomllib")
        self.assertTrue(FLEET_PROFILES, "control: no fleet profiles found")
        modes = {p.name: sbm.mode_from_profile(p)[0] for p in FLEET_PROFILES}
        self.assertEqual(modes.pop("m3-macos-fleet.toml"), "live")
        self.assertTrue(modes, "control: m3 was the only profile read")
        self.assertEqual(set(modes.values()), {"off"}, modes)

    def test_mode_of_a_parsed_profile(self):
        self.assertEqual(sbm.mode_of({})[0], "off")
        self.assertEqual(sbm.mode_of({"schedule_backstop": "dry-run"})[0], "dry-run")
        self.assertEqual(sbm.mode_of({"schedule_backstop": "live"})[0], "live")
        self.assertIsNone(sbm.mode_of({"schedule_backstop": "on"})[0])
        self.assertIsNone(sbm.mode_of({"schedule_backstop": True})[0])

    def test_without_tomllib_the_mode_is_unknown_not_off(self):
        # /usr/bin/python3 is 3.9: reading "off" there would quietly demote m3.
        mode, why = sbm.mode_from_profile(ROOT / "profiles" / "m3-macos-fleet.toml")
        if sbm.tomllib is None:
            self.assertIsNone(mode)
            self.assertIn("tomllib", why)
        else:
            self.assertEqual(mode, "live")

    def test_fleet_validator_rejects_an_unknown_mode(self):
        if sbm.tomllib is None:
            self.skipTest("macos_fleet_lanes needs tomllib")
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.toml"
            source = (ROOT / "profiles" / "m3-macos-fleet.toml").read_text()
            self.assertIn('schedule_backstop = "live"', source)
            good = run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                        ROOT / "profiles" / "m3-macos-fleet.toml"])
            self.assertEqual(good.returncode, 0, good.stderr)
            bad.write_text(source.replace('schedule_backstop = "live"', 'schedule_backstop = "on"'))
            res = run([sys.executable, "scripts/macos_fleet_lanes.py", "validate", bad])
            self.assertNotEqual(res.returncode, 0)
            self.assertIn("schedule_backstop must be one of", res.stderr)


class Installer(unittest.TestCase):
    def test_plan_writes_nothing_and_calls_no_bootstrap(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(td)
            res = fx.install("--plan")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("plan:", res.stdout)
            self.assertFalse(fx.target.exists(), "--plan must not write the agent")
            self.assertFalse([c for c in fx.call_log() if not c.startswith("print")])

    def test_live_renders_the_generation_script_with_dispatch_on(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(td)
            res = fx.install()
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            spec = fx.spec()
            self.assertEqual(spec["Label"], LABEL)
            # The installed generation, never a copied script that self-update
            # does not refresh.
            self.assertEqual(spec["ProgramArguments"],
                             ["/bin/bash", f"{fx.home}/.local/bin/tartci", "schedule-backstop"])
            self.assertNotIn(".local/share/tartci", fx.target.read_text())
            env = spec["EnvironmentVariables"]
            self.assertEqual(env["TARTCI_BACKSTOP_APPLY"], "1")
            self.assertEqual(env["TARTCI_BACKSTOP_AUTHORITY"], "1")
            self.assertEqual(env["TARTCI_BACKSTOP_REPO"], "Generous-Corp/pulp")
            log = fx.call_log()
            self.assertTrue(any(c.startswith("bootstrap") for c in log), log)
            self.assertTrue(any(c.startswith("kickstart") for c in log), log)

    def test_dry_run_renders_dispatch_off(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(td, 'schedule_backstop = "dry-run"')
            res = fx.install()
            self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
            env = fx.spec()["EnvironmentVariables"]
            self.assertEqual(env["TARTCI_BACKSTOP_APPLY"], "0")
            self.assertEqual(env["TARTCI_BACKSTOP_AUTHORITY"], "0")

    def test_off_installs_nothing(self):
        for line in ("", 'schedule_backstop = "off"'):
            with tempfile.TemporaryDirectory() as td:
                fx = Fixture(td, line)
                res = fx.install()
                self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
                self.assertIn("nothing to install", res.stdout)
                self.assertFalse(fx.target.exists())
                self.assertFalse([c for c in fx.call_log() if not c.startswith("print")])

    def test_off_leaves_a_present_agent_alone(self):
        # A snapshot that predates the key reads as off; removing the one live
        # dispatcher on that would be silent.
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(td)
            self.assertEqual(fx.install().returncode, 0)
            before = fx.target.read_bytes()
            fx.profile.write_text('schema = 1\nname = "t"\n')
            res = fx.install()
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("left alone", res.stdout)
            self.assertEqual(fx.target.read_bytes(), before)
            self.assertEqual(sum(c.startswith("bootout") for c in fx.call_log()), 0)

    def test_invalid_mode_changes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(td, 'schedule_backstop = "yes"')
            res = fx.install()
            self.assertEqual(res.returncode, 5, res.stdout + res.stderr)
            self.assertFalse(fx.target.exists())

    def test_reinstall_is_a_no_op_even_after_a_watchdog_rewrite(self):
        # The launchd watchdog rewrites this agent with sorted keys; that is
        # the same agent and must not cost a bootout.
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(td)
            self.assertEqual(fx.install().returncode, 0)
            fx.target.write_bytes(plistlib.dumps(fx.spec(), sort_keys=True))
            fx.calls.unlink()
            res = fx.install()
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("already installed and loaded", res.stdout)
            self.assertEqual([c for c in fx.call_log() if not c.startswith("print")], [])

    def test_live_to_dry_run_rewrites_and_reloads(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(td)
            self.assertEqual(fx.install().returncode, 0)
            fx.profile.write_text('schema = 1\nname = "t"\nschedule_backstop = "dry-run"\n')
            fx.calls.unlink()
            res = fx.install()
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(fx.spec()["EnvironmentVariables"]["TARTCI_BACKSTOP_APPLY"], "0")
            log = fx.call_log()
            self.assertTrue(any(c.startswith("bootout") for c in log), log)
            self.assertTrue(any(c.startswith("bootstrap") for c in log), log)

    def test_uninstall_boots_out_and_removes(self):
        with tempfile.TemporaryDirectory() as td:
            fx = Fixture(td)
            self.assertEqual(fx.install().returncode, 0)
            res = fx.install("--uninstall")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse(fx.target.exists())
            self.assertTrue(any(c.startswith("bootout") for c in fx.call_log()))

    def test_rejects_an_unknown_argument(self):
        self.assertEqual(run([INSTALLER, "--wat"]).returncode, 2)


class NeverTouchesTheRealDomainFromATempHome(unittest.TestCase):
    """A temp-HOME install must not replace the host's real dispatcher.

    Each refusal runs against a recording double the guard judges as the real
    launchctl, so a guard broken on purpose lands in the double, not in this
    machine's gui/<uid> domain.
    """

    def test_temp_home_is_refused_before_any_launchctl_call_or_write(self):
        for flag in ("--install", "--uninstall"):
            with tempfile.TemporaryDirectory() as td:
                fx = Fixture(td)
                env = fx.env(TARTCI_LAUNCHD_GUARD_TREAT_AS_REAL="1")
                env.pop("TARTCI_AGENTS_DIR")
                res = run([INSTALLER, flag], env)
                self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
                self.assertIn("is not this account's home", res.stderr)
                self.assertFalse(fx.target.exists())
                self.assertFalse(fx.calls.exists(), "launchctl was called despite the refusal")


if __name__ == "__main__":
    unittest.main()
