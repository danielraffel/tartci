#!/usr/bin/env python3
"""Tests for upstream Tart discovery and reviewed pin generation."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tart_pin
import tart_pin_update as updater


TART_SHA = "a" * 64
SOFTNET_SHA = "b" * 64


class TartPinUpdateTests(unittest.TestCase):
    def feed(self, *versions: str) -> str:
        entries = []
        for version in versions:
            day = {"2.40.1": "14", "2.41.1": "15"}.get(version, "01")
            entries.append(
                f'<entry><title>{version}</title>'
                f'<link href="https://github.com/openai/tart/releases/tag/{version}" />'
                f'<updated>2026-10-{day}T12:00:00Z</updated></entry>'
            )
        return '<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>"

    def candidate(self) -> updater.Candidate:
        return updater.Candidate(
            "2.41.1", TART_SHA, "0.24.1", SOFTNET_SHA,
            "https://github.com/openai/tart/releases/tag/2.41.1",
            "2026-10-15T12:00:00Z",
            "https://github.com/openai/tart/releases/download/2.41.1/tart_2.41.1_checksums.txt",
        )

    def fake_fetch(self, *, softnet_sha: str = SOFTNET_SHA):
        checksums_url = "https://github.com/openai/tart/releases/download/2.41.1/tart_2.41.1_checksums.txt"
        return {
            updater.TART_RELEASES_ATOM: self.feed("2.40.1", "2.41.1"),
            checksums_url: f"{TART_SHA}  tart.tar.gz\n",
            updater.TART_FORMULA: (
                'version "2.41.1"\n'
                'url "https://github.com/openai/tart/releases/download/2.41.1/tart.tar.gz"\n'
                f'sha256 "{TART_SHA}"\n'
            ),
            updater.SOFTNET_FORMULA: (
                'version "0.24.1"\n'
                'url "https://github.com/openai/softnet/releases/download/0.24.1/softnet.tar.gz"\n'
                f'sha256 "{softnet_sha}"\n'
            ),
        }

    def test_discovery_uses_numeric_atom_release_and_matches_both_sources(self) -> None:
        values = self.fake_fetch()
        candidate = updater.discover(values.__getitem__)
        self.assertEqual(self.candidate(), candidate)

    def test_discovery_refuses_release_checksum_mismatch(self) -> None:
        values = self.fake_fetch()
        values[next(key for key in values if key.endswith("checksums.txt"))] = f"{'c' * 64}  tart.tar.gz\n"
        with self.assertRaises(updater.DiscoveryError):
            updater.discover(values.__getitem__)

    def test_discovery_refuses_tap_version_that_does_not_match_release(self) -> None:
        values = self.fake_fetch()
        values[updater.TART_FORMULA] = values[updater.TART_FORMULA].replace("2.41.1", "2.40.1")
        with self.assertRaises(updater.DiscoveryError):
            updater.discover(values.__getitem__)

    def test_render_pin_changes_only_version_and_digest_fields(self) -> None:
        original = (
            "# keep this provenance comment\n\n"
            "[tart]\nversion = \"2.40.1\"\nsha256 = \"" + "c" * 64 + "\"\n"
            "formula = \"openai/tools/tart\"\n\n"
            "[softnet]\nversion = \"0.24.0\"\nsha256 = \"" + "d" * 64 + "\"\n"
            "formula = \"openai/tools/softnet\"\n"
        )
        rendered = updater.render_pin(original, self.candidate())
        self.assertIn("# keep this provenance comment", rendered)
        self.assertIn('version = "2.41.1"', rendered)
        self.assertIn(f'sha256 = "{TART_SHA}"', rendered)
        self.assertIn('version = "0.24.1"', rendered)
        self.assertIn(f'sha256 = "{SOFTNET_SHA}"', rendered)
        self.assertIn('formula = "openai/tools/tart"', rendered)

    def test_render_pin_refuses_missing_table_field(self) -> None:
        with self.assertRaises(updater.DiscoveryError):
            updater.render_pin('[tart]\nversion = "2.40.1"\n[softnet]\nversion = "0.24.0"\n', self.candidate())

    def test_report_marks_same_candidate_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fleet").mkdir()
            (root / "fleet" / "tart.toml").write_text(
                f'[tart]\nversion = "2.41.1"\nsha256 = "{TART_SHA}"\n'
                f'[softnet]\nversion = "0.24.1"\nsha256 = "{SOFTNET_SHA}"\n'
            )
            value = updater.report(root, self.candidate())
            self.assertFalse(value["changed"])
            self.assertEqual(tart_pin.load_pin(root).version, "2.41.1")

    def test_workflow_is_scheduled_review_only_and_does_not_auto_merge(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" /
                    "tart-pin-update.yml").read_text()
        self.assertIn('cron: "17 6 * * *"', workflow)
        self.assertIn("workflow_dispatch", workflow)
        self.assertIn("pull-requests: write", workflow)
        self.assertIn("gh pr create", workflow)
        self.assertNotIn("gh pr merge", workflow)
        self.assertIn("canary-first self-update engine", workflow)


if __name__ == "__main__":
    unittest.main()
