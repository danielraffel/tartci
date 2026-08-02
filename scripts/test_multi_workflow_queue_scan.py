#!/usr/bin/env python3
"""Regression coverage for one macOS lane watching several workflows."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
RELEASE_TEMPLATE = (
    ROOT
    / "launchd"
    / "com.danielraffel.pulp.tart-runner-macos-release.plist.template"
)


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class MultiWorkflowQueueScanTests(unittest.TestCase):
    def test_tier_rejects_nonexclusive_comma_label_set(self) -> None:
        env = {
            **os.environ,
            "TARTCI_RUNNER_WORKFLOW_TIERS": "shared,low|Release-path PR gate",
        }
        result = subprocess.run(
            ["bash", str(RUNNER), "--print-name"],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("one exclusive class label", result.stderr)

    def test_plural_env_reaches_all_release_workflows_through_one_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_exec(root / "tart", "#!/usr/bin/env bash\nexit 0\n")
            _write_exec(
                root / "fake-gh",
                """#!/usr/bin/env python3
import json
import sys

path = sys.argv[-1]
names = ["Release CLI", "Release-path PR gate", "Sign and Release", "Other"]
if "/actions/runs?status=queued" in path:
    print(json.dumps({"workflow_runs": [
        {"id": index + 1, "name": name, "status": "queued",
         "created_at": f"2026-01-01T00:0{index}:00Z",
         "updated_at": f"2026-01-01T00:0{index}:00Z"}
        for index, name in enumerate(names)
    ]}))
elif "/actions/runs?status=in_progress" in path:
    print(json.dumps({"workflow_runs": []}))
elif "/jobs?" in path:
    run_id = int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])
    print(json.dumps({"jobs": ([{
        "id": 100 + run_id,
        "status": "queued",
        "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm-release"]
    }] if run_id <= 3 else [])}))
else:
    raise SystemExit(f"unexpected API path: {path}")
""",
            )
            search_path = [
                str(root),
                *[
                    item
                    for item in (
                        "/bin",
                        "/usr/bin",
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                    )
                    if Path(item).exists()
                ],
            ]
            env = {
                "HOME": str(root),
                "PATH": os.pathsep.join(search_path),
                "TART_HOME": str(root / "vms"),
                "TARTCI_STATE_DIR": str(root / "state"),
                "TARTCI_GH_CLI": "fake-gh",
                "TARTCI_RUNNER_LABELS": (
                    "self-hosted,macOS,ARM64,pulp-build-vm-release"
                ),
                "TARTCI_RUNNER_WORKFLOW_NAME": "ignored legacy value",
                "TARTCI_RUNNER_WORKFLOW_NAMES": (
                    "Release CLI\nRelease-path PR gate\n"
                    "Sign and Release\nRelease CLI"
                ),
                "TARTCI_QUEUE_STAGGER_MAX_SECS": "0",
            }
            result = subprocess.run(
                ["bash", str(RUNNER), "--print-queue"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            state_files = list((root / "state").glob("*.state.json"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "3", result.stdout + result.stderr)
        self.assertEqual(state_files, [], "queue probe clobbered supervisor state")

    def test_local_print_modes_do_not_write_lifecycle_state(self) -> None:
        for mode in ("--print-priority-demand", "--print-host-health"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _write_exec(root / "tart", "#!/usr/bin/env bash\nexit 0\n")
                _write_exec(root / "fake-gh", "#!/usr/bin/env bash\nexit 99\n")
                search_path = [
                    str(root),
                    *[
                        item
                        for item in ("/bin", "/usr/bin", "/usr/local/bin")
                        if Path(item).exists()
                    ],
                ]
                env = {
                    "HOME": str(root),
                    "PATH": os.pathsep.join(search_path),
                    "TART_HOME": str(root / "vms"),
                    "TARTCI_STATE_DIR": str(root / "state"),
                    "TARTCI_GH_CLI": "fake-gh",
                    "TARTCI_RUNNER_LABELS": (
                        "self-hosted,macOS,ARM64,pulp-build-vm-release"
                    ),
                }
                result = subprocess.run(
                    ["bash", str(RUNNER), mode],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=env,
                )

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "0")
                self.assertEqual(
                    list((root / "state").glob("*.state.json")),
                    [],
                    f"{mode} clobbered supervisor state",
                )

    def test_release_template_lists_every_shared_pool_workflow(self) -> None:
        body = RELEASE_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("<key>TARTCI_RUNNER_WORKFLOW_TIERS</key>", body)
        self.assertNotIn("<key>TARTCI_RUNNER_WORKFLOW_NAMES</key>", body)
        self.assertNotIn("<key>TARTCI_RUNNER_WORKFLOW_NAME</key>", body)
        self.assertIn(
            "<string>pulp-release-tagged|Release CLI\n"
            "pulp-release-tagged|Sign and Release\n"
            "pulp-release-pr-gate|Release-path PR gate</string>",
            body,
        )

    def test_tier_selection_prefers_tagged_work_over_older_pr_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_exec(root / "tart", "#!/usr/bin/env bash\nexit 0\n")
            _write_exec(
                root / "fake-gh",
                """#!/usr/bin/env python3
import json
import sys

path = sys.argv[-1]
runs = [
    {"id": 1, "name": "Release-path PR gate", "status": "queued",
     "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"},
    {"id": 2, "name": "Release CLI", "status": "queued",
     "created_at": "2026-01-01T00:05:00Z", "updated_at": "2026-01-01T00:05:00Z"},
]
if "/actions/runs?status=queued" in path:
    print(json.dumps({"workflow_runs": runs}))
