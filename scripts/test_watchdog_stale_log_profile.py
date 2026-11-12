#!/usr/bin/env python3
"""The watchdog's frozen-lane threshold lives in the fleet profile (#195).

m1, m5 and m5studio ran `--stale-log-seconds 4500` from hand-edited plists
while the template rendered the 1800 default, so any re-render brought back
the heals #191 diagnosed. The profile's `[launchd_watchdog]
stale_log_seconds` now renders TARTCI_WATCHDOG_STALE_LOG_SECONDS, which the
watchdog reads as its default.
"""

from __future__ import annotations

import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import testing_support  # noqa: E402
testing_support.skip_module_without_tomllib()
import macos_fleet_lanes as fleet  # noqa: E402
import support_agents as sa  # noqa: E402
import tartci_launchd_watchdog as wd  # noqa: E402

ENV = "TARTCI_WATCHDOG_STALE_LOG_SECONDS"


def render_env(profile: dict) -> dict:
    system = sa.System(Path("/Users/x"), "launchctl")
    body, err = system.render(sa.REGISTRY["launchd-watchdog"], profile)
    assert body is not None, err
    return plistlib.loads(body)["EnvironmentVariables"]


@unittest.skipIf(sa.tomllib is None, "needs tomllib")
class RenderTests(unittest.TestCase):
    def test_every_shipped_profile_renders_its_threshold(self) -> None:
        for path in sorted((ROOT / "profiles").glob("*-macos-fleet.toml")):
            data, why = sa.load_profile(path)
            self.assertIsNotNone(data, why)
            with self.subTest(profile=path.name):
                self.assertEqual(render_env(data).get(ENV), "4500")
                self.assertTrue(render_env(data).get("TART_HOME"))

    def test_a_profile_without_the_table_renders_no_override(self) -> None:
        # Control, same instrument: only the table is missing.
        self.assertNotIn(ENV, render_env({"host": {"id": "t", "tart_home": "/Users/x/VMs"}}))


class WatchdogDefaultTests(unittest.TestCase):
    def test_the_rendered_value_is_the_default(self) -> None:
        with mock.patch.dict(os.environ, {ENV: "4500"}):
            self.assertEqual(wd.default_stale_log_seconds(), 4500)

    def test_absent_or_malformed_keeps_the_built_in_default(self) -> None:
        for raw in (None, "", "abc", "0", "-5"):
            env = {k: v for k, v in os.environ.items() if k != ENV}
            if raw is not None:
                env[ENV] = raw
            with self.subTest(raw=raw), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(wd.default_stale_log_seconds(), wd.DEFAULT_STALE_LOG_S)

    def test_the_cli_flag_still_wins(self) -> None:
        source = (HERE / "tartci_launchd_watchdog.py").read_text()
        self.assertIn('"--stale-log-seconds", type=int, default=default_stale_log_seconds()',
                      source)


class ValidatorTests(unittest.TestCase):
    def check(self, table: str) -> None:
        base = (ROOT / "profiles" / "m5-macos-fleet.toml").read_text()
        start = base.index("[launchd_watchdog]")
        end = base.index("\n[", start + 1)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "p.toml"
            path.write_text(base[:start] + table + base[end:])
            fleet.load(path)

    def test_bounds_and_keys(self) -> None:
        self.check("[launchd_watchdog]\nstale_log_seconds = 600\n")
        for bad in ("stale_log_seconds = 599", "stale_log_seconds = 14401",
                    'stale_log_seconds = "4500"', "other = 1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.check(f"[launchd_watchdog]\n{bad}\n")


if __name__ == "__main__":
    unittest.main()
