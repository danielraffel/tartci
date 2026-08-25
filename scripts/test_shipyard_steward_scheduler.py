#!/usr/bin/env python3
"""Hermetic tests for the additive Shipyard stewardship scheduler."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from scripts import shipyard_steward_scheduler as scheduler


SCRIPT = Path(__file__).with_name("shipyard_steward_scheduler.py")


class StewardSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.calls = self.root / "calls"
        self.shipyard = (self.root / "shipyard").resolve()
        self.shipyard.write_text(
            """#!/bin/sh
set -eu
fence=absent
[ ! -f "$QUARANTINE_FILE" ] || fence=present
printf '%s|cwd=%s|gh=%s|github=%s|fence=%s\\n' "$*" "$PWD" "${GH_TOKEN-unset}" "${GITHUB_TOKEN-unset}" "$fence" >> "$CALLS"
if [ "$1" = "--version" ]; then
  printf 'shipyard 0.113.0\\n'
  exit 0
fi
if [ "$1 $2 $3" = "--json runner steward" ]; then
  repo="$5"
  case ",$FAIL_REPOS," in *",$repo,"*) exit 9 ;; esac
  case ",$NOISY_REPOS," in *",$repo,"*) python3 -c 'print("x" * (5 * 1024 * 1024))'; exit 0 ;; esac
  case ",$DETACHED_REPOS," in
    *",$repo,"*)
      python3 -c 'import os,sys,time; os.setsid(); open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(20)' "$DETACHED_PID_FILE" &
      sleep 3
      ;;
  esac
  case ",$SLOW_REPOS," in *",$repo,"*) sleep 3 ;; esac
  printf '{"schema_version":1,"command":"runner.steward","apply":true,"handoff_ledger":"ledger","repos":[{"repo":"%s","errors":[]}]}\\n' "$repo"
  exit 0
fi
if [ "$1 $2 $3" = "--json runner recovery-worker" ]; then
  if [ "$DETACHED_RECOVERY" = 1 ]; then
    python3 -c 'import os,sys,time; os.setsid(); open(sys.argv[1], "w").write(str(os.getpid())); time.sleep(20)' "$DETACHED_PID_FILE" &
    sleep 3
  fi
  printf '{"schema_version":1,"command":"runner:recovery-worker","apply":true,"requests":[]}\\n'
  exit 0
