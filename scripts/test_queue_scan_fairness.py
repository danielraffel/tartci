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


def event_run(run_id: int, created: str, page: str, event: str) -> dict:
    return {**run(run_id, created, page), "event": event}


def scanner() -> queue_scan.QueueScanner:
    value = object.__new__(queue_scan.QueueScanner)
    value.args = argparse.Namespace(
        newest_quota=2, max_job_fetches=5, page_offset_cap=64, merge_group_quota=2
    )
    return value


class QueueScanFairnessTests(unittest.TestCase):
    def _vellum_queue(self) -> list[dict]:
        """The 2026-11-15 shape: two ghost PR runs from 11-11, PR runs created
        after the merge group, and the merge-group run whose second job (the
        sterile consumer) is queued while the run itself is `queued`."""
        runs = [
            event_run(1, "2026-11-11T20:28:00Z", "queued:1", "pull_request"),
            event_run(2, "2026-11-11T20:33:00Z", "queued:1", "pull_request"),
            event_run(50, "2026-11-15T10:35:00Z", "queued:1", "merge_group"),
        ]
        runs += [
            event_run(60 + i, f"2026-11-15T11:{10 + i}:00Z", "queued:1", "pull_request")
            for i in range(6)
        ]
        return runs

    def test_merge_group_run_is_fetched_on_every_refresh(self) -> None:
        value = scanner()
        runs = self._vellum_queue()
        state: dict = {}
        for refresh in range(12):
            selected, _ = value._candidate_runs(runs, state)
            ids = [int(item["id"]) for item in selected]
            self.assertIn(50, ids, f"refresh {refresh}: {ids}")
            self.assertLessEqual(len(ids), 5, ids)
            # The newest PR runs keep their latency quota.
            self.assertTrue({65, 64} <= set(ids), ids)

    def test_control_without_the_quota_the_merge_group_run_is_starved(self) -> None:
        value = scanner()
        value.args.merge_group_quota = 0
        runs = self._vellum_queue()
        state: dict = {}
        seen = [
            50 in {int(item["id"]) for item in value._candidate_runs(runs, state)[0]}
            for _ in range(3)
        ]
        self.assertFalse(all(seen), seen)

    def test_merge_group_quota_never_takes_the_last_rotation_slot(self) -> None:
        value = scanner()
        value.args.merge_group_quota = 99
        runs = [
            event_run(100 + i, f"2026-11-15T10:{i:02d}:00Z", "queued:1", "merge_group")
            for i in range(8)
        ]
        runs.append(event_run(1, "2026-11-11T20:00:00Z", "queued:1", "pull_request"))
        selected, _ = value._candidate_runs(runs, {})
        ids = [int(item["id"]) for item in selected]
        self.assertLessEqual(len(ids), 5, ids)
        self.assertIn(1, ids, "the oldest run lost its reserved slot")

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

    def test_reserved_oldest_rechecks_only_after_negative_ttl(self) -> None:
        value = scanner()
        value.args.negative_ttl = 300
        runs = [run(1, "2026-11-11T21:00:00Z", "queued:1"),
                run(2, "2026-11-11T20:59:00Z", "queued:1")]
        runs += [run(100 + i, f"2026-11-10T00:{i % 60:02d}:00Z", "queued:2") for i in range(99)]
        runs.append(run(1000, "2026-11-01T00:00:00Z", "queued:2"))
        state = {"run_item_offsets": {"queued:2": {"offset": 50, "last_seen": 1}}}
        value.now = 10_000
        first, _ = value._candidate_runs(runs, state)
        self.assertIn(1000, [int(item["id"]) for item in first])
        # Within the TTL the slot goes back to rotation: the full capacity of
        # three backlog fetches advances the page cursor.
        value.now += 121
        before = state["run_item_offsets"]["queued:2"]["offset"]
        second, rotated = value._candidate_runs(runs, state)
        self.assertNotIn(1000, [int(item["id"]) for item in second])
        self.assertEqual(len(rotated), 3)
        self.assertEqual(
            (state["run_item_offsets"]["queued:2"]["offset"] - before) % 100, 3
        )
        # Once the TTL has passed the oldest run is reserved again.
        value.now += 300
        third, _ = value._candidate_runs(runs, state)
        self.assertIn(1000, [int(item["id"]) for item in third])
        # A different oldest run is reserved immediately.
        value.now += 1
        runs.append(run(2000, "2026-10-01T00:00:00Z", "queued:2"))
        fourth, _ = value._candidate_runs(runs, state)
        self.assertIn(2000, [int(item["id"]) for item in fourth])
        # Alternating back to a page whose oldest run was reserved within the
        # TTL does not spend the slot again.
        value.now += 1
        runs.pop()
        fifth, rotated = value._candidate_runs(runs, state)
        self.assertNotIn(1000, [int(item["id"]) for item in fifth])
        self.assertEqual(len(rotated), 3)


if __name__ == "__main__":
    unittest.main()
