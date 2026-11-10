#!/usr/bin/env python3
"""Pin the release-critical Rust and Xcode contract in Pulp's macOS golden manifest."""

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
        self.assertEqual(toolchain["rust"], "1.97.1")
        self.assertIn("x86_64-apple-darwin", toolchain["rust_targets"])
        self.assertIn("rustup", self.manifest["brew"]["packages"])

    def test_xcode_and_guest_macos_match_the_fleet_hosts(self) -> None:
        # The golden runs the Xcode every fleet host runs, so one ccache serves
        # hosts and VMs (compiler_check=content hashes the compiler). Xcode
        # 27.2 needs macOS 26.6+, so the base moves with it, pinned by digest.
        toolchain = self.manifest["toolchain"]
        self.assertEqual(("27.2", "27B52"), (toolchain["xcode"], toolchain["xcode_build"]))
        self.assertEqual(("27.2", "26B123"), (toolchain["macos"], toolchain["macos_build"]))
        base = self.manifest["base"]
        self.assertRegex(base, r"^ghcr\.io/cirruslabs/macos-golden-gate-base@sha256:[0-9a-f]{64}$")

    def test_toolchain_block_is_generated_from_pulp(self) -> None:
        # Pulp's tools/toolchain/generate.py owns these keys; a floating
        # selector here means the block was hand-edited or is from an old run.
        toolchain = self.manifest["toolchain"]
        for key in ("cmake", "ninja", "python", "node", "tart", "clang_format"):
            self.assertRegex(toolchain[key], r"^[0-9]+\.[0-9]+\.[0-9]+$", key)

    def test_generated_toolchain_pins_the_source_revision(self) -> None:
        # The generated block is copied from the companion Pulp change. Keep
        # its immutable source revision alongside the digest so a manifest
        # cannot claim new pins without naming the revision they came from.
        # That revision lives in [toolchain_source], never in [source]: [source]
        # selects the render dependencies, and moving it to the toolchain
        # revision silently changes the baked Skia (c368b19e pins chrome/m158).
        toolchain_source = self.manifest["toolchain_source"]
        self.assertEqual(
            toolchain_source["commit"],
            "c368b19e47e334be8a29c59e7108794a5570e3f2",
        )
        self.assertEqual(toolchain_source["manifest"], "tools/toolchain/manifest.toml")
        self.assertEqual(
            toolchain_source["repository"], self.manifest["source"]["repository"]
        )
        self.assertEqual(self.manifest["source"]["manifest"], "tools/deps/manifest.json")
        text = (ROOT / "manifests/pulp.macos.toml").read_text()
        self.assertIn(
            "source_sha256 = "
            "7b695d2cbaf8c4a0eb572c673416b76cd728c23ef50c4126ffb0f621d7926959",
            text,
        )

    def test_ccache_capacity_preserves_cross_head_warmth(self) -> None:
        self.assertEqual(self.manifest["caches"]["ccache_max"], "40G")


if __name__ == "__main__":
    unittest.main(verbosity=2)
