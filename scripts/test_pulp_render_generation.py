#!/usr/bin/env python3
"""Focused fail-closed tests for Pulp golden render identity receipts."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "providers/common/pulp-render-generation.py"
SPEC = importlib.util.spec_from_file_location("pulp_render_generation", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
generation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generation)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PulpRenderGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / "pulp"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True
        )
        (self.repo / "tools/deps").mkdir(parents=True)
        (self.repo / "tools/scripts").mkdir(parents=True)
        (self.repo / "external/skia-build").mkdir(parents=True)
        (self.repo / "external/v8-build/linux-x64").mkdir(parents=True)
        self.skia_receipt = self.repo / "external/skia-build/.skia-generation-manifest.json"
        self.v8_receipt = self.repo / "external/v8-build/linux-x64/.v8-generation-manifest.json"
        self.skia_receipt.write_text('{"deep":"skia"}\n')
        self.v8_receipt.write_text('{"deep":"v8"}\n')
        self.capability = Path(self.temp.name) / "capability.json"
        self.manifest = {
            "dependencies": [
                {
                    "name": "Skia",
                    "version": "chrome/m153",
                    "determinism": {
                        "skia_commit": "1" * 40,
                        "built_dawn": "2" * 40,
                        "release_assets": {
                            "linux-x64": {"sha256": "3" * 64}
                        },
                    },
                },
                {
                    "name": "V8",
                    "version": "v8-m153-test",
                    "determinism": {
                        "milestone": 153,
                        "pair_kind": "chromium-milestone",
                        "paired_skia": "1" * 40,
                        "paired_dawn": "2" * 40,
                        "release_assets": {
                            "linux-x64": {"sha256": "4" * 64}
                        },
                    },
                },
            ]
        }
        manifest_path = self.repo / "tools/deps/manifest.json"
        manifest_path.write_text(json.dumps(self.manifest))
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "fixture"], check=True
        )
        self.pulp_sha = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        self.write_capability()
        self.args = argparse.Namespace(
            repo=self.repo,
            pulp_sha=self.pulp_sha,
            pulp_repository="https://github.com/Generous-Corp/pulp",
            platform="linux-x64",
            capability_result=self.capability,
            v8_disposition="baked-provider-only",
            parent_kind="proxmox-template",
            parent_identity="9005",
            parent_digest="5" * 64,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_capability(self, **changes: object) -> None:
        result: dict[str, object] = {
            "schema_version": 1,
            "status": "pass",
            "platform": "linux-x64",
            "asset_sha256": "3" * 64,
            "generation_receipt_sha256": digest(self.skia_receipt),
            "probes": [{"compile": "pass", "link": "pass", "run": "pass"}],
        }
        result.update(changes)
        self.capability.write_text(json.dumps(result))

    def modules(self, skia_valid: bool = True, v8_valid: bool = True):
        skia = types.SimpleNamespace(
            GENERATION_RECEIPT=".skia-generation-manifest.json",
            cache_generation_valid=lambda *_: skia_valid,
        )
        v8 = types.SimpleNamespace(
            GENERATION_RECEIPT=".v8-generation-manifest.json",
            generation_valid=lambda *_: v8_valid,
        )
        return [skia, v8]

    def verify(self, **module_validity: bool):
        with mock.patch.object(
            generation,
            "load_module",
            side_effect=self.modules(**module_validity),
        ):
            return generation.verify(self.args)

    def test_success_binds_exact_source_provider_receipts_parent_and_v8(self) -> None:
        receipt = self.verify()
        self.assertEqual(receipt["pulp"]["commit"], self.pulp_sha)
        self.assertEqual(receipt["parent"]["identity"], "9005")
        self.assertEqual(receipt["skia_dawn"]["release"], "chrome/m153")
        self.assertEqual(receipt["skia_dawn"]["built_dawn"], "2" * 40)
        self.assertEqual(
            receipt["skia_dawn"]["generation_receipt_sha256"],
            digest(self.skia_receipt),
        )
        self.assertEqual(receipt["v8"]["disposition"], "baked-provider-only")
        self.assertEqual(
            receipt["v8"]["generation_receipt_sha256"], digest(self.v8_receipt)
        )

    def test_planted_old_m149_manifest_fails_before_receipt(self) -> None:
        self.manifest["dependencies"][0]["version"] = "chrome/m149"
        (self.repo / "tools/deps/manifest.json").write_text(json.dumps(self.manifest))
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "plant m149"], check=True
        )
        self.args.pulp_sha = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        with self.assertRaisesRegex(ValueError, "exact chrome/m153"):
            self.verify()

    def test_wrong_pulp_head_fails_before_provider_validation(self) -> None:
        self.args.pulp_sha = "f" * 40
        with self.assertRaisesRegex(ValueError, "immutable requested SHA"):
            self.verify()

    def test_shallow_skia_stamp_cannot_replace_deep_receipt(self) -> None:
        with self.assertRaisesRegex(ValueError, "deep receipt validator"):
            self.verify(skia_valid=False)

    def test_header_only_capability_claim_is_rejected(self) -> None:
        self.write_capability(probes=[])
        with self.assertRaisesRegex(ValueError, "no compile/link/run probes"):
            self.verify()

    def test_mismatched_v8_pair_is_rejected(self) -> None:
        self.manifest["dependencies"][1]["determinism"]["paired_dawn"] = "9" * 40
        (self.repo / "tools/deps/manifest.json").write_text(json.dumps(self.manifest))
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", "plant V8 mismatch"],
            check=True,
        )
        self.args.pulp_sha = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        with self.assertRaisesRegex(ValueError, "paired Dawn"):
            self.verify()

    def test_invalid_v8_generation_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "V8 extracted generation"):
            self.verify(v8_valid=False)


class PulpRenderManifestTests(unittest.TestCase):
    def test_macos_and_linux_share_one_immutable_m153_source(self) -> None:
        import tomllib

        manifests = []
        for name in ("pulp.macos.toml", "pulp.linux.toml"):
            with (ROOT / "manifests" / name).open("rb") as handle:
                manifests.append(tomllib.load(handle))
        sources = [item["source"] for item in manifests]
        self.assertEqual(sources[0], sources[1])
        self.assertRegex(sources[0]["commit"], r"^[0-9a-f]{40}$")
        for manifest in manifests:
            self.assertEqual(manifest["skia"]["release"], "chrome/m153")
            self.assertEqual(manifest["v8"]["disposition"], "baked-provider-only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
