#!/usr/bin/env python3
"""Behavioral regressions for the sealed resident launcher app."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/macos/tartci-launcher/main.swift"


@unittest.skipUnless(os.uname().sysname == "Darwin", "macOS native launcher")
class LauncherRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.app = cls.root / "TartCILauncher.app"
        cls.launcher = cls.app / "Contents/MacOS/tartci-launcher"
        support = cls.app / "Contents/Resources/support"
        cls.launcher.parent.mkdir(parents=True)
        support.mkdir(parents=True)
        cls.pid_file = cls.root / "descendant.pid"
        lanes = {}
        for lane, mode in (
            ("exit-zero", "exit-zero"),
            ("exit-one", "exit-one"),
            ("spawn-and-exit", "spawn-and-exit"),
            ("term-group", "term-group"),
        ):
            lanes[lane] = {"environment": {
                "HOME": str(cls.root),
                "PATH": "/usr/bin:/bin",
                "TART_HOME": "/Volumes/Workshop/VMs",
                "TARTCI_TEST_MODE": mode,
                "TARTCI_TEST_PID_FILE": str(cls.pid_file),
            }}
        (cls.app / "Contents/Resources/lanes.json").write_text(
            json.dumps({"schema": 1, "lanes": lanes})
        )
        launch = support / ".tartci-launch"
        launch.write_text(
            "#!/bin/sh\n"
            "case \"$TARTCI_TEST_MODE\" in\n"
            "  exit-zero) exit 0 ;;\n"
            "  exit-one) exit 1 ;;\n"
            "  spawn-and-exit) trap '' TERM; while :; do sleep 1; done & echo $! > \"$TARTCI_TEST_PID_FILE\"; exit 0 ;;\n"
            "  term-group) sh -c 'trap \"\" TERM INT HUP; while :; do sleep 1; done' & echo $! > \"$TARTCI_TEST_PID_FILE\"; trap 'exit 0' TERM INT HUP; while :; do sleep 1; done ;;\n"
            "  *) exit 99 ;;\n"
            "esac\n"
        )
        launch.chmod(0o755)
        subprocess.run([
            "xcrun", "swiftc", "-O", "-D", "TARTCI_TESTING",
            "-framework", "Security", str(SOURCE), "-o", str(cls.launcher),
        ], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.pid_file.unlink(missing_ok=True)

    def run_lane(self, lane: str, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.launcher), "--lane", lane], check=False, **kwargs
        )

    def test_propagates_child_exit_and_discards_ambient_policy(self) -> None:
        poisoned = dict(os.environ)
        poisoned.update({
            "TARTCI_TEST_MODE": "exit-one",
            "PULP_UNTRUSTED": "1",
            "SHIPYARD_UNTRUSTED": "1",
            "PATH": "/tmp/not-real",
        })
        self.assertEqual(self.run_lane("exit-zero", env=poisoned).returncode, 0)
        self.assertEqual(self.run_lane("exit-one").returncode, 1)

    def test_rejects_arbitrary_execution_and_unknown_lane(self) -> None:
        self.assertEqual(subprocess.run([
            str(self.launcher), "--", "/usr/bin/true",
        ], check=False).returncode, 64)
        self.assertEqual(self.run_lane("not-signed").returncode, 77)
        self.assertEqual(subprocess.run([
            str(self.launcher), "--probe-store", "/tmp",
        ], check=False).returncode, 64)

    def test_kills_descendant_after_direct_child_exits(self) -> None:
        result = self.run_lane("spawn-and-exit", timeout=5)
        self.assertEqual(result.returncode, 0)
        descendant = int(self.pid_file.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"descendant {descendant} survived launcher exit")

    def test_term_kills_full_child_group(self) -> None:
        launcher = subprocess.Popen([
            str(self.launcher), "--lane", "term-group",
        ])
        try:
            deadline = time.monotonic() + 3
            while not self.pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(self.pid_file.exists())
            descendant = int(self.pid_file.read_text())
            launcher.send_signal(signal.SIGTERM)
            self.assertEqual(launcher.wait(timeout=5), 0)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.kill(descendant, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail(f"descendant {descendant} survived TERM cleanup")
        finally:
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait()


if __name__ == "__main__":
    unittest.main()
