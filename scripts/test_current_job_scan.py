#!/usr/bin/env python3
"""Behavioral regressions for bounded assignment receipt discovery."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts/current_job_scan.py"
RUNNER = ROOT / "providers/tart-macos/runner.sh"
RUNNER_NAME = "studio-pulp-gate-01-59011-9"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from urllib.parse import parse_qs, urlparse
p = urlparse("https://x/" + sys.argv[-1]); path = p.path; page = int(parse_qs(p.query).get("page", ["1"])[0])
kind = os.environ.get("FAKE_KIND", "active")
runner = "studio-pulp-gate-01-59011-9"
if path.endswith("/actions/workflows"):
    print(json.dumps({"total_count": 1, "workflows": [{"id": 99, "name": "Build and Test"}]}))
elif path.endswith("/actions/runs"):
    runs = ([{"id": i, "name": "Other", "workflow_id": 77} for i in range(1, 101)] if page == 1
            else [{"id": 333, "name": "Build and Test", "workflow_id": 99}])
    print(json.dumps({"total_count": 101, "workflow_runs": runs}))
elif path.endswith("/jobs"):
    run_id = int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])
    jobs = []
    if run_id == 333:
        jobs = [{"id": 444, "status": "in_progress", "runner_name": runner}]
    elif run_id == 1 and kind == "ambiguous":
        jobs = [{"id": 445, "status": "in_progress", "runner_name": runner}]
    print(json.dumps({"total_count": len(jobs), "jobs": jobs}))
elif path.endswith("/actions/jobs/444"):
    status = "completed" if kind == "terminal" else "in_progress"
    assigned = "somebody-else" if kind == "changed" else runner
    print(json.dumps({"id": 444, "status": status, "conclusion": "success" if status == "completed" else None, "runner_name": assigned}))
elif path.endswith("/actions/runs/333"):
    workflow_id = 88 if kind == "unexpected" else 99
    name = "Surprise" if kind == "unexpected" else "Build and Test"
    status = "completed" if kind == "terminal" else "in_progress"
    print(json.dumps({"id": 333, "workflow_id": workflow_id, "name": name,
                      "status": status, "conclusion": "cancelled" if status == "completed" else None}))
else: raise SystemExit(4)
'''


class CurrentJobScanTests(unittest.TestCase):
    def _scan(self, kind: str = "active", *extra: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-gh"
            _write_exec(fake, FAKE)
            return subprocess.run(
                ["python3", str(SCANNER), "--repo", "Generous-Corp/pulp",
                 "--runner", RUNNER_NAME, "--workflow", "Build and Test",
                 "--gh-cli", str(fake),
                 "--observation-lock-file", str(Path(directory) / "observation.lock"),
                 *extra],
                env={**os.environ, "FAKE_KIND": kind}, text=True, capture_output=True, check=False,
            )

    def test_assignment_beyond_first_repository_run_page_is_exactly_confirmed(self) -> None:
        result = self._scan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["kind"], "active")
        self.assertEqual(json.loads(result.stdout)["job_id"], 444)

    def test_unexpected_workflow_assignment_is_typed_not_idle(self) -> None:
        receipt = json.loads(self._scan("unexpected").stdout)
        self.assertEqual(receipt["kind"], "unexpected_assignment")
        self.assertEqual(receipt["workflow_name"], "Surprise")

    def test_ambiguous_assignment_is_typed(self) -> None:
        receipt = json.loads(self._scan("ambiguous").stdout)
        self.assertEqual(receipt, {"kind": "ambiguous_assignment", "matches": 2})

    def test_same_count_deeper_page_churn_cannot_confirm_no_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-gh"
            counter = Path(directory) / "counter"
            body = f'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
p = urlparse("https://x/" + sys.argv[-1]); page = int(parse_qs(p.query).get("page", ["1"])[0])
counter = Path({str(counter)!r})
if p.path.endswith("/actions/workflows"):
 print(json.dumps({{"total_count":1,"workflows":[{{"id":99,"name":"Build and Test"}}]}}))
elif p.path.endswith("/actions/runs"):
 n = int(counter.read_text()) if counter.exists() else 0; counter.write_text(str(n + 1))
 sweep = n // 3
 runs = ([{{"id":i,"name":"Build and Test"}} for i in range(1,101)] if page == 1
         else [{{"id":333 + sweep,"name":"Build and Test"}}])
 print(json.dumps({{"total_count":101,"workflow_runs":runs}}))
elif p.path.endswith("/jobs"):
 print(json.dumps({{"total_count":0,"jobs":[]}}))
else: raise SystemExit(4)
'''
            _write_exec(fake, body)
            result = subprocess.run(
                ["python3", str(SCANNER), "--repo", "x/y", "--runner", "r",
                 "--workflow", "Build and Test", "--gh-cli", str(fake),
                 "--scan-timeout", "60",
                 "--observation-lock-file", str(Path(directory) / "observation.lock")],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("scheduler pages changed", json.loads(result.stdout)["detail"])

    def test_exact_revalidation_reports_terminal_and_disallows_guessing(self) -> None:
        result = self._scan("terminal", "--mode", "revalidate", "--run-id", "333", "--job-id", "444")
        self.assertEqual(json.loads(result.stdout)["kind"], "terminal")
        changed = self._scan("changed", "--mode", "revalidate", "--run-id", "333", "--job-id", "444")
        self.assertEqual(json.loads(changed.stdout)["kind"], "assignment_changed")

    def test_api_failure_has_typed_error_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-gh"
            _write_exec(fake, "#!/usr/bin/env bash\nexit 9\n")
            result = subprocess.run(
                ["python3", str(SCANNER), "--repo", "x/y", "--runner", "r",
                 "--workflow", "w", "--gh-cli", str(fake),
                 "--observation-lock-file", str(Path(directory) / "observation.lock")], text=True,
                capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["kind"], "observation_error")

    def test_churn_in_first_page_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-gh"
            counter = Path(directory) / "counter"
            body = f'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
p = urlparse("https://x/" + sys.argv[-1]); page = int(parse_qs(p.query).get("page", ["1"])[0])
counter = Path({str(counter)!r})
if p.path.endswith("/actions/workflows"):
    print(json.dumps({{"total_count": 1, "workflows": [{{"id": 99, "name": "Build and Test"}}]}}))
elif p.path.endswith("/actions/runs"):
    seen = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(seen + 1))
    start = 1000 if seen >= 2 else 1
    runs = ([{{"id": start + i, "name": "Build and Test"}} for i in range(100)] if page == 1
            else [{{"id": 333, "name": "Build and Test"}}])
    print(json.dumps({{"total_count": 101, "workflow_runs": runs}}))
else: raise SystemExit(4)
'''
            _write_exec(fake, body)
            result = subprocess.run(
                ["python3", str(SCANNER), "--repo", "x/y", "--runner", "r",
                 "--workflow", "Build and Test", "--gh-cli", str(fake),
                 "--scan-timeout", "60",
                 "--observation-lock-file", str(Path(directory) / "observation.lock")],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["kind"], "observation_error")
        self.assertIn("first page changed", json.loads(result.stdout)["detail"])

    def test_parallel_api_fanout_is_rejected(self) -> None:
        result = self._scan("active", "--parallelism", "2")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--parallelism must be 1", result.stderr)

    def test_host_lock_timeout_fails_closed_before_api_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "observation.lock"
            marker = Path(directory) / "called"
            fake = Path(directory) / "fake-gh"
            _write_exec(fake, f"#!/usr/bin/env bash\ntouch {str(marker)!r}\nexit 9\n")
            with lock.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    ["python3", str(SCANNER), "--repo", "x/y", "--runner", "r",
                     "--workflow", "w", "--gh-cli", str(fake),
                     "--observation-lock-file", str(lock),
                     "--observation-lock-timeout", "0.1"],
                    text=True, capture_output=True, check=False,
                )
        self.assertEqual(result.returncode, 2)
        self.assertIn("host queue observation lock timed out", result.stdout)
        self.assertFalse(marker.exists())

if __name__ == "__main__":
    unittest.main(verbosity=2)
