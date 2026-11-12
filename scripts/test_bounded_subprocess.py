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
                        # Write then rename: open(..., 'w') creates the file
                        # before the pid is in it, and a loaded host let the
                        # reader see it empty (int('') in a full-suite run).
                        f"\"import os,time; open({str(pid_file)!r}+'.tmp','w').write(str(os.getpid())); "
                        f"os.replace({str(pid_file)!r}+'.tmp',{str(pid_file)!r}); time.sleep(60)\""
                        "], timeout=60, operation='signal-test')"
                    ),
                ],
                cwd=ROOT,
            )
            # Interpreter start-up alone can take seconds on a loaded host.
            deadline = time.monotonic() + 30
            child_pid = None
            while time.monotonic() < deadline and child_pid is None:
                text = pid_file.read_text() if pid_file.exists() else ""
                child_pid = int(text) if text.strip().isdigit() else None
                if child_pid is None:
                    time.sleep(0.01)
            self.assertIsNotNone(child_pid, "observation child never started")

            driver.send_signal(signal.SIGTERM)
            self.assertEqual(driver.wait(timeout=5), -signal.SIGTERM)

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and process_exists(child_pid):
                time.sleep(0.01)
            self.assertFalse(process_exists(child_pid), "observation child survived parent SIGTERM")


if __name__ == "__main__":
    unittest.main()
