#!/usr/bin/env python3
"""Recovery drills: the real recovery code, run end to end against a staged fault.

Unit tests elsewhere prove each piece against in-process fakes. These drills
run the shipped entry points as subprocesses in a temporary HOME, so nothing
on the machine running them changes and no gate host is taken out of service:

  host-off   `tartci_launchd_watchdog.py` (the 5-minute heal pass) finds a
             host a failed self-update left OFF and puts it back with
             `pool on`, through the installed-shim path it uses on a host.
             The shim is a stand-in that records the call and writes the
             pool state the way `tartci pool on` does.
  sensor     host_vitals_sensor.refresh() reinstalls a drifted host-vitals
             sensor with Pulp's REAL installer. It needs a Pulp
             tools/scripts checkout (the reclaim pass keeps one at
             ~/.tartci/state/reclaim/pulp-reapers, or set
             TARTCI_PULP_TOOLS_SCRIPTS) and skips, saying so, without one.
             `launchctl` is a stand-in on PATH: a real bootstrap from a
             temporary HOME would register the temp plist in the real gui
             domain and shadow the real sensor (fleet_reasons.json,
             launchd_registrations).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import host_vitals_sensor as hvs  # noqa: E402


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@unittest.skipIf(sys.version_info < (3, 11), "the watchdog needs a tomllib interpreter")
class HostOffDrill(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="tartci-host-off-drill-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.sdir = self.home / ".tartci" / "state" / "self-update"
        self.sdir.mkdir(parents=True)
        self.pool = self.home / ".config" / "tartci" / "pool-state"
        self.pool.parent.mkdir(parents=True)
        self.calls = self.home / "shim-calls"
        self.fail_first = self.home / "fail-first-pool-on"
        # The installed shim `_installed_tartci()` resolves on a host.
        _executable(self.home / ".local" / "bin" / "tartci", f"""#!/bin/bash
echo "$*" >> {self.calls}
if [ "$1 $2" = "pool on" ]; then
  if [ -e {self.fail_first} ]; then
    rm -f {self.fail_first}
    echo "pool on refused: signed launch helper could not prove external-volume access" >&2
    exit 9
  fi
  printf 'on\\n' > {self.pool}
  echo "state: on"
