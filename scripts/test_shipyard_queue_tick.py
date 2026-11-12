#!/usr/bin/env python3
"""Hermetic control-plane tests for the Shipyard queue janitor."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).with_name("shipyard_queue_tick.sh")


class QueueTickControlTests(unittest.TestCase):
    def run_tick(
        self,
        *,
        held: bool,
        legacy_full_live: bool = False,
        apply: bool = True,
        repo_root: bool = True,
        states: list[dict[str, object]] | None = None,
        discard_rc: int = 0,
        discard_error: str = "",
        gh_state: str = "OPEN",
        gh_state_rc: int = 0,
        gh_state_error: str = "",
        ledger_seed: object | None = None,
        extra_env: dict[str, str] | None = None,
        invalid_tmpdir: bool = False,
        reap_opt_in: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], str, object | None]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/owner/repo.git",
                ],
                check=True,
            )
            calls = root / "calls"
            shipyard = root / "shipyard"
            shipyard.write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
if [ "$1" = "--version" ]; then
  printf 'shipyard 0.80.0\\n'
elif [ "$1 $2" = "auth export" ]; then
  printf '{"schema_version":1,"command":"auth.export","bundle":{"version":2}}\\n'
elif [ "$1 $2" = "merge-queue status" ]; then
  printf '{"held":%s,"authority_matches":false}\\n' "$HELD"
elif [ "$1" = "rescue" ]; then
  pwd -P > "$CALLS.rescue-cwd"
  [ -z "$RESCUE_SLEEP" ] || sleep "$RESCUE_SLEEP"
  [ -z "$RESCUE_ERROR" ] || printf '%s\\n' "$RESCUE_ERROR" >&2
  printf '%s\\n' "$RESCUE_JSON"
  exit "$RESCUE_RC"
elif [ "$1 $2" = "ship-state list" ]; then
  printf '%s\\n' "$STATES"
elif [ "$1 $2" = "ship-state discard" ]; then
  if [ "$FAKE_NO_REPO_FLAG" = "1" ] && [ "$4" = "--repo" ]; then
    printf "error: unexpected argument '--repo' found\\n" >&2
    exit 2
  fi
  [ -z "$DISCARD_ERROR" ] || printf '%s\\n' "$DISCARD_ERROR" >&2
  exit "$DISCARD_RC"
else
  exit 97
fi
""",
                encoding="utf-8",
            )
            shipyard.chmod(0o755)
            ghapp = root / "ghapp"
            ghapp.write_text(
                """#!/bin/sh
printf 'ghapp %s\\n' "$*" >> "$CALLS"
case "$*" in
  "pr list "*)
    printf '[]\\n'
    exit "$GH_REPO_RC"
    ;;
  *"--json state"*)
    case "$*" in
      *"--repo $GH_MERGED_REPO "*) [ -n "$GH_MERGED_REPO" ] && { printf 'MERGED\\n'; exit 0; } ;;
    esac
    printf '%s\\n' "$GH_STATE"
    printf '%s\\n' "$GH_STATE_ERROR" >&2
    exit "$GH_STATE_RC"
    ;;
esac
exit 98
""",
                encoding="utf-8",
            )
            ghapp.chmod(0o755)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("SHIPYARD_", "TARTCI_"))
            }
            env.update(
                {
                    "PATH": f"{root}:/usr/bin:/bin",
                    "HOME": str(root),
                    "CALLS": str(calls),
                    "HELD": "true" if held else "false",
                    "SHIPYARD_TICK_APPLY": "1" if apply else "0",
                    "SHIPYARD_QUEUE_GH_CLI": "ghapp",
                    "STATES": json.dumps({"states": states or []}),
                    "DISCARD_RC": str(discard_rc),
                    "DISCARD_ERROR": discard_error,
                    "GH_STATE": gh_state,
                    "GH_STATE_RC": str(gh_state_rc),
                    "GH_STATE_ERROR": gh_state_error,
                    "GH_REPO_RC": "0",
                    "FAKE_NO_REPO_FLAG": "0",
                    "GH_MERGED_REPO": "",
                    "RESCUE_JSON": json.dumps({"runs": []}),
                    "RESCUE_RC": "0",
                    "RESCUE_ERROR": "",
                    "RESCUE_SLEEP": "",
                }
            )
            if legacy_full_live:
                # What an install from before the reaper-only tick carries.
                env.update(
                    {
                        "SHIPYARD_TICK_REAP_ONLY": "0",
                        "SHIPYARD_QUEUE_AUTHORITY": "1",
                        "SHIPYARD_TICK_MERGE_METHOD": "merge",
                        "SHIPYARD_QUEUE_COMMAND_TIMEOUT_SECS": "45",
                    }
                )
            if extra_env:
                env.update(extra_env)
            if invalid_tmpdir:
                blocked_tmp = root / "not-a-directory"
                blocked_tmp.write_text("blocked", encoding="utf-8")
                env["TMPDIR"] = str(blocked_tmp)
            ledger_path = root / ".local/state/tartci/shipyard-queue-tick-invalid.json"
            if ledger_seed is not None:
                ledger_path.parent.mkdir(parents=True, exist_ok=True)
                ledger_path.write_text(json.dumps(ledger_seed), encoding="utf-8")
            if repo_root:
                env["SHIPYARD_QUEUE_REPO_ROOT"] = str(root)
            if reap_opt_in:
                opt_in = root / ".config/tartci/reap-superseded-merge-groups.enabled"
                opt_in.parent.mkdir(parents=True, exist_ok=True)
                opt_in.write_text("", encoding="utf-8")
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            ledger = (
                json.loads(ledger_path.read_text(encoding="utf-8"))
                if ledger_path.exists()
                else None
            )
            rescue_cwd = Path(f"{calls}.rescue-cwd")
            self.last_rescue_cwd = (
                rescue_cwd.read_text(encoding="utf-8").strip() if rescue_cwd.exists() else None
            )
            reap_log = root / "Library/Logs/tartci/superseded-merge-groups.log"
            self.last_reap_log = (
                reap_log.read_text(encoding="utf-8") if reap_log.exists() else ""
            )
            health_path = root / "Library/Logs/shipyard-queue-tick.health.json"
            self.last_health = (
                health_path.read_text(encoding="utf-8") if health_path.exists() else "{}"
            )
            return (
                result,
                calls.read_text(encoding="utf-8") if calls.exists() else "",
                ledger,
            )

    SUPERSEDED = {
        "runs": [
            {"run_id": 38849842963, "branch": "gh-readonly-queue/main/pr-10377-66ffb1a6",
             "status": "cancelled", "decision": "cancel",
             "evidence": "queue ref absent from ls-remote; 1 active job(s) still hold capacity"},
            {"run_id": 38850408818, "branch": "gh-readonly-queue/main/pr-10398-8000be88",
             "status": "keep", "decision": "keep", "evidence": "queue ref is live"},
        ]
    }

    @staticmethod
    def rescue_calls(calls: str) -> list[str]:
        return [line for line in calls.splitlines() if line.startswith("rescue ")]

    def test_reaper_opt_in_cancels_with_apply_and_logs_each_cancel(self) -> None:
        result, calls, _ = self.run_tick(
            held=False, reap_opt_in=True,
            extra_env={"RESCUE_JSON": json.dumps(self.SUPERSEDED)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.rescue_calls(calls),
            ["rescue --superseded-merge-group --repo Generous-Corp/pulp --json --apply"],
        )
        self.assertIn("reap-superseded Generous-Corp/pulp: mode=apply exit 0 cancelled=1 keep=1",
                      result.stdout)
        rows = [line.split("\t") for line in self.last_reap_log.splitlines()]
        self.assertEqual(len(rows), 1, self.last_reap_log)
        self.assertEqual(rows[0][2:6], ["Generous-Corp/pulp", "38849842963",
                                        "gh-readonly-queue/main/pr-10377-66ffb1a6", "cancelled"])
        self.assertIn("queue ref absent", rows[0][6])

    def test_reaper_without_opt_in_only_reports(self) -> None:
        report = {"runs": [dict(self.SUPERSEDED["runs"][0], status="would-cancel")]}
        result, calls, _ = self.run_tick(
            held=False, extra_env={"RESCUE_JSON": json.dumps(report)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.rescue_calls(calls),
            ["rescue --superseded-merge-group --repo Generous-Corp/pulp --json"],
        )
        self.assertIn("mode=dry-run exit 0 would-cancel=1", result.stdout)
        self.assertIn("\twould-cancel\t", self.last_reap_log)

    def test_a_failing_reaper_never_fails_the_tick(self) -> None:
        result, calls, _ = self.run_tick(
            held=False, reap_opt_in=True, states=self.stale_open_state(), gh_state="OPEN",
            extra_env={"RESCUE_RC": "1", "RESCUE_JSON": "not json",
                       "RESCUE_ERROR": "github: HTTP 502 Bad Gateway"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unreadable output (github: HTTP 502 Bad Gateway); skipped", result.stdout)
        # The rest of the tick still ran.
        self.assertIn("ghapp pr view 42", calls)
        self.assertIn("mode=reap reaped=0 open=1", result.stdout)
        self.assertEqual(self.last_reap_log, "")

    def test_a_hung_reaper_is_cut_off_and_the_tick_goes_on(self) -> None:
        started = time.monotonic()
        result, calls, _ = self.run_tick(
            held=False, reap_opt_in=True, states=self.stale_open_state(), gh_state="OPEN",
            extra_env={"RESCUE_SLEEP": "30", "SHIPYARD_QUEUE_REAP_TIMEOUT_SECS": "1"},
        )
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode=apply timed out after 1.0s; skipped", result.stdout)
        self.assertIn("ghapp pr view 42", calls)

    def test_without_a_repo_root_the_reaper_is_skipped_and_shipyard_never_called(self) -> None:
        # Run from $HOME, Shipyard cannot resolve its token from a git remote.
        result, calls, _ = self.run_tick(held=False, reap_opt_in=True, repo_root=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rescue_calls(calls), [])
        self.assertIn("reap-superseded Generous-Corp/pulp: skipped: no repo root configured",
                      result.stdout)
        self.assertIn("mode=reap", result.stdout)

    def test_a_configured_root_that_does_not_exist_skips_the_reaper(self) -> None:
        result, calls, _ = self.run_tick(
            held=False, reap_opt_in=True, repo_root=False,
            extra_env={"SHIPYARD_QUEUE_REPO_ROOT": "/nonexistent/tartci-reaper-root"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rescue_calls(calls), [])
        self.assertIn("skipped: no repo root configured", result.stdout)

    def test_one_repo_root_never_serves_several_repositories(self) -> None:
        result, calls, _ = self.run_tick(
            held=False, reap_opt_in=True,
            extra_env={"SHIPYARD_QUEUE_REAP_REPOS": "Generous-Corp/pulp danielraffel/tartci"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rescue_calls(calls), [])
        self.assertIn("skipped: one repo root cannot serve 2 repositories", result.stdout)
        self.assertIn("(Generous-Corp/pulp danielraffel/tartci)", result.stdout)
        self.assertIn("mode=reap", result.stdout)

    def test_the_reaper_runs_from_the_repo_root(self) -> None:
        # A checkout apart from HOME, so running from HOME cannot pass.
        with tempfile.TemporaryDirectory() as checkout:
            result, calls, _ = self.run_tick(
                held=False, reap_opt_in=True, repo_root=False,
                extra_env={"RESCUE_JSON": json.dumps(self.SUPERSEDED),
                           "SHIPYARD_QUEUE_REPO_ROOT": checkout},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(self.rescue_calls(calls)), 1)
            self.assertEqual(self.last_rescue_cwd, os.path.realpath(checkout))

    def test_a_held_queue_skips_the_reaper(self) -> None:
        result, calls, _ = self.run_tick(held=True, reap_opt_in=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rescue_calls(calls), [])

    def test_central_hold_exits_before_ship_state_or_github_reads(self) -> None:
        result, calls, _ = self.run_tick(held=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local merge-queue hold active", result.stdout)
        self.assertEqual(calls.splitlines(), ["--version", "merge-queue status --json"])
        self.assertNotIn("ghapp", calls)

    def test_open_pr_is_kept_and_never_merged(self) -> None:
        result, calls, _ = self.run_tick(
            held=False, states=self.stale_open_state(), gh_state="OPEN"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("owner/repo#42: open", result.stdout)
        self.assertIn("mode=reap reaped=0 open=1", result.stdout)
        # Control: the tick did read the PR, so the absences below are real.
        self.assertIn("ghapp pr view 42", calls)
        for forbidden in ("auto-merge", "ship-state reconcile", "ship-state discard"):
            self.assertNotIn(forbidden, calls)

    def test_legacy_full_live_install_only_reaps_and_says_so(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            legacy_full_live=True,
            states=self.stale_open_state(),
            gh_state="OPEN",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ghapp pr view 42", calls)
        self.assertNotIn("auto-merge", calls)
        self.assertNotIn("ship-state reconcile", calls)
        self.assertNotIn("auth token", calls)
        self.assertIn("legacy_full_live_ignored", result.stdout)
        health = json.loads(self.last_health)
        self.assertEqual(health["status"], "healthy")
        self.assertIn("legacy_full_live_ignored", health["reason"])

    def test_merged_pr_is_reaped(self) -> None:
        result, calls, _ = self.run_tick(
            held=False, states=self.stale_open_state(), gh_state="MERGED"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reaped (MERGED)", result.stdout)
        self.assertIn("ship-state discard 42", calls)

    def test_a_shared_pr_number_archives_only_that_repos_record(self) -> None:
        # Shipyard 0.254.0 refuses a bare PR number two repositories share.
        states = [dict(self.stale_open_state()[0], repo="owner/a"),
                  dict(self.stale_open_state()[0], repo="owner/b")]
        result, calls, _ = self.run_tick(
            held=False, states=states, gh_state="OPEN",
            extra_env={"GH_MERGED_REPO": "owner/a"})
        self.assertEqual(result.returncode, 0, result.stderr)
        discards = [line for line in calls.splitlines() if line.startswith("ship-state discard")]
        self.assertEqual(discards, ["ship-state discard 42 --repo owner/a"])
        self.assertIn("owner/b#42: open", result.stdout)

    def test_a_shipyard_without_repo_falls_back_to_the_bare_discard(self) -> None:
        result, calls, _ = self.run_tick(
            held=False, states=self.stale_open_state(), gh_state="MERGED",
            extra_env={"FAKE_NO_REPO_FLAG": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        discards = [line for line in calls.splitlines() if line.startswith("ship-state discard")]
        self.assertEqual(discards, ["ship-state discard 42 --repo owner/repo",
                                    "ship-state discard 42"])
        self.assertIn("reaped (MERGED)", result.stdout)

    def test_any_other_discard_failure_is_not_retried_bare(self) -> None:
        # Control: only an unrecognised --repo falls back. A new Shipyard that
        # refuses for a real reason must not be retried without the repo.
        result, calls, _ = self.run_tick(
            held=False, states=self.stale_open_state(), gh_state="MERGED",
            discard_rc=8, discard_error="ship-state for #42 is locked by another writer")
        discards = [line for line in calls.splitlines() if line.startswith("ship-state discard")]
        self.assertEqual(discards, ["ship-state discard 42 --repo owner/repo"])

    def test_failed_discard_names_its_reason(self) -> None:
        result, _, _ = self.run_tick(
            held=False,
            states=self.stale_open_state(),
            gh_state="MERGED",
            discard_rc=8,
            discard_error="ship-state for #42 is locked by another writer",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "discard failed (ship-state for #42 is locked by another writer)",
            result.stdout,
        )

    def test_failed_pr_read_names_its_reason(self) -> None:
        result, _, _ = self.run_tick(
            held=False,
            states=self.stale_open_state(),
            gh_state="",
            gh_state_rc=1,
            gh_state_error="gh: Bad credentials (HTTP 401)",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "GitHub read failed (exit 1: gh: Bad credentials (HTTP 401))",
            result.stdout,
        )

    def test_requires_configured_app_wrapper(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            extra_env={"SHIPYARD_QUEUE_GH_CLI": ""},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires SHIPYARD_QUEUE_GH_CLI", result.stdout)
        self.assertNotIn("ship-state list", calls)

    def test_dry_run_ignores_missing_repo_root_placeholder(self) -> None:
        result, calls, _ = self.run_tick(held=False, apply=False, repo_root=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mode=dry-run", result.stdout)
        self.assertIn("ship-state list --json", calls)

    def test_incompatible_shipyard_exits_before_control_or_github_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = root / "calls"
            shipyard = root / "shipyard"
            shipyard.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\nprintf 'shipyard 0.79.9\\n'\n",
                encoding="utf-8",
            )
            shipyard.chmod(0o755)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("SHIPYARD_", "TARTCI_"))
            }
            env.update(
                {
                    "PATH": f"{root}:/usr/bin:/bin",
                    "HOME": str(root),
                    "CALLS": str(calls),
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("Shipyard 0.80.0 or newer is required", result.stdout)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["--version"])

    def test_invalid_tunables_fail_before_shipyard_or_github_reads(self) -> None:
        for key, value, expected in (
            ("SHIPYARD_TICK_APPLY", "yes", "APPLY must be 0 or 1"),
            ("SHIPYARD_TICK_HEARTBEAT_FRESH_SECS", "-1", "freshness must be"),
            ("SHIPYARD_QUEUE_INVALID_THRESHOLD", "0", "invalid threshold"),
            (
                "SHIPYARD_TICK_HEARTBEAT_FRESH_SECS",
                "9223372036854775808",
                "freshness must be",
            ),
            (
                "SHIPYARD_QUEUE_INVALID_THRESHOLD",
                "9223372036854775808",
                "invalid threshold",
            ),
        ):
            with self.subTest(key=key):
                result, calls, _ = self.run_tick(
                    held=False,
                    extra_env={key: value},
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertEqual(calls, "")

    def test_mktemp_failure_is_unhealthy_not_success(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            invalid_tmpdir=True,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("could not create queue-tick scratch directory", result.stdout)
        self.assertEqual(calls, "")

    @staticmethod
    def stale_open_state() -> list[dict[str, object]]:
        return [
            {
                "pr": 42,
                "repo": "owner/repo",
                "created_at": "",
                "updated_at": "",
                "dispatched_runs": [],
            }
        ]

    def test_dry_run_not_found_does_not_advance_quarantine_ledger(self) -> None:
        result, _, ledger = self.run_tick(
            held=False,
            apply=False,
            states=self.stale_open_state(),
            gh_state="",
            gh_state_rc=1,
            gh_state_error="HTTP 404: pull request not found",
            ledger_seed={"owner/repo#42": 1},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run does not advance", result.stdout)
        self.assertEqual(ledger, {"owner/repo#42": 1})

    def test_generic_github_error_resets_not_found_confirmation(self) -> None:
        result, _, ledger = self.run_tick(
            held=False,
            states=self.stale_open_state(),
            gh_state="",
            gh_state_rc=1,
            gh_state_error="network timeout",
            ledger_seed={"owner/repo#42": 2},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(ledger, {})

    def test_failed_state_read_never_trusts_partial_stdout(self) -> None:
        for state in ("MERGED", "OPEN"):
            with self.subTest(state=state):
                result, calls, _ = self.run_tick(
                    held=False,
                    states=self.stale_open_state(),
                    gh_state=state,
                    gh_state_rc=1,
                    gh_state_error="network timeout",
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("GitHub read failed (exit 1: network timeout) — skip (fail closed)", result.stdout)
                self.assertNotIn("ship-state discard 42", calls)
                self.assertNotIn("ship-state reconcile 42", calls)
                self.assertNotIn("auto-merge 42", calls)

    def test_unreadable_repo_does_not_confirm_pr_not_found(self) -> None:
        result, _, ledger = self.run_tick(
            held=False,
            states=self.stale_open_state(),
            gh_state="",
            gh_state_rc=1,
            gh_state_error="HTTP 404: pull request not found",
            ledger_seed={"owner/repo#42": 2},
            extra_env={"GH_REPO_RC": "1"},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not confirmed by a readable repository", result.stdout)
        self.assertEqual(ledger, {})

    def test_failed_quarantine_discard_preserves_confirmation_ledger(self) -> None:
        result, calls, ledger = self.run_tick(
            held=False,
            states=self.stale_open_state(),
            discard_rc=8,
            gh_state="",
            gh_state_rc=1,
            gh_state_error="HTTP 404: pull request not found",
            ledger_seed={"owner/repo#42": 2},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("confirmation ledger preserved", result.stdout)
        self.assertIn("ship-state discard 42", calls)
        self.assertEqual(ledger, {"owner/repo#42": 3})

    def test_successful_quarantine_discard_then_resets_ledger(self) -> None:
        result, calls, ledger = self.run_tick(
            held=False,
            states=self.stale_open_state(),
            gh_state="",
            gh_state_rc=1,
            gh_state_error="HTTP 404: pull request not found",
            ledger_seed={"owner/repo#42": 2},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("quarantined recoverably", result.stdout)
        self.assertIn("ship-state discard 42", calls)
        self.assertEqual(ledger, {})

    def test_existing_pr_resets_not_found_ledger_even_when_discard_fails(self) -> None:
        result, calls, ledger = self.run_tick(
            held=False,
            states=self.stale_open_state(),
            discard_rc=8,
            gh_state="MERGED",
            ledger_seed={"owner/repo#42": 2},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ship-state discard 42", calls)
        self.assertEqual(ledger, {})

    def test_corrupt_ledger_blocks_discard_before_ship_state_or_github(self) -> None:
        result, calls, ledger = self.run_tick(
            held=False,
            states=[
                {
                    "pr": 42,
                    "repo": "owner/repo",
                    "created_at": "",
                    "updated_at": "",
                    "dispatched_runs": [],
                }
            ],
            gh_state="MERGED",
            ledger_seed="corrupt-ledger",
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("invalid-ledger integrity/writability check failed", result.stdout)
        self.assertNotIn("ship-state discard", calls)
        self.assertNotIn("ship-state list", calls)
        self.assertEqual(ledger, "corrupt-ledger")

    def test_malformed_state_record_fails_before_github_or_shipyard_mutation(self) -> None:
        base = {
            "pr": 42,
            "repo": "owner/repo",
            "created_at": "",
            "updated_at": "",
            "dispatched_runs": [],
        }
        malformed = (
            {**base, "pr": "999\n42"},
            {**base, "pr": True},
            {**base, "repo": "owner/repo\n77\towner/repo"},
            {**base, "repo": "owner/repo\tother"},
            {**base, "created_at": 123},
            {**base, "updated_at": "2026-01-01T00:00:00Z\n77"},
            {**base, "created_at": "not-a-timestamp"},
            {**base, "updated_at": "2026-01-01T00:00:00"},
            {**base, "updated_at": "2026-02-30T00:00:00Z"},
            {**base, "dispatched_runs": "not-a-list"},
            {**base, "dispatched_runs": [{"last_heartbeat_at": 123}]},
            {**base, "dispatched_runs": [{"last_heartbeat_at": "now\n77"}]},
            {
                **base,
                "dispatched_runs": [{"last_heartbeat_at": "not-a-timestamp"}],
            },
        )
        for state in malformed:
            with self.subTest(state=state):
                result, calls, _ = self.run_tick(
                    held=False,
                    # A valid record before the malformed one proves validation
                    # is all-or-nothing: no partial row may become actionable.
                    states=[base, state],
                    gh_state="MERGED",
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("shipyard ship-state payload malformed", result.stdout)
                self.assertIn("ship-state list --json", calls)
                self.assertNotIn("ghapp pr view", calls)
                self.assertNotIn("ship-state discard", calls)
                self.assertNotIn("ship-state reconcile", calls)
                self.assertNotIn("auto-merge", calls)

    def test_control_requires_exact_json_booleans(self) -> None:
        result, calls, _ = self.run_tick(held=False, extra_env={"HELD": '"false"'})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("merge-queue control schema malformed", result.stdout)
        self.assertNotIn("ship-state list", calls)
        self.assertNotIn("ghapp", calls)

if __name__ == "__main__":
    unittest.main()
