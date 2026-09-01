#!/usr/bin/env python3
"""Behavioral regressions for bounded observation process ownership."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ParentSignalCleanupTests(unittest.TestCase):
    def test_sigterm_reaps_private_observation_group(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pid_file = Path(raw) / "child.pid"
            driver = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys; "
                        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
                        "from bounded_subprocess import run_bounded; "
                        "run_bounded([sys.executable, '-c', "
                        f"\"import os,time; open({str(pid_file)!r},'w').write(str(os.getpid())); time.sleep(60)\""
                        "], timeout=60, operation='signal-test')"
                    ),
                ],
                cwd=ROOT,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not pid_file.exists():
                time.sleep(0.01)
            self.assertTrue(pid_file.exists(), "observation child never started")
            child_pid = int(pid_file.read_text())

            driver.send_signal(signal.SIGTERM)
            self.assertEqual(driver.wait(timeout=5), -signal.SIGTERM)

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and process_exists(child_pid):
                time.sleep(0.01)
            self.assertFalse(process_exists(child_pid), "observation child survived parent SIGTERM")


if __name__ == "__main__":
    unittest.main()
