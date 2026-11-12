#!/usr/bin/env python3
"""Regression tests for queue-scan fairness under a large queued backlog."""
from __future__ import annotations

import argparse
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "scripts"))
import queue_scan


def run(run_id: int, created: str, page: str) -> dict:
    return {
        "id": run_id,
        "created_at": created,
        "_page_key": page,
        "_page_sweep": 0,
    }


def scanner() -> queue_scan.QueueScanner:
    value = object.__new__(queue_scan.QueueScanner)
    value.args = argparse.Namespace(newest_quota=2, max_job_fetches=5, page_offset_cap=64)
    return value


class QueueScanFairnessTests(unittest.TestCase):
    def test_oldest_run_is_always_reserved_for_the_sweep(self) -> None:
        value = scanner()
        runs = [
            run(1, "2026-11-11T20:00:00Z", "queued:1"),
            run(2, "2026-11-11T19:59:00Z", "queued:1"),
            run(3, "2026-11-11T19:58:00Z", "pending:2"),
            run(4, "2026-11-11T19:57:00Z", "in_progress:2"),
            run(5, "2026-11-10T00:00:00Z", "queued:2"),
            run(6, "2026-11-09T00:00:00Z", "pending:2"),
        ]
        selected, _ = value._candidate_runs(runs, {})
        self.assertIn(6, {int(item["id"]) for item in selected})

    def test_newest_latency_quota_is_preserved_alongside_oldest_run(self) -> None:
        value = scanner()
        runs = [
            run(10, "2026-11-11T21:00:00Z", "queued:1"),
            run(11, "2026-11-11T20:59:00Z", "queued:1"),
            run(12, "2026-11-10T00:00:00Z", "queued:2"),
        ]
        selected, _ = value._candidate_runs(runs, {})
        self.assertEqual([int(item["id"]) for item in selected[:2]], [10, 11])
        self.assertIn(12, {int(item["id"]) for item in selected})

    def test_oldest_reserved_on_full_page_larger_than_capacity(self) -> None:
        value = scanner()
        # One full older page (100 runs) far larger than capacity; the oldest
        # run (id 1000) sits at the end, outside the rotating offset window.
        runs = [run(1, "2026-11-11T21:00:00Z", "queued:1"),
                run(2, "2026-11-11T20:59:00Z", "queued:1")]
        runs += [run(100 + i, f"2026-11-10T00:{i % 60:02d}:00Z", "queued:2") for i in range(99)]
        runs.append(run(1000, "2026-11-01T00:00:00Z", "queued:2"))
        state = {"run_item_offsets": {"queued:2": {"offset": 50, "last_seen": 1}}}
        selected, rotated = value._candidate_runs(runs, state)
        ids = [int(item["id"]) for item in selected]
        self.assertIn(1000, ids)
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(set(ids)), 5)


if __name__ == "__main__":
    unittest.main()
