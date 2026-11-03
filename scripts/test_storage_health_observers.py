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
import calendar
import json
import os
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

    def test_host_verdict_is_not_the_last_lane_to_write(self):
        """A healthy lane writing after a quarantined one must not turn the
        host-level fields healthy."""
        with tempfile.TemporaryDirectory() as temp:
            host = Host(temp)
            host.probe("slot2", LANE_TIMEOUT)
            host.probe("slot2", LANE_TIMEOUT)
            rc, _ = host.probe("gate", OK)
            self.assertEqual(rc, storage_health.HEALTHY)
            document = json.loads(host.state.read_text())
            self.assertEqual(document["status"], "quarantined")
            self.assertEqual(storage_health.host_verdict(document)[0], "quarantined")

    def test_host_verdict_rules(self):
        def obs(status, at, reason="r"):
            return {"status": status, "reason": reason, "checked_at": at}
        a, b = "2026-11-03T10:00:00Z", "2026-11-03T10:05:00Z"
        now = 1793700600.0  # 10:10Z
        verdict = lambda doc, **kw: storage_health.host_verdict(doc, now=now, **kw)
        self.assertEqual(verdict({"observers": {"x": obs("healthy", a), "y": obs("healthy", b)}}),
                         ("healthy", "r", b))
        self.assertEqual(verdict({"observers": {"x": obs("healthy", a), "y": obs("degraded", b, "d")}}),
                         ("degraded", "d", b))
        self.assertEqual(verdict({"observers": {"x": obs("quarantined", a, "q"), "y": obs("healthy", b)}}),
                         ("quarantined", "q", b))
        self.assertEqual(verdict({"observers": {"x": obs("healthy", a)}}, marker_present=True)[0],
                         "quarantined")
        legacy = {"status": "degraded", "reason": "old", "checked_at": a}
        self.assertEqual(verdict(legacy), ("degraded", "old", a))

    def test_host_verdict_is_independent_of_the_local_timezone(self):
        """A just-written UTC timestamp must be fresh under any TZ, including
        a daylight-saving one (the old mktime parse was an hour off there)."""
        import time
        at = "2026-07-03T10:05:00Z"
        now = calendar.timegm(time.strptime("2026-07-03T10:05:05Z", "%Y-%m-%dT%H:%M:%SZ"))
        doc = {"observers": {"a": {"status": "healthy", "reason": "ok", "checked_at": at}}}
        saved = os.environ.get("TZ")
        try:
            for zone in ("UTC", "America/Los_Angeles", "Asia/Kolkata"):
                os.environ["TZ"] = zone
                time.tzset()
                with self.subTest(zone=zone):
                    self.assertEqual(storage_health.host_verdict(doc, now=now),
                                     ("healthy", "ok", at))
            for winter in ("2026-01-03T10:05:00Z",):
                os.environ["TZ"] = "America/Los_Angeles"
                time.tzset()
                w = {"observers": {"a": {"status": "healthy", "reason": "ok", "checked_at": winter}}}
                wn = calendar.timegm(time.strptime("2026-01-03T10:05:05Z", "%Y-%m-%dT%H:%M:%SZ"))
                self.assertEqual(storage_health.host_verdict(w, now=wn)[0], "healthy")
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()

    def test_worker_persists_a_fresh_verdict_under_a_daylight_saving_zone(self):
        import time
        from unittest.mock import patch
        at = "2026-07-03T10:05:00Z"
        epoch = calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ"))
        real_gmtime = time.gmtime
        saved = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        try:
            with tempfile.TemporaryDirectory() as temp:
                host = Host(temp)
                with patch.object(storage_health.time, "time", return_value=float(epoch + 5)), \
                        patch.object(storage_health.time, "gmtime",
                                     lambda *a: real_gmtime(*(a or (epoch,)))):
                    rc, result = host.probe("gate", OK)
                document = json.loads(host.state.read_text())
        finally:
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()
        self.assertEqual(rc, storage_health.HEALTHY, result)
        self.assertEqual(document["status"], "healthy")
        self.assertEqual(document["checked_at"], at)
        self.assertEqual(storage_health.host_verdict(document, now=epoch + 5)[:1], ("healthy",))

    def test_fallback_never_reports_status_none(self):
        self.assertEqual(storage_health.host_verdict({})[0], "unknown")
        self.assertEqual(storage_health.host_verdict({"status": None, "observers": {}})[0], "unknown")
        self.assertEqual(storage_health.host_verdict({"status": "degraded"})[0], "degraded")
        self.assertEqual(storage_health.host_verdict({}, marker_present=True)[0], "quarantined")

    def test_only_recent_observers_vote(self):
        def obs(status, at):
            return {"status": status, "reason": "r", "checked_at": at}
        old, recent = "2026-11-03T09:00:00Z", "2026-11-03T10:05:00Z"
        now = 1793700600.0  # 10:10Z
        stale_q = {"observers": {"gone": obs("quarantined", old), "live": obs("healthy", recent)}}
        self.assertEqual(storage_health.host_verdict(stale_q, now=now)[0], "healthy")
        live_q = {"observers": {"gone": obs("quarantined", recent), "live": obs("healthy", recent)}}
        self.assertEqual(storage_health.host_verdict(live_q, now=now)[0], "quarantined")
        only_old = {"observers": {"gone": obs("healthy", old)}}
        self.assertEqual(storage_health.host_verdict(only_old, marker_present=True, now=now)[0],
                         "quarantined")

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
