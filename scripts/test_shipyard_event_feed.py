#!/usr/bin/env python3
"""Behavioral tests for the Shipyard push-feed reader and its staleness fence.

The property under test is the one that has repeatedly failed in production:
a measurement that could not see the queue must never render as an empty queue.
Every negative path here asserts the verdict degrades to a scan, and the
"no matching events" case -- the one that looks most like emptiness -- is
asserted to be STALE rather than a success with zero results.

Run:  python3 scripts/test_shipyard_event_feed.py
"""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import shipyard_event_feed as feed

NOW = 1_000_000.0
LABELS = ["self-hosted", "macos", "pulp-build-vm", "pulp-gate-fast"]
REQUIRE = "pulp-gate-fast"
REPO = "Generous-Corp/pulp"


def job(**over):
    payload = {
        "repo": REPO,
        "status": "queued",
        "job_id": 1,
        "run_id": 2,
        "labels": ["self-hosted", "macos", "pulp-gate-fast"],
    }
    payload.update(over)
    return {"kind": "workflow_job", "payload": payload}


class FakeDaemon:
    """A Unix-socket daemon speaking the real newline-JSON IPC contract."""

    def __init__(self, path, status, events, refuse=None):
        self.path, self.status, self.events, self.refuse = path, status, events, refuse
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.3)
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        try:
            conn.settimeout(2.0)
            conn.sendall(
                json.dumps(
                    {"protocol": 3, "shipyard_version": "test", "type": "hello"}
                ).encode()
                + b"\n"
            )
            req = conn.makefile("rb").readline()
            kind = json.loads(req or b"{}").get("type")
            if kind == "status":
                conn.sendall(json.dumps(self.status).encode() + b"\n")
            elif kind == "subscribe":
                if self.refuse:
                    conn.sendall(
                        json.dumps({"error": self.refuse, "retryable": True}).encode()
                        + b"\n"
                    )
                else:
                    for ev in self.events:
                        conn.sendall(json.dumps(ev).encode() + b"\n")
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


def status_frame(registered=(REPO,), configured=(REPO,), last_event_at=NOW - 10):
    return {
        "type": "status",
        "registered_repos": list(registered),
        "configured_repos": list(configured),
        "last_event_at": last_event_at,
        "subscribers": 0,
        "last_error": None,
    }


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "daemon.sock"
        self.daemon = None

    def tearDown(self):
        if self.daemon:
            self.daemon.close()
        self.tmp.cleanup()

    def run_feed(self, status, events, refuse=None, **kw):
        self.daemon = FakeDaemon(self.path, status, events, refuse)
        return feed.read_feed(
            REPO,
            REQUIRE,
            LABELS,
            socket_path=self.path,
            collect_timeout_s=0.6,
            connect_timeout_s=2.0,
            now=NOW,
            **kw,
        )

    # --- the crux -------------------------------------------------------

    def test_live_feed_with_no_matching_events_is_stale_not_empty(self):
        """A live feed showing nothing must NOT render as an empty queue."""
        res = self.run_feed(status_frame(), [])
        self.assertIs(res.verdict, feed.Verdict.STALE)
        self.assertTrue(res.should_fall_back_to_scan())
        self.assertFalse(res.demand_is_known_absent())
        self.assertIn("not evidence", res.detail)

    def test_no_verdict_can_ever_claim_demand_is_absent(self):
        """The invariant: no verdict proves an empty queue."""
        for verdict in feed.Verdict:
            self.assertFalse(
                feed.FeedResult(verdict, "x").demand_is_known_absent(),
                f"{verdict} must never claim demand is known absent",
            )

    # --- liveness fence -------------------------------------------------

    def test_configured_but_unregistered_repo_is_unavailable(self):
        """The live m3 defect: webhook registration failed, feed is blind."""
        res = self.run_feed(status_frame(registered=(), configured=(REPO,)), [job()])
        self.assertIs(res.verdict, feed.Verdict.UNAVAILABLE)
        self.assertIn("not registered", res.detail)
        self.assertTrue(res.should_fall_back_to_scan())

    def test_stale_last_event_is_stale_even_with_matching_events(self):
        res = self.run_feed(status_frame(last_event_at=NOW - 9999), [job()])
        self.assertIs(res.verdict, feed.Verdict.STALE)
        self.assertIn("freshness window", res.detail)

    def test_missing_last_event_timestamp_is_stale(self):
        res = self.run_feed(status_frame(last_event_at=None), [job()])
        self.assertIs(res.verdict, feed.Verdict.STALE)

    def test_absent_socket_is_unavailable(self):
        res = feed.read_feed(
            REPO, REQUIRE, LABELS, socket_path=self.path / "nope", now=NOW
        )
        self.assertIs(res.verdict, feed.Verdict.UNAVAILABLE)
        self.assertTrue(res.should_fall_back_to_scan())

    def test_subscriber_capacity_refusal_is_unavailable(self):
        res = self.run_feed(
            status_frame(), [], refuse=feed.IPC_ERROR_SUBSCRIBER_CAPACITY
        )
        self.assertIs(res.verdict, feed.Verdict.UNAVAILABLE)

    # --- positive path + matching rule ----------------------------------

    def test_matching_queued_job_is_fresh(self):
        res = self.run_feed(status_frame(), [job()])
        self.assertIs(res.verdict, feed.Verdict.FRESH)
        self.assertEqual(res.matched, 1)
        self.assertFalse(res.should_fall_back_to_scan())

    def test_job_needing_a_label_this_runner_lacks_does_not_match(self):
        res = self.run_feed(
            status_frame(), [job(labels=["self-hosted", "pulp-gate-fast", "linux"])]
        )
        self.assertIs(res.verdict, feed.Verdict.STALE)

    def test_job_without_the_required_label_does_not_match(self):
        res = self.run_feed(status_frame(), [job(labels=["self-hosted", "macos"])])
        self.assertIs(res.verdict, feed.Verdict.STALE)

    def test_non_queued_job_does_not_match(self):
        res = self.run_feed(status_frame(), [job(status="completed")])
        self.assertIs(res.verdict, feed.Verdict.STALE)

    def test_other_repo_does_not_match(self):
        res = self.run_feed(status_frame(), [job(repo="danielraffel/shipyard")])
        self.assertIs(res.verdict, feed.Verdict.STALE)

    # --- malformed input is refused, never silently skipped --------------

    def test_malformed_labels_refuse_rather_than_skip(self):
        res = self.run_feed(status_frame(), [job(labels="not-a-list")])
        self.assertIs(res.verdict, feed.Verdict.STALE)
        self.assertIn("malformed", res.detail)

    def test_required_label_absent_from_runner_labels_is_a_config_error(self):
        with self.assertRaises(feed.FeedError):
            feed.read_feed(REPO, "nope-label", LABELS, socket_path=self.path, now=NOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)
