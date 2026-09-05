#!/usr/bin/env python3
"""A priority job already assigned to a runner is not unmet demand.

A secondary lane yields its VM slot while a higher-priority lane has work
waiting. The demand count therefore has to mean "priority work that still needs
a slot". An `in_progress` job that already carries a `runner_name` is being
served: counting it makes every *other* host in the fleet reserve capacity for
work that is already under way, which is how a host with free slots refuses a
release build while another host runs the gate job it is yielding to.

The queued/in_progress window itself is deliberate and stays: a priority run's
hosted resolver leg can flip to `in_progress` before its self-hosted leg is
queued, and dropping the window would let a secondary lane take the gate's slot
in that gap. Only the *assigned* subset is excluded, and only when the caller
opts in.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = ROOT / "scripts" / "queue_scan.py"
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"

LANE_LABELS = "self-hosted,macos,arm64,pulp-build,pulp-build-vm,pulp-build-pr-head"

STUB = """#!/usr/bin/env python3
import json, sys, os
path = sys.argv[-1]
jobs = json.load(open(os.environ["FAKE_GH_JOBS"]))
if "/actions/workflows?" in path:
    print(json.dumps({"workflows": [
        {"id": 7, "name": "Build and Test", "path": ".github/workflows/build.yml"}
    ]}))
elif "/actions/runs?status=in_progress" in path or "/runs?status=in_progress" in path:
    print(json.dumps({"workflow_runs": [{
        "id": 1, "name": "Build and Test", "status": "in_progress",
        "created_at": "2026-09-05T00:00:00Z", "updated_at": "2026-09-05T00:00:00Z"}]}))
elif "status=" in path and "/runs" in path:
    print(json.dumps({"workflow_runs": []}))
elif "/jobs?" in path:
    print(json.dumps({"jobs": jobs}))
else:
    raise SystemExit("unexpected API path: " + path)
"""


def _job(job_id: int, status: str, runner_name: str | None) -> dict:
    job = {
        "id": job_id,
        "status": status,
        "created_at": "2026-09-05T00:00:00Z",
        "labels": LANE_LABELS.split(","),
    }
    if runner_name is not None:
        job["runner_name"] = runner_name
    return job


def _scan(tmp: Path, jobs: list[dict], exclude_assigned: int) -> int:
    """Run queue_scan against a stubbed gh and return its demand count."""
    gh = tmp / "fake-gh"
    gh.write_text(STUB, encoding="utf-8")
    gh.chmod(0o755)
    jobs_path = tmp / "jobs.json"
    jobs_path.write_text(json.dumps(jobs), encoding="utf-8")
    out = subprocess.run(
        [
            sys.executable, str(SCAN),
            "--repo", "Generous-Corp/pulp",
            "--workflow", "Build and Test",
            "--labels", LANE_LABELS,
            "--job-statuses", "queued,in_progress",
            "--provider", "test",
            "--lane-id", f"test-{exclude_assigned}",
            "--state-file", str(tmp / f"state-{exclude_assigned}.json"),
            "--max-age-seconds", "0",
            "--match-labels", "1",
            "--exclude-assigned", str(exclude_assigned),
            "--gh-cli", str(gh),
        ],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "HOME": str(tmp), "FAKE_GH_JOBS": str(jobs_path)},
    )
    if out.returncode != 0:
        raise AssertionError(f"queue_scan rc={out.returncode}: {out.stderr[-500:]}")
    return int(out.stdout.strip().splitlines()[-1])


class PriorityDemandExcludesAssigned(unittest.TestCase):
    def test_assigned_in_progress_job_is_not_demand(self) -> None:
        """The fix: a job already running on a runner needs no further slot."""
        jobs = [_job(1, "in_progress", "studio-pulp-gate-01")]
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self.assertEqual(_scan(tmp, jobs, exclude_assigned=0), 1)
            self.assertEqual(_scan(tmp, jobs, exclude_assigned=1), 0)

    def test_queued_job_is_still_demand(self) -> None:
        """Negative control. A queued job has no runner and MUST still count,
        otherwise the flag would silence real demand and a secondary lane would
        steal the priority lane's slot."""
        jobs = [_job(2, "queued", None)]
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self.assertEqual(_scan(tmp, jobs, exclude_assigned=1), 1)

    def test_unassigned_in_progress_job_is_still_demand(self) -> None:
        """Negative control for the documented race the queued/in_progress
        window exists to cover: an in_progress job with no runner yet is not
        being served, so it must survive the exclusion."""
        jobs = [_job(3, "in_progress", None)]
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            self.assertEqual(_scan(tmp, jobs, exclude_assigned=1), 1)

    def test_default_is_unchanged(self) -> None:
        """Ordinary queue scans must be untouched: the flag defaults to off."""
        jobs = [_job(4, "in_progress", "studio-pulp-gate-01")]
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_scan(Path(d), jobs, exclude_assigned=0), 1)

    def test_priority_demand_passes_the_flag(self) -> None:
        """The runner's priority_demand() must actually opt in, or the fix is
        inert in production even though every scan test above passes."""
        body = RUNNER.read_text(encoding="utf-8")
        start = body.index("priority_demand()")
        end = body.index("reclaim_runner_name()", start)
        self.assertIn("--exclude-assigned 1", body[start:end])


if __name__ == "__main__":
    unittest.main()