fi
exit 0
""")
        # Nothing the pass might shell out to reaches the network or a VM.
        self.bin = self.home / "bin"
        for tool in ("ghapp", "gh", "tart", "launchctl"):
            _executable(self.bin / tool, "#!/bin/sh\nexit 1\n")

    def leave_off(self, minutes_ago: float) -> float:
        at = time.time() - minutes_ago * 60
        (self.sdir / "last.json").write_text(json.dumps({
            "at": _iso(at), "status": "failed", "host_off": True, "pool_state": "off",
            "target": "a" * 40,
            "error": "pool on failed: the signed launch helper's volume probe timed out"}))
        self.pool.write_text("off\n")
        os.utime(self.pool, (at, at))
        return at

    def heal_pass(self) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(self.home),
            "TARTCI_HOME": str(self.home / ".tartci"),
            "TARTCI_POOL_STATE_FILE": str(self.pool),
            "TARTCI_HOST_OFF_ISSUE": "0",
            "PATH": f"{self.bin}:/usr/bin:/bin",
        }
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [sys.executable, str(HERE / "tartci_launchd_watchdog.py"),
             "--stale-log-seconds", "4500", "--launch-agents-dir", str(agents),
             "--fleet-config", str(self.home / "absent-profile.toml")],
            capture_output=True, text=True, env=env, timeout=120)

    def shim_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def events(self) -> str:
        path = self.sdir / "events.jsonl"
        return path.read_text() if path.exists() else ""

    def test_a_host_left_off_is_put_back_in_service_by_the_heal_pass(self) -> None:
        self.leave_off(minutes_ago=20)
        proc = self.heal_pass()
        output = proc.stdout + proc.stderr
        self.assertIn("pool on", self.shim_calls(), output)
        self.assertEqual(self.pool.read_text().strip(), "on", output)
        self.assertIn("pool on succeeded", output)
        last = json.loads((self.sdir / "last.json").read_text())
        self.assertFalse(last["host_off"], last)
        self.assertEqual(last["recovered_by"], "launchd-watchdog")
        # Recovered in the same pass, so it was never reported as an outage.
        self.assertIn("pool on succeeded after 20 min OFF", self.events())
        self.assertNotIn("host_off_unexpected", self.events())

    def test_a_failed_pool_on_is_retried_after_the_backoff_and_then_recovers(self) -> None:
        self.leave_off(minutes_ago=20)
        self.fail_first.touch()
        first = self.heal_pass()
        self.assertIn("WARN host-off", first.stdout, first.stdout + first.stderr)
        self.assertEqual(self.pool.read_text().strip(), "off")
        self.assertIn("host_off_recovery_failed", self.events())
        # Still OFF past the 15-minute bound: loud, once.
        self.assertEqual(self.events().count("host_off_unexpected"), 1)
        # The next pass inside the backoff does not hammer `pool on`...
        self.heal_pass()
        self.assertEqual(self.shim_calls().count("pool on"), 1)
        # ...and the first pass after it does, and recovers.
        record = json.loads((self.sdir / "recovery.json").read_text())
        record["attempted_at"] = _iso(time.time() - 6 * 60)
        (self.sdir / "recovery.json").write_text(json.dumps(record))
        self.heal_pass()
        self.assertEqual(self.shim_calls().count("pool on"), 2)
        self.assertEqual(self.pool.read_text().strip(), "on")

    def test_a_host_someone_turned_off_is_left_off(self) -> None:
        # Control: the same staged update failure, but a person ran `pool off`
        # afterwards. The drill must show recovery is not blind `pool on`.
        at = self.leave_off(minutes_ago=20)
        later = at + 10 * 60
        os.utime(self.pool, (later, later))
        proc = self.heal_pass()
        self.assertNotIn("pool on", self.shim_calls(), proc.stdout + proc.stderr)
        self.assertEqual(self.pool.read_text().strip(), "off")


def pulp_tools_scripts() -> Path | None:
    for candidate in (os.environ.get("TARTCI_PULP_TOOLS_SCRIPTS"),
                      str(Path.home() / ".tartci" / "state" / "reclaim" / "pulp-reapers"
                          / "tools" / "scripts")):
        if candidate and (Path(candidate) / hvs.INSTALLER).is_file():
            return Path(candidate)
    return None


@unittest.skipIf(pulp_tools_scripts() is None,
                 "no Pulp tools/scripts checkout with install_host_vitals_sensor.sh "
                 "(set TARTCI_PULP_TOOLS_SCRIPTS); the real-installer drill did not run")
class SensorRefreshDrill(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="tartci-sensor-drill-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.source = pulp_tools_scripts()
        self.bin = self.home / ".local" / "bin"
        self.plist = self.home / "Library" / "LaunchAgents" / f"{hvs.LABEL}.plist"
        self.launchctl_log = self.home / "launchctl-calls"
        stubs = self.home / "stubs"
        _executable(stubs / "launchctl", f"#!/bin/sh\necho \"$*\" >> {self.launchctl_log}\nexit 0\n")
        self.env = {"HOME": str(self.home), "PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin"}

    def refresh(self) -> dict:
        saved = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        try:
            return hvs.refresh(self.source, True, bin_dir=self.bin, plist=self.plist)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_a_drifted_sensor_is_reinstalled_by_pulps_own_installer(self) -> None:
        # Stage the 2026-09-25 shape: an installed, loaded sensor whose scripts
        # predate origin/main.
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text("<plist/>")
        for name in hvs.SENSOR_FILES:
            _executable(self.bin / name, f"#!/bin/sh\n# {name} from 2026-09-25\n")
        before = hvs.drift(self.source, self.bin, self.plist)
        self.assertEqual(before["state"], "drift", before)
        out = self.refresh()
        self.assertEqual(out["state"], "refreshed", out)
        self.assertEqual(hvs.drift(self.source, self.bin, self.plist)["state"], "current")
        for name in hvs.SENSOR_FILES:
            self.assertEqual((self.bin / name).read_bytes(), (self.source / name).read_bytes())
        self.assertIn(str(self.bin / "host_vitals_sensor.sh"), self.plist.read_text())
        # The installer re-registered the agent, through the stand-in only.
        self.assertIn("bootstrap", self.launchctl_log.read_text())

    def test_a_current_sensor_is_left_alone(self) -> None:
        # Control: with nothing drifted the installer never runs.
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text("<plist/>")
        self.bin.mkdir(parents=True)
        for name in hvs.SENSOR_FILES:
            shutil.copy(self.source / name, self.bin / name)
        self.assertEqual(self.refresh()["state"], "current")
        self.assertFalse(self.launchctl_log.exists())


if __name__ == "__main__":
    unittest.main()
