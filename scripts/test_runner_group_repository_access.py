#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "runner_group_repository_access.py"


FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
from urllib.parse import parse_qs, urlparse

with open(os.environ["ACCESS_STATE"], encoding="utf-8") as handle:
    state = json.load(handle)
with open(os.environ["ACCESS_CALLS"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "argv": sys.argv[1:],
        "repo": os.environ.get("SHIPYARD_GH_APP_REPO"),
        "gh_repo": os.environ.get("GH_REPO"),
    }) + "\n")
path = sys.argv[-1]
if state.get("api_error"):
    print("gh: Resource not accessible by integration (HTTP 403)", file=sys.stderr)
    raise SystemExit(1)
if "/repositories?" not in path:
    print(json.dumps({"id": 3, "visibility": state.get("visibility", "selected")}))
    raise SystemExit(0)
parsed = urlparse("https://example.invalid/" + path)
page = int(parse_qs(parsed.query).get("page", ["1"])[0])
pages = state.get("pages", [[]])
repositories = pages[page - 1] if page <= len(pages) else []
print(json.dumps({
    "total_count": sum(len(items) for items in pages),
    "repositories": [{"full_name": name} for name in repositories],
}))
'''


class RunnerGroupRepositoryAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gh = self.root / "fake-gh"
        self.gh.write_text(FAKE_GH, encoding="utf-8")
        self.gh.chmod(self.gh.stat().st_mode | stat.S_IXUSR)
        self.state = self.root / "state.json"
        self.calls = self.root / "calls.jsonl"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_check(self, group: int, state: dict) -> subprocess.CompletedProcess[str]:
        self.state.write_text(json.dumps(state), encoding="utf-8")
        env = os.environ.copy()
        env["ACCESS_STATE"] = str(self.state)
        env["ACCESS_CALLS"] = str(self.calls)
        return subprocess.run(
            [
                "python3", str(CHECK),
                "--repo", "Generous-Corp/pulp",
                "--runner-group-id", str(group),
                "--gh-cli", str(self.gh),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=env,
        )

    def test_repository_scoped_registration_is_intrinsically_visible(self) -> None:
        result = self.run_check(1, {"api_error": True})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["registration_scope"], "repository")
        self.assertFalse(self.calls.exists(), "repository scope should not query org policy")

    def test_selected_org_group_must_include_the_repository(self) -> None:
        result = self.run_check(3, {"pages": [["Generous-Corp/forge"]]})
        self.assertEqual(result.returncode, 3)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["verdict"], "deny")
        self.assertIn("must select only", receipt["reason"])

    def test_selected_org_group_rejects_cross_repository_assignment_scope(self) -> None:
        result = self.run_check(
            3,
            {"pages": [["Generous-Corp/forge"], ["Generous-Corp/pulp"]]},
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["verdict"], "deny")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call["repo"] == "Generous-Corp/pulp" for call in calls))
        self.assertTrue(all(call["gh_repo"] == "Generous-Corp/pulp" for call in calls))

    def test_selected_org_group_admits_only_exact_single_repository(self) -> None:
        result = self.run_check(3, {"pages": [["Generous-Corp/pulp"]]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["registration_scope"],
            "organization-single-repository",
        )

    def test_unknown_policy_and_api_denial_fail_closed(self) -> None:
        for state in ({"visibility": "mystery"}, {"api_error": True}):
            with self.subTest(state=state):
                self.calls.unlink(missing_ok=True)
                result = self.run_check(3, state)
                self.assertEqual(result.returncode, 2)
                self.assertIn("access error", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
