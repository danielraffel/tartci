#!/usr/bin/env python3
"""The opt-in admission precheck overlap, driven through the real code.

The serial pre-clone precheck asks Shipyard the same admission question the
parallel boundary proof asks again beside the clone and boot. In the
2026-11-07..09 lane logs it held the VM slot for p50 37-48 s on every served
job (about 6 slot-hours a day fleet-wide). With TARTCI_ADMISSION_PRECHECK_OVERLAP=1
run_one skips it, the parallel proof answers it, and the JIT boundary refuses
exactly as before. A boundary refusal on an overlapped run opens a backoff
during which the serial precheck runs again, so a lane whose admission
authority is down cannot boot a VM every poll.

Every behavioural assertion here has a control beside it:

* overlap on: the run reaches the clone without the serial precheck, and the
  parallel proof is started before the clone; control: knob off (the default),
  the same refusal never reaches the clone;
* backoff open: the serial precheck runs again and a refusal stops the clone;
  control: an expired backoff overlaps again;
* a serial admit closes the backoff; a serial refusal leaves it open;
* the boundary opens the backoff only for an overlapped run, only on refusal;
* parallel proofs off, or a parked warm VM, keep the serial precheck;
* configuration validation, profile rendering and profile validation.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import testing_support  # noqa: E402,F401
import test_admission_precheck as precheck  # noqa: E402
import test_boundary_proof_parallel as boundary  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PROOF_LIB = ROOT / "providers/tart-macos/boundary-proof.lib.sh"
RUNNER = ROOT / "providers/tart-macos/runner.sh"
OVERLAP_ON = {"TARTCI_BOUNDARY_PROOF_PARALLEL": "1", "TARTCI_ADMISSION_PRECHECK_OVERLAP": "1",
              "JIT_GH_CLI": "stub-gh"}


def counting_shipyard(h: precheck.RunOneHarness, verdict: str, reason: str, code: int) -> Path:
    """A stub shipyard that logs every call, so a test can count the asks."""
    calls = h.tmp / "shipyard-calls"
    payload = h.tmp / "verdict.json"
    import json  # noqa: PLC0415
    payload.write_text(json.dumps(precheck.make_envelope(verdict, reason)), encoding="utf-8")
    path = h.bin / "stub-shipyard"
    path.write_text(
        "#!/bin/bash\n"
        f"echo call >>{str(calls)!r}\n"
        f"cat {str(payload)!r}\nprintf '\\n'\nexit {code}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    gh = h.bin / "stub-gh"
    gh.write_text("#!/bin/bash\necho '{}'\n", encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return calls


def wait_for_proof(h: precheck.RunOneHarness) -> None:
    """The harness exits at clone_start; let the detached proof finish so the
    temporary directory is not removed under it."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        done = list(h.state.glob("*.boundary-proof/access.rc"))
        if done and list(h.state.glob("*.boundary-proof/admission.rc")):
            return
        time.sleep(0.1)
    raise AssertionError("the overlapped run never finished its parallel proof")


def backoff_file(h: precheck.RunOneHarness) -> Path:
    return h.state / "lane-01.precheck-overlap-backoff"


