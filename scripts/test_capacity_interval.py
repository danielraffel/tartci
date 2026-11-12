#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import capacity_interval

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "scripts/fixtures/capacity_interval/supply.jsonl"


class CapacityIntervalTests(unittest.TestCase):
    def _samples(self):
        return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]

    def test_adjacent_samples_are_capped_and_source_evidence_preserved(self):
        samples = self._samples()
        self.assertIsInstance(samples[0]["now"], int)
        rows = capacity_interval.export_intervals(samples)
        self.assertEqual(len(rows), 5)
        m3 = [row for row in rows if row["host"] == "m3"]
        self.assertEqual(m3[0]["duration_s"], 20)
        self.assertEqual(m3[1]["duration_s"], 120)
        self.assertEqual(m3[1]["timestamp_source"], "fetched_at")
        self.assertEqual(m3[2]["reason"], "observation_gap")
        self.assertIsNone(m3[2]["interval_end"])
        self.assertEqual(m3[1]["supply"]["lease_fit"], 0)
        self.assertEqual(m3[1]["supply"]["lanes"][0]["state"], "in_flight")
        m5 = [row for row in rows if row["host"] == "m5"]
        source = next(row for row in m5 if row["supply"] is not None)
        self.assertEqual(source["verdict"], "unknown")
        self.assertTrue(source["supply"]["reason"].startswith("stale heartbeat"))

    def test_until_closes_final_interval_without_changing_observations(self):
        rows = capacity_interval.export_intervals(self._samples(), "2026-10-16T15:01:00Z")
        m3_last = next(row for row in rows if row["host"] == "m3" and row["interval_end"] == "2026-10-16T15:01:00Z")
        self.assertEqual(m3_last["duration_s"], 40)
        self.assertEqual(m3_last["supply"]["free"], 0)

    def test_long_observation_gap_emits_explicit_unknown_intervals(self):
        sample = self._samples()[0]
        later = dict(sample, now=sample["now"] + 180, fetched_at=sample["fetched_at"] + 180)
        rows = capacity_interval.export_intervals([sample, later])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["duration_s"], 120)
        self.assertEqual(rows[1]["verdict"], "unknown")
        self.assertEqual(rows[1]["reason"], "observation_gap")
        self.assertEqual(rows[1]["duration_s"], 60)
        self.assertIsNone(rows[1]["supply"])
        self.assertEqual(rows[3]["reason"], "observation_gap")
        self.assertIsNone(rows[3]["interval_end"])

    def test_error_shape_can_use_observed_or_fetched_time_and_is_preserved(self):
        source = {
            "schema": capacity_interval.SOURCE_SCHEMA,
            "host": "m3", "repo": "Generous-Corp/pulp", "class": "pulp-build-pr-head",
            "observed_at": "2026-10-16T15:00:00Z", "fetched_at": "2026-10-16T15:00:01Z",
            "verdict": "unknown", "reason": "report failed: launchd unreadable",
        }
        row = capacity_interval.export_intervals([source])[0]
        self.assertEqual(row["interval_start"], "2026-10-16T15:00:01Z")
        self.assertEqual(row["timestamp_source"], "fetched_at")
        self.assertEqual(row["supply"], source)
        fetched = dict(source)
        fetched.pop("observed_at")
        fetched["fetched_at"] = "2026-10-16T15:00:02Z"
        self.assertEqual(capacity_interval.export_intervals([fetched])[0]["interval_start"], "2026-10-16T15:00:02Z")

    def test_invalid_preferred_clock_is_unavailable_without_fallback(self):
        source = dict(self._samples()[0], fetched_at="bad", now=1792162801,
                      verdict="unknown", reason="report failed: bad timestamp")
        row = capacity_interval.export_intervals([source])[0]
        self.assertEqual(row["timestamp_source"], "unavailable")
        self.assertIsNone(row["interval_start"])
        observed = capacity_interval.export_intervals(
            [source], observed_at="2026-10-16T15:00:30Z"
        )[0]
        self.assertEqual(observed["timestamp_source"], "unavailable")
        self.assertIsNone(observed["interval_start"])

    def test_mixed_timestamp_sources_for_one_identity_fail_closed(self):
        sample = self._samples()[0]
        fetched = dict(sample, now=sample["now"] + 1)
        fetched.pop("fetched_at")
        with self.assertRaisesRegex(ValueError, "mixed timestamp sources"):
            capacity_interval.export_intervals([sample, fetched])

    def test_mixed_timestamp_sources_across_hosts_fail_closed(self):
        sample = self._samples()[0]
        other_host = dict(sample, host="m5")
        other_host.pop("fetched_at")
        with self.assertRaisesRegex(ValueError, "mixed timestamp sources in export"):
            capacity_interval.export_intervals([sample, other_host])

    def test_literal_gate_supply_error_shapes_are_retained_without_abort(self):
        local_error = {
            "schema": capacity_interval.SOURCE_SCHEMA, "verdict": "unknown",
            "reason": "report failed: launchd unreadable", "host": "m3",
            "repo": "Generous-Corp/pulp", "class": "pulp-build-pr-head",
            "fetched_at": 1792162800, "free": 0, "in_flight": 0,
        }
        peer_error = {
            "schema": capacity_interval.SOURCE_SCHEMA, "verdict": "unknown",
            "reason": "ssh m5: TimeoutExpired", "host": "m5", "repo": "Generous-Corp/pulp",
            "class": "pulp-build-pr-head", "fetched_at": 1792162800,
        }
        rows = capacity_interval.export_intervals([local_error, peer_error])
        self.assertEqual(len(rows), 4)
        source_rows = [row for row in rows if row["supply"] is not None]
        self.assertEqual([row["supply"] for row in source_rows], [local_error, peer_error])
        self.assertEqual({row["host"] for row in source_rows}, {"m3", "m5"})
        self.assertTrue(all(row["timestamp_source"] == "fetched_at" for row in source_rows))

    def test_legacy_unstamped_error_is_retained_as_diagnostic(self):
        legacy = {"verdict": "unknown", "reason": "ssh m5: TimeoutExpired", "host": "m5"}
        row = capacity_interval.export_intervals([legacy])[0]
        self.assertEqual(row["reason"], "invalid_supply_sample")
        self.assertIsNone(row["interval_start"])
        self.assertEqual(row["supply"], legacy)

    def test_invalid_timestamp_identity_and_verdict_fail_closed(self):
        base = self._samples()[0]
        for field, value, message in (
            ("now", "2026-10-16T15:00:00", "timezone-aware"),
            ("host", "", "non-empty"), ("repo", 7, "non-empty"),
            ("class", "   ", "non-empty"), ("verdict", "free", "unsupported"),
        ):
            with self.subTest(field=field):
                bad = dict(base, **{field: value})
                bad.pop("fetched_at", None)
                with self.assertRaisesRegex(ValueError, message):
                    capacity_interval.export_intervals([bad])

    def test_same_time_identical_samples_deduplicate_but_conflicting_evidence_fails(self):
        sample = self._samples()[0]
        rows = capacity_interval.export_intervals([sample, dict(sample)])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["duration_s"], 120)
        for conflicting in (dict(sample, free=0), dict(sample, free=True)):
            with self.assertRaisesRegex(ValueError, "conflicting supply evidence"):
                capacity_interval.export_intervals([sample, conflicting])

    def test_invalid_schema_and_cli_exports_fixture_as_jsonl(self):
        with self.assertRaisesRegex(ValueError, "expected tartci.gate-supply/v1"):
            capacity_interval.export_intervals([{"schema": "wrong"}])
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/capacity_interval.py"), "--input", str(FIXTURE)],
            check=True, capture_output=True, text=True,
        )
        rows = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual([row["schema"] for row in rows], [capacity_interval.SCHEMA] * 5)
        self.assertEqual(rows[0]["host"], "m3")
        self.assertEqual(rows[0]["timestamp_source"], "fetched_at")


if __name__ == "__main__":
    unittest.main()
