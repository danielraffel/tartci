#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TartImageSyncTests(unittest.TestCase):
    def test_plan_prefers_reachable_lan_and_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bindir = Path(td) / "bin"
            bindir.mkdir()
            log = Path(td) / "ssh.log"
            ssh = bindir / "ssh"
            ssh.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYNC_TEST_LOG\"\ncase \" $* \" in *' m1-lan '*) exit 0;; *) exit 1;; esac\n")
            ssh.chmod(0o755)
            env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", SYNC_TEST_LOG=str(log))
            result = subprocess.run(
                [str(ROOT / "tartci"), "tart-image-sync", "--name", "golden:latest",
                 "--destination", "m1-lan", "--fallback", "m1",
                 "--source-tart-home", "/Volumes/Workshop/VMs",
                 "--destination-tart-home", "/Users/test/VMs",
                 "--staging", "/tmp/tartci-test-stage"],
                text=True, capture_output=True, env=env, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("destination=m1-lan", result.stdout)
            self.assertIn("plan_only=true", result.stdout)
            self.assertNotIn("rsync", log.read_text())

    def test_import_requires_apply(self) -> None:
        result = subprocess.run(
            [str(ROOT / "tartci"), "tart-image-sync", "--name", "x",
             "--destination", "m1", "--source-tart-home", "/a",
             "--destination-tart-home", "/b", "--import"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --apply", result.stderr)

    def test_image_name_cannot_escape_tart_store(self) -> None:
        result = subprocess.run(
            [str(ROOT / "tartci"), "tart-image-sync", "--name", "../golden",
             "--destination", "m1", "--source-tart-home", "/a",
             "--destination-tart-home", "/b"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("image name", result.stderr)


if __name__ == "__main__":
    unittest.main()