fi
exit 97
""",
            encoding="utf-8",
        )
        self.shipyard.chmod(0o755)
        self.repos: list[tuple[str, Path]] = []
        for name in ("one", "two", "three"):
            checkout = self.root / name
            subprocess.run(["git", "init", "-q", str(checkout)], check=True)
            identity = f"owner/{name}"
            subprocess.run(
                ["git", "-C", str(checkout), "remote", "add", "origin", f"git@github.com:{identity}.git"],
                check=True,
            )
            self.repos.append((identity, checkout.resolve()))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_scheduler(
        self,
        *,
        enabled: bool,
        authority: bool = True,
        fail_repos: str = "",
        slow_repos: str = "",
        noisy_repos: str = "",
        detached_repos: str = "",
        detached_recovery: bool = False,
        config_mode: int = 0o600,
        invalid_utf8: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None, dict[str, object] | None]:
        config = self.root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "enabled": enabled,
                    "authority": authority,
                    "shipyard": str(self.shipyard),
                    "repositories": [
                        {"repo": identity, "checkout": str(checkout)}
                        for identity, checkout in self.repos
                    ],
                    "steward_timeout_seconds": 1,
                    "recovery_timeout_seconds": 2,
                    "max_log_bytes": 1024,
                    "log_generations": 2,
                }
            ),
            encoding="utf-8",
        )
        if invalid_utf8:
            config.write_bytes(b"\xff\xfe")
        config.chmod(config_mode)
        report = self.root / "report.json"
        health = self.root / "health.json"
        environment = os.environ.copy()
        environment.update(
            {
                "CALLS": str(self.calls),
                "FAIL_REPOS": fail_repos,
                "SLOW_REPOS": slow_repos,
                "NOISY_REPOS": noisy_repos,
                "DETACHED_REPOS": detached_repos,
                "DETACHED_PID_FILE": str(self.root / "detached.pid"),
                "DETACHED_RECOVERY": "1" if detached_recovery else "0",
                "QUARANTINE_FILE": str(self.root / "scheduler.quarantine.json"),
                "GH_TOKEN": "must-not-leak",
                "GITHUB_TOKEN": "must-not-leak",
            }
        )
        result = subprocess.run(
            [
                str(SCRIPT),
                "--config", str(config),
                "--report", str(report),
                "--health", str(health),
                "--log", str(self.root / "scheduler.log"),
                "--lock", str(self.root / "scheduler.lock"),
                "--quarantine", str(self.root / "scheduler.quarantine.json"),
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        return (
            result,
            json.loads(report.read_text()) if report.exists() else None,
            json.loads(health.read_text()) if health.exists() else None,
        )

    def call_lines(self) -> list[str]:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def test_disabled_is_noop_with_atomic_status(self) -> None:
        result, report, health = self.run_scheduler(enabled=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.call_lines(), [])
        self.assertEqual(report["status"], "disabled")
        self.assertEqual(health["status"], "disabled")

    def test_post_sigkill_reap_is_bounded(self) -> None:
        process = mock.Mock(pid=4242)
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["shipyard"], 2),
            subprocess.TimeoutExpired(["shipyard"], 2),
        ]
        process.poll.return_value = None
        with mock.patch.object(scheduler.os, "killpg") as killpg:
            self.assertFalse(scheduler.terminate_group(process))
        self.assertEqual(
            killpg.call_args_list,
            [mock.call(4242, signal.SIGTERM), mock.call(4242, signal.SIGKILL)],
        )
        self.assertEqual(process.wait.call_args_list, [mock.call(timeout=2), mock.call(timeout=2)])

    def test_each_repo_isolated_then_exactly_one_recovery(self) -> None:
        result, report, health = self.run_scheduler(enabled=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.call_lines()
        self.assertEqual(len(calls), 5)
        self.assertTrue(calls[0].startswith("--version|"))
        self.assertIn("fence=absent", calls[0])
        for index, (identity, checkout) in enumerate(self.repos, start=1):
            self.assertIn(f"--json runner steward --repo {identity} --apply", calls[index])
            self.assertIn(f"cwd={checkout}", calls[index])
            self.assertIn("gh=unset|github=unset", calls[index])
            self.assertIn("fence=present", calls[index])
        self.assertIn("--json runner recovery-worker --once --apply", calls[-1])
        self.assertIn("fence=present", calls[-1])
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(health["status"], "healthy")
        self.assertFalse((self.root / "scheduler.quarantine.json").exists())

    def test_repo_failure_does_not_block_peers_or_single_recovery(self) -> None:
        result, report, health = self.run_scheduler(enabled=True, fail_repos="owner/two")
        self.assertEqual(result.returncode, 1)
        calls = self.call_lines()
        self.assertEqual(sum("runner steward" in line for line in calls), 3)
        self.assertEqual(sum("recovery-worker" in line for line in calls), 1)
        self.assertEqual([row["status"] for row in report["repositories"]], ["ok", "error", "ok"])
        self.assertEqual(health["status"], "unhealthy")

    def test_any_timed_out_repo_quarantines_before_peers(self) -> None:
        result, report, health = self.run_scheduler(enabled=True, slow_repos="owner/one")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(report["repositories"][0]["timed_out"])
        self.assertEqual(len(report["repositories"]), 1)
        self.assertFalse(report["recovery"]["attempted"])
        self.assertEqual(report["status"], "quarantined")
        self.assertEqual(health["status"], "quarantined")
        self.assertTrue((self.root / "scheduler.quarantine.json").exists())

    def test_detached_pipe_holder_cannot_extend_post_timeout_drain_bound(self) -> None:
        started = time.monotonic()
        result, report, _ = self.run_scheduler(enabled=True, detached_repos="owner/one")
        elapsed = time.monotonic() - started
        pid_path = self.root / "detached.pid"
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertEqual(result.returncode, 1)
        self.assertLess(elapsed, 7)
        self.assertTrue(report["repositories"][0]["drain_incomplete"])
        self.assertIn("retained output", report["repositories"][0]["error"])
        self.assertIn("termination proof", report["error"])
        self.assertEqual(len(report["repositories"]), 1)
        self.assertFalse(report["recovery"]["attempted"])
        self.assertTrue((self.root / "scheduler.quarantine.json").exists())
        calls_before = self.call_lines()
        blocked, blocked_report, blocked_health = self.run_scheduler(enabled=True)
        self.assertEqual(blocked.returncode, 2)
        self.assertEqual(self.call_lines(), calls_before)
        self.assertEqual(blocked_report["status"], "quarantined")
        self.assertEqual(blocked_health["status"], "quarantined")

    def test_insecure_config_fails_before_shipyard(self) -> None:
        (self.root / "report.json").write_text(
            '{"status":"stale-healthy"}\n', encoding="utf-8"
        )
        result, report, health = self.run_scheduler(enabled=True, config_mode=0o644)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["status"], "unhealthy")
        self.assertEqual(self.call_lines(), [])
        self.assertEqual(health["status"], "unhealthy")
        self.assertIn("mode 600", health["reason"])

    def test_detached_recovery_descendant_quarantines_future_ticks(self) -> None:
        result, report, health = self.run_scheduler(enabled=True, detached_recovery=True)
        pid_path = self.root / "detached.pid"
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(report["repositories"]), 3)
        self.assertTrue(report["recovery"]["drain_incomplete"])
        self.assertEqual(report["status"], "quarantined")
        self.assertEqual(health["status"], "quarantined")

    def test_enabled_without_authority_runs_nothing(self) -> None:
        result, report, _ = self.run_scheduler(enabled=True, authority=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.call_lines(), [])
        self.assertEqual(report["status"], "unhealthy")

    def test_non_utf8_config_replaces_stale_health_with_unhealthy(self) -> None:
        result, report, health = self.run_scheduler(enabled=True, invalid_utf8=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["status"], "unhealthy")
        self.assertEqual(health["status"], "unhealthy")
        self.assertIn("valid JSON", health["reason"])

    def test_live_checkout_writable_by_other_users_is_rejected(self) -> None:
        checkout = self.repos[0][1]
        checkout.chmod(0o777)
        try:
            result, report, health = self.run_scheduler(enabled=True)
        finally:
            checkout.chmod(0o755)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["status"], "unhealthy")
        self.assertEqual(self.call_lines(), [])
        self.assertIn("other local users", health["reason"])

    def test_output_is_capped_while_noisy_repo_is_drained(self) -> None:
        result, report, _ = self.run_scheduler(enabled=True, noisy_repos="owner/one")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(report["repositories"][0]["stdout_truncated"])
        self.assertEqual(report["repositories"][1]["status"], "ok")

    def test_case_variant_duplicate_is_rejected_before_shipyard(self) -> None:
        self.repos.append(("OWNER/ONE", self.repos[0][1]))
        result, report, health = self.run_scheduler(enabled=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report["status"], "unhealthy")
        self.assertEqual(self.call_lines(), [])
        self.assertIn("unique", health["reason"])

    def test_sigterm_ends_active_command_group_without_starting_peers(self) -> None:
        config = self.root / "signal-config.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "enabled": True,
                    "authority": True,
                    "shipyard": str(self.shipyard),
                    "repositories": [
                        {"repo": identity, "checkout": str(checkout)}
                        for identity, checkout in self.repos
                    ],
                    "steward_timeout_seconds": 10,
                    "recovery_timeout_seconds": 10,
                    "max_log_bytes": 1024,
                    "log_generations": 2,
                }
            ),
            encoding="utf-8",
        )
        config.chmod(0o600)
        environment = os.environ.copy()
        environment.update(
            {
                "CALLS": str(self.calls),
                "FAIL_REPOS": "",
                "SLOW_REPOS": "owner/one",
                "NOISY_REPOS": "",
                "DETACHED_REPOS": "",
                "DETACHED_PID_FILE": str(self.root / "signal-detached.pid"),
                "DETACHED_RECOVERY": "0",
                "QUARANTINE_FILE": str(self.root / "signal.quarantine.json"),
            }
        )
        process = subprocess.Popen(
            [
                str(SCRIPT), "--config", str(config),
                "--report", str(self.root / "signal-report.json"),
                "--health", str(self.root / "signal-health.json"),
                "--log", str(self.root / "signal.log"),
                "--lock", str(self.root / "signal.lock"),
                "--quarantine", str(self.root / "signal.quarantine.json"),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if any("runner steward --repo owner/one" in line for line in self.call_lines()):
                break
            time.sleep(0.02)
        self.assertTrue(any("runner steward --repo owner/one" in line for line in self.call_lines()))
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 128 + signal.SIGTERM)
        calls = self.call_lines()
        self.assertEqual(sum("runner steward" in line for line in calls), 1)
        self.assertEqual(sum("recovery-worker" in line for line in calls), 0)


if __name__ == "__main__":
    unittest.main()
