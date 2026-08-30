#!/usr/bin/env python3
"""Regressions for bounded live assignment receipt discovery."""
from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts" / "current_job_scan.py"
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class CurrentJobScanTests(unittest.TestCase):
    def _scan(self, fake_body: str, *extra: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-gh"
            _write_exec(fake, fake_body)
            return subprocess.run(
                [
                    "python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                    "--runner", "studio-pulp-gate-01-59011-9",
                    "--workflow", "Build and Test", "--gh-cli", str(fake), *extra,
                ],
                text=True, capture_output=True, check=False,
            )

    def test_assignment_beyond_first_run_page_is_captured(self) -> None:
        result = self._scan(r'''#!/usr/bin/env python3
import json, sys
from urllib.parse import parse_qs, urlparse
p = urlparse("https://x/" + sys.argv[-1]); page = int(parse_qs(p.query).get("page", ["1"])[0])
if p.path.endswith("/actions/runs"):
    runs = ([{"id": i, "name": "Build and Test"} for i in range(1, 101)] if page == 1
            else [{"id": 33327626489, "name": "Build and Test"}])
    print(json.dumps({"total_count": 101, "workflow_runs": runs}))
elif "/actions/runs/" in p.path and p.path.endswith("/jobs"):
    run_id = int(p.path.split("/actions/runs/", 1)[1].split("/", 1)[0])
    jobs = ([{"id": 99300536389, "status": "in_progress", "runner_name": "studio-pulp-gate-01-59011-9"}]
            if run_id == 33327626489 else [])
    print(json.dumps({"total_count": len(jobs), "jobs": jobs}))
else: raise SystemExit(4)
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "33327626489\t99300536389\tBuild and Test",
        )

    def test_api_failure_fails_closed(self) -> None:
        result = self._scan("#!/usr/bin/env bash\nexit 9\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("failed closed", result.stderr)

    def test_duplicate_active_assignment_fails_closed(self) -> None:
        result = self._scan(r'''#!/usr/bin/env python3
import json, sys
from urllib.parse import urlparse
p = urlparse("https://x/" + sys.argv[-1])
if p.path.endswith("/actions/runs"):
    print(json.dumps({"total_count": 2, "workflow_runs": [
        {"id": 1, "name": "Build and Test"}, {"id": 2, "name": "Build and Test"}]}))
elif "/actions/runs/" in p.path and p.path.endswith("/jobs"):
    run_id = int(p.path.split("/actions/runs/", 1)[1].split("/", 1)[0])
    print(json.dumps({"total_count": 1, "jobs": [{"id": 100 + run_id,
        "status": "in_progress", "runner_name": "studio-pulp-gate-01-59011-9"}]}))
else: raise SystemExit(4)
''')
        self.assertEqual(result.returncode, 2)
        self.assertIn("matched 2 active jobs", result.stderr)

    def test_page_cap_fails_closed_without_unbounded_calls(self) -> None:
        result = self._scan(r'''#!/usr/bin/env python3
import json
print(json.dumps({"total_count": 101, "workflow_runs": [
    {"id": i, "name": "Other"} for i in range(1, 101)]}))
''', "--max-pages", "1")
        self.assertEqual(result.returncode, 2)
        self.assertIn("pagination truncated at 1 pages", result.stderr)

    def test_running_job_never_becomes_idle_when_observation_fails(self) -> None:
        body = RUNNER.read_text(encoding="utf-8")
        self.assertIn('assigned=1\n      assigned_at="$now"', body)
        self.assertIn(
            'heartbeat "$([ "$assigned" = 1 ] && printf job-running || printf idle-wait)"',
            body,
        )
        self.assertIn('CURRENT_JOB_CAPTURE_STATUS="error"', body)
        self.assertIn('"assignment_observation":', body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