class OverlapSkipsTheSerialPrecheckTests(unittest.TestCase):
    def test_overlap_reaches_the_clone_without_the_serial_precheck(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = precheck.RunOneHarness(Path(raw))
            # A refusing authority: only the boundary may act on it now.
            counting_shipyard(h, "error", "observation_failed", 1)
            result = h.run(env_overrides=OVERLAP_ON)
            wait_for_proof(h)
            self.assertEqual(result.returncode, precheck.CLONE_REACHED_EXIT, result.stderr)
            names = h.event_names()
            self.assertIn("admission_precheck_overlap", names)
            self.assertNotIn("admission_precheck", names)
            self.assertNotIn("admission_precheck_error", names)
            # The proof that replaces it starts after the lease, before the clone.
            self.assertLess(names.index("admission_parallel_start"), names.index("clone_start"))
            self.assertIn("admission-precheck", h.heartbeats.read_text().split())

    def test_control_knob_off_keeps_the_serial_precheck(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = precheck.RunOneHarness(Path(raw))
            calls = counting_shipyard(h, "error", "observation_failed", 1)
            result = h.run(env_overrides={"TARTCI_BOUNDARY_PROOF_PARALLEL": "1"})
            self.assertEqual(result.returncode, 1, result.stderr)
            names = h.event_names()
            self.assertIn("admission_precheck_error", names)
            self.assertNotIn("clone_start", names)
            self.assertNotIn("admission_precheck_overlap", names)
            self.assertEqual(len(calls.read_text().split()), 1)

    def test_parallel_proofs_off_keeps_the_serial_precheck(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = precheck.RunOneHarness(Path(raw))
            counting_shipyard(h, "error", "observation_failed", 1)
            result = h.run(env_overrides={"TARTCI_ADMISSION_PRECHECK_OVERLAP": "1",
                                          "TARTCI_BOUNDARY_PROOF_PARALLEL": "0"})
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("admission_precheck_error", h.event_names())
            self.assertNotIn("clone_start", h.event_names())


class BackoffTests(unittest.TestCase):
    def test_open_backoff_runs_the_serial_precheck_again(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = precheck.RunOneHarness(Path(raw))
            counting_shipyard(h, "defer", "stale_compatible_runs", 3)
            opened = int(time.time()) - 300
            backoff_file(h).write_text(f"{opened}\n")
            result = h.run(env_overrides=OVERLAP_ON)
            self.assertEqual(result.returncode, 3, result.stderr)
            names = h.event_names()
            self.assertIn("admission_precheck_deferred", names)
            self.assertNotIn("clone_start", names)
            # A serial refusal keeps the backoff open: it re-stamps it, so the
            # overlap comes back only after an admit.
            self.assertGreater(int(backoff_file(h).read_text()), opened)
            self.assertIn("stage=serial", h.detail_for("precheck_overlap_backoff"))

    def test_control_expired_backoff_overlaps_again(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = precheck.RunOneHarness(Path(raw))
            counting_shipyard(h, "defer", "stale_compatible_runs", 3)
            backoff_file(h).write_text(f"{int(time.time()) - 601}\n")
            result = h.run(env_overrides=OVERLAP_ON)
            wait_for_proof(h)
            self.assertEqual(result.returncode, precheck.CLONE_REACHED_EXIT, result.stderr)
            self.assertIn("admission_precheck_overlap", h.event_names())

    def test_a_serial_admit_closes_the_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = precheck.RunOneHarness(Path(raw))
            counting_shipyard(h, "admit", "clean", 0)
            backoff_file(h).write_text(f"{int(time.time())}\n")
            result = h.run(env_overrides=OVERLAP_ON)
            wait_for_proof(h)
            self.assertEqual(result.returncode, precheck.CLONE_REACHED_EXIT, result.stderr)
            names = h.event_names()
            self.assertIn("admission_precheck", names)
            self.assertIn("precheck_overlap_backoff_cleared", names)
            self.assertFalse(backoff_file(h).exists())

    def test_unreadable_or_future_backoff_counts_as_open(self) -> None:
        for content in ("garbage\n", f"{int(time.time()) + 86400}\n", ""):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as raw:
                h = precheck.RunOneHarness(Path(raw))
                counting_shipyard(h, "error", "observation_failed", 1)
                backoff_file(h).write_text(content)
                result = h.run(env_overrides=OVERLAP_ON)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn("clone_start", h.event_names())

    def test_zero_window_never_backs_off(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = precheck.RunOneHarness(Path(raw))
            counting_shipyard(h, "error", "observation_failed", 1)
            backoff_file(h).write_text(f"{int(time.time())}\n")
            result = h.run(env_overrides={**OVERLAP_ON,
                                          "TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS": "0"})
            wait_for_proof(h)
            self.assertEqual(result.returncode, precheck.CLONE_REACHED_EXIT, result.stderr)


class BoundaryOpensTheBackoffTests(unittest.TestCase):
    """The real boundary block, fed by the real parallel proof."""

    def _run(self, verdict: tuple[str, str, int], overlapped: int) -> tuple[subprocess.CompletedProcess, boundary.Harness]:
        raw = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, raw, True)
        h = boundary.Harness(Path(raw))
        h.stub_shipyard([verdict])
        h.stub_gh()
        result = h.run(
            boundary.boundary_function()
            + "RUNNER_NAME=lane-01\n"
            + f"PRECHECK_OVERLAP_RUN={overlapped}\n"
            + "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 1\n"
            + "rc=0; boundary || rc=$?\nexit $rc\n",
            env={"TARTCI_ADMISSION_PRECHECK_OVERLAP": "1"},
        )
        return result, h

    def test_an_overlapped_refusal_discards_and_opens_the_backoff(self) -> None:
        result, h = self._run(("defer", "stale_compatible_runs", 3), overlapped=1)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(h.discards.read_text().split(), ["discard"])
        self.assertIn("precheck_overlap_backoff", h.event_names())
        self.assertIn("stage=boundary", h.events.read_text())
        stamp = (h.state / "lane-01.precheck-overlap-backoff").read_text().strip()
        self.assertLessEqual(abs(int(stamp) - time.time()), 30)

    def test_control_a_serial_run_refusal_opens_no_backoff(self) -> None:
        result, h = self._run(("defer", "stale_compatible_runs", 3), overlapped=0)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(h.discards.read_text().split(), ["discard"])
        self.assertNotIn("precheck_overlap_backoff", h.event_names())
        self.assertFalse((h.state / "lane-01.precheck-overlap-backoff").exists())

    def test_a_stale_refusal_then_a_fresh_admit_opens_no_backoff(self) -> None:
        raw = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, raw, True)
        h = boundary.Harness(Path(raw))
        # The parallel proof refuses; it is stale by the boundary, and the
        # synchronous ask admits. The run is admitted: no backoff.
        h.stub_shipyard([("defer", "stale_compatible_runs", 3), ("admit", "clean", 0)])
        h.stub_gh()
        result = h.run(
            boundary.boundary_function()
            + "RUNNER_NAME=lane-01\nPRECHECK_OVERLAP_RUN=1\nPRECHECK_OVERLAP_NOTED=0\n"
            + "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 1\n"
            + "sleep 3\n"
            + "rc=0; boundary || rc=$?\n"
            + "tartci_boundary_proof_abandon\nexit $rc\n",
            env={"TARTCI_ADMISSION_PRECHECK_OVERLAP": "1", "TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("admission_parallel_stale", h.event_names())
        self.assertFalse((h.state / "lane-01.precheck-overlap-backoff").exists())
        self.assertNotIn("precheck_overlap_backoff", h.event_names())

    def test_control_an_overlapped_admit_opens_no_backoff(self) -> None:
        result, h = self._run(("admit", "clean", 0), overlapped=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(h.discards.exists())
        self.assertFalse((h.state / "lane-01.precheck-overlap-backoff").exists())


class UnconsumedProofTests(unittest.TestCase):
    """A run that left before its boundary still records its proof's refusal."""

    def _run(self, verdict: tuple[str, str, int], overlapped: int, noted: int = 0
             ) -> tuple[subprocess.CompletedProcess, boundary.Harness]:
        raw = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, raw, True)
        h = boundary.Harness(Path(raw))
        h.stub_shipyard([verdict])
        h.stub_gh()
        result = h.run(
            "RUNNER_NAME=lane-01\n"
            f"PRECHECK_OVERLAP_RUN={overlapped}\nPRECHECK_OVERLAP_NOTED={noted}\n"
            "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 1\n"
            "_tartci_boundary_proof_join\n"
            # Dropping the proof is what records it: every exit path drops it.
            "tartci_boundary_proof_abandon\n",
            env={"TARTCI_ADMISSION_PRECHECK_OVERLAP": "1"},
        )
        return result, h

    def test_an_unconsumed_refusal_opens_the_backoff(self) -> None:
        result, h = self._run(("error", "observation_failed", 1), overlapped=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((h.state / "lane-01.precheck-overlap-backoff").exists())
        self.assertIn("stage=pre_boundary", h.events.read_text())

    def test_controls_admit_serial_run_or_already_noted_record_nothing(self) -> None:
        for verdict, overlapped, noted in ((("admit", "clean", 0), 1, 0),
                                           (("error", "observation_failed", 1), 0, 0),
                                           (("error", "observation_failed", 1), 1, 1)):
            with self.subTest(verdict=verdict[0], overlapped=overlapped, noted=noted):
                result, h = self._run(verdict, overlapped, noted)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((h.state / "lane-01.precheck-overlap-backoff").exists())
                self.assertNotIn("precheck_overlap_backoff", h.event_names())


class LibraryTests(unittest.TestCase):
    def _bash(self, script: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as raw:
            full = os.environ.copy()
            full.update(env)
            return subprocess.run(
                ["/bin/bash", "-c",
                 "set -euo pipefail\n"
                 f"source {str(PROOF_LIB)!r}\n"
                 "event(){ :; }\n"
                 f"STATE_DIR={raw!r}\nRUNNER_NAME=lane-01\n" + script],
                env=full, text=True, capture_output=True, check=False, timeout=30)

    def test_a_parked_warm_vm_keeps_the_serial_precheck(self) -> None:
        on = self._bash("WARM_VM=''\ntartci_precheck_overlap_active && echo yes || echo no\n", OVERLAP_ON)
        self.assertEqual(on.stdout.strip(), "yes", on.stderr)
        warm = self._bash("WARM_VM=parked-vm\ntartci_precheck_overlap_active && echo yes || echo no\n", OVERLAP_ON)
        self.assertEqual(warm.stdout.strip(), "no", warm.stderr)

    def test_the_backoff_is_shared_by_the_lane_slots(self) -> None:
        script = ("RUNNER_NAME=lane-02\n"
                  "tartci_precheck_overlap_note_refusal 3\n"
                  "RUNNER_NAME=lane-01\n"
                  "WARM_VM=''\ntartci_precheck_overlap_active && echo yes || echo no\n")
        shared = self._bash(script, {**OVERLAP_ON, "TARTCI_RUNNER_NAME_PREFIX": "lane"})
        self.assertEqual(shared.stdout.strip(), "no", shared.stderr)
        # Control: without the lane prefix each slot keeps its own file.
        alone = self._bash(script, OVERLAP_ON)
        self.assertEqual(alone.stdout.strip(), "yes", alone.stderr)

    def test_a_leading_zero_stamp_is_decimal(self) -> None:
        script = ("printf '0%s\\n' \"$(date +%s)\" >\"$STATE_DIR/lane-01.precheck-overlap-backoff\"\n"
                  "WARM_VM=''\ntartci_precheck_overlap_active && echo yes || echo no\n")
        result = self._bash(script, OVERLAP_ON)
        self.assertEqual(result.stdout.strip(), "no", result.stderr)

    def test_default_is_off(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("TARTCI_ADMISSION_PRECHECK_OVERLAP")}
        result = subprocess.run(
            ["/bin/bash", "-c", f"set -euo pipefail\nsource {str(PROOF_LIB)!r}\n"
             "WARM_VM=''\ntartci_precheck_overlap_active && echo yes || echo no\n"],
            env=env, text=True, capture_output=True, check=False, timeout=30)
        self.assertEqual(result.stdout.strip(), "no", result.stderr)

    def test_invalid_configuration_is_refused(self) -> None:
        for name, value in (
            ("TARTCI_ADMISSION_PRECHECK_OVERLAP", "yes"),
            ("TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS", "3601"),
            ("TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS", "soon"),
        ):
            with self.subTest(name=name, value=value):
                result = self._bash("tartci_precheck_overlap_validate\n", {name: value})
                self.assertEqual(result.returncode, 2, result.stderr)
        ok = self._bash("tartci_precheck_overlap_validate\n",
                        {"TARTCI_ADMISSION_PRECHECK_OVERLAP": "1",
                         "TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS": "3600"})
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_the_supervisor_validates_the_knobs_at_start(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("tartci_precheck_overlap_validate \\\n  || die", source)


class ProfileTests(unittest.TestCase):
    def _profile(self, extra: str) -> dict:
        import macos_fleet_lanes as fleet  # noqa: PLC0415
        text = (ROOT / "profiles" / "m3-macos-fleet.toml").read_text()
        anchor = 'id = "pulp-gate"\n'
        return fleet.tomllib.loads(text.replace(anchor, anchor + extra, 1))

    @testing_support.requires_tomllib
    def test_renders_on_every_slot_of_the_lane(self) -> None:
        import macos_fleet_lanes as fleet  # noqa: PLC0415
        data = self._profile("admission_precheck_overlap = true\n"
                             "admission_precheck_overlap_backoff_seconds = 300\n")
        lane = next(row for row in data["lane"] if row["id"] == "pulp-gate")
        for slot in (1, 2):
            env = fleet.lane_plist(data, lane, slot=slot)["EnvironmentVariables"]
            self.assertEqual(env["TARTCI_ADMISSION_PRECHECK_OVERLAP"], "1")
            self.assertEqual(env["TARTCI_ADMISSION_PRECHECK_OVERLAP_BACKOFF_SECS"], "300")
        control = self._profile("")
        lane = next(row for row in control["lane"] if row["id"] == "pulp-gate")
        self.assertNotIn("TARTCI_ADMISSION_PRECHECK_OVERLAP",
                         fleet.lane_plist(control, lane)["EnvironmentVariables"])

    def _validate(self, text: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "p.toml"
            path.write_text(text)
            return subprocess.run([sys.executable, str(ROOT / "scripts/macos_fleet_lanes.py"),
                                   "validate", str(path)], text=True, capture_output=True, check=False)

    @testing_support.requires_tomllib
    def test_validation(self) -> None:
        base = (ROOT / "profiles" / "m3-macos-fleet.toml").read_text()
        anchor = 'id = "pulp-gate"\n'
        good = base.replace(anchor, anchor + "admission_precheck_overlap = true\n", 1)
        result = self._validate(good)
        self.assertEqual(result.returncode, 0, result.stderr)
        for bad in ('admission_precheck_overlap = "yes"\n',
                    "admission_precheck_overlap_backoff_seconds = 300\n",
                    "admission_precheck_overlap = true\nadmission_precheck_overlap_backoff_seconds = 3601\n"):
            with self.subTest(bad=bad):
                self.assertNotEqual(self._validate(base.replace(anchor, anchor + bad, 1)).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
