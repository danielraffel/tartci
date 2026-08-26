#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("tart_source_fingerprint.py")


class TartSourceFingerprintTests(unittest.TestCase):
    def test_fingerprint_changes_when_disk_identity_changes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            image = Path(td) / "vms" / "golden:latest"
            image.mkdir(parents=True)
            (image / "config.json").write_text("{}")
            (image / "disk.img").write_bytes(b"disk")
            (image / "nvram.bin").write_bytes(b"nvram")
            first = subprocess.check_output(
                [str(SCRIPT), "--tart-home", td, "--name", "golden:latest"], text=True
            )
            (image / "disk.img").write_bytes(b"changed")
            second = subprocess.check_output(
                [str(SCRIPT), "--tart-home", td, "--name", "golden:latest"], text=True
            )
            self.assertNotEqual(json.loads(first), json.loads(second))

    def test_unsafe_name_is_rejected(self) -> None:
        result = subprocess.run(
            [str(SCRIPT), "--tart-home", "/tmp", "--name", "../escape"],
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
