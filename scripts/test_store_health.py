#!/usr/bin/env python3
"""Hermetic tests for the bounded Tart store health probe."""

from __future__ import annotations

import os
import sys
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store_health  # noqa: E402
import tartci_launchd_watchdog as watchdog  # noqa: E402


class StoreHealthContractTests(unittest.TestCase):
    def test_probe_and_state_contract(self) -> None:
        main()


class TccWedgeWatchdogTests(unittest.TestCase):
    """store_health_pass past the threshold: classify, heal once, reprobe."""

    def run_passes(self, td: str, *, scan_total: int, heal_rc: int = 0,
                   wrapper_present: bool = True, reprobe_ok: bool = True,
                   status_only: bool = False) -> tuple:
        import tcc_wedge
        state_home = Path(td) / "state-home"
        wrapper = Path(td) / "tartci-heal-tcc"
        if wrapper_present:
            wrapper.write_text("#!/bin/sh\nexit 0\n")
            wrapper.chmod(0o755)
        heal_calls: list = []

        def heal_runner(argv, timeout):
            heal_calls.append(argv)
            return heal_rc, ""
        probes = [
            {"ok": False, "reason": "timeout after 10s"},
            {"ok": False, "reason": "timeout after 10s"},
        ]
        if scan_total and wrapper_present and heal_rc == 0 and not status_only:
            probes.append({"ok": reprobe_ok, "reason": None if reprobe_ok else "timeout after 10s",
                           "device_id": "7", "latency_ms": 8.1, "path": td})
        with mock.patch.dict(os.environ, {"TARTCI_HOME": str(state_home)}), \
                mock.patch.object(watchdog, "resolve_tart_home", return_value=(td, "test")), \
                mock.patch.object(watchdog.store_health, "probe", side_effect=probes), \
                mock.patch.object(tcc_wedge, "WRAPPER", str(wrapper)), \
                mock.patch.object(tcc_wedge, "scan_log",
                                  lambda window_s, before=None: {
                                      "ok": True, "window_s": window_s, "total": scan_total,
                                      "first": "f", "last": "l",
                                      "by_signature": {"removable_volumes_auth_failed": scan_total}}), \
                mock.patch.object(tcc_wedge, "control_probe",
                                  lambda path, timeout_s: {"ok": True, "path": path,
                                                           "latency_ms": 0.05}), \
                mock.patch.object(tcc_wedge, "wrapper_insecurity", lambda path: ""), \
                mock.patch.object(tcc_wedge, "run_privileged_bounded", heal_runner):
            first = watchdog.store_health_pass(
                participation_file=str(Path(td) / "participation"), status_only=False,
                dry_run=False, failure_threshold=2)
            second = watchdog.store_health_pass(
                participation_file=str(Path(td) / "participation"), status_only=status_only,
                dry_run=False, failure_threshold=2)
        saved = store_health.load(str(state_home / "state" / "store-health.json"))
        return first, second, saved, heal_calls, wrapper

    def test_wedge_is_healed_once_and_the_store_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first, second, saved, heal_calls, wrapper = self.run_passes(td, scan_total=3)
        self.assertEqual(first["state"], "suspect")
        self.assertNotIn("classification", first, "below the threshold nothing is classified")
        self.assertEqual(second["classification"]["kind"], "tcc_attribution_wedge")
        self.assertEqual(heal_calls, [["/usr/bin/sudo", "-n", str(wrapper)]])
        self.assertEqual(second["heal"]["outcome"], "ran")
        self.assertTrue(second["heal"]["reprobe"]["ok"])
        self.assertEqual(second["action"], "tcc-healed")
        self.assertEqual(second["state"], "healthy")
        self.assertFalse(second["drain_required"])
        self.assertEqual(saved["state"], "healthy")
        self.assertEqual(saved["last_heal"]["outcome"], "ran")
        self.assertNotIn("failing_since", saved)

    def test_a_status_pass_classifies_but_never_heals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, second, saved, heal_calls, _ = self.run_passes(td, scan_total=3, status_only=True)
        self.assertEqual(second["classification"]["kind"], "tcc_attribution_wedge")
        self.assertEqual(second["heal"]["outcome"], "disabled")
        self.assertEqual(heal_calls, [], "a status pass must not run the root action")
        self.assertEqual(second["state"], "draining")
        self.assertNotIn("heal", saved, "a status pass writes no state")

    def test_a_classifier_crash_still_saves_state_and_reports(self) -> None:
        import tcc_wedge
        with tempfile.TemporaryDirectory() as td:
            state_home = Path(td) / "state-home"
            probes = [{"ok": False, "reason": "timeout after 10s"}] * 2
            with mock.patch.dict(os.environ, {"TARTCI_HOME": str(state_home)}), \
                    mock.patch.object(watchdog, "resolve_tart_home", return_value=(td, "test")), \
                    mock.patch.object(watchdog.store_health, "probe", side_effect=probes), \
                    mock.patch.object(tcc_wedge, "watchdog_step", side_effect=RuntimeError("boom")):
                for _ in range(2):
                    result = watchdog.store_health_pass(
                        participation_file=str(Path(td) / "participation"), status_only=False,
                        dry_run=False, failure_threshold=2)
            saved = store_health.load(str(state_home / "state" / "store-health.json"))
        self.assertEqual(result["state"], "draining")
        self.assertEqual(result["classification"]["kind"], "classifier_error")
        self.assertEqual(result["action"], "report-only")
        self.assertEqual(saved["state"], "draining")

    def test_no_signature_is_the_negative_control(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first, second, saved, heal_calls, _ = self.run_passes(td, scan_total=0)
        self.assertEqual(second["state"], "draining")
        self.assertEqual(second["classification"]["kind"], "stalled_unclassified")
        self.assertEqual(heal_calls, [], "no signature, no root action")
        self.assertNotIn("heal", second)
        self.assertEqual(second["action"], "report-only")
        self.assertIsInstance(saved.get("failing_since"), (int, float))

    def test_missing_wrapper_records_the_gap_and_escalates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, second, saved, heal_calls, _ = self.run_passes(td, scan_total=3,
                                                              wrapper_present=False)
        self.assertEqual(heal_calls, [])
        self.assertEqual(second["heal"]["outcome"], "wrapper_missing")
        self.assertEqual(second["state"], "draining")
        self.assertNotIn("last_heal", saved, "a missing wrapper spends no heal budget")

    def test_a_heal_that_does_not_recover_the_store_keeps_draining(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            _, second, saved, heal_calls, _ = self.run_passes(td, scan_total=3,
                                                              reprobe_ok=False)
        self.assertEqual(len(heal_calls), 1)
        self.assertEqual(second["heal"]["outcome"], "ran")
        self.assertFalse(second["heal"]["reprobe"]["ok"])
        self.assertEqual(second["state"], "draining")
        self.assertEqual(saved["last_heal"]["outcome"], "ran")

    def launchd_passes(self, td: str, env: dict) -> tuple:
        import tcc_wedge
        state_home = Path(td) / "state-home"
        wrapper = Path(td) / "tartci-heal-tcc"
        wrapper.write_text("#!/bin/sh\nexit 0\n")
        wrapper.chmod(0o755)
        heal_calls: list = []
        launchd_calls: list = []

        def fake_launchd_probe(store_path, internal_dir, timeout_s):
            launchd_calls.append((store_path, internal_dir, timeout_s))
            return {"outcome": "hung_on_root", "volume_root": "/Volumes/Example",
                    "started_epoch": 1.0, "waited_s": timeout_s}
        probes = [{"ok": False, "reason": "timeout after 10s"}] * 2 + [
            {"ok": True, "device_id": "7", "latency_ms": 8.1, "path": "/Volumes/Example/VMs"}]
        with mock.patch.dict(os.environ, dict(env, TARTCI_HOME=str(state_home),
                                              TARTCI_TCC_CONTEXT_PROBE="/usr/bin/true")), \
                mock.patch.object(watchdog, "resolve_tart_home",
                                  return_value=("/Volumes/Example/VMs", "test")), \
                mock.patch.object(watchdog.store_health, "probe", side_effect=probes), \
                mock.patch.object(tcc_wedge, "WRAPPER", str(wrapper)), \
                mock.patch.object(tcc_wedge, "launchd_probe", fake_launchd_probe), \
                mock.patch.object(tcc_wedge, "scan_ipc",
                                  lambda start, end: {"state": "absent", "unanswered": 0}), \
                mock.patch.object(tcc_wedge, "scan_log",
                                  lambda window_s, before=None: {
                                      "ok": True, "window_s": window_s, "total": 0,
                                      "by_signature": {"removable_volumes_auth_failed": 0}}), \
                mock.patch.object(tcc_wedge, "control_probe",
                                  lambda path, timeout_s: {"ok": True, "path": path}), \
                mock.patch.object(tcc_wedge, "wrapper_insecurity", lambda path: ""), \
                mock.patch.object(tcc_wedge, "run_privileged_bounded",
                                  lambda argv, timeout: (heal_calls.append(argv), 0, "")[1:]):
            results = [watchdog.store_health_pass(
                participation_file=str(Path(td) / "participation"), status_only=False,
                dry_run=False, failure_threshold=2) for _ in range(2)]
        return results, heal_calls, launchd_calls, state_home

    def test_the_watchdog_runs_the_fresh_launchd_probe_against_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (first, second), heal_calls, launchd_calls, state_home = self.launchd_passes(td, {})
        self.assertEqual(first["probe_context"], "launchd watchdog (launchd job)")
        self.assertEqual(launchd_calls, [("/Volumes/Example/VMs",
                                          str(state_home / "state"), 10.0)])
        self.assertEqual(second["classification"]["kind"], "tcc_attribution_wedge")
        self.assertEqual(second["classification"]["path"], "launchd_root_hang")
        self.assertEqual(len(heal_calls), 1)
        self.assertEqual(second["state"], "healthy")

    def test_the_launchd_probe_switch_leaves_a_silent_hang_unclassified(self) -> None:
        # Negative control: identical readings without the launchd probe.
        with tempfile.TemporaryDirectory() as td:
            (_, second), heal_calls, launchd_calls, _ = self.launchd_passes(
                td, {"TARTCI_TCC_LAUNCHD_PROBE": "0"})
        self.assertEqual(launchd_calls, [])
        self.assertEqual(heal_calls, [])
        self.assertEqual(second["classification"]["kind"], "stalled_unclassified")

    def test_a_disabled_launchd_probe_is_named_in_the_classification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (_, second), _, _, _ = self.launchd_passes(
                td, {"TARTCI_TCC_LAUNCHD_PROBE": "0"})
        evidence = second["classification"]["evidence"]["launchd_probe"]
        self.assertEqual(evidence["outcome"], "disabled")
        self.assertIn("TARTCI_TCC_LAUNCHD_PROBE=0", evidence["detail"])
        self.assertIn("TARTCI_TCC_LAUNCHD_PROBE=0", second["classification"]["reason"])

    def test_the_second_wedge_within_the_interval_is_rate_limited(self) -> None:
        import tcc_wedge
        with tempfile.TemporaryDirectory() as td:
            _, _, saved, heal_calls, wrapper = self.run_passes(td, scan_total=3)
            state_home = Path(td) / "state-home"
            # The store wedges again right after the heal: classify, but no second root action.
            probes = [{"ok": False, "reason": "timeout after 10s"}] * 2
            with mock.patch.dict(os.environ, {"TARTCI_HOME": str(state_home)}), \
                    mock.patch.object(watchdog, "resolve_tart_home", return_value=(td, "test")), \
                    mock.patch.object(watchdog.store_health, "probe", side_effect=probes), \
                    mock.patch.object(tcc_wedge, "WRAPPER", str(wrapper)), \
                    mock.patch.object(tcc_wedge, "scan_log",
                                      lambda window_s, before=None: {
                                          "ok": True, "window_s": window_s, "total": 9,
                                          "by_signature": {"removable_volumes_auth_failed": 9}}), \
                    mock.patch.object(tcc_wedge, "control_probe",
                                      lambda path, timeout_s: {"ok": True, "path": path}), \
                    mock.patch.object(tcc_wedge, "wrapper_insecurity", lambda path: ""), \
                    mock.patch.object(tcc_wedge, "run_privileged_bounded",
                                      lambda argv, timeout: (heal_calls.append(argv), 0, "")[1:]):
                for _ in range(2):
                    again = watchdog.store_health_pass(
                        participation_file=str(Path(td) / "participation"), status_only=False,
                        dry_run=False, failure_threshold=2)
        self.assertEqual(len(heal_calls), 1, "one wrapper run per interval")
        self.assertEqual(again["classification"]["kind"], "tcc_attribution_wedge")
        self.assertEqual(again["heal"]["outcome"], "rate_limited")


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        result = store_health.probe(td, timeout_s=1)
        assert result["ok"] is True, result
        assert result["device_id"]
        assert not list(Path(td).iterdir()), "probe must not create files"
        cli = subprocess.run(
            [sys.executable, str(Path(store_health.__file__)), "--path", td, "--json"],
            capture_output=True, text=True, check=False,
        )
        assert cli.returncode == 0, cli.stderr
        assert '"ok": true' in cli.stdout

    with mock.patch.object(
        store_health,
        "run_bounded",
        side_effect=store_health.ObservationError(
            "tart_store_probe", "timeout", "timed out after 0.01s"),
    ):
        result = store_health.probe("/wedged/Atelier", timeout_s=0.01)
    assert result["ok"] is False
    assert result["reason"] == "timeout after 0.01s"

    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "does-not-exist"
        result = store_health.probe(str(missing), timeout_s=1)
        assert result["ok"] is False, result
        assert not missing.exists(), "probe must not create a missing store"

    healthy = store_health.next_state({}, {"ok": True})
    assert healthy["state"] == "healthy"
    first = store_health.next_state({}, {"ok": False, "reason": "EINTR"})
    assert first["state"] == "suspect"
    second = store_health.next_state(first, {"ok": False, "reason": "timeout"})
    assert second["state"] == "draining"
    assert second["drain_required"] is True
    recovered = store_health.next_state(
        second, {"ok": True, "device_id": "same", "path": "/Volumes/Example/VMs"})
    assert recovered["state"] == "healthy"
    assert recovered["consecutive_failures"] == 0
    assert recovered["drain_required"] is False
    changed = store_health.next_state(
        {"last_result": {"ok": True, "device_id": "old"}},
        {"ok": True, "device_id": "new"}, threshold=1)
    assert changed["state"] == "draining"
    assert "device identity changed" in changed["last_result"]["reason"]

    with tempfile.TemporaryDirectory() as td:
        state_home = Path(td) / "state-home"
        participation = Path(td) / "participation"
        participation.write_text("1\n")
        with mock.patch.dict(os.environ, {"TARTCI_HOME": str(state_home),
                                          "TARTCI_STORE_HEALTH_DRAIN": "1"}), \
                mock.patch.object(watchdog, "resolve_tart_home", return_value=(td, "test")), \
                mock.patch.object(watchdog.store_health, "probe", side_effect=[
                    {"ok": False, "reason": "timeout"},
                    {"ok": False, "reason": "EINTR"},
                ]), \
                mock.patch.object(watchdog.subprocess, "run") as run, \
                mock.patch.object(watchdog, "pool_participating", return_value=True):
            run.return_value.returncode = 0
            watchdog.store_health_pass(
                participation_file=str(participation), status_only=False,
                dry_run=False, failure_threshold=2)
            result = watchdog.store_health_pass(
                participation_file=str(participation), status_only=False,
                dry_run=False, failure_threshold=2)
        assert result["state"] == "draining"
        assert result["action"] == "drained"
        assert any(call.args[0][1:3] == ["pool", "drain"] for call in run.call_args_list)
    print("PASS store health")


if __name__ == "__main__":
    main()
