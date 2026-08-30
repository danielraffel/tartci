#!/usr/bin/env python3
from __future__ import annotations

import plistlib
import json
import tomllib
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "profiles" / "m1-macos-fleet.toml"
HOST_CONFIGS = {
    "m1": ROOT / "profiles" / "m1-macos-fleet.toml",
    "studio": ROOT / "profiles" / "m3-macos-fleet.toml",
    "m5": ROOT / "profiles" / "m5-macos-fleet.toml",
}
RUNNER_GROUP_IDS = {
    "Generous-Corp/pulp": 3,
    "Generous-Corp/forge": 11,
    "Generous-Corp/vellum": 8,
}


class MacosFleetLaneTests(unittest.TestCase):
    def test_all_host_profiles_validate_with_exact_paths_and_routing(self) -> None:
        expected = {
            "m1": ("/Users/danielraffel/VMs", "vellum-host-m1", False),
            "studio": ("/Volumes/Workshop/VMs", "vellum-host-m3", False),
            "m5": ("/Users/danielraffel/VMs", "vellum-host-m5", False),
        }
        for host_id, config in HOST_CONFIGS.items():
            with self.subTest(host=host_id):
                valid = subprocess.run(
                    [str(ROOT / "tartci"), "fleet-macos", "validate", str(config)],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(valid.returncode, 0, valid.stderr)
                self.assertIn(f"host={host_id} lanes=3", valid.stdout)
                data = tomllib.loads(config.read_text())
                self.assertEqual(data["host"]["id"], host_id)
                self.assertEqual(data["host"]["home"], "/Users/danielraffel")
                self.assertEqual(data["host"]["tart_home"], expected[host_id][0])
                self.assertEqual(
                    next(lane for lane in data["lane"] if lane["id"] == "vellum-gate")["labels"][-1],
                    expected[host_id][1],
                )
                pulp_labels = next(
                    lane for lane in data["lane"] if lane["id"] == "pulp-gate"
                )["labels"]
                pulp_lane = next(
                    lane for lane in data["lane"] if lane["id"] == "pulp-gate"
                )
                self.assertEqual("pulp-gate-fast" in pulp_labels, expected[host_id][2])
                self.assertEqual(
                    [tier["label"] for tier in pulp_lane["tier"]],
                    ["pulp-build-merge-group", "pulp-build-pr-head"],
                )
                self.assertEqual(pulp_lane["assignment_mode"], "event-class-v2")
                self.assertEqual(pulp_lane["assignment_omit_labels"], ["pulp-gate-fast"])
                self.assertEqual(
                    pulp_lane["supervisors"], 2
                )
                self.assertEqual(
                    {lane["repo"]: lane["runner_group_id"] for lane in data["lane"]},
                    RUNNER_GROUP_IDS,
                )
                self.assertEqual(
                    next(lane for lane in data["lane"] if lane["id"] == "forge-gate")["workflows"],
                    ["build", "protected macOS build"],
                )
                self.assertEqual(
                    next(lane for lane in data["lane"] if lane["id"] == "forge-gate")["chrome_app_dir"],
                    "/Applications/Google Chrome.app",
                )
                self.assertEqual(
                    data["stacked_images"],
                    {
                        "enabled": False,
                        "minimum_macos_major": 27,
                        "minimum_tart_version": "2.36.0",
                        "registry_username_file": "/Users/danielraffel/.config/pulp/secrets/ghcr-stackbench-username",
                        "registry_token_file": "/Users/danielraffel/.config/pulp/secrets/ghcr-stackbench-token",
                        "flat_rollback": "pulp-build-runner:latest",
                    },
                )
                with tempfile.TemporaryDirectory() as td:
                    rendered = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "render", str(config),
                         "--output", td],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(rendered.returncode, 0, rendered.stderr)
                    values = [
                        plistlib.loads(path.read_bytes())
                        for path in Path(td).glob("*.plist")
                    ]
                    receipt_dirs = {
                        value["EnvironmentVariables"]["TARTCI_DISK_DENIAL_RECEIPT_DIR"]
                        for value in values
                    }
                    self.assertEqual(
                        receipt_dirs,
                        {f"{data['host']['home']}/.tartci/state/disk-admission"},
                    )
                    self.assertEqual(
                        {
                            value["EnvironmentVariables"]["TARTCI_RECEIPT_HOST_ID"]
                            for value in values
                        },
                        {host_id},
                    )
                    self.assertEqual(
                        {
                            value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]:
                            value["EnvironmentVariables"]["TARTCI_RUNNER_GROUP_ID"]
                            for value in values
                        },
                        {
                            repo: str(group_id)
                            for repo, group_id in RUNNER_GROUP_IDS.items()
                        },
                    )
                    self.assertEqual(
                        {
                            value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]:
                            value["EnvironmentVariables"]["SHIPYARD_GH_APP_REPO"]
                            for value in values
                        },
                        {repo: repo for repo in RUNNER_GROUP_IDS},
                    )
                    self.assertTrue(all(
                        not any(
                            key.startswith("TARTCI_STACKED_")
                            or key.startswith("TARTCI_REGISTRY_")
                            or key == "TARTCI_FLAT_ROLLBACK_GOLDEN"
                            for key in value["EnvironmentVariables"]
                        )
                        for value in values
                    ))
                    chrome_routes = {
                        value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]:
                        value["EnvironmentVariables"].get("TARTCI_RUNNER_CHROME_APP_DIR")
                        for value in values
                    }
                    self.assertEqual(
                        chrome_routes,
                        {
                            "Generous-Corp/pulp": None,
                            "Generous-Corp/forge": "/Applications/Google Chrome.app",
                            "Generous-Corp/vellum": None,
                        },
                    )
                    pulp_plists = [
                        value for value in values
                        if value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]
                        == "Generous-Corp/pulp"
                    ]
                    self.assertEqual(len(pulp_plists), 2)
                    identities = {
                        (
                            value["Label"],
                            value["EnvironmentVariables"]["TARTCI_RUNNER_SLOT"],
                            value["EnvironmentVariables"]["TARTCI_STATE_DIR"],
                            value["EnvironmentVariables"]["TARTCI_EVENT_LOG"],
                            value["EnvironmentVariables"]["TARTCI_MACOS_LOGS"],
                            value["EnvironmentVariables"]["TARTCI_QUEUE_LANE_ID"],
                            value["EnvironmentVariables"]["TARTCI_RUNNER_NAME_PREFIX"],
                        )
                        for value in pulp_plists
                    }
                    self.assertEqual(len(identities), len(pulp_plists))
                    for value in pulp_plists:
                        env = value["EnvironmentVariables"]
                        self.assertEqual(env["TARTCI_RUNNER_ASSIGNMENT_MODE"], "event-class-v2")
                        self.assertEqual(env["TARTCI_ASSIGNMENT_V2_OMIT_LABELS"], "pulp-gate-fast")
                        self.assertEqual(env["TARTCI_ASSIGNMENT_V2_REQUIRED_OMIT_LABELS"], "pulp-gate-fast")
                        self.assertEqual(
                            env["TARTCI_ASSIGNMENT_V2_CLASS_LABELS"],
                            "pulp-build-merge-group,pulp-build-pr-head",
                        )

    def test_m3_and_m5_profiles_retire_the_exact_live_gate_controllers(self) -> None:
        m3 = tomllib.loads(HOST_CONFIGS["studio"].read_text())
        m5 = tomllib.loads(HOST_CONFIGS["m5"].read_text())
        self.assertEqual(
            [
                "com.danielraffel.pulp.tart-runner",
                "com.danielraffel.pulp.tart-runner-slot2",
            ],
            next(lane for lane in m3["lane"] if lane["id"] == "pulp-gate")
            ["replaces_launchd_labels"],
        )
        self.assertEqual(
            [
                "com.danielraffel.pulp.tart-runner-macos-gate",
                "com.danielraffel.pulp.tart-runner-macos-gate-slot2",
            ],
            next(lane for lane in m5["lane"] if lane["id"] == "pulp-gate")
            ["replaces_launchd_labels"],
        )

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
            self.assertEqual(len(files), 4)
            values = [plistlib.loads(path.read_bytes()) for path in files]
            self.assertTrue(all(value["RunAtLoad"] for value in values))
            self.assertTrue(all(".tart-runner-" in value["Label"] for value in values))
            self.assertTrue(all("--name" not in value["ProgramArguments"] for value in values))
            self.assertTrue(all(value["EnvironmentVariables"]["TARTCI_GH_CLI"] == "ghapp" for value in values))
            self.assertTrue(all(
                value["EnvironmentVariables"]["SHIPYARD_GH_APP_REPO"]
                == value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]
                for value in values
            ))
            self.assertTrue(all(value["EnvironmentVariables"]["TARTCI_ADMISSION_CLEAN_MODE"] == "required" for value in values))
            self.assertTrue(all(value["EnvironmentVariables"]["TART_HOME"] == "/Users/danielraffel/VMs" for value in values))
            self.assertTrue(all(Path(value["StandardOutPath"]).parent == Path("/Users/danielraffel/Library/Logs/tartci") for value in values))
            repos = {value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"] for value in values}
            self.assertEqual(repos, {"Generous-Corp/pulp", "Generous-Corp/forge", "Generous-Corp/vellum"})
            self.assertEqual(
                {
                    value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]:
                    value["EnvironmentVariables"]["TARTCI_RUNNER_GROUP_ID"]
                    for value in values
                },
                {repo: str(group_id) for repo, group_id in RUNNER_GROUP_IDS.items()},
            )
            pulp = next(value for value in values if value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"].endswith("/pulp"))
            self.assertNotIn("pulp-gate-fast", pulp["EnvironmentVariables"]["TARTCI_RUNNER_LABELS"])
            self.assertEqual(pulp["EnvironmentVariables"]["TARTCI_VM_LEASE_PRIORITY"], "vm")
            self.assertTrue(pulp["EnvironmentVariables"]["TARTCI_RUNNER_WORKFLOW_TIERS"].startswith("pulp-build-merge-group|"))
            self.assertIn("pulp-build-pr-head", pulp["EnvironmentVariables"]["TARTCI_RUNNER_WORKFLOW_TIERS"])
            self.assertEqual(pulp["EnvironmentVariables"]["TARTCI_RUNNER_ASSIGNMENT_MODE"], "event-class-v2")
            self.assertEqual(
                ["com.danielraffel.pulp.tart-runner-macos-gate"],
                next(lane for lane in tomllib.loads(CONFIG.read_text())["lane"] if lane["id"] == "pulp-gate")["replaces_launchd_labels"],
            )
            self.assertEqual(
                ["com.danielraffel.forge.tart-runner-macos"],
                next(lane for lane in tomllib.loads(CONFIG.read_text())["lane"] if lane["id"] == "forge-gate")["replaces_launchd_labels"],
            )
            forge = next(value for value in values if value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"].endswith("/forge"))
            self.assertEqual(
                forge["EnvironmentVariables"]["TARTCI_RUNNER_CHROME_APP_DIR"],
                "/Applications/Google Chrome.app",
            )
            self.assertNotIn("TARTCI_JIT_GH_CLI", forge["EnvironmentVariables"])
            self.assertNotIn("TARTCI_JIT_GH_CLI", pulp["EnvironmentVariables"])
            self.assertEqual(
                ["com.danielraffel.vellum.tart-runner-macos"],
                next(lane for lane in tomllib.loads(CONFIG.read_text())["lane"] if lane["id"] == "vellum-gate")["replaces_launchd_labels"],
            )

    def test_receipt_verifies_exact_plists_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            render = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "render", str(CONFIG), "--output", str(agents)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(render.returncode, 0, render.stderr)
            receipt = root / "receipt.json"
            write = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "write-receipt", str(CONFIG),
                 "--agents-dir", str(agents), "--output", str(receipt)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(write.returncode, 0, write.stderr)
            self.assertEqual(json.loads(receipt.read_text())["schema"], 1)
            verify = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(CONFIG), "--agents-dir", str(agents)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)
            stale = agents / "com.danielraffel.tartci.tart-runner-macos-fleet.m1.removed.plist"
            stale.write_bytes(next(agents.glob("*.plist")).read_bytes())
            extra = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(CONFIG), "--agents-dir", str(agents)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(extra.returncode, 2)
            self.assertIn("does not exactly match", extra.stderr)
            stale.unlink()
            target = next(agents.glob("*.plist"))
            target.write_bytes(target.read_bytes() + b"\n")
            rejected = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(CONFIG), "--agents-dir", str(agents)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("failed receipt verification", rejected.stderr)

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

    def test_supervisor_count_wrong_type_fails_without_traceback(self) -> None:
        body = CONFIG.read_text().replace("supervisors = 2", 'supervisors = "2"', 1)
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad-supervisors.toml"
            bad.write_text(body)
            result = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                text=True, capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("supervisors must be 1 or 2", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_chrome_mount_is_forge_only_and_path_normalized(self) -> None:
        base = CONFIG.read_text()
        fixtures = {
            "relative": base.replace(
                'chrome_app_dir = "/Applications/Google Chrome.app"',
                'chrome_app_dir = "Google Chrome.app"',
                1,
            ),
            "wrong-app": base.replace(
                'chrome_app_dir = "/Applications/Google Chrome.app"',
                'chrome_app_dir = "/Applications/Chromium.app"',
                1,
            ),
            "non-forge": base.replace(
                'golden = "pulp-build-runner:latest"\npriority = "vm"',
                'golden = "pulp-build-runner:latest"\nchrome_app_dir = "/Applications/Google Chrome.app"\npriority = "vm"',
                1,
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            for name, body in fixtures.items():
                with self.subTest(name=name):
                    path = Path(td) / f"{name}.toml"
                    path.write_text(body)
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("chrome_app_dir", result.stderr)

    def test_stacked_images_cannot_activate_before_graduation(self) -> None:
        body = CONFIG.read_text().replace("enabled = false", "enabled = true", 1)
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "premature-stacked.toml"
            bad.write_text(body)
            result = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("before provider support and benchmark graduation", result.stderr)

    def test_stacked_image_secret_paths_reject_noncanonical_traversal(self) -> None:
        token = "/Users/danielraffel/.config/pulp/secrets/ghcr-stackbench-token"
        fixtures = {
            "terminal-parent": token.replace("ghcr-stackbench-token", ".."),
            "terminal-current": token.replace("ghcr-stackbench-token", "."),
            "nested-parent": token.replace("ghcr-stackbench-token", "nested/../token"),
        }
        with tempfile.TemporaryDirectory() as td:
            for name, path in fixtures.items():
                with self.subTest(name=name):
                    bad = Path(td) / f"stacked-secret-{name}.toml"
                    bad.write_text(CONFIG.read_text().replace(token, path, 1))
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn("absolute host-local Pulp secret path", result.stderr)

    def test_stacked_image_secret_paths_must_be_distinct(self) -> None:
        username = "/Users/danielraffel/.config/pulp/secrets/ghcr-stackbench-username"
        token = "/Users/danielraffel/.config/pulp/secrets/ghcr-stackbench-token"
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "stacked-secret-identical.toml"
            bad.write_text(CONFIG.read_text().replace(token, username, 1))
            result = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("username and token paths must be distinct", result.stderr)

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
runner_group_id=11
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

    def test_protected_runner_group_id_is_required_and_non_default(self) -> None:
        base = '''schema=1
[host]
id="m1"
home="/h"
tart_home="/v"
cache_root="/c"
log_root="/l"
[[lane]]
id="forge"
repo="Generous-Corp/forge"
runner_group_id=GROUP
golden="g"
labels=["self-hosted","macOS","ARM64"]
workflows=["Build"]
'''
        fixtures = {
            "missing": base.replace("runner_group_id=GROUP\n", ""),
            "default": base.replace("GROUP", "1"),
            "zero": base.replace("GROUP", "0"),
            "negative": base.replace("GROUP", "-1"),
            "boolean": base.replace("GROUP", "true"),
            "string": base.replace("GROUP", '"11"'),
        }
        with tempfile.TemporaryDirectory() as td:
            for name, body in fixtures.items():
                with self.subTest(name=name):
                    bad = Path(td) / f"runner-group-{name}.toml"
                    bad.write_text(body)
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn("runner_group_id", result.stderr)

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
runner_group_id=3
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
runner_group_id=3
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
runner_group_id=3
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

    def test_replacement_cannot_name_a_rendered_fleet_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "self-replacing.toml"
            bad.write_text('''schema=1
name="self-replacing"
[host]
id="m1"
home="/h"
tart_home="/v"
cache_root="/c"
log_root="/l"
[[lane]]
id="forge"
repo="Generous-Corp/forge"
runner_group_id=11
golden="g"
labels=["self-hosted","macOS","ARM64"]
workflows=["Build"]
replaces_launchd_labels=["com.danielraffel.tartci.tart-runner-macos-fleet.m1.forge"]
''')
            result = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("may not name rendered", result.stderr)

    def test_only_the_exact_legacy_pulp_base_label_is_allowed_without_a_suffix(self) -> None:
        base = '''schema=1
name="replacement-check"
[host]
id="m1"
home="/h"
tart_home="/v"
cache_root="/c"
log_root="/l"
[[lane]]
id="forge"
repo="Generous-Corp/forge"
runner_group_id=11
golden="g"
labels=["self-hosted","macOS","ARM64"]
workflows=["Build"]
replaces_launchd_labels=["REPLACEMENT"]
'''
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            allowed = root / "allowed.toml"
            allowed.write_text(base.replace("REPLACEMENT", "com.danielraffel.pulp.tart-runner"))
            accepted = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(allowed)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            rejected = root / "rejected.toml"
            rejected.write_text(base.replace("REPLACEMENT", "com.danielraffel.forge.tart-runner"))
            denied = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(rejected)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(denied.returncode, 2)
            self.assertIn("replaces_launchd_labels", denied.stderr)

    def test_supplied_replacement_labels_must_be_an_array_even_when_falsy(self) -> None:
        base = '''schema=1
name="replacement-type-check"
[host]
id="m1"
home="/h"
tart_home="/v"
cache_root="/c"
log_root="/l"
[[lane]]
id="forge"
repo="Generous-Corp/forge"
runner_group_id=11
golden="g"
labels=["self-hosted","macOS","ARM64"]
workflows=["Build"]
replaces_launchd_labels=REPLACEMENT
'''
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, replacement in (("empty-string", '""'), ("empty-table", "{}")):
                with self.subTest(name=name):
                    profile = root / f"{name}.toml"
                    profile.write_text(base.replace("REPLACEMENT", replacement))
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(profile)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("replaces_launchd_labels", result.stderr)


if __name__ == "__main__":
    unittest.main()
