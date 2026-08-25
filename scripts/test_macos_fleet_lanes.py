#!/usr/bin/env python3
from __future__ import annotations

import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "profiles" / "m1-macos-fleet.toml"


class MacosFleetLaneTests(unittest.TestCase):
    def test_checked_in_config_validates_and_renders_dormant_dynamic_lanes(self) -> None:
        valid = subprocess.run(
            [str(ROOT / "tartci"), "fleet-macos", "validate", str(CONFIG)],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertIn("lanes=3", valid.stdout)
        with tempfile.TemporaryDirectory() as td:
            rendered = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "render", str(CONFIG), "--output", td],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            files = sorted(Path(td).glob("*.plist"))
            self.assertEqual(len(files), 3)
            values = [plistlib.loads(path.read_bytes()) for path in files]
            self.assertTrue(all(value["RunAtLoad"] for value in values))
            self.assertTrue(all(".tart-runner-" in value["Label"] for value in values))
            self.assertTrue(all("--name" not in value["ProgramArguments"] for value in values))
            self.assertTrue(all(value["EnvironmentVariables"]["TARTCI_GH_CLI"] == "ghapp" for value in values))
            self.assertTrue(all(value["EnvironmentVariables"]["TARTCI_ADMISSION_CLEAN_MODE"] == "required" for value in values))
            self.assertTrue(all(value["EnvironmentVariables"]["TART_HOME"] == "/Users/danielraffel/VMs" for value in values))
            self.assertTrue(all(Path(value["StandardOutPath"]).parent == Path("/Users/danielraffel/Library/Logs/tartci") for value in values))
            repos = {value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"] for value in values}
            self.assertEqual(repos, {"Generous-Corp/pulp", "Generous-Corp/forge", "Generous-Corp/vellum"})
            pulp = next(value for value in values if value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"].endswith("/pulp"))
            self.assertNotIn("pulp-gate-fast", pulp["EnvironmentVariables"]["TARTCI_RUNNER_LABELS"])
            self.assertEqual(pulp["EnvironmentVariables"]["TARTCI_VM_LEASE_PRIORITY"], "vm")
            self.assertTrue(pulp["EnvironmentVariables"]["TARTCI_RUNNER_WORKFLOW_TIERS"].startswith("pulp-build-merge-group|"))
            self.assertNotIn("pulp-build-pr-head", pulp["EnvironmentVariables"]["TARTCI_RUNNER_WORKFLOW_TIERS"])

    def test_invalid_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.toml"
            bad.write_text('schema=1\n[host]\nid="m1"\nhome="/x"\ntart_home="relative"\ncache_root="/c"\nlog_root="/l"\n')
            result = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("absolute path", result.stderr)

    def test_scalar_workflow_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.toml"
            bad.write_text('''schema=1
[host]
id="m1"
home="/h"
tart_home="/v"
cache_root="/c"
log_root="/l"
[[lane]]
id="forge"
repo="Generous-Corp/forge"
golden="g"
labels=["self-hosted","macOS","ARM64"]
workflows="Build"
''')
            result = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("string array", result.stderr)

    def test_scalar_types_fail_closed_without_traceback(self) -> None:
        fixtures = [
            'schema=1\nhost="m1"\n',
            '''schema=1
[host]
id="m1"
home="/h"
tart_home="/v"
cache_root="/c"
log_root="/l"
[[lane]]
id="x"
repo="Generous-Corp/pulp"
golden=true
labels=["self-hosted","macOS","ARM64"]
workflows=["Build"]
''',
            '''schema=1
[host]
id="m1"
home="/h"
tart_home="/v"
cache_root="/c"
log_root="/l"
[[lane]]
id="x"
repo="Generous-Corp/pulp"
golden="g"
min_queued_age_seconds=true
labels=["self-hosted","macOS","ARM64"]
workflows=["Build"]
''',
        ]
        with tempfile.TemporaryDirectory() as td:
            for index, body in enumerate(fixtures):
                bad = Path(td) / f"bad-{index}.toml"
                bad.write_text(body)
                result = subprocess.run(
                    [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("Traceback", result.stderr)

    def test_unknown_keys_and_routing_delimiters_are_rejected(self) -> None:
        base = '''schema=1
[host]
id="m1"
home="/h"
tart_home="/v"
cache_root="/c"
log_root="/l"
[[lane]]
id="x"
repo="Generous-Corp/pulp"
golden="g"
labels=["self-hosted","macOS","ARM64"]
workflows=["Build"]
'''
        fixtures = [
            base + 'min_queue_age_seconds=600\n',
            base.replace('id="x"', 'id=1'),
            base.replace('workflows=["Build"]', 'workflows=["Build\\nOther"]'),
            base.replace('"ARM64"', '"ARM64,extra"'),
            base.replace('workflows=["Build"]', 'priority=[]\nworkflows=["Build"]'),
        ]
        with tempfile.TemporaryDirectory() as td:
            for index, body in enumerate(fixtures):
                bad = Path(td) / f"routing-{index}.toml"
                bad.write_text(body)
                result = subprocess.run(
                    [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 2, body)


if __name__ == "__main__":
    unittest.main()
