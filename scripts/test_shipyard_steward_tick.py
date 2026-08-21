#!/usr/bin/env python3
"""Hermetic tests for the deterministic cross-repository steward tick."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).with_name("shipyard_steward_tick.sh")
SERVICE = Path(__file__).with_name("shipyard_queue_service_tick.sh")
SERVICE_SUPPORT = Path(__file__).with_name("shipyard_queue_service_tick.py")


class StewardTickTests(unittest.TestCase):
    def run_tick(
        self,
        *,
        authority_matches: bool = True,
        steward_sleep: int = 0,
        timeout: int = 2,
        reap_only: bool = False,
        incomplete_report: bool = False,
        authority_config: str = "1",
    ) -> tuple[subprocess.CompletedProcess[str], str, dict[str, object] | None]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin",
                 "https://github.com/Generous-Corp/pulp.git"],
                check=True,
            )
            calls = root / "calls"
            shipyard = root / "shipyard"
            shipyard.write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
case "$1 $2" in
  "--version ") printf 'shipyard 0.97.1\\n' ;;
  "merge-queue status") printf '{"held":false,"authority_matches":%s}\\n' "$MATCHES" ;;
  "auth export") printf '{"schema_version":1,"command":"auth.export","bundle":{"version":2}}\\n' ;;
  "runner steward")
    sleep "$STEWARD_SLEEP"
    printf '%s\\n' "$STEWARD_REPORT"
    ;;
  *) exit 97 ;;
esac
""",
                encoding="utf-8",
            )
            ghapp = root / "ghapp"
            ghapp.write_text(
                """#!/bin/sh
printf 'ghapp %s\\n' "$*" >> "$CALLS"
case "$1 $2" in
  "auth token") printf 'app-token\\n' ;;
  "pr list") printf '[]\\n' ;;
  *) exit 98 ;;
esac
""",
                encoding="utf-8",
            )
            shipyard.chmod(0o755)
            ghapp.chmod(0o755)
            config = root / "queue-tick.env"
            config.write_text(
                f"SHIPYARD_QUEUE_REPO_ROOT={repo}\n"
                f"SHIPYARD_QUEUE_AUTHORITY={authority_config}\n"
                "SHIPYARD_QUEUE_GH_CLI=ghapp\n",
                encoding="utf-8",
            )
            config.chmod(0o600)
            health = root / "health.json"
            report = root / "report.json"
            repo_reports = [
                {
                    "repo": repo_name,
                    "base": "main",
                    "allow_auto_merge": False,
                    "merge_queue": True,
                    "merge_path": "native_queue_exact_head",
                    "required_contexts": [],
                    "prs": [],
                    "cancellations": [],
                    "errors": [],
                }
                for repo_name in (
                    "Generous-Corp/forge",
                    "Generous-Corp/pulp",
                    "Generous-Corp/vellum",
                )
            ]
            if incomplete_report:
                repo_reports.pop()
            steward_report = json.dumps(
                {
                    "schema_version": 1,
                    "command": "runner.steward",
                    "apply": True,
                    "handoff_ledger": str(root / "merge-steward.json"),
                    "repos": repo_reports,
                },
                separators=(",", ":"),
            )
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("SHIPYARD_", "TARTCI_"))}
            env.update({
                "HOME": str(root),
                "PATH": f"{root}:/usr/bin:/bin",
                "CALLS": str(calls),
                "MATCHES": "true" if authority_matches else "false",
                "STEWARD_SLEEP": str(steward_sleep),
                "STEWARD_REPORT": steward_report,
                "SHIPYARD_TICK_APPLY": "1",
                "SHIPYARD_TICK_REAP_ONLY": "1" if reap_only else "0",
                "SHIPYARD_QUEUE_CANONICAL_CONFIG": str(config),
                "SHIPYARD_STEWARD_HEALTH_FILE": str(health),
                "SHIPYARD_STEWARD_REPORT_FILE": str(report),
                "SHIPYARD_STEWARD_TIMEOUT_SECS": str(timeout),
            })
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)], env=env, text=True,
                capture_output=True, check=False,
            )
            health_value = json.loads(health.read_text(encoding="utf-8")) if health.exists() else None
            return (
                result,
                calls.read_text(encoding="utf-8") if calls.exists() else "",
                health_value,
            )

    def test_runs_exact_three_repo_apply_command(self) -> None:
        result, calls, health = self.run_tick()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "runner steward --repo Generous-Corp/pulp --repo Generous-Corp/forge "
            "--repo Generous-Corp/vellum --apply --json", calls,
        )
        self.assertEqual(health["status"], "healthy")

    def test_wrong_authority_fails_before_github_or_steward(self) -> None:
        result, calls, health = self.run_tick(authority_matches=False)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("ghapp", calls)
        self.assertNotIn("runner steward", calls)
        self.assertEqual(health["status"], "unhealthy")

    def test_incomplete_repository_receipt_fails_closed(self) -> None:
        result, calls, health = self.run_tick(incomplete_report=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("runner steward", calls)
        self.assertEqual(health["status"], "unhealthy")
        self.assertIn("report is malformed", health["reason"])

    def test_live_mode_without_authority_fails_closed(self) -> None:
        result, calls, health = self.run_tick(authority_config="0")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("ghapp", calls)
        self.assertNotIn("runner steward", calls)
        self.assertEqual(health["status"], "unhealthy")
        self.assertIn("requires SHIPYARD_QUEUE_AUTHORITY=1", health["reason"])

    def test_reap_only_authority_does_not_run_mutating_steward(self) -> None:
        result, calls, health = self.run_tick(reap_only=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("runner steward", calls)
        self.assertEqual(health["status"], "healthy")

    def test_timeout_is_bounded_and_degraded(self) -> None:
        result, calls, health = self.run_tick(steward_sleep=3, timeout=1)
        self.assertEqual(result.returncode, 2)
        self.assertIn("runner steward", calls)
        self.assertEqual(health["status"], "unhealthy")
        self.assertIn("failed or timed out", health["reason"])

    def test_service_starts_both_lanes_before_waiting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(SERVICE, root / SERVICE.name)
            shutil.copy2(SERVICE_SUPPORT, root / SERVICE_SUPPORT.name)
            for name, own, peer in (
                ("shipyard_queue_tick.sh", "legacy", "steward"),
                ("shipyard_steward_tick.sh", "steward", "legacy"),
            ):
                path = root / name
                path.write_text(
                    f"""#!/bin/sh
{('[ "$SHIPYARD_TICK_REAP_ONLY" = "1" ] || exit 9' if own == 'legacy' else ':')}
touch "$STATE/{own}"
i=0
while [ ! -f "$STATE/{peer}" ] && [ "$i" -lt 50 ]; do
  sleep 0.01
  i=$((i + 1))
done
[ -f "$STATE/{peer}" ]
""", encoding="utf-8")
                path.chmod(0o755)
            env = os.environ.copy()
            env["STATE"] = str(root)
            result = subprocess.run(
                ["/bin/bash", str(root / SERVICE.name)], env=env,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_service_stop_terminates_both_child_process_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(SERVICE, root / SERVICE.name)
            shutil.copy2(SERVICE_SUPPORT, root / SERVICE_SUPPORT.name)
            for name, lane in (
                ("shipyard_queue_tick.sh", "legacy"),
                ("shipyard_steward_tick.sh", "steward"),
            ):
                path = root / name
                body = "while :; do sleep 1; done"
                if lane == "steward":
                    body = """echo "$$" > "$STATE/steward-process-group"
python3 -c 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)' &
wait"""
                path.write_text(
                    f"""#!/bin/sh
trap 'touch "$STATE/{lane}-stopped"; exit 0' TERM INT HUP
touch "$STATE/{lane}-started"
{body}
""", encoding="utf-8")
                path.chmod(0o755)
            env = os.environ.copy()
            env["STATE"] = str(root)
            process = subprocess.Popen(
                ["/bin/bash", str(root / SERVICE.name)], env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not all(
                (root / f"{lane}-started").exists()
                for lane in ("legacy", "steward")
            ):
                time.sleep(0.02)
            self.assertTrue((root / "legacy-started").exists())
            self.assertTrue((root / "steward-started").exists())
            process.terminate()
            process.wait(timeout=5)
            self.assertTrue((root / "legacy-stopped").exists())
            self.assertTrue((root / "steward-stopped").exists())
            process_group = int(
                (root / "steward-process-group").read_text(encoding="utf-8")
            )
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    os.killpg(process_group, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.fail("steward process group survived service termination")

    def test_service_defers_signal_until_spawned_child_is_registered(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "shipyard_queue_service_tick_registration_test", SERVICE_SUPPORT
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(service)

        class FakeProcess:
            pid = 4242

            def poll(self) -> None:
                return None

            def wait(self, timeout: int | None = None) -> int:
                return 0

        fake_process = FakeProcess()

        def spawn(*_args: object, **_kwargs: object) -> FakeProcess:
            service.stop(signal.SIGTERM, None)
            return fake_process

        with (
            mock.patch.object(service.subprocess, "Popen", side_effect=spawn),
            mock.patch.object(service.os, "killpg") as killpg,
        ):
            with self.assertRaises(service.ServiceInterrupted) as raised:
                service.main()
            self.assertEqual(raised.exception.signum, signal.SIGTERM)
            self.assertEqual(service.CHILDREN, [fake_process])
            service.terminate_children()
            killpg.assert_any_call(fake_process.pid, signal.SIGTERM)
            killpg.assert_any_call(fake_process.pid, signal.SIGKILL)

    def test_service_spawn_failure_does_not_suppress_peer_lane(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "shipyard_queue_service_tick_spawn_failure_test", SERVICE_SUPPORT
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        service = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(service)

        class FakeProcess:
            pid = 4243

            def poll(self) -> int:
                return 0

            def wait(self, timeout: int | None = None) -> int:
                return 0

        fake_process = FakeProcess()
        attempts = 0

        def spawn(*_args: object, **_kwargs: object) -> FakeProcess:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("synthetic legacy spawn failure")
            return fake_process

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_health = root / "legacy-health.json"
            steward_health = root / "steward-health.json"
            environment = {
                "SHIPYARD_QUEUE_HEALTH_FILE": str(legacy_health),
                "SHIPYARD_STEWARD_HEALTH_FILE": str(steward_health),
            }
            with (
                mock.patch.dict(os.environ, environment),
                mock.patch.object(
                    service.subprocess, "Popen", side_effect=spawn
                ),
            ):
                self.assertEqual(service.main(), 1)
            self.assertEqual(attempts, 2)
            self.assertEqual(service.CHILDREN, [fake_process])
            value = json.loads(legacy_health.read_text(encoding="utf-8"))
            self.assertEqual(value["status"], "unhealthy")
            self.assertIn("could not start", value["reason"])

    def test_total_steward_deadline_does_not_block_legacy_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(SERVICE, root / SERVICE.name)
            shutil.copy2(SERVICE_SUPPORT, root / SERVICE_SUPPORT.name)
            legacy = root / "shipyard_queue_tick.sh"
            legacy.write_text(
                "#!/bin/sh\ntouch \"$STATE/legacy-complete\"\n",
                encoding="utf-8",
            )
            legacy.chmod(0o755)
            steward = root / "shipyard_steward_tick.sh"
            steward.write_text(
                "#!/bin/sh\nwhile :; do sleep 1; done\n",
                encoding="utf-8",
            )
            steward.chmod(0o755)
            health = root / "steward-health.json"
            env = os.environ.copy()
            env.update(
                {
                    "STATE": str(root),
                    "SHIPYARD_SERVICE_STEWARD_TIMEOUT_SECS": "1",
                    "SHIPYARD_SERVICE_LEGACY_TIMEOUT_SECS": "2",
                    "SHIPYARD_STEWARD_HEALTH_FILE": str(health),
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(root / SERVICE.name)], env=env,
                text=True, capture_output=True, timeout=5, check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertTrue((root / "legacy-complete").exists())
            self.assertEqual(
                json.loads(health.read_text(encoding="utf-8"))["status"],
                "unhealthy",
            )

    def test_invalid_service_timeout_replaces_both_stale_health_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(SERVICE, root / SERVICE.name)
            shutil.copy2(SERVICE_SUPPORT, root / SERVICE_SUPPORT.name)
            legacy_health = root / "legacy-health.json"
            steward_health = root / "steward-health.json"
            for health in (legacy_health, steward_health):
                health.write_text(
                    '{"status":"healthy","reason":"stale"}\n',
                    encoding="utf-8",
                )
            env = os.environ.copy()
            env.update(
                {
                    "SHIPYARD_QUEUE_HEALTH_FILE": str(legacy_health),
                    "SHIPYARD_STEWARD_HEALTH_FILE": str(steward_health),
                    "SHIPYARD_SERVICE_STEWARD_TIMEOUT_SECS": "invalid",
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(root / SERVICE.name)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            for health in (legacy_health, steward_health):
                value = json.loads(health.read_text(encoding="utf-8"))
                self.assertEqual(value["status"], "unhealthy")
                self.assertIn(
                    "SHIPYARD_SERVICE_STEWARD_TIMEOUT_SECS",
                    value["reason"],
                )

    def test_unwritable_timeout_health_does_not_stop_peer_lane(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(SERVICE, root / SERVICE.name)
            shutil.copy2(SERVICE_SUPPORT, root / SERVICE_SUPPORT.name)
            legacy = root / "shipyard_queue_tick.sh"
            legacy.write_text(
                "#!/bin/sh\nsleep 2\ntouch \"$STATE/legacy-complete\"\n",
                encoding="utf-8",
            )
            legacy.chmod(0o755)
            steward = root / "shipyard_steward_tick.sh"
            steward.write_text(
                "#!/bin/sh\nwhile :; do sleep 1; done\n",
                encoding="utf-8",
            )
            steward.chmod(0o755)
            not_a_directory = root / "not-a-directory"
            not_a_directory.write_text("file", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "STATE": str(root),
                    "SHIPYARD_SERVICE_STEWARD_TIMEOUT_SECS": "1",
                    "SHIPYARD_SERVICE_LEGACY_TIMEOUT_SECS": "3",
                    "SHIPYARD_STEWARD_HEALTH_FILE": str(
                        not_a_directory / "health.json"
                    ),
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(root / SERVICE.name)],
                env=env,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertTrue((root / "legacy-complete").exists())
            self.assertIn("could not publish", result.stderr)

    def test_unexpected_child_failure_replaces_stale_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(SERVICE, root / SERVICE.name)
            shutil.copy2(SERVICE_SUPPORT, root / SERVICE_SUPPORT.name)
            legacy = root / "shipyard_queue_tick.sh"
            legacy.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
            legacy.chmod(0o755)
            steward = root / "shipyard_steward_tick.sh"
            steward.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            steward.chmod(0o755)
            legacy_health = root / "legacy-health.json"
            legacy_health.write_text(
                '{"status":"healthy","reason":"stale"}\n',
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "SHIPYARD_QUEUE_HEALTH_FILE": str(legacy_health),
                    "SHIPYARD_STEWARD_HEALTH_FILE": str(
                        root / "steward-health.json"
                    ),
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(root / SERVICE.name)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            value = json.loads(legacy_health.read_text(encoding="utf-8"))
            self.assertEqual(value["status"], "unhealthy")
            self.assertIn("exit=7", value["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