elif "/actions/runs?status=in_progress" in path:
    print(json.dumps({"workflow_runs": []}))
elif "/jobs?" in path:
    run_id = int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])
    label = "pulp-release-pr-gate" if run_id == 1 else "pulp-release-tagged"
    print(json.dumps({"jobs": [{
        "id": 100 + run_id,
        "status": "queued",
        "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm-release", label]
    }]}))
else:
    raise SystemExit(f"unexpected API path: {path}")
""",
            )
            search_path = [
                str(root),
                *[
                    item
                    for item in ("/bin", "/usr/bin", "/usr/local/bin")
                    if Path(item).exists()
                ],
            ]
            env = {
                "HOME": str(root),
                "PATH": os.pathsep.join(search_path),
                "TART_HOME": str(root / "vms"),
                "TARTCI_STATE_DIR": str(root / "state"),
                "TARTCI_GH_CLI": "fake-gh",
                "TARTCI_RUNNER_LABELS": (
                    "self-hosted,macOS,ARM64,pulp-build-vm-release"
                ),
                "TARTCI_RUNNER_WORKFLOW_TIERS": (
                    "pulp-release-tagged|Release CLI\n"
                    "pulp-release-tagged|Sign and Release\n"
                    "pulp-release-pr-gate|Release-path PR gate"
                ),
                "TARTCI_QUEUE_STAGGER_MAX_SECS": "0",
            }
            result = subprocess.run(
                ["bash", str(RUNNER), "--print-selection"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            recheck = subprocess.run(
                [
                    "bash",
                    str(RUNNER),
                    "--print-higher-priority-demand",
                    "1",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        fields = result.stdout.strip().split("\t")
        self.assertEqual(fields[0], "1")
        self.assertEqual(
            fields[1],
            "self-hosted,macOS,ARM64,pulp-build-vm-release,pulp-release-tagged",
        )
        self.assertEqual(fields[2], "0")
        self.assertEqual(recheck.returncode, 0, recheck.stderr)
        self.assertEqual(recheck.stdout.strip(), "1")

    def test_tier_selection_falls_through_to_pr_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_exec(root / "tart", "#!/usr/bin/env bash\nexit 0\n")
            _write_exec(
                root / "fake-gh",
                """#!/usr/bin/env python3
import json
import os
import sys

path = sys.argv[-1]
high_visible = os.path.exists(os.path.join(os.environ["HOME"], "high-visible"))
if path.endswith("/actions/workflows?per_page=100"):
    print(json.dumps({"workflows": [{"id": 99, "name": "Release-path PR gate"}]}))
elif "/actions/workflows/99/runs?status=queued" in path:
    print(json.dumps({"workflow_runs": [{
        "id": 1, "name": "Release-path PR gate", "status": "queued",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"
    }]}))
elif "/actions/workflows/99/runs?status=in_progress" in path:
    print(json.dumps({"workflow_runs": []}))
elif "/actions/runs?status=queued" in path:
    runs = [{
        "id": 1, "name": "Release-path PR gate", "status": "queued",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"
    }]
    if high_visible:
        runs.append({
            "id": 2, "name": "Release CLI", "status": "queued",
            "created_at": "2026-01-01T00:05:00Z", "updated_at": "2026-01-01T00:05:00Z"
        })
    print(json.dumps({"workflow_runs": runs}))
elif "/actions/runs?status=in_progress" in path:
    print(json.dumps({"workflow_runs": []}))
elif "/jobs?" in path:
    run_id = int(path.split("/actions/runs/", 1)[1].split("/", 1)[0])
    label = "pulp-release-tagged" if run_id == 2 else "pulp-release-pr-gate"
    print(json.dumps({"jobs": [{
        "id": 100 + run_id, "status": "queued",
        "labels": ["self-hosted", "macOS", "ARM64", "pulp-build-vm-release", label]
    }]}))
else:
    raise SystemExit(f"unexpected API path: {path}")
""",
            )
            search_path = [
                str(root),
                *[
                    item
                    for item in ("/bin", "/usr/bin", "/usr/local/bin")
                    if Path(item).exists()
                ],
            ]
            env = {
                "HOME": str(root),
                "PATH": os.pathsep.join(search_path),
                "TART_HOME": str(root / "vms"),
                "TARTCI_STATE_DIR": str(root / "state"),
                "TARTCI_GH_CLI": "fake-gh",
                "TARTCI_RUNNER_LABELS": (
                    "self-hosted,macOS,ARM64,pulp-build-vm-release"
                ),
                "TARTCI_RUNNER_WORKFLOW_TIERS": (
                    "pulp-release-tagged|Release CLI\n"
                    "pulp-release-tagged|Sign and Release\n"
                    "pulp-release-pr-gate|Release-path PR gate"
                ),
                "TARTCI_QUEUE_STAGGER_MAX_SECS": "0",
            }
            result = subprocess.run(
                ["bash", str(RUNNER), "--print-selection"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            (root / "high-visible").touch()
            recheck = subprocess.run(
                [
                    "bash",
                    str(RUNNER),
                    "--print-higher-priority-demand",
                    "1",
                ],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        fields = result.stdout.strip().split("\t")
        self.assertEqual(fields[0], "1")
        self.assertEqual(
            fields[1],
            "self-hosted,macOS,ARM64,pulp-build-vm-release,pulp-release-pr-gate",
        )
        self.assertEqual(fields[2], "1")
        self.assertEqual(recheck.returncode, 0, recheck.stderr)
        self.assertEqual(
            recheck.stdout.strip(),
            "1",
            "pre-mint recheck reused the fresh empty cache and missed tagged work",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
