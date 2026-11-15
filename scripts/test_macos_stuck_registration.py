#!/usr/bin/env python3
"""A busy registration GitHub will not delete must not wedge the lane forever."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).parents[1] / "providers" / "tart-macos" / "runner.sh"
NAME = "m1-vellum-gate-01-54538-6"


def extract(name: str) -> str:
    text = RUNNER.read_text()
    start = text.index(f"\n{name}(){{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


class StuckRegistrationTests(unittest.TestCase):
    def run_reclaim(self, td: Path, calls: int, delete_ok: bool, env: str = "",
                    err: str = "HTTP 422: Runner is busy"):
        deletes = td / "deletes.txt"
        gh = td / "fake-gh"
        gh.write_text(
            "#!/bin/bash\n"
            'if [ "$1" = "api" ] && [ "$2" = "-X" ] && [ "$3" = "DELETE" ]; then\n'
            f'  echo x >> "{deletes}"\n'
            f"  [ {0 if delete_ok else 1} = 1 ] && echo '{err}' >&2\n"
            f"  exit {0 if delete_ok else 1}\n"
            "fi\n"
            'case "$2" in\n'
            '  *"actions/runs?"*) echo 77;;\n'
            f'  *"actions/runs/77/jobs"*) echo "run_id=77 job_id=555";;\n'
            f'  *) echo 33570;;\n'
            "esac\n"
        )
        gh.chmod(0o755)
        script = (
            "set -u\n"
            f'STATE_DIR="{td}"; GH_CLI="{gh}"; REPO=o/r; RUNNER_API_ROOT=https://api/r\n'
            f"{env}\n"
            "sleep(){ :; }\n"
            "tart(){ :; }\n"
            "note(){ printf 'NOTE %s\\n' \"$*\"; }\n"
            "event(){ printf 'EVENT %s\\n' \"$*\"; }\n"
            + extract("stuck_registration_file")
            + extract("stuck_registration_job")
            + extract("reclaim_runner_name")
            + "STUCK_REGISTRATION_NAME=''\n"
            + f'for c in $(seq 1 {calls}); do reclaim_runner_name "{NAME}" https://api/r; done\n'
            + 'echo "STUCK=$STUCK_REGISTRATION_NAME"\n'
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env=dict(os.environ))
        n = len(deletes.read_text().split()) if deletes.exists() else 0
        return n, proc.stdout, proc.stderr

    def test_busy_delete_stops_after_n_and_reports(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            n, out, _ = self.run_reclaim(Path(t), calls=1, delete_ok=False)
        self.assertEqual(n, 3, "default bound is 3 deletes")
        self.assertIn("FINDING stuck_runner_registration", out)
        self.assertIn("run_id=77 job_id=555", out)
        self.assertIn(f"STUCK={NAME}", out)
        self.assertEqual(out.count("FINDING"), 1)

    def test_bound_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            n, _, _ = self.run_reclaim(
                Path(t), calls=1, delete_ok=False,
                env="TARTCI_STUCK_REGISTRATION_MAX_DELETES=2")
        self.assertEqual(n, 2)

    def test_stuck_registration_is_not_retried_every_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            n, out, _ = self.run_reclaim(Path(t), calls=5, delete_ok=False)
        self.assertEqual(n, 3, "later cycles must not delete again inside the retry window")
        self.assertEqual(out.count("FINDING"), 1)
        self.assertIn("not retrying", out)

    def test_periodic_retry_after_window(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            n, out, _ = self.run_reclaim(
                Path(t), calls=3, delete_ok=False,
                env="TARTCI_STUCK_REGISTRATION_RETRY_SECS=0")
        self.assertEqual(n, 3 + 2, "every later cycle retries once when the window is 0")
        self.assertIn("retrying stuck runner registration", out)

    def test_transient_failures_never_count_as_stuck(self) -> None:
        for err in ("HTTP 502 Bad Gateway", "dial tcp: i/o timeout", "HTTP 403: rate limit exceeded"):
            with tempfile.TemporaryDirectory() as t:
                n, out, _ = self.run_reclaim(Path(t), calls=1, delete_ok=False, err=err)
                self.assertEqual(n, 18, err)
                self.assertNotIn("FINDING", out)
                self.assertIn("STUCK=\n", out)
                self.assertEqual(list(Path(t).glob("stuck-registration-*")), [])

    def test_sweep_matches_the_fresh_name(self) -> None:
        sweep = extract("sweep_lane_ghost_runners")
        self.assertIn("(-[0-9]+)?$", sweep)
        self.assertTrue(re.search(r"^m1-vellum-gate-01-[0-9]+-[0-9]+(-[0-9]+)?$", NAME + "-1789000000"))

    def test_successful_delete_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            n, out, _ = self.run_reclaim(Path(t), calls=1, delete_ok=True)
        self.assertEqual(n, 1)
        self.assertNotIn("FINDING", out)
        self.assertIn("STUCK=\n", out)

    def test_boot_path_uses_a_fresh_name(self) -> None:
        text = RUNNER.read_text()
        self.assertIn('if [ "$STUCK_REGISTRATION_NAME" = "$vm" ]', text)
        self.assertIn('vm="$(ephemeral_boot_name "$i")-$(date +%s)"', text)


if __name__ == "__main__":
    unittest.main()
