#!/usr/bin/env python3
"""Per-lane storage-health hysteresis on a host whose lanes share one store.

Every admitting lane on a host probes the same Tart store and records its
result in one shared state file. These tests pin the contract that one lane's
own probe failures do not hold a lane the store answers in quarantine, while a
store-wide failure still refuses admission on every lane until each has seen
the store recover for itself.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import storage_health  # noqa: E402

OK = (True, "directory_metadata_ok", 0.0)
LANE_TIMEOUT = (False, "probe_timeout", 5.0)
STORE_WEDGED = (False, "probe_timeout_unreaped", 5.0)
MISSING = (False, "path_missing", 0.0)


class Host:
    def __init__(self, temp: str) -> None:
        self.root = Path(temp) / "volume"
        self.root.mkdir()
        self.state = Path(temp) / "state" / "storage-health.json"
        self.state.parent.mkdir()

    @property
    def marker(self) -> Path:
        return Path(str(self.state) + ".quarantine")

    def probe(self, observer: str, outcome: tuple[bool, str, float]) -> tuple[int, dict]:
        args = argparse.Namespace(path=str(self.root), state=str(self.state),
                                  observer=observer, timeout=5.0,
                                  failure_threshold=2, recovery_threshold=3)
        out = io.StringIO()
        with patch.object(storage_health, "run_probe_bounded", return_value=outcome), \
                contextlib.redirect_stdout(out):
            rc = storage_health.run_worker(args)
        return rc, json.loads(out.getvalue())


class ObserverHysteresisTests(unittest.TestCase):
    def test_lane_local_timeouts_do_not_hold_a_healthy_sibling(self):
        """m3, 2026-10-30: slot2 timed out every probe for 7.5 h while the
        merge-group lane's probe answered in 0.0 s; the shared streak kept
        the merge-group lane refused 1,065 times."""
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            for _ in range(10):
                gate_rc, gate = host.probe("pulp-gate", OK)
                slot2_rc, _ = host.probe("pulp-gate-slot2", LANE_TIMEOUT)
            self.assertEqual(gate_rc, storage_health.HEALTHY, gate)
            self.assertEqual(slot2_rc, storage_health.QUARANTINED)
            self.assertFalse(host.marker.exists(),
                             "a lane-local timeout must not quarantine the host")

    def test_unknown_recovers_to_ok_after_a_full_window(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            self.assertEqual(host.probe("a", LANE_TIMEOUT)[0], storage_health.DEGRADED)
            self.assertEqual(host.probe("a", LANE_TIMEOUT)[0], storage_health.QUARANTINED)
            for _ in range(2):
                rc, result = host.probe("a", OK)
                self.assertEqual(rc, storage_health.QUARANTINED)
                self.assertEqual(result["reason"], "recovery_check_pending")
                # A sibling's failures interleaved with the recovery window
                # do not restart it.
                host.probe("b", LANE_TIMEOUT)
            self.assertEqual(host.probe("a", OK)[0], storage_health.HEALTHY)

    def test_store_wide_failure_refuses_every_lane_until_each_recovers(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            for lane in ("a", "b"):
                self.assertEqual(host.probe(lane, OK)[0], storage_health.HEALTHY)
            rc, _ = host.probe("a", STORE_WEDGED)
            self.assertEqual(rc, storage_health.QUARANTINED)
            self.assertTrue(host.marker.exists())
            # b's own probe passes, but the store-wide evidence refuses it
            # until b has seen a full recovery window itself.
            for _ in range(2):
                rc, result = host.probe("b", OK)
                self.assertEqual(rc, storage_health.QUARANTINED, result)
            self.assertEqual(host.probe("b", OK)[0], storage_health.HEALTHY)
            self.assertFalse(host.marker.exists())
            # a acknowledged the marker it raised; it still needs its own window.
            self.assertEqual(host.probe("a", OK)[0], storage_health.QUARANTINED)

    def test_a_newer_store_wide_failure_restarts_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            host.probe("a", STORE_WEDGED)
            host.probe("b", OK)
            host.probe("b", OK)
            host.probe("a", STORE_WEDGED)
            self.assertEqual(host.probe("b", OK)[0], storage_health.QUARANTINED)
            self.assertTrue(host.marker.exists())

    def test_genuinely_unhealthy_store_refuses_every_lane(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            for _ in range(3):
                results = [host.probe(lane, MISSING)[0] for lane in ("a", "b", "c")]
            self.assertEqual(results, [storage_health.QUARANTINED] * 3)

    def test_unreadable_shared_state_still_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            host.state.write_text("not json")
            rc, result = host.probe("a", OK)
            self.assertEqual(rc, storage_health.QUARANTINED)
            self.assertEqual(result["reason"], "health_state_unreadable")

    def test_probe_runs_without_holding_the_shared_lock(self):
        """A lane whose probe runs to its timeout must not hold siblings'
        workers on the lock past their supervisor deadline."""
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            lock = host.state.with_suffix(host.state.suffix + ".lock")
            seen = []

            def probe(*_args):
                import fcntl
                with lock.open("a+") as other:
                    try:
                        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        seen.append("free")
                        fcntl.flock(other.fileno(), fcntl.LOCK_UN)
                    except BlockingIOError:
                        seen.append("held")
                return OK

            args = argparse.Namespace(path=str(host.root), state=str(host.state),
                                      observer="a", timeout=5.0,
                                      failure_threshold=2, recovery_threshold=3)
            with patch.object(storage_health, "run_probe_bounded", side_effect=probe), \
                    contextlib.redirect_stdout(io.StringIO()):
                storage_health.run_worker(args)
            self.assertEqual(seen, ["free"])

    def test_runner_names_its_lane_as_the_observer(self):
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        body = runner[runner.index("tartci_storage_health_check(){"):]
        body = body[:body.index("\nruntime_emit_complete(){")]
        self.assertIn('--observer "${TARTCI_QUEUE_LANE_ID:-$RUNNER_NAME-$SLOT}"', body)


if __name__ == "__main__":
    unittest.main()
