#!/usr/bin/env python3
"""Tests for the shared immutable Pulp source resolver and its consumers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "providers/common/pulp-source-pin.py"
SPEC = importlib.util.spec_from_file_location("pulp_source_pin", MODULE)
assert SPEC is not None and SPEC.loader is not None
source_pin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(source_pin)


class PulpSourcePinTests(unittest.TestCase):
    def test_real_pulp_provisioners_share_linux_manifest_resolver(self) -> None:
        literal = "21fbc9da9214d4e6279fa2e8b4e70df9bed8662a"
        for relative in (
            "providers/tart-linux/provision.sh",
            "providers/proxmox-linux/bake-pulp-golden.sh",
        ):
            source = (ROOT / relative).read_text()
            self.assertIn('MANIFEST="$ROOT/manifests/pulp.linux.toml"', source)
            self.assertIn("pulp-source-pin.py", source)
            self.assertNotIn(literal, source)
            self.assertNotIn("  --pulp-sha)", source)
            self.assertNotIn("PULP_SHA=\"21fbc", source)

    def test_resolver_rejects_a_branch_like_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "bad.toml"
            manifest.write_text(
                """
[source]
repository = "https://github.com/Generous-Corp/pulp"
commit = "main"
manifest = "tools/deps/manifest.json"
"""
            )
            with self.assertRaisesRegex(ValueError, "immutable lowercase 40-hex"):
                source_pin.resolve(manifest)

    def test_real_linux_manifest_resolves_exact_source(self) -> None:
        repository, commit = source_pin.resolve(
            ROOT / "manifests/pulp.linux.toml",
            "chrome/m153",
            "baked-provider-only",
        )
        self.assertEqual(repository, "https://github.com/Generous-Corp/pulp")
        self.assertRegex(commit, r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
