#!/usr/bin/env python3
"""Regression tests for storage-health timeout process ownership."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import storage_health  # noqa: E402


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StorageHealthProcessGroupTests(unittest.TestCase):
    def test_timeout_reaps_descendant_process_group(self) -> None:
        """A timed-out worker must not leave its probe descendant behind."""
        with tempfile.TemporaryDirectory() as raw:
            pid_file = Path(raw) / "grandchild.pid"
            program = (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)'])\n"
                "open(sys.argv[1], 'w').write(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            worker = subprocess.Popen(
                [sys.executable, "-c", program, str(pid_file)],
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and (not pid_file.exists() or not pid_file.read_text()):
                    time.sleep(0.01)
                self.assertTrue(pid_file.exists() and pid_file.read_text(), "worker did not start its descendant")
                grandchild = int(pid_file.read_text())

                self.assertTrue(storage_health.reap_after_kill(worker))
                self.assertFalse(process_exists(worker.pid), "worker leader was not reaped")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and process_exists(grandchild):
                    time.sleep(0.01)
                self.assertFalse(process_exists(grandchild), "probe descendant survived timeout")
            finally:
                # Keep the test bounded even if an assertion fails before the
                # helper gets a chance to terminate the private session.
                if process_exists(worker.pid):
                    storage_health.reap_after_kill(worker)

    def test_timeout_reaps_descendant_in_its_own_session(self) -> None:
        """The real probe child runs in its own session, outside the worker's
        group. Killing only the worker's group orphaned it to launchd, still
        blocked in open(); m3 held five such probes for up to 24 minutes."""
        with tempfile.TemporaryDirectory() as raw:
            pid_file = Path(raw) / "grandchild.pid"
            program = (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(60)'], start_new_session=True)\n"
                "open(sys.argv[1], 'w').write(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            worker = subprocess.Popen(
                [sys.executable, "-c", program, str(pid_file)],
                start_new_session=True,
            )
            grandchild = 0
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and (not pid_file.exists() or not pid_file.read_text()):
                    time.sleep(0.01)
                self.assertTrue(pid_file.exists() and pid_file.read_text(), "worker did not start its descendant")
                grandchild = int(pid_file.read_text())
                self.assertEqual(os.getpgid(grandchild), grandchild, "descendant is not in its own session")

                self.assertTrue(storage_health.reap_after_kill(worker))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and process_exists(grandchild):
                    time.sleep(0.01)
                self.assertFalse(process_exists(grandchild), "probe in its own session survived the worker timeout")
            finally:
                if process_exists(worker.pid):
                    storage_health.reap_after_kill(worker)
                if grandchild and process_exists(grandchild):
                    os.kill(grandchild, 9)

    def test_timeout_spawns_private_session(self) -> None:
        source = (ROOT / "scripts" / "storage_health.py").read_text()
        self.assertGreaterEqual(source.count("start_new_session=True"), 2)
        self.assertIn("os.killpg(pgid, signal.SIGKILL)", source)


if __name__ == "__main__":
    unittest.main()
