#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "providers/common/pulp-golden-receipt.py"
spec = importlib.util.spec_from_file_location("receipt", PATH)
receipt = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(receipt)


class ReceiptTests(unittest.TestCase):
    def good(self, path: Path, baked: str = "2026-10-01T00:00:00Z") -> None:
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "manifest_sha256": "a" * 64,
                    "source_commit": "b" * 40,
                    "provider_digests": {"skia": "c" * 64},
                    "image_digest": "d" * 64,
                    "baked_at": baked,
                    "observed_versions": {"cmake": "homebrew", "python": "3.12"},
                }
            ),
            encoding="utf-8",
        )

    def test_fresh_receipt_is_advisory_green(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "r.json"
            self.good(path)
            result, code = receipt.inspect(
                path,
                now=dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc),
                max_age_days=14,
            )
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "fresh")

    def test_stale_receipt_only_refuses_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "r.json"
            self.good(path)
            now = dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc)
            result, code = receipt.inspect(path, now=now, max_age_days=14)
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "stale")
            _, code = receipt.inspect(path, now=now, max_age_days=14, promotion=True)
            self.assertEqual(code, 2)

    def test_manifest_mismatch_is_negative_control(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "r.json"
            self.good(path)
            _, code = receipt.inspect(
                path,
                now=dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc),
                max_age_days=14,
                expected_manifest="e" * 64,
                promotion=True,
            )
            self.assertEqual(code, 2)

    def test_missing_field_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "r.json"
            self.good(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            del value["image_digest"]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ValueError):
                receipt.load(path)


if __name__ == "__main__":
    unittest.main()
