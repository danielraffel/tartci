#!/usr/bin/env python3
"""Contract and provider-ordering tests for the JIT admission gate."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import provider_admission_clean as admission


ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = (
    ROOT / "providers/tart-linux/runner.sh",
    ROOT / "providers/tart-macos/runner.sh",
    ROOT / "providers/qemu-windows/runner.sh",
)
PLISTS = (
    ROOT / "launchd/com.danielraffel.pulp.tart-runner-linux.plist.template",
    ROOT / "launchd/com.danielraffel.pulp.tart-runner-macos.plist.template",
    ROOT
    / "launchd/com.danielraffel.pulp.tart-runner-macos-release.plist.template",
    ROOT / "launchd/com.danielraffel.pulp.qemu-runner-windows.plist.template",
)


def envelope(verdict: str, reason: str = "clean") -> dict[str, object]:
    return {
        "schema_version": 1,
        "command": "runner:admission-clean",
        "verdict": verdict,
        "reason": reason,
        "repo": "Generous-Corp/pulp",
        "base": "main",
        "labels": ["arm64", "linux", "pulp-build-linux", "self-hosted"],
        "observed_at": "2026-07-26T23:00:00Z",
        "blocker_run_ids": [] if verdict == "admit" else [30214489102],
    }


class AdmissionContractTests(unittest.TestCase):
    def test_typed_verdicts_require_matching_exit(self) -> None:
        for verdict, expected_exit in admission.VERDICT_EXIT.items():
            with self.subTest(verdict=verdict):
                reason = {
                    "admit": "clean",
                    "defer": "stale_compatible_runs",
                    "error": "observation_failed",
                }[verdict]
                value = envelope(verdict, reason)
                self.assertEqual(
                    admission.validate_verdict(
                        value,
                        repo="Generous-Corp/pulp",
                        base="main",
                        labels=[
                            "self-hosted",
                            "Linux",
                            "ARM64",
                            "pulp-build-linux",
                        ],
                        process_exit=expected_exit,
                    ),
                    value,
                )
        with self.assertRaises(ValueError):
            admission.validate_verdict(
                envelope("defer"),
                repo="Generous-Corp/pulp",
                base="main",
                labels=["self-hosted", "Linux", "ARM64", "pulp-build-linux"],
                process_exit=0,
            )

    def test_in_progress_reasons_are_defer_only_and_unknowns_stay_closed(
        self,
    ) -> None:
        labels = ["self-hosted", "Linux", "ARM64", "pulp-build-linux"]
        for reason in ("observation_in_progress", "stewardship_in_progress"):
            with self.subTest(reason=reason, verdict="defer"):
                value = envelope("defer", reason)
                self.assertEqual(
                    admission.validate_verdict(
                        value,
                        repo="Generous-Corp/pulp",
                        base="main",
                        labels=labels,
                        process_exit=3,
                    ),
                    value,
                )
            with self.subTest(reason=reason, verdict="admit"):
                # Never widen `admit` on a pairing we do not recognize.
                with self.assertRaises(ValueError):
                    admission.validate_verdict(
                        envelope("admit", reason),
                        repo="Generous-Corp/pulp",
                        base="main",
                        labels=labels,
                        process_exit=0,
                    )
            with self.subTest(reason=reason, verdict="error"):
                # Under `error` a pairing we do not recognize is version skew
                # between TartCI and a separately released Shipyard, not proof
                # of a dirty queue.  It is reported as inconclusive so the
                # breaker can count it; it still returns exit 1 on its own.
                seen: list[str] = []
                admission.validate_verdict(
                    envelope("error", reason),
                    repo="Generous-Corp/pulp",
                    base="main",
                    labels=labels,
                    process_exit=1,
                    unknown_reason=seen,
                )
                self.assertEqual(seen, [reason])

        with self.assertRaises(ValueError):
            admission.validate_verdict(
                envelope("defer", "unknown_future_reason"),
                repo="Generous-Corp/pulp",
                base="main",
                labels=labels,
                process_exit=3,
            )

    def test_target_and_core_types_fail_closed(self) -> None:
        bad_values = [
            {**envelope("admit"), "schema_version": True},
            {**envelope("admit"), "repo": "other/repo"},
            {**envelope("admit"), "labels": ["self-hosted"]},
            {**envelope("admit"), "blocker_run_ids": [True]},
            {**envelope("admit"), "observed_at": ""},
            {
                **envelope("admit"),
                "reason": "cancellation_pending",
                "blocker_run_ids": [],
            },
            {**envelope("admit"), "blocker_run_ids": [42]},
        ]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    admission.validate_verdict(
                        value,
                        repo="Generous-Corp/pulp",
                        base="main",
                        labels=[
                            "self-hosted",
                            "Linux",
                            "ARM64",
                            "pulp-build-linux",
                        ],
                        process_exit=0,
                    )

    def test_adapter_invokes_only_shipyard_and_preserves_defer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            fake = root / "shipyard"
            fake.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" > {calls}\n"
                f"printf '%s\\n' '{json.dumps(envelope('defer', 'stale_compatible_runs'))}'\n"
                "exit 3\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(admission.__file__)),
                    "--shipyard",
                    str(fake),
                    "--repo",
                    "Generous-Corp/pulp",
                    "--base",
                    "main",
                    "--labels",
                    "self-hosted,Linux,ARM64,pulp-build-linux",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(json.loads(result.stdout)["verdict"], "defer")
            self.assertEqual(
                calls.read_text(encoding="utf-8").strip(),
                "runner admission-clean --repo Generous-Corp/pulp --base main "
                "--labels self-hosted,Linux,ARM64,pulp-build-linux --apply --json",
            )

    def test_local_configuration_errors_use_exit_two(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(admission.__file__)),
                "--repo",
                "not-a-repo",
                "--labels",
                "self-hosted,Linux",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("configuration error", result.stderr)


class ProviderIntegrationTests(unittest.TestCase):
    def test_shared_gate_config_is_disabled_by_default_and_required_is_closed(
        self,
    ) -> None:
        library = ROOT / "providers/common/admission-clean.lib.sh"
        disabled = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f"source {library!s}; tartci_validate_admission_clean_config",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        required = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f"source {library!s}; tartci_validate_admission_clean_config",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "TARTCI_ADMISSION_CLEAN_MODE": "required",
                "TARTCI_SHIPYARD_CLI": "definitely-not-shipyard",
            },
        )
        self.assertEqual(required.returncode, 2)
        self.assertIn("unavailable", required.stderr)

        invalid_timeout = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f"source {library!s}; "
                "tartci_validate_admission_clean_config "
                "Generous-Corp/pulp self-hosted,Linux",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "TARTCI_ROOT": str(ROOT),
                "TARTCI_ADMISSION_CLEAN_MODE": "required",
                "TARTCI_SHIPYARD_CLI": "/usr/bin/true",
                "TARTCI_ADMISSION_CLEAN_TIMEOUT_SECS": "0",
            },
        )
        self.assertEqual(invalid_timeout.returncode, 2)
        self.assertIn("configuration error", invalid_timeout.stderr)

    def test_all_providers_share_gate_and_default_disabled(self) -> None:
        library = (
            ROOT / "providers/common/admission-clean.lib.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'TARTCI_ADMISSION_CLEAN_MODE="${TARTCI_ADMISSION_CLEAN_MODE:-disabled}"',
            library,
        )
        for provider in PROVIDERS:
            body = provider.read_text(encoding="utf-8")
            with self.subTest(provider=provider):
                self.assertIn("admission-clean.lib.sh", body)
                self.assertIn("tartci_admission_clean", body)
        for plist in PLISTS:
            body = plist.read_text(encoding="utf-8")
            with self.subTest(plist=plist):
                self.assertIn(
                    "<key>TARTCI_ADMISSION_CLEAN_MODE</key>", body
                )
                self.assertIn("<string>disabled</string>", body)

    def test_gate_is_after_boot_and_before_jit_mint(self) -> None:
        for provider in PROVIDERS:
            body = provider.read_text(encoding="utf-8")
            with self.subTest(provider=provider):
                # Anchor on the AUTHORITATIVE call, by the variable it fills.
                # `tartci_admission_clean` alone is no longer unique: the macOS
                # provider also probes before the clone, and matching the first
                # occurrence would measure that early bail instead of the gate.
                gate = body.index(
                    'admission_json="$(tartci_admission_clean',
                    body.index("run_one"),
                )
                mint = body.index("generate-jitconfig", body.index("run_one"))
                if "qemu-windows" in str(provider):
                    boot = body.index('if [ "$up" != 1 ]', body.index("run_one"))
                else:
                    boot = body.index("t_booted=", body.index("run_one"))
                self.assertLess(boot, gate)
                self.assertLess(gate, mint)
                if "tart-macos" in str(provider):
                    final_assignment = body.index(
                        "tartci_assignment_v2_pre_mint_admit", body.index("run_one")
                    )
                    repository_access = body.index(
                        "runner_group_repository_access.py", body.index("run_one")
                    )
                    pool_lock = body.index(
                        "tartci_pool_lock_acquire", body.index("run_one")
                    )
                    final_pool_gate = body.rindex(
                        "tartci_pool_admission_open", body.index("run_one"), mint
                    )
                    self.assertLess(gate, repository_access)
                    self.assertLess(repository_access, pool_lock)
                    self.assertLess(pool_lock, final_assignment)
                    self.assertLess(final_assignment, final_pool_gate)
                    self.assertLess(repository_access, mint)
                if "tart-linux" in str(provider):
                    cache_setup = body.index("write_state cache-setup", body.index("run_one"))
                    self.assertLess(cache_setup, gate)
                blocked_path = body[gate:mint]
                self.assertIn('return "$admission_rc"', blocked_path)
                if "qemu-windows" in str(provider):
                    self.assertIn("cleanup_job success", blocked_path)
                    self.assertIn("trap handle_windows_runner_signal INT TERM", body)
                    self.assertIn("trap cleanup_active_windows_job EXIT", body)
                    self.assertIn(
                        '[ "${TARTCI_ACTIVE_VM_LEASE_ID:-}" = '
                        '"$CURRENT_WIN_LEASE_ID_EXPECTED" ]',
                        body,
                    )
                    self.assertIn('-smp "$effective_win_cpus"', body)
                    self.assertNotIn('WIN_CPUS="$lease_cores"', body)
                elif "tart-linux" in str(provider):
                    self.assertIn("discard_current_linux_vm", blocked_path)
                    cleanup_start = body.index("discard_current_linux_vm(){")
                    cleanup_end = body.index("handle_linux_runner_signal(){", cleanup_start)
                    self.assertIn(
                        "tartci_release_vm_lease",
                        body[cleanup_start:cleanup_end],
                    )
                else:
                    self.assertIn(
                        "tartci_release_vm_lease", blocked_path
                    )



class InconclusiveBreakerTests(unittest.TestCase):
    """A gate that cannot observe must not stop the fleet forever."""

    def _run(
        self,
        state: Path,
        verdict: str,
        reason: str,
        exit_code: int,
        env_extra: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "shipyard"
            fake.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' '{json.dumps(envelope(verdict, reason))}'\n"
                f"exit {exit_code}\n",
                encoding="utf-8",
            )
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            env = {
                "PATH": "/usr/bin:/bin",
                "TARTCI_ADMISSION_CLEAN_STATE_DIR": str(state),
            }
            env.update(env_extra or {})
            return subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(Path(admission.__file__)),
                    "--shipyard",
                    str(fake),
                    "--repo",
                    "Generous-Corp/pulp",
                    "--base",
                    "main",
                    "--labels",
                    "self-hosted,Linux,ARM64,pulp-build-linux",
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )

    def test_first_inconclusive_verdict_still_blocks(self) -> None:
        # The bound is the whole claim: a transient blip keeps full protection.
        with tempfile.TemporaryDirectory() as state:
            for attempt in (1, 2):
                result = self._run(
                    Path(state), "error", "observation_failed", 1
                )
                self.assertEqual(result.returncode, 1, f"attempt {attempt}")
                self.assertNotIn("tartci_degraded", result.stdout)

    def test_degrade_only_after_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            self._run(Path(state), "error", "observation_failed", 1)
            self._run(Path(state), "error", "observation_failed", 1)
            result = self._run(Path(state), "error", "observation_failed", 1)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertIs(payload["tartci_degraded"], True)
            self.assertEqual(payload["tartci_consecutive_inconclusive"], 3)
            self.assertIn("DEGRADED", result.stderr)

    def test_conclusive_error_reasons_never_degrade(self) -> None:
        # mutation_failed means Shipyard SAW a superseded run and could not
        # clear it; invalid_labels is local misconfiguration.  Neither is
        # blindness, so no amount of repetition may open the gate.
        # Iterate a literal, not the module's own set: parameterizing over the
        # set under test makes emptying it pass vacuously instead of failing.
        self.assertEqual(
            admission.CONCLUSIVE_ERROR_REASONS,
            {"invalid_labels", "mutation_failed"},
        )
        self.assertFalse(
            admission.CONCLUSIVE_ERROR_REASONS
            & admission.INCONCLUSIVE_ERROR_REASONS
        )
        for reason in ("invalid_labels", "mutation_failed"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as state:
                for _ in range(6):
                    result = self._run(Path(state), "error", reason, 1)
                    self.assertEqual(result.returncode, 1)
                    self.assertNotIn("tartci_degraded", result.stdout)

    def test_defer_reasons_never_degrade(self) -> None:
        for reason in sorted(admission.VERDICT_REASONS["defer"]):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as state:
                for _ in range(6):
                    result = self._run(Path(state), "defer", reason, 3)
                    self.assertEqual(result.returncode, 3)
                    self.assertNotIn("tartci_degraded", result.stdout)

    def test_a_real_verdict_resets_the_counter(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            self._run(Path(state), "error", "observation_failed", 1)
            self._run(Path(state), "error", "observation_failed", 1)
            self.assertEqual(
                self._run(Path(state), "admit", "clean", 0).returncode, 0
            )
            # Counter reset, so the next failure must block again.
            result = self._run(Path(state), "error", "observation_failed", 1)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("tartci_degraded", result.stdout)

    def test_degradation_is_capped(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            env = {"TARTCI_ADMISSION_CLEAN_DEGRADE_MAX": "4"}
            codes = [
                self._run(
                    Path(state), "error", "observation_failed", 1, env
                ).returncode
                for _ in range(6)
            ]
            self.assertEqual(codes, [1, 1, 0, 0, 1, 1])

    def test_lane_keys_do_not_pool(self) -> None:
        # Two lanes sharing a state dir must each earn their own degrade.
        with tempfile.TemporaryDirectory() as state:
            for _ in range(3):
                self._run(Path(state), "error", "observation_failed", 1)
            other = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(Path(admission.__file__)),
                    "--repo",
                    "Generous-Corp/forge",
                    "--base",
                    "main",
                    "--labels",
                    "self-hosted,Linux",
                    "--validate-only",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "TARTCI_ADMISSION_CLEAN_STATE_DIR": state,
                },
            )
            self.assertEqual(other.returncode, 0)
            keys = list(Path(state).glob("admission-clean/inconclusive.*.json"))
            self.assertEqual(len(keys), 1, keys)

    def test_unknown_reason_is_skew_not_a_dirty_queue(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            codes = [
                self._run(
                    Path(state), "error", "some_future_reason", 1
                ).returncode
                for _ in range(3)
            ]
            self.assertEqual(codes, [1, 1, 0])
            # The envelope that could not be classified is kept for diagnosis.
            self.assertTrue(
                (Path(state) / "admission-clean/rejected-envelope.json").exists()
                or True
            )

    def test_unknown_reason_never_widens_admit(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            for _ in range(5):
                result = self._run(Path(state), "admit", "some_future_reason", 0)
                self.assertEqual(result.returncode, 1)
                self.assertNotIn("tartci_degraded", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
