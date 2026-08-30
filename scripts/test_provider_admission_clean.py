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
                gate = body.index("tartci_admission_clean", body.index("run_one"))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
