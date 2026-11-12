"""power_status reads the configured AC profile, not live assertions."""

from __future__ import annotations

import testing_support  # noqa: E402
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import power_status  # noqa: E402

HERE = Path(__file__).resolve().parent

STUDIO = """\
System-wide power settings:
Currently in use:
AC Power:
 Sleep On Power Button 1
 displaysleep         10
 sleep                0
 autorestart          1
"""

LAPTOP = """\
Battery Power:
 sleep                1
 displaysleep         2
AC Power:
 sleep                10
 displaysleep         10
"""


class Isolated(unittest.TestCase):
    """Every test reads its own empty TARTCI_HOME, never the host's sleep cache."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"TARTCI_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)


class PowerStatusTests(Isolated):
    def test_never_sleeping_studio_is_ok(self) -> None:
        value = power_status.status(STUDIO)
        self.assertEqual(value["state"], "ok")
        self.assertEqual(value["autorestart"], 1)
        self.assertEqual(power_status.describe(value), "power: ok (never sleeps on AC)")

    def test_ac_sleep_warns_and_ignores_the_battery_section(self) -> None:
        value = power_status.status(LAPTOP)
        self.assertEqual(value, {"state": "sleeps", "sleep_minutes": 10, "autorestart": None,
                                 "sleep_events": {"state": "unmeasured"}})
        self.assertIn("power: WARN sleeps after 10 min", power_status.describe(value))
        self.assertIn("Prevent automatic sleeping", power_status.describe(value))

    def test_multiword_keys_parse(self) -> None:
        self.assertEqual(power_status.parse_custom(STUDIO)["Sleep On Power Button"], 1)

    def test_no_ac_profile_is_unknown_not_ok(self) -> None:
        for text in ("", "Battery Power:\n sleep 0\n", "pmset: command not found\n"):
            with self.subTest(text=text):
                self.assertEqual(power_status.status(text)["state"], "unknown")


if __name__ == "__main__":
    unittest.main()


# Two powerd lines in `log show --style compact` form, from m5studio on
# 2026-09-30 (the 20:31-20:56Z drop), plus an unrelated line.
LOG = """\
2026-09-30 13:32:10.144 Df powerd[423:1a] [com.apple.powerd:sleepWake] Entering Sleep state due to 'Maintenance Sleep'
2026-09-30 13:32:55.041 Df powerd[423:1a] [com.apple.powerd:sleepWake] DarkWake from Deep Idle [CDNP]
2026-09-30 13:33:12.512 Df powerd[423:1a] [com.apple.powerd:sleepWake] Entering Sleep state due to 'Idle Sleep'
"""


class SleepEventTests(Isolated):
    def fake_log(self, text: str, rc: int = 0):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, rc, text, "" if rc == 0 else "log: denied")
        return run, calls

    def test_sleeps_in_the_last_hour_are_counted_and_shown(self) -> None:
        run, calls = self.fake_log(LOG)
        value = power_status.refresh_sleep_events(run=run)
        self.assertEqual(value["count"], 2)
        self.assertIn(power_status.SLEEP_PREDICATE, calls[0])
        status = power_status.status(STUDIO)
        self.assertEqual(status["sleep_events"]["count"], 2)
        # Configured to stay awake, yet it slept: that is a warning, not ok.
        self.assertTrue(power_status.describe(status).startswith("power: WARN"))
        self.assertIn("2 sleep(s) in the last hour", power_status.describe(status))

    def test_a_host_that_did_not_sleep_stays_ok(self) -> None:
        # Control, same instrument: zero sleeps reads ok and says it measured.
        run, _ = self.fake_log("2026-09-30 13:32:55 powerd DarkWake from Deep Idle\n")
        power_status.refresh_sleep_events(run=run)
        line = power_status.describe(power_status.status(STUDIO))
        self.assertEqual(line, "power: ok (never sleeps on AC); 0 sleep(s) in the last hour")

    def test_an_unreadable_log_or_old_reading_is_never_a_zero(self) -> None:
        run, _ = self.fake_log("", rc=1)
        power_status.refresh_sleep_events(run=run)
        self.assertIn("sleep count unknown", power_status.describe(power_status.status(STUDIO)))
        path = power_status.sleep_cache_path()
        path.write_text(json.dumps({"count": 0, "measured_at": 1.0}))
        self.assertIn("STALE", power_status.describe(power_status.status(STUDIO)))


class ReadinessTests(Isolated):
    @testing_support.requires_tomllib
    def test_a_host_set_to_sleep_on_ac_is_not_ready(self) -> None:
        import macos_fleet_lanes as fleet
        problem = fleet.idle_sleep_problem(LAPTOP)
        self.assertEqual(problem["code"], "host_idle_sleep_enabled")
        self.assertIn("10 min", problem["detail"])
        self.assertIsNone(fleet.idle_sleep_problem(STUDIO))
        body = (HERE / "macos_fleet_lanes.py").read_text()
        body = body[body.index("def fleet_readiness("):]
        self.assertIn("host_conditions = [c for c in (idle_sleep_problem(),)",
                      body[:body.index("\ndef ")])
        self.assertIn('"host_conditions": host_conditions,', body[:body.index("\ndef ")])
        # A host condition is not a problem: it must not gate fleet_ready.
        self.assertNotIn("problems.append(sleeps)", body)

    def test_the_heal_pass_refreshes_the_count(self) -> None:
        source = (HERE / "tartci_launchd_watchdog.py").read_text()
        main = source[source.index("def main("):]
        self.assertIn("power_status.refresh_sleep_events()", main)


class FleetDoctorPowerTests(Isolated):
    def test_doctor_flags_a_sleeping_host_and_explains_it(self) -> None:
        import json
        import fleet_doctor

        sleeping = fleet_doctor.check_power(power_status.status(LAPTOP))
        self.assertEqual((sleeping.state, sleeping.code), (fleet_doctor.PROBLEM, "power_sleeps"))
        awake = fleet_doctor.check_power(power_status.status(STUDIO))
        self.assertEqual((awake.state, awake.code), (fleet_doctor.OK, "power_ok"))
        unknown = fleet_doctor.check_power(power_status.status(""))
        self.assertEqual(unknown.state, fleet_doctor.UNKNOWN)
        reasons = json.loads((Path(__file__).resolve().parent / "fleet_reasons.json").read_text())
        for code in ("power_ok", "power_sleeps", "power_unknown"):
            self.assertIn(code, fleet_doctor.CODES)
            self.assertIn(code, json.dumps(reasons))
        self.assertIn("Prevent automatic sleeping", json.dumps(reasons))
