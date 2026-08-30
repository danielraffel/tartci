#!/usr/bin/env python3
"""Fail-closed tests for Pulp macOS golden preparation/readiness."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests/pulp.macos.toml"
MODULE = ROOT / "providers/common/pulp-macos-readiness.py"
PROVISION = ROOT / "providers/tart-macos/provision.sh"
SPEC = importlib.util.spec_from_file_location("pulp_macos_readiness", MODULE)
assert SPEC is not None and SPEC.loader is not None
readiness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(readiness)


class PulpMacosReadinessTests(unittest.TestCase):
    def test_real_manifest_is_truthfully_unready_with_exact_requirements(self) -> None:
        payload = readiness.report(MANIFEST)
        self.assertEqual(payload["status"], "unready")
        required = payload["required"]
        self.assertRegex(required["pulp_commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(required["skia_release"], "chrome/m153")
        self.assertEqual(required["v8_disposition"], "baked-provider-only")

    def test_pin_bump_remains_unready_instead_of_accepting_stale_golden(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            changed = Path(temp) / "pulp.macos.toml"
            source = MANIFEST.read_text().replace(
                'commit     = "21fbc9da9214d4e6279fa2e8b4e70df9bed8662a"',
                'commit     = "2222222222222222222222222222222222222222"',
            )
            changed.write_text(source)
            payload = readiness.report(changed)
            self.assertEqual(payload["required"]["pulp_commit"], "2" * 40)
            self.assertEqual(payload["status"], "unready")

    def test_status_only_flip_cannot_claim_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            changed = Path(temp) / "pulp.macos.toml"
            changed.write_text(
                MANIFEST.read_text().replace('status = "unready"', 'status = "ready"')
            )
            with self.assertRaisesRegex(ValueError, "receipt-backed"):
                readiness.report(changed)

    def test_operator_surface_reports_requirements_and_exits_nonzero_without_tart(
        self,
    ) -> None:
        result = subprocess.run(
            ["/bin/bash", str(PROVISION), "pulp-readiness"],
            text=True,
            capture_output=True,
            env={
                "HOME": os.environ["HOME"],
                "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            },
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "unready")
        self.assertEqual(payload["required"]["skia_release"], "chrome/m153")


if __name__ == "__main__":
    unittest.main(verbosity=2)
