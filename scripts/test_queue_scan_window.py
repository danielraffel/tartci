#!/usr/bin/env python3
"""Regression tests for bounded provider queue scans.

GitHub can leave workflow runs in `queued` after their useful jobs are cancelled
or completed. With an oldest-first scan and a 30-run fetch cap, 30 such records
hid every newer servable job and left healthy VM supervisors reporting
`queued=0`.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACOS = ROOT / "providers" / "tart-macos" / "runner.sh"
PROVIDERS = [
    MACOS,
    ROOT / "providers" / "tart-linux" / "runner.sh",
    ROOT / "providers" / "qemu-windows" / "runner.sh",
]


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class QueueScanWindowTests(unittest.TestCase):
    def test_more_than_thirty_stale_runs_do_not_hide_new_eligible_job(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stale_runs = [
                {
                    "id": 1000 + index,
                    "name": "Build and Test",
                    "status": "queued",
                    "created_at": f"2026-01-01T00:{index:02d}:00Z",
                }
                for index in range(31)
            ]
            eligible = {
                "id": 2000,
                "name": "Build and Test",
                "status": "queued",
                "created_at": "2026-01-02T00:00:00Z",
            }
            payload_path = tmp / "runs.json"
            payload_path.write_text(
                json.dumps({"workflow_runs": [*stale_runs, eligible]}),
                encoding="utf-8",
            )

            _write_exec(tmp / "tart", "#!/usr/bin/env bash\nexit 0\n")
            _write_exec(
                tmp / "fake-gh",
                """#!/usr/bin/env python3
import json
import os
import sys

path = sys.argv[-1]
if path.endswith("/actions/workflows?per_page=100"):
    print(json.dumps({"workflows": [{"id": 99, "name": "Build and Test"}]}))
elif "/actions/workflows/99/runs?status=queued" in path:
    print(open(os.environ["RUNS_PAYLOAD"], encoding="utf-8").read())
elif "/actions/workflows/99/runs?status=in_progress" in path:
    print(json.dumps({"workflow_runs": []}))
elif "/actions/runs/2000/jobs" in path:
    print(json.dumps({"jobs": [{
        "status": "queued",
        "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm"]
    }]}))
elif "/actions/runs/" in path and "/jobs" in path:
    print(json.dumps({"jobs": []}))
else:
    raise SystemExit(f"unexpected API path: {path}")
""",
            )
            base_path = [
                str(tmp),
                *[
                    directory
                    for directory in (
                        "/bin",
                        "/usr/bin",
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                    )
                    if Path(directory).exists()
                ],
            ]
            env = {
                "HOME": str(tmp),
                "PATH": os.pathsep.join(base_path),
                "RUNS_PAYLOAD": str(payload_path),
                "TART_HOME": str(tmp / "vms"),
                "TARTCI_STATE_DIR": str(tmp / "state"),
                "TARTCI_GH_CLI": "fake-gh",
                "TARTCI_RUNNER_WORKFLOW_NAME": "Build and Test",
            }
            result = subprocess.run(
                [
                    "bash",
                    str(MACOS),
                    "--print-queue",
                    "--labels",
                    "self-hosted,macOS,ARM64,pulp-build-vm",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "1", result.stdout + result.stderr)

    def test_all_bounded_provider_scans_prefer_recent_runs(self) -> None:
        for provider in PROVIDERS:
            body = provider.read_text(encoding="utf-8")
            self.assertIn(
                'runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)',
                body,
                provider,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
