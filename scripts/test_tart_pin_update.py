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

    def refused(self, values) -> str:
        with self.assertRaises(updater.DiscoveryError) as ctx:
            updater.discover(values.__getitem__)
        return str(ctx.exception)

    def test_discovery_refuses_softnet_url_outside_openai_release(self) -> None:
        values = self.fake_fetch()
        values[updater.SOFTNET_FORMULA] = values[updater.SOFTNET_FORMULA].replace(
            "openai/softnet", "evil/softnet")
        self.assertIn("not an OpenAI release", self.refused(values))

    def test_discovery_refuses_malformed_softnet_sha(self) -> None:
        self.assertIn("Softnet formula sha256", self.refused(self.fake_fetch(softnet_sha="B" * 10)))

    def test_discovery_refuses_softnet_version_url_mismatch(self) -> None:
        values = self.fake_fetch()
        values[updater.SOFTNET_FORMULA] = values[updater.SOFTNET_FORMULA].replace(
            "download/0.24.1/", "download/0.23.0/")
        self.assertIn("does not point at release 0.24.1", self.refused(values))

    def test_discovery_refuses_non_atom_and_empty_feeds(self) -> None:
        for body in ("not xml <", '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'):
            values = self.fake_fetch()
            values[updater.TART_RELEASES_ATOM] = body
            self.refused(values)

    def test_discovery_refuses_feed_with_only_prerelease_or_non_numeric_tags(self) -> None:
        values = self.fake_fetch()
        values[updater.TART_RELEASES_ATOM] = self.feed("2.41.1-rc1", "nightly")
        self.assertIn("no stable numeric", self.refused(values))

    def test_discovery_refuses_duplicate_or_missing_tarball_checksum(self) -> None:
        key = next(k for k in self.fake_fetch() if k.endswith("checksums.txt"))
        for body in (f"{TART_SHA}  tart.tar.gz\n{TART_SHA}  tart.tar.gz\n",
                     f"{TART_SHA}  other.tar.gz\n"):
            values = self.fake_fetch()
            values[key] = body
            self.assertIn("one valid tart.tar.gz", self.refused(values))

    def test_main_exits_2_on_discovery_error_and_write_updates_pin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fleet").mkdir()
            pin = root / "fleet" / "tart.toml"
            original = (f'[tart]\nversion = "2.40.1"\nsha256 = "{"c" * 64}"\n'
                        f'[softnet]\nversion = "0.24.0"\nsha256 = "{"d" * 64}"\n')
            pin.write_text(original)
            real = updater.discover
            try:
                def boom(*_a, **_k):
                    raise updater.DiscoveryError("boom")
                updater.discover = boom
                self.assertEqual(2, updater.main(["--root", str(root), "--write"]))
                self.assertEqual(original, pin.read_text())
                updater.discover = lambda *_a, **_k: self.candidate()
                self.assertEqual(0, updater.main(["--root", str(root), "--write"]))
            finally:
                updater.discover = real
            loaded = tart_pin.load_pin(root)
            self.assertEqual(("2.41.1", TART_SHA, "0.24.1", SOFTNET_SHA),
                             (loaded.version, loaded.sha256, loaded.softnet_version, loaded.softnet_sha256))

    def test_workflow_text_does_not_claim_softnet_matches_upstream_bytes(self) -> None:
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" /
                    "tart-pin-update.yml").read_text()
        self.assertNotIn("upstream release bytes", workflow)
        self.assertIn("Softnet's version and sha256 come from the OpenAI tools tap", workflow)

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
