#!/usr/bin/env python3
"""Hermetic control-plane tests for the Shipyard queue janitor."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("shipyard_queue_tick.sh")


class QueueTickControlTests(unittest.TestCase):
    def run_tick(
        self,
        *,
        held: bool,
        authority: bool,
        authority_matches: bool | None = None,
        apply: bool = True,
        repo_root: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
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
  printf 'shipyard 0.79.0\\n'
elif [ "$1 $2" = "merge-queue status" ]; then
  printf '{"held":%s,"authority_matches":%s}\\n' "$HELD" "$AUTHORITY_MATCHES"
elif [ "$1 $2" = "ship-state list" ]; then
  printf '{"states":[]}\\n'
else
  exit 97
fi
""",
                encoding="utf-8",
            )
            shipyard.chmod(0o755)
            ghapp = root / "ghapp"
            ghapp.write_text(
                "#!/bin/sh\nprintf 'ghapp %s\\n' \"$*\" >> \"$CALLS\"\nexit 98\n",
                encoding="utf-8",
            )
            ghapp.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{root}:/usr/bin:/bin",
                    "CALLS": str(calls),
                    "HELD": "true" if held else "false",
                    "AUTHORITY_MATCHES": "true"
                    if (authority if authority_matches is None else authority_matches)
                    else "false",
                    "SHIPYARD_TICK_APPLY": "1" if apply else "0",
                    "SHIPYARD_TICK_REAP_ONLY": "0",
                    "SHIPYARD_QUEUE_AUTHORITY": "1" if authority else "0",
                }
            )
            if repo_root:
                env["SHIPYARD_QUEUE_REPO_ROOT"] = str(root)
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            return result, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_central_hold_exits_before_ship_state_or_github_reads(self) -> None:
        result, calls = self.run_tick(held=True, authority=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local merge-queue hold active", result.stdout)
        self.assertEqual(calls.splitlines(), ["--version", "merge-queue status --json"])
        self.assertNotIn("ghapp", calls)

    def test_non_authority_full_live_is_forced_to_reap_only(self) -> None:
        result, calls = self.run_tick(held=False, authority=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FULL-LIVE refused", result.stdout)
        self.assertIn("mode=reap-only", result.stdout)
        self.assertIn("ship-state list --json", calls)
        self.assertNotIn("ghapp", calls)

    def test_explicit_authority_can_enter_live_mode(self) -> None:
        result, calls = self.run_tick(held=False, authority=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("FULL-LIVE refused", result.stdout)
        self.assertIn("mode=live", result.stdout)
        self.assertIn("ship-state list --json", calls)

    def test_authority_flag_without_machine_match_is_forced_reap_only(self) -> None:
        result, calls = self.run_tick(
            held=False, authority=True, authority_matches=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runner tag does not match mutation_machine", result.stdout)
        self.assertIn("mode=reap-only", result.stdout)
        self.assertNotIn("ghapp", calls)

    def test_dry_run_ignores_missing_repo_root_placeholder(self) -> None:
        result, calls = self.run_tick(
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
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALLS\"\nprintf 'shipyard 0.78.0\\n'\n",
                encoding="utf-8",
            )
            shipyard.chmod(0o755)
            env = os.environ.copy()
            env.update({"PATH": f"{root}:/usr/bin:/bin", "CALLS": str(calls)})
            result = subprocess.run(
                ["/bin/bash", str(SCRIPT)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Shipyard 0.79.0 or newer is required", result.stdout)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["--version"])


if __name__ == "__main__":
    unittest.main()
