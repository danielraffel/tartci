#!/usr/bin/env python3
"""Pin the release-critical Rust contract in Pulp's macOS golden manifest."""

from __future__ import annotations

import pathlib
import unittest

try:
    import tomllib
except ModuleNotFoundError:  # macOS system Python before 3.11
    tomllib = None


ROOT = pathlib.Path(__file__).resolve().parents[1]


@unittest.skipIf(tomllib is None, "tomllib requires Python 3.11+")
class PulpMacosManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with (ROOT / "manifests/pulp.macos.toml").open("rb") as handle:
            cls.manifest = tomllib.load(handle)

    def test_release_rust_toolchain_is_baked(self) -> None:
        toolchain = self.manifest["toolchain"]
        self.assertEqual(toolchain["rust"], "stable")
        self.assertIn("x86_64-apple-darwin", toolchain["rust_targets"])
        self.assertIn("rustup", self.manifest["brew"]["packages"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
