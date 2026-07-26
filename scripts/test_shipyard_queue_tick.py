#!/usr/bin/env python3
"""Hermetic control-plane tests for the Shipyard queue janitor."""

from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("shipyard_queue_tick.sh")
INSTALLER = Path(__file__).with_name("install_shipyard_queue_tick.sh")
DISABLE_ORCHARD = Path(__file__).with_name("disable_orchard.sh")


class QueueTickControlTests(unittest.TestCase):
    def run_tick(
        self,
        *,
        held: bool,
        authority: bool,
        authority_matches: bool | None = None,
        apply: bool = True,
        repo_root: bool = True,
        states: list[dict[str, object]] | None = None,
        reconcile_rc: int = 0,
        reconcile_ok: bool = True,
        reconcile_output: str | None = None,
        auto_merge_rc: int = 3,
        auto_merge_output: str | None = None,
        discard_rc: int = 0,
        gh_state: str = "OPEN",
        gh_state_rc: int = 0,
        gh_state_error: str = "",
        ledger_seed: object | None = None,
        extra_env: dict[str, str] | None = None,
        invalid_tmpdir: bool = False,
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
  printf '{"held":%s,"authority_matches":%s}\\n' "$HELD" "$AUTHORITY_MATCHES"
elif [ "$1 $2" = "ship-state list" ]; then
  printf '%s\\n' "$STATES"
elif [ "$1 $2" = "ship-state reconcile" ]; then
  printf 'reconcile-gh-token=%s\\n' "${GH_TOKEN:+set}" >> "$CALLS"
  printf '%s\\n' "$RECONCILE_OUTPUT"
  exit "$RECONCILE_RC"
elif [ "$1 $2" = "ship-state discard" ]; then
  exit "$DISCARD_RC"
elif [ "$1" = "auto-merge" ]; then
  printf 'auto-merge-gh-token=%s\\n' "${GH_TOKEN:+set}" >> "$CALLS"
  printf '%s\\n' "$AUTO_MERGE_OUTPUT"
  exit "$AUTO_MERGE_RC"
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
  "auth token")
    printf 'app-token\\n'
    exit "$GH_TOKEN_RC"
    ;;
  "pr list "*)
    printf '[]\\n'
    exit "$GH_REPO_RC"
    ;;
  *"--json state"*)
    printf '%s\\n' "$GH_STATE"
    printf '%s\\n' "$GH_STATE_ERROR" >&2
    exit "$GH_STATE_RC"
    ;;
  *"--json mergeable,mergeStateStatus,isDraft"*) printf '%s\\n' "$GH_INFO"; exit "$GH_INFO_RC" ;;
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
            if auto_merge_output is None:
                auto_merge_output = json.dumps(
                    {
                        "schema_version": 1,
                        "command": "auto-merge",
                        "event": "in-flight",
                        "pr": 42,
                        "evidence": {},
                    }
                )
            if reconcile_output is None:
                reconcile_output = json.dumps(
                    {
                        "schema_version": 1,
                        "command": "ship-state:reconcile",
                        "results": [
                            {
                                "pr": 42,
                                "ok": reconcile_ok,
                                "changes": [],
                            }
                        ],
                    }
                )
            env.update(
                {
                    "PATH": f"{root}:/usr/bin:/bin",
                    "HOME": str(root),
                    "CALLS": str(calls),
                    "HELD": "true" if held else "false",
                    "AUTHORITY_MATCHES": "true"
                    if (authority if authority_matches is None else authority_matches)
                    else "false",
                    "SHIPYARD_TICK_APPLY": "1" if apply else "0",
                    "SHIPYARD_TICK_REAP_ONLY": "0",
                    "SHIPYARD_QUEUE_AUTHORITY": "1" if authority else "0",
                    "SHIPYARD_QUEUE_GH_CLI": "ghapp",
                    "STATES": json.dumps({"states": states or []}),
                    "RECONCILE_RC": str(reconcile_rc),
                    "RECONCILE_OK": "true" if reconcile_ok else "false",
                    "RECONCILE_OUTPUT": reconcile_output,
                    "DISCARD_RC": str(discard_rc),
                    "AUTO_MERGE_RC": str(auto_merge_rc),
                    "AUTO_MERGE_OUTPUT": auto_merge_output,
                    "GH_STATE": gh_state,
                    "GH_STATE_RC": str(gh_state_rc),
                    "GH_STATE_ERROR": gh_state_error,
                    "GH_INFO": json.dumps(
                        {
                            "mergeable": "MERGEABLE",
                            "mergeStateStatus": "CLEAN",
                            "isDraft": False,
                        }
                    ),
                    "GH_INFO_RC": "0",
                    "GH_REPO_RC": "0",
                    "GH_TOKEN_RC": "0",
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
            return (
                result,
                calls.read_text(encoding="utf-8") if calls.exists() else "",
                ledger,
            )

    def test_central_hold_exits_before_ship_state_or_github_reads(self) -> None:
        result, calls, _ = self.run_tick(held=True, authority=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local merge-queue hold active", result.stdout)
        self.assertEqual(calls.splitlines(), ["--version", "merge-queue status --json"])
        self.assertNotIn("ghapp", calls)

    def test_non_authority_full_live_is_unhealthy(self) -> None:
        result, calls, _ = self.run_tick(held=False, authority=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("FULL-LIVE requires SHIPYARD_QUEUE_AUTHORITY=1", result.stdout)
        self.assertNotIn("ship-state list --json", calls)
        self.assertNotIn("ghapp", calls)

    def test_explicit_authority_can_enter_live_mode(self) -> None:
        result, calls, _ = self.run_tick(held=False, authority=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FULL-LIVE refused", result.stdout)
        self.assertIn("mode=live", result.stdout)
        self.assertIn("ship-state list --json", calls)
        self.assertIn("ghapp pr list --repo owner/repo", calls)

    def test_live_mode_requires_configured_app_wrapper(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            authority=True,
            extra_env={"SHIPYARD_QUEUE_GH_CLI": ""},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires SHIPYARD_QUEUE_GH_CLI", result.stdout)
        self.assertNotIn("ship-state list", calls)

    def test_live_mode_broken_app_auth_fails_even_with_empty_state(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            authority=True,
            states=[],
            extra_env={"GH_REPO_RC": "1"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("authority-repo read failed", result.stdout)
        self.assertIn("ghapp pr list --repo owner/repo", calls)
        self.assertNotIn("ship-state list", calls)

    def test_live_mode_broken_app_token_fails_before_state(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            authority=True,
            states=[],
            extra_env={"GH_TOKEN_RC": "1"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("could not provide a bounded token", result.stdout)
        self.assertIn("ghapp auth token", calls)
        self.assertNotIn("ship-state list", calls)

    def test_authority_flag_without_machine_match_is_unhealthy(self) -> None:
        result, calls, _ = self.run_tick(
            held=False, authority=True, authority_matches=False
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("runner tag does not match merge_queue.mutation_machine", result.stdout)
        self.assertNotIn("ghapp", calls)

    def test_dry_run_ignores_missing_repo_root_placeholder(self) -> None:
        result, calls, _ = self.run_tick(
            held=False, authority=False, apply=False, repo_root=False
        )
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
            ("SHIPYARD_TICK_REAP_ONLY", "2", "REAP_ONLY must be 0 or 1"),
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
            ("SHIPYARD_TICK_MERGE_METHOD", "octopus", "must be merge, squash, or rebase"),
        ):
            with self.subTest(key=key):
                result, calls, _ = self.run_tick(
                    held=False,
                    authority=True,
                    extra_env={key: value},
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertEqual(calls, "")

    def test_mktemp_failure_is_unhealthy_not_success(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            authority=True,
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

    def test_reconcile_failure_blocks_auto_merge_and_degrades_tick(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            authority=True,
            states=self.stale_open_state(),
            reconcile_rc=9,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ship-state reconcile failed", result.stdout)
        self.assertIn("ship-state reconcile 42", calls)
        self.assertNotIn("auto-merge 42", calls)

    def test_reconcile_json_error_blocks_auto_merge_even_with_zero_exit(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            authority=True,
            states=self.stale_open_state(),
            reconcile_ok=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("ship-state reconcile failed", result.stdout)
        self.assertNotIn("auto-merge 42", calls)

    def test_reconcile_requires_exact_typed_envelope(self) -> None:
        malformed = (
            '{"results":[{"pr":42,"ok":true,"changes":[]}]}',
            '{"schema_version":true,"command":"ship-state:reconcile","results":[{"pr":42,"ok":true,"changes":[]}]}',
            '{"schema_version":1,"command":"wrong","results":[{"pr":42,"ok":true,"changes":[]}]}',
            '{"schema_version":1,"command":"ship-state:reconcile","results":[{"pr":true,"ok":true,"changes":[]}]}',
            '{"schema_version":1,"command":"ship-state:reconcile","results":[{"pr":42,"ok":true,"changes":[1]}]}',
            '{"schema_version":1,"command":"ship-state:reconcile","results":[{"pr":42,"ok":true,"changes":[]}]} trailing',
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                result, calls, _ = self.run_tick(
                    held=False,
                    authority=True,
                    states=self.stale_open_state(),
                    reconcile_output=payload,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("ship-state reconcile failed", result.stdout)
                self.assertNotIn("auto-merge 42", calls)

    def test_auto_merge_failure_degrades_tick(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            authority=True,
            states=self.stale_open_state(),
            auto_merge_rc=1,
            auto_merge_output='{"error":"boom"}',
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("auto-merge failed (exit 1)", result.stdout)
        self.assertIn("auto-merge 42", calls)

    def test_auto_merge_in_flight_is_healthy_waiting(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            authority=True,
            states=self.stale_open_state(),
            auto_merge_rc=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not green yet / in flight", result.stdout)
        self.assertIn("reconcile-gh-token=set", calls)
        self.assertIn("auto-merge-gh-token=set", calls)

    def test_routine_auto_merge_exit_one_events_are_healthy_waiting_or_stalled(self) -> None:
        for event, message in (
            ("target-failed", "waiting for a new green head"),
            ("superseded-sha", "stalled pending re-validation"),
        ):
            with self.subTest(event=event):
                result, _, _ = self.run_tick(
                    held=False,
                    authority=True,
                    states=self.stale_open_state(),
                    auto_merge_rc=1,
                    auto_merge_output=json.dumps(
                        {
                            "schema_version": 1,
                            "command": "auto-merge",
                            "event": event,
                            "pr": 42,
                            **(
                                {"failing_targets": ["macos"], "evidence": {}}
                                if event == "target-failed"
                                else {"validated": "old", "current": "new"}
                            ),
                        }
                    ),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(message, result.stdout)

    def test_dry_run_not_found_does_not_advance_quarantine_ledger(self) -> None:
        result, _, ledger = self.run_tick(
            held=False,
            authority=True,
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
            authority=True,
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
                    authority=True,
                    states=self.stale_open_state(),
                    gh_state=state,
                    gh_state_rc=1,
                    gh_state_error="network timeout",
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("GitHub read failed — skip (fail closed)", result.stdout)
                self.assertNotIn("ship-state discard 42", calls)
                self.assertNotIn("ship-state reconcile 42", calls)
                self.assertNotIn("auto-merge 42", calls)

    def test_failed_mergeability_read_never_trusts_partial_stdout(self) -> None:
        result, calls, _ = self.run_tick(
            held=False,
            authority=True,
            states=self.stale_open_state(),
            extra_env={"GH_INFO_RC": "1"},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("mergeability read failed — skip (fail closed)", result.stdout)
        self.assertNotIn("ship-state reconcile 42", calls)
        self.assertNotIn("auto-merge 42", calls)

    def test_malformed_mergeability_types_block_mutation(self) -> None:
        malformed = (
            '{"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":"false"}',
            '{"mergeable":"BOGUS","mergeStateStatus":"CLEAN","isDraft":false}',
            '{"mergeable":"MERGEABLE","mergeStateStatus":"BOGUS","isDraft":false}',
            '{"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":false} trailing',
            '{"mergeable":"MERGEABLE","mergeStateStatus":"CLEAN","isDraft":false,"extra":1}',
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                result, calls, _ = self.run_tick(
                    held=False,
                    authority=True,
                    states=self.stale_open_state(),
                    extra_env={"GH_INFO": payload},
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("mergeability schema malformed", result.stdout)
                self.assertNotIn("ship-state reconcile 42", calls)
                self.assertNotIn("auto-merge 42", calls)

    def test_auto_merge_success_requires_exact_single_json_verdict(self) -> None:
        malformed = (
            '{"event":"merged"} trailing',
            '{"event":"merged","status":"merged"}',
            '{"event":true}',
            '{"message":"already-merged"}',
            'already-merged',
            '{"schema_version":true,"command":"auto-merge","event":"merged","pr":42}',
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                result, _, _ = self.run_tick(
                    held=False,
                    authority=True,
                    states=self.stale_open_state(),
                    auto_merge_rc=0,
                    auto_merge_output=payload,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("success without a merged verdict", result.stdout)

    def test_unreadable_repo_does_not_confirm_pr_not_found(self) -> None:
        result, _, ledger = self.run_tick(
            held=False,
            authority=True,
            states=self.stale_open_state(),
            gh_state="",
            gh_state_rc=1,
            gh_state_error="HTTP 404: pull request not found",
            ledger_seed={"owner/repo#42": 2},
            extra_env={"GH_REPO_RC": "1", "SHIPYARD_TICK_REAP_ONLY": "1"},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("not confirmed by a readable repository", result.stdout)
        self.assertEqual(ledger, {})

    def test_failed_quarantine_discard_preserves_confirmation_ledger(self) -> None:
        result, calls, ledger = self.run_tick(
            held=False,
            authority=True,
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
            authority=True,
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
            authority=True,
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
            authority=True,
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
                    authority=True,
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
        for held, authority in (('"false"', "true"), ("false", '"true"')):
            with self.subTest(held=held, authority=authority):
                result, calls, _ = self.run_tick(
                    held=False,
                    authority=True,
                    extra_env={"HELD": held, "AUTHORITY_MATCHES": authority},
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("merge-queue control schema malformed", result.stdout)
                self.assertNotIn("ship-state list", calls)
                self.assertNotIn("ghapp", calls)


class QueueTickInstallerTests(unittest.TestCase):
    def test_installer_deploys_and_verifies_launchd_executable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            repo = home / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/owner/repo.git",
                ],
                check=True,
            )
            fake_bin = home / "bin"
            fake_bin.mkdir()
            (fake_bin / "plutil").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "ghapp").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "launchctl").write_text(
                """#!/bin/sh
if [ "$1" = "print" ]; then
  printf '%s\\n' "$HOME/.config/shipyard/queue-tick.env"
  printf '%s\\n' "$HOME/.local/share/tartci/scripts/shipyard_queue_tick.sh"
elif [ "$1" = "kickstart" ]; then
  mkdir -p "$HOME/Library/Logs"
  printf '{"status":"healthy"}\\n' > "$HOME/Library/Logs/shipyard-queue-tick.health.json"
fi
exit 0
""",
                encoding="utf-8",
            )
            for command in ("plutil", "ghapp", "launchctl"):
                (fake_bin / command).chmod(0o755)
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("SHIPYARD_", "TARTCI_"))
            }
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER),
                    "--repo-root",
                    "repo",
                    "--authority",
                    "--mode",
                    "live",
                    "--gh-cli",
                    "ghapp",
                    "--install",
                ],
                env=env,
                cwd=home,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = (
                home / ".local/share/tartci/scripts/shipyard_queue_tick.sh"
            )
            self.assertTrue(os.access(installed, os.X_OK))
            self.assertEqual(
                installed.read_bytes(),
                SCRIPT.read_bytes(),
            )
            config = home / ".config/shipyard/queue-tick.env"
            self.assertIn(
                f"SHIPYARD_QUEUE_REPO_ROOT={repo.resolve()}",
                config.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "SHIPYARD_QUEUE_AUTHORITY=1",
                config.read_text(encoding="utf-8"),
            )
            self.assertIn("installed and started", result.stdout)
            with (home / "Library/LaunchAgents/com.danielraffel.shipyard.queue-tick.plist").open("rb") as source:
                plist = plistlib.load(source)
            environment = plist["EnvironmentVariables"]
            self.assertEqual(environment["SHIPYARD_TICK_APPLY"], "1")
            self.assertEqual(environment["SHIPYARD_TICK_REAP_ONLY"], "0")

    def test_live_mode_requires_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER),
                    "--repo-root",
                    str(repo),
                    "--mode",
                    "live",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires --authority", result.stderr)

    def test_failed_candidate_rolls_back_prior_bytes_and_loaded_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            repo = home / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/owner/repo.git"],
                check=True,
            )
            install_dir = home / ".local/share/tartci/scripts"
            config_dir = home / ".config/shipyard"
            agents = home / "Library/LaunchAgents"
            logs = home / "Library/Logs"
            for path in (install_dir, config_dir, agents, logs):
                path.mkdir(parents=True)
            installed = install_dir / "shipyard_queue_tick.sh"
            config = config_dir / "queue-tick.env"
            plist = agents / "com.danielraffel.shipyard.queue-tick.plist"
            installed.write_bytes(b"prior-script")
            config.write_bytes(b"prior-config")
            plist.write_bytes(b"prior-plist")
            calls = home / "calls"
            fake_bin = home / "bin"
            fake_bin.mkdir()
            (fake_bin / "ghapp").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "plutil").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (fake_bin / "launchctl").write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
case "$1" in
  print)
    printf '%s\\n' "$HOME/.config/shipyard/queue-tick.env"
    printf '%s\\n' "$HOME/.local/share/tartci/scripts/shipyard_queue_tick.sh"
    exit 0
    ;;
  kickstart)
    printf '{"status":"degraded"}\\n' > "$HOME/Library/Logs/shipyard-queue-tick.health.json"
    exit 0
    ;;
esac
exit 0
""",
                encoding="utf-8",
            )
            for command in ("ghapp", "plutil", "sleep", "launchctl"):
                (fake_bin / command).chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "CALLS": str(calls),
                }
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(INSTALLER),
                    "--repo-root",
                    str(repo),
                    "--authority",
                    "--mode",
                    "live",
                    "--gh-cli",
                    "ghapp",
                    "--install",
                ],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("rolling back", result.stderr)
            self.assertEqual(installed.read_bytes(), b"prior-script")
            self.assertEqual(config.read_bytes(), b"prior-config")
            self.assertEqual(plist.read_bytes(), b"prior-plist")
            call_text = calls.read_text(encoding="utf-8")
            self.assertGreaterEqual(call_text.count("bootstrap"), 2)
            self.assertGreaterEqual(call_text.count("bootout"), 2)


class NoOrchardCleanupTests(unittest.TestCase):
    def test_cleanup_is_idempotent_and_verifies_both_labels_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            agents = home / "Library/LaunchAgents"
            agents.mkdir(parents=True)
            labels = (
                "com.danielraffel.tartci.orchard-controller",
                "com.danielraffel.tartci.orchard-worker",
            )
            for label in labels:
                (agents / f"{label}.plist").write_text("retired", encoding="utf-8")
            calls = home / "calls"
            fake_bin = home / "bin"
            fake_bin.mkdir()
            (fake_bin / "launchctl").write_text(
                """#!/bin/sh
printf '%s\\n' "$*" >> "$CALLS"
case "$1" in
  bootout) exit 0 ;;
  print) exit 1 ;;
esac
exit 2
""",
                encoding="utf-8",
            )
            (fake_bin / "launchctl").chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                    "CALLS": str(calls),
                }
            )
            for _ in range(2):
                result = subprocess.run(
                    ["/bin/bash", str(DISABLE_ORCHARD), "--apply"],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("LaunchAgents are absent", result.stdout)
            for label in labels:
                self.assertFalse((agents / f"{label}.plist").exists())
                self.assertIn(f"bootout gui/{os.getuid()}/{label}", calls.read_text())
                self.assertIn(f"print gui/{os.getuid()}/{label}", calls.read_text())

    def test_cleanup_fails_when_a_retired_label_remains_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            fake_bin = home / "bin"
            fake_bin.mkdir()
            (fake_bin / "launchctl").write_text(
                "#!/bin/sh\n[ \"$1\" = print ] && exit 0\nexit 0\n",
                encoding="utf-8",
            )
            (fake_bin / "launchctl").chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(DISABLE_ORCHARD), "--apply"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("still loaded", result.stderr)


if __name__ == "__main__":
    unittest.main()
