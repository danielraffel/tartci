#!/usr/bin/env python3
"""The watchdog's TCC context probe is a per-host profile key.

`scripts/tcc_wedge.py` classifies a TCC wedge (and lets the watchdog run the
root heal) only when TARTCI_TCC_CONTEXT_PROBE names a command that opens the
store from a context holding TCC access. That variable lived nowhere a host
could set without hand-editing its plist. The profile's `[launchd_watchdog]
tcc_context_probe` now renders it through the support-agents step.

Pins: the shipped m3 profile renders the variable, and tcc_wedge splits it back
to exactly the profile's argv; a profile without the key renders nothing; the
validator refuses a probe that cannot open the store (no store word, a
look-alike prefix, a relative program, a string instead of a list) at install
time; and a value that skipped validation still renders nothing.

Run:  python3 scripts/test_watchdog_tcc_context_probe.py
"""

from __future__ import annotations

import plistlib
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import testing_support  # noqa: E402
testing_support.skip_module_without_tomllib()
import macos_fleet_lanes as fleet  # noqa: E402
import support_agents as sa  # noqa: E402
import tcc_wedge as tw  # noqa: E402

ENV = "TARTCI_TCC_CONTEXT_PROBE"
# The validator tests splice into the m3 profile, so the store is m3's own.
STORE = sa.load_profile(ROOT / "profiles" / "m3-macos-fleet.toml")[0]["host"]["tart_home"]
GOOD = ["/usr/bin/ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "localhost",
        "/usr/bin/python3 -c 'import os,sys; os.close(os.open(sys.argv[1], "
        "os.O_RDONLY | os.O_DIRECTORY))' " + STORE]


def render_env(profile: dict) -> dict:
    system = sa.System(Path("/Users/x"), "launchctl")
    body, err = system.render(sa.REGISTRY["launchd-watchdog"], profile)
    assert body is not None, err
    return plistlib.loads(body)["EnvironmentVariables"]


def profile_with(probe) -> dict:
    return {"host": {"id": "t", "tart_home": STORE},
            "launchd_watchdog": {"stale_log_seconds": 4500, "tcc_context_probe": probe}}


class RecordingRunner:
    def __init__(self) -> None:
        self.argv = None

    def __call__(self, argv, timeout, operation):
        self.argv = list(argv)

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""
        return Done()


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class RenderTests(unittest.TestCase):
    def test_m3_renders_a_probe_tcc_wedge_splits_back_to_the_profile_argv(self) -> None:
        data, why = sa.load_profile(ROOT / "profiles" / "m3-macos-fleet.toml")
        self.assertIsNotNone(data, why)
        declared = data["launchd_watchdog"]["tcc_context_probe"]
        env = render_env(data)
        self.assertIn(ENV, env)
        runner = RecordingRunner()
        probe = tw.context_probe_from_env({ENV: env[ENV]}, runner=runner)
        self.assertIsNotNone(probe)
        self.assertTrue(probe()["ok"])
        self.assertEqual(runner.argv, declared)
        self.assertIn(data["host"]["tart_home"], " ".join(declared))

    def test_quotes_and_spaces_survive_the_round_trip(self) -> None:
        tricky = ["/bin/sh", "-c", "test -d \"$1\" && echo 'it''s' ok", "probe", STORE + "/a b"]
        env = render_env(profile_with(tricky))
        self.assertEqual(shlex.split(env[ENV]), tricky)

    def test_a_profile_without_the_key_renders_no_probe(self) -> None:
        # Control, same instrument: the shipped m1 profile has the table but not the key.
        data, why = sa.load_profile(ROOT / "profiles" / "m1-macos-fleet.toml")
        self.assertIsNotNone(data, why)
        self.assertNotIn("tcc_context_probe", data["launchd_watchdog"])
        env = render_env(data)
        self.assertNotIn(ENV, env)
        self.assertEqual(env.get("TARTCI_WATCHDOG_STALE_LOG_SECONDS"), "4500")

    def test_an_unvalidated_bad_value_renders_nothing(self) -> None:
        # A profile that skipped the installer's validator must not render a
        # probe that cannot fail: the heal stays off instead.
        for bad in ("/usr/bin/true " + STORE, ["/usr/bin/true"], ["/usr/bin/ssh", "localhost", "true"]):
            with self.subTest(bad=bad):
                self.assertNotIn(ENV, render_env(profile_with(bad)))
        self.assertIn(ENV, render_env(profile_with(GOOD)))


class ValidatorTests(unittest.TestCase):
    def check(self, table: str) -> None:
        base = (ROOT / "profiles" / "m3-macos-fleet.toml").read_text()
        start = base.index("[launchd_watchdog]")
        end = base.index("\n[", start + 1)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "p.toml"
            path.write_text(base[:start] + table + base[end:])
            fleet.load(path)

    @staticmethod
    def toml(probe) -> str:
        if isinstance(probe, list):
            words = ", ".join('"' + w.replace("\\", "\\\\").replace('"', '\\"')
                              .replace("\n", "\\n") + '"' for w in probe)
            value = f"[{words}]"
        else:
            value = '"' + probe + '"'
        return f"[launchd_watchdog]\nstale_log_seconds = 4500\ntcc_context_probe = {value}\n"

    def test_the_shipped_value_and_a_direct_open_are_accepted(self) -> None:
        self.check(self.toml(GOOD))
        self.check(self.toml(["/usr/bin/stat", "-f", "%d", STORE + "/"]))
        self.check(self.toml(["/bin/ls", "-d", STORE + "/pulp-vm-01"]))

    def test_a_probe_that_cannot_open_the_store_is_refused(self) -> None:
        for bad in (
            "/usr/bin/ssh localhost /usr/bin/stat " + STORE,          # a string, not an argv
            ["/usr/bin/true"],                                         # one word
            [STORE + "/probe"],                                        # one word, even on the store
            ["/usr/bin/ssh", "localhost", "/usr/bin/true"],            # reaches the host only
            ["/usr/bin/stat", str(Path(STORE).parent)],               # the volume, not the store
            ["/usr/bin/stat", STORE + "X"],                            # a look-alike prefix
            ["/usr/bin/stat", "/tmp/" + STORE],                        # store as a suffix
            ["ssh", "localhost", "stat " + STORE],                     # relative program
            ["/usr/bin/stat", ""],                                     # empty word
            ["/usr/bin/stat", STORE + "\n/usr/bin/true"],              # line break
            ["/usr/bin/ssh", "localhost", "stat '" + STORE],           # unbalanced quote
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.check(self.toml(bad))

    def test_unknown_keys_are_still_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.check("[launchd_watchdog]\nstale_log_seconds = 4500\ncontext_probe = []\n")

    def test_the_store_comes_from_this_hosts_profile(self) -> None:
        problem = sa.context_probe_problem(["/usr/bin/stat", "/Volumes/Example/VMs"], STORE)
        self.assertIsNotNone(problem)
        self.assertIsNone(sa.context_probe_problem(["/usr/bin/stat", STORE], STORE))
        self.assertIsNotNone(sa.context_probe_problem(["/usr/bin/stat", STORE], None))


if __name__ == "__main__":
    unittest.main()
