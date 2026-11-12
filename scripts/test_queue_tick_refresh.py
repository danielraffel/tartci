#!/usr/bin/env python3
"""An installed queue tick follows the running tartci, and drift is shown.

The tick used to be copied out of the generation, so m3 ran a 2026-08-15 copy
in `mode=live` after the merge path was removed. It now runs
`~/.local/bin/tartci queue-tick`; an agent still installed the old way is the
drift the watchdog repairs.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import queue_tick_refresh as qtr  # noqa: E402
import tartci_launchd_watchdog as wd  # noqa: E402
import testing_support  # noqa: E402


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "generation"
        (self.root / "scripts").mkdir(parents=True)
        self.home = self.tmp / "home"
        self.plist = self.home / "Library" / "LaunchAgents" / f"{qtr.LABEL}.plist"
        self.config = self.tmp / "queue-tick.env"
        self.calls: list[list[str]] = []
        self.loaded = True
        # Stand-in installer: renders the agent the way the real one does and
        # records its arguments, so the test can see what it was asked to keep.
        desired = qtr.desired_program(self.plist)
        render = self.tmp / "render.py"
        render.write_text("import json, plistlib, sys\n"
                          "value = plistlib.loads(open(sys.argv[1], 'rb').read())\n"
                          f"value['ProgramArguments'] = json.loads({json.dumps(json.dumps(desired))})\n"
                          "open(sys.argv[1], 'wb').write(plistlib.dumps(value))\n")
        (self.root / "scripts" / qtr.INSTALLER).write_text(
            "#!/bin/bash\nset -e\n"
            f"echo \"$*\" > {self.tmp}/installer-args\n"
            f"/usr/bin/python3 {render} {self.plist}\n")

    def install_old(self, apply: str | None = "1", gh_cli: str = "/x/ghapp") -> None:
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        env = {} if apply is None else {"SHIPYARD_TICK_APPLY": apply}
        self.plist.write_bytes(plistlib.dumps({
            "Label": qtr.LABEL, "EnvironmentVariables": env,
            "ProgramArguments": ["/bin/bash",
                                 f"{self.home}/.local/share/tartci/scripts/shipyard_queue_tick.sh"]}))
        self.config.write_text(f"SHIPYARD_QUEUE_REPO_ROOT=\nSHIPYARD_QUEUE_GH_CLI={gh_cli}\n"
                               "SHIPYARD_QUEUE_AUTHORITY=1\n")

    def runner(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] == "launchctl":
            return subprocess.CompletedProcess(argv, 0 if self.loaded else 113, "",
                                               "" if self.loaded else "Could not find service")
        return subprocess.run(argv, **kw)

    def drift(self):
        return qtr.drift(self.plist, self.runner)

    def refresh(self, fix=True):
        return qtr.refresh(fix, self.root, self.plist, self.config, self.runner)

    def installer_calls(self):
        return [c for c in self.calls if c[0] == "/bin/bash"]


class DriftTests(Fixture):
    def test_states(self) -> None:
        self.assertEqual(self.drift()["state"], "not_installed")
        self.install_old()
        self.assertEqual(self.drift()["state"], "drift")
        self.assertIn("a copied tick", self.drift()["detail"])
        self.loaded = False
        self.assertEqual(self.drift()["state"], "drift_unloaded")
        value = plistlib.loads(self.plist.read_bytes())
        value["ProgramArguments"] = qtr.desired_program(self.plist)
        self.plist.write_bytes(plistlib.dumps(value))
        self.assertEqual(self.drift()["state"], "current")

    def test_the_status_line_names_a_stale_copy(self) -> None:
        self.assertIn("queue tick: DRIFT", qtr.status_line({"state": "drift", "detail": "x"}))
        self.assertIn("NOT LOADED", qtr.status_line({"state": "drift_unloaded", "detail": "x"}))
        self.assertIsNone(qtr.status_line({"state": "current"}))
        self.assertIsNone(qtr.status_line({"state": "not_installed"}))

    @testing_support.requires_tomllib
    def test_pool_status_names_a_stale_copy(self) -> None:
        import macos_fleet_lanes as lanes
        with mock.patch.object(qtr, "status_line", return_value="queue tick: DRIFT (x)"):
            self.assertIn("queue tick: DRIFT (x)", lanes.tool_freshness_summary()["lines"])

    def test_the_template_is_what_drift_calls_current(self) -> None:
        raw = (HERE.parent / "launchd" / f"{qtr.LABEL}.plist.template").read_text()
        value = plistlib.loads(raw.replace("$HOME", str(self.home)).encode())
        self.assertEqual(value["ProgramArguments"], qtr.desired_program(self.plist))


class RefreshTests(Fixture):
    def test_a_drifted_loaded_tick_is_reinstalled_with_its_own_settings(self) -> None:
        self.install_old(apply="1")
        out = self.refresh()
        self.assertEqual(out["state"], "refreshed", out)
        self.assertEqual(self.drift()["state"], "current")
        args = (self.tmp / "installer-args").read_text().split()
        self.assertEqual(args, ["--gh-cli", "/x/ghapp", "--mode", "reap", "--install"])

    def test_a_dry_run_tick_stays_dry_run(self) -> None:
        self.install_old(apply="0")
        self.refresh()
        self.assertIn("dry-run", (self.tmp / "installer-args").read_text())

    def test_an_unloaded_tick_is_never_switched_back_on(self) -> None:
        # m1 and m5 carry a July copy that someone unloaded.
        self.install_old()
        self.loaded = False
        self.assertEqual(self.refresh()["state"], "drift_unloaded")
        self.assertEqual(self.installer_calls(), [])

    def test_settings_it_cannot_read_are_refused_not_guessed(self) -> None:
        self.install_old(apply=None)
        out = self.refresh()
        self.assertEqual(out["state"], "refresh_refused")
        self.assertIn("SHIPYARD_TICK_APPLY", out["detail"])
        self.assertEqual(self.installer_calls(), [])

    def test_current_and_dry_run_do_nothing(self) -> None:
        # Controls: nothing drifted, or fix=False, runs no installer.
        self.install_old()
        self.assertEqual(self.refresh(fix=False)["state"], "would_refresh")
        self.assertEqual(self.installer_calls(), [])
        self.refresh()
        self.calls.clear()
        self.assertEqual(self.refresh()["state"], "current")
        self.assertEqual(self.installer_calls(), [])

    def test_a_failing_installer_is_reported(self) -> None:
        self.install_old()
        (self.root / "scripts" / qtr.INSTALLER).write_text("#!/bin/bash\necho no >&2\nexit 2\n")
        out = self.refresh()
        self.assertEqual(out["state"], "refresh_failed")
        self.assertIn("installer exit 2", out["detail"])


class WatchdogPassTests(unittest.TestCase):
    def test_the_heal_pass_logs_a_reinstall_and_a_failure_but_not_a_quiet_host(self) -> None:
        cases = {"current": None, "not_installed": None, "drift_unloaded": None,
                 "refreshed": "queue tick reinstalled", "refresh_failed": "WARN queue tick"}
        for state, expected in cases.items():
            with self.subTest(state=state), \
                    mock.patch.object(qtr, "refresh", return_value={"state": state, "files": [],
                                                                    "args": [], "detail": "d"}):
                line = wd.queue_tick_pass()
                if expected is None:
                    self.assertIsNone(line)
                else:
                    self.assertIn(expected, line)

    def test_the_heal_pass_calls_it(self) -> None:
        body = (HERE / "tartci_launchd_watchdog.py").read_text()
        main = body[body.index("def main("):]
        self.assertIn("tick_line = queue_tick_pass()", main)


if __name__ == "__main__":
    unittest.main()
