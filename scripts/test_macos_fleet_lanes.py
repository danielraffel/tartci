#!/usr/bin/env python3
from __future__ import annotations

import plistlib
import json
import datetime as dt
import os
import re
import shutil
import tomllib
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import tartci_support_manifest as support_manifest
import network_profile as network
import macos_fleet_lanes as fleet
import macos_launcher_probe


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "profiles" / "m1-macos-fleet.toml"
HOST_CONFIGS = {
    "m1": ROOT / "profiles" / "m1-macos-fleet.toml",
    "studio": ROOT / "profiles" / "m3-macos-fleet.toml",
    "m5": ROOT / "profiles" / "m5-macos-fleet.toml",
}
RUNNER_GROUP_IDS = {
    "Generous-Corp/pulp": 1,
    "danielraffel/spectr": 1,
    "Generous-Corp/forge": 11,
    "Generous-Corp/vellum": 8,
}


def write_support_manifest(root: Path, path: Path) -> None:
    path.write_text(json.dumps({
        "schema": 2,
        "repository": "https://github.com/danielraffel/tartci.git",
        "source_commit": "a" * 40,
        "members": [
            support_manifest.member(root, name)
            for name in sorted(support_manifest.filesystem_names(root))
        ],
    }))


def freeze_support_cohort(root: Path, manifest: Path) -> None:
    for name in support_manifest.filesystem_names(root):
        target = root / name
        target.chmod((target.stat().st_mode & 0o777) & ~0o222)
    manifest.chmod(0o444)
    for directory in [root, *root.rglob("*")]:
        if directory.is_dir() and not directory.is_symlink():
            directory.chmod(0o555)


def thaw_support_cohort(root: Path) -> None:
    for directory in [root, *root.rglob("*")]:
        if directory.is_dir() and not directory.is_symlink():
            directory.chmod(0o755)


def copy_support_cohort(root: Path) -> None:
    for name in sorted(support_manifest.filesystem_names(ROOT)):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)


class MacosFleetLaneTests(unittest.TestCase):
    def test_m3_worktree_cleanup_contract_is_dormant_and_rendered(self) -> None:
        data = fleet.load(HOST_CONFIGS["studio"])
        self.assertFalse(data["worktree_cleanup"]["apply"])
        for body in fleet.rendered_plists(data).values():
            env = plistlib.loads(body)["EnvironmentVariables"]
            cleanup_keys = {key for key in env if key.startswith("TARTCI_WORKTREE_CLEANUP_")}
            self.assertEqual(cleanup_keys, set())
        for profile in (HOST_CONFIGS["m1"], HOST_CONFIGS["m5"]):
            for body in fleet.rendered_plists(fleet.load(profile)).values():
                env = plistlib.loads(body)["EnvironmentVariables"]
                self.assertFalse(any(key.startswith("TARTCI_WORKTREE_CLEANUP_") for key in env))

    def test_enabled_cleanup_authority_renders_only_m3_pulp_slots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"m3.toml"; body=HOST_CONFIGS["studio"].read_text().replace("apply = false","apply = true")
            path.write_text(body); rendered=fleet.rendered_plists(fleet.load(path)); pulp_count=0
            for plist_body in rendered.values():
                env=plistlib.loads(plist_body)["EnvironmentVariables"]; keys={key for key in env if key.startswith("TARTCI_WORKTREE_CLEANUP_")}
                if env["TARTCI_QUEUE_LANE_ID"] in {"studio-pulp-gate","studio-pulp-gate-slot2"}:
                    pulp_count+=1; self.assertEqual(env["TARTCI_RUNNER_REPO"],"Generous-Corp/pulp"); self.assertIn("TARTCI_WORKTREE_CLEANUP_PROVIDER",keys)
                else: self.assertEqual(keys,set())
            self.assertEqual(pulp_count,2)

    def test_worktree_cleanup_rejected_outside_exact_m3_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.toml"
            path.write_text(HOST_CONFIGS["studio"].read_text().replace(
                'provider = "merged-main-v1"', 'provider = "other"'
            ))
            with self.assertRaisesRegex(ValueError, "reviewed M3"):
                fleet.load(path)

    def test_external_volume_profile_uses_stable_signed_resident_launcher(self) -> None:
        data = fleet.load(HOST_CONFIGS["studio"])
        rendered = fleet.rendered_plists(data)
        for body in rendered.values():
            value = plistlib.loads(body)
            arguments = value["ProgramArguments"]
            self.assertEqual(
                arguments,
                [
                    "/Users/danielraffel/.local/libexec/TartCILauncher.app/Contents/MacOS/tartci-launcher",
                    "--lane",
                    value["EnvironmentVariables"]["TARTCI_QUEUE_LANE_ID"],
                ],
            )

    def test_external_volume_profile_fails_closed_without_launch_helper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m3.toml"
            body = HOST_CONFIGS["studio"].read_text()
            start = body.index("[launch_helper]\n")
            end = body.index("[stacked_images]\n", start)
            path.write_text(body[:start] + body[end:])
            with self.assertRaisesRegex(ValueError, "external-volume"):
                fleet.load(path)

    def test_private_launcher_rejects_another_external_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m3.toml"
            path.write_text(HOST_CONFIGS["studio"].read_text().replace(
                "/Volumes/Workshop/VMs", "/Volumes/Another/VMs"
            ))
            with self.assertRaisesRegex(ValueError, "private M3"):
                fleet.load(path)

    def test_launchd_context_probe_is_bounded_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            config = root / "m3.toml"
            config.write_text(
                HOST_CONFIGS["studio"].read_text()
                .replace("/Users/danielraffel", str(home))
            )
            helper = {
                "path": str(home / ".local/libexec/tartci-launcher"),
                "sha256": "a" * 64,
                "designated_requirement_sha256": "b" * 64,
            }
            missing = subprocess.CompletedProcess(
                [], 113, "", "Could not find service\n"
            )
            ok = subprocess.CompletedProcess([], 0, "", "")
            terminal = subprocess.CompletedProcess(
                [], 0, "state = exited\nlast exit code = 0\n", ""
            )
            with mock.patch.object(fleet, "verify_receipt", return_value={
                "launch_helper": helper,
            }), mock.patch.object(
                macos_launcher_probe.subprocess, "run",
                side_effect=[missing, ok, terminal, ok],
            ) as run:
                result = fleet.probe_launch_helper(
                    Path("receipt"), config, root / "agents", root / "support"
                )
            self.assertTrue(result["passed"])
            self.assertEqual(result["path"], "/Volumes/Workshop/VMs")
            self.assertEqual(run.call_args_list[-1].args[0][1], "bootout")

            cleanup_failed = subprocess.CompletedProcess(
                [], 5, "", "bootout failed\n"
            )
            with mock.patch.object(fleet, "verify_receipt", return_value={
                "launch_helper": helper,
            }), mock.patch.object(
                macos_launcher_probe.subprocess, "run",
                side_effect=[missing, ok, terminal, cleanup_failed],
            ):
                with self.assertRaisesRegex(ValueError, "could not remove"):
                    fleet.probe_launch_helper(
                        Path("receipt"), config, root / "agents", root / "support"
                    )

            probe_failed = subprocess.CompletedProcess(
                [], 0, "state = exited\nlast exit code = 74\n", ""
            )
            with mock.patch.object(fleet, "verify_receipt", return_value={
                "launch_helper": helper,
            }), mock.patch.object(
                macos_launcher_probe.subprocess, "run",
                side_effect=[missing, ok, probe_failed, cleanup_failed],
            ):
                with self.assertRaisesRegex(
                    ValueError, "exited 74; .*could not remove"
                ):
                    fleet.probe_launch_helper(
                        Path("receipt"), config, root / "agents", root / "support"
                    )

    def test_exit_timeout_accepts_live_macos26_and_macos27_renderings(self) -> None:
        self.assertTrue(fleet._loaded_exit_timeout_matches(
            "\texit timeout = 30 seconds\n", 30
        ))
        self.assertTrue(fleet._loaded_exit_timeout_matches(
            "\texit timeout = 30\n", 30
        ))
        self.assertFalse(fleet._loaded_exit_timeout_matches(
            "\texit timeout = 31\n", 30
        ))

    def test_persistent_plist_records_require_safe_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            label = "actions.runner.owner.repo.preamble"
            path = root / f"{label}.plist"
            path.write_bytes(plistlib.dumps({"Label": label}))
            path.chmod(0o644)
            data = {"host": {"persistent_runner_labels": [label]}}
            self.assertEqual(
                0o644,
                fleet.persistent_plist_records(data, root)[path.name]["mode"],
            )
            path.chmod(0o600)
            self.assertEqual(
                0o600,
                fleet.persistent_plist_records(data, root)[path.name]["mode"],
            )
            path.chmod(0o666)
            with self.assertRaisesRegex(ValueError, "group/world-writable"):
                fleet.persistent_plist_records(data, root)
            path.chmod(0o644)
            with mock.patch.object(fleet.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaisesRegex(ValueError, "owned by the caller"):
                    fleet.persistent_plist_records(data, root)
            path.unlink()
            target = root / "target.plist"
            target.write_bytes(plistlib.dumps({"Label": label}))
            path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "unavailable"):
                fleet.persistent_plist_records(data, root)

    def test_persistent_loaded_verifier_binds_keepalive_and_exact_environment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td)
            name = "actions.runner.owner.repo.preamble.plist"
            label = name.removesuffix(".plist")
            value = {
                "Label": label,
                "ProgramArguments": ["/usr/bin/true", "--serve"],
                "WorkingDirectory": "/tmp/work",
                "StandardOutPath": "/tmp/out",
                "StandardErrorPath": "/tmp/err",
                "EnvironmentVariables": {"RUNNER_ALLOW_RUNASROOT": "0"},
                "ProcessType": "Interactive",
                "SessionCreate": True,
                "RunAtLoad": True,
            }
            payload = plistlib.dumps(value)
            output = (
                "\tstate = running\n"
                "\tpid = 4242\n"
                f"\tpath = {agents / name}\n"
                "\tprogram = /usr/bin/true\n"
                "\targuments = {\n\t\t/usr/bin/true\n\t\t--serve\n\t}\n"
                "\tworking directory = /tmp/work\n"
                "\tstdout path = /tmp/out\n"
                "\tstderr path = /tmp/err\n"
                "\texit timeout = 5 seconds\n"
                "\tenvironment = {\n"
                "\t\tRUNNER_ALLOW_RUNASROOT => 0\n"
                "\t\tOSLogRateLimit => 64\n"
                f"\t\tXPC_SERVICE_NAME => {label}\n"
                "\t}\n"
                "\tspawn type = interactive (4)\n"
                "\tproperties = runatload | creates session | inferred program\n"
            )
            fleet._verify_persistent_loaded_output(name, payload, output, agents)
            with self.assertRaisesRegex(ValueError, "is not running"):
                fleet._verify_persistent_loaded_output(
                    name,
                    payload,
                    output.replace("state = running", "state = exited"),
                    agents,
                )
            with self.assertRaisesRegex(ValueError, "no numeric pid"):
                fleet._verify_persistent_loaded_output(
                    name,
                    payload,
                    output.replace("\tpid = 4242\n", ""),
                    agents,
                )
            with self.assertRaisesRegex(ValueError, "keepalive does not match"):
                fleet._verify_persistent_loaded_output(
                    name,
                    payload,
                    output.replace(
                        "properties = runatload",
                        "properties = keepalive | runatload",
                    ),
                    agents,
                )
            with self.assertRaisesRegex(ValueError, "session creation does not match"):
                fleet._verify_persistent_loaded_output(
                    name,
                    payload,
                    output.replace(" | creates session", ""),
                    agents,
                )
            with self.assertRaisesRegex(ValueError, "process type does not match"):
                fleet._verify_persistent_loaded_output(
                    name,
                    payload,
                    output.replace("spawn type = interactive (4)", "spawn type = background (3)"),
                    agents,
                )
            with self.assertRaisesRegex(ValueError, "exit timeout does not match"):
                fleet._verify_persistent_loaded_output(
                    name,
                    payload,
                    output.replace("exit timeout = 5", "exit timeout = 6"),
                    agents,
                )
            with self.assertRaisesRegex(ValueError, "environment does not match"):
                fleet._verify_persistent_loaded_output(
                    name,
                    payload,
                    output.replace(
                        "\t}\n\tspawn type",
                        "\t\tTARTCI_UNRECEIPTED => 1\n\t}\n\tspawn type",
                    ),
                    agents,
                )

    def test_readiness_requires_receipted_persistent_runner_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            dynamic = "dynamic"
            persistent = "actions.runner.owner.repo.preamble"
            state_dir = root / "state"
            state_dir.mkdir()
            start = "Mon Sep  1 00:00:00 2026"
            (state_dir / "dynamic.state.json").write_text(json.dumps({
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "supervisor_pid": "101",
                "supervisor_pid_started_at": start,
            }))
            (agents / f"{dynamic}.plist").write_bytes(plistlib.dumps({
                "EnvironmentVariables": {
                    "HOME": str(root), "TARTCI_STATE_DIR": str(state_dir),
                },
            }))
            (agents / f"{persistent}.plist").write_text("persistent")
            receipt = {
                "plists": {f"{dynamic}.plist": "a"},
                "persistent_plists": {f"{persistent}.plist": {}},
                "retired_launchd_labels": [],
            }
            running = subprocess.CompletedProcess(
                [], 0, "state = running\npid = 101\n", ""
            )
            missing = subprocess.CompletedProcess(
                [], 113, "", "Could not find service\n"
            )
            process_table = subprocess.CompletedProcess(
                [], 0,
                f"101 1 {start} bash {root}/.local/share/tartci-generations/current/providers/tart-macos/runner.sh --loop\n",
                "",
            )
            domain = subprocess.CompletedProcess([], 0, "", "")
            args = (Path("receipt"), Path("config"), agents, Path("support"))
            with mock.patch.object(fleet, "verify_receipt", return_value=receipt), \
                 mock.patch.object(
                     fleet.subprocess, "run",
                     side_effect=[running, missing, process_table, domain],
                 ), \
                 mock.patch.object(fleet, "verify_loaded_snapshot", return_value={}):
                value = fleet.fleet_readiness(
                    *args, participating=True, pool_state="on"
                )
            self.assertFalse(value["fleet_ready"])
            self.assertIn(
                "unloaded_service", {item["code"] for item in value["problems"]}
            )

    def test_receipt_backed_readiness_separates_intent_from_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            receipt = {"plists": {"one.plist": "a", "two.plist": "b"},
                       "retired_launchd_labels": []}
            heartbeat_ts = dt.datetime.now(dt.timezone.utc).isoformat()
            starts = {
                101: "Mon Sep  1 00:00:00 2026",
                102: "Mon Sep  1 00:00:01 2026",
            }
            for index, label in enumerate(("one", "two"), start=101):
                state_dir = root / f"{label}-state"
                state_dir.mkdir()
                (state_dir / f"{label}.state.json").write_text(json.dumps({
                    "ts": heartbeat_ts,
                    "supervisor_pid": str(index),
                    "supervisor_pid_started_at": starts[index],
                }))
                (agents / f"{label}.plist").write_bytes(plistlib.dumps({
                    "EnvironmentVariables": {
                        "HOME": str(root), "TARTCI_STATE_DIR": str(state_dir),
                    },
                }))
            running_one = subprocess.CompletedProcess(
                [], 0, "state = running\npid = 101\n", ""
            )
            running_two = subprocess.CompletedProcess(
                [], 0, "state = running\npid = 102\n", ""
            )
            missing = subprocess.CompletedProcess(
                [], 113, "", "Could not find service\n"
            )
            domain = subprocess.CompletedProcess([], 0, "", "")
            unmanaged_list = subprocess.CompletedProcess(
                [], 0, "PID\tStatus\tLabel\n-\t0\tcom.example.idle\n", ""
            )
            process_table = subprocess.CompletedProcess(
                [], 0,
                f"101 1 {starts[101]} bash {root}/.local/share/tartci-generations/current/providers/tart-macos/runner.sh --loop\n"
                f"102 1 {starts[102]} bash {root}/.local/share/tartci-generations/current/providers/tart-macos/runner.sh --loop\n"
                f"555 101 {starts[101]} bash {root}/.local/share/tartci-generations/current/providers/tart-macos/runner.sh --loop\n",
                "",
            )
            args = (Path("receipt"), Path("config"), agents, Path("support"))
            with mock.patch.object(fleet, "verify_receipt", return_value=receipt), \
                 mock.patch.object(
                     fleet.subprocess, "run",
                     side_effect=[running_one, missing, process_table, unmanaged_list, domain],
                 ):
                value = fleet.fleet_readiness(*args, participating=True, pool_state="on")
            self.assertFalse(value["fleet_ready"])
            self.assertEqual(value["verified_running_supervisors"], 0)
            self.assertEqual(value["expected_supervisors"], 2)
            self.assertEqual(
                {problem["code"] for problem in value["problems"]},
                {"unloaded_service", "orphaned_supervisor"},
            )

            with mock.patch.object(fleet, "verify_receipt", return_value=receipt), \
                 mock.patch.object(
                     fleet.subprocess, "run",
                     side_effect=[running_one, running_two, process_table, domain],
                 ), \
                 mock.patch.object(fleet, "verify_loaded_snapshot", return_value={}):
                value = fleet.fleet_readiness(*args, participating=True, pool_state="on")
            self.assertTrue(value["fleet_ready"])
            self.assertEqual(value["verified_running_supervisors"], 2)
            # The full readiness path reports config verdicts beside, not in,
            # problems; with no installed profile they are not applicable.
            self.assertEqual(value["config"]["profile_drift"]["state"], "not_applicable")
            self.assertEqual(value["config"]["supply"]["state"], "not_applicable")

            orphan_table = subprocess.CompletedProcess(
                [], 0,
                process_table.stdout
                + f"999 1 Mon Sep  1 00:00:02 2026 bash {root}/.local/share/tartci-generations/old/providers/tart-macos/runner.sh --loop\n",
                "",
            )
            with mock.patch.object(fleet, "verify_receipt", return_value=receipt), \
                 mock.patch.object(
                     fleet.subprocess, "run",
                     side_effect=[running_one, running_two, orphan_table, unmanaged_list, domain],
                 ), \
                 mock.patch.object(fleet, "verify_loaded_snapshot", return_value={}):
                value = fleet.fleet_readiness(*args, participating=True, pool_state="on")
            self.assertFalse(value["fleet_ready"])
            self.assertIn("orphaned_supervisor", {
                problem["code"] for problem in value["problems"]
            })
            unexpected_domain = subprocess.CompletedProcess(
                [], 0,
                "34889 143 com.danielraffel.tartci.tart-runner-macos-fleet.m3.extra\n",
                "",
            )
            with mock.patch.object(fleet, "verify_receipt", return_value=receipt), \
                 mock.patch.object(
                     fleet.subprocess, "run",
                     side_effect=[running_one, running_two, process_table, unexpected_domain],
                 ), \
                 mock.patch.object(fleet, "verify_loaded_snapshot", return_value={}):
                value = fleet.fleet_readiness(*args, participating=True, pool_state="on")
            self.assertIn("unexpected_managed_service", {
                problem["code"] for problem in value["problems"]
            })

            with mock.patch.object(fleet, "verify_receipt", return_value=receipt), \
                 mock.patch.object(
                     fleet.subprocess, "run",
                     side_effect=[
                         missing, missing,
                         subprocess.CompletedProcess([], 0, "", ""), domain,
                     ],
                 ):
                value = fleet.fleet_readiness(*args, participating=False, pool_state="off")
            self.assertFalse(value["fleet_ready"])
            self.assertEqual(value["problems"], [])

            with mock.patch.object(fleet, "verify_receipt", side_effect=ValueError("tampered")):
                value = fleet.fleet_readiness(*args, participating=True, pool_state="on")
            # None, never 0: an unverifiable receipt means the supervisors were
            # not checked, which must not be reported as having checked them and
            # found none running. A healthy fleet observed from the wrong root
            # would otherwise read as a dead one.
            self.assertIsNone(value["verified_running_supervisors"])
            self.assertEqual(value["problems"][0]["code"], "receipt_mismatch")

    def test_heartbeat_identity_spans_wrapper_and_direct_topologies(self) -> None:
        """A lane on an external volume runs through a signed launch_helper, so
        launchd's job pid is the helper and the heartbeat is written by its
        child. A lane on the internal disk writes from the job pid itself. Both
        are healthy, so identity is ancestry rather than pid equality -- but it
        stays scoped to the lane, and the recorded start time is still compared
        against the writer, which is what guards against pid reuse.
        """
        wrapper_start = "Mon Sep  1 00:00:00 2026"
        direct_start = "Mon Sep  1 00:00:01 2026"
        child_start = "Mon Sep  1 00:00:02 2026"
        heartbeat_ts = dt.datetime.now(dt.timezone.utc).isoformat()

        def scenario(one_writer: int, one_writer_start: str,
                     release_managed: bool | None = None):
            td = tempfile.TemporaryDirectory()
            root = Path(td.name)
            agents = root / "agents"
            agents.mkdir()
            receipt = {"plists": {"one.plist": "a", "two.plist": "b"},
                       "retired_launchd_labels": []}
            writers = {"one": (one_writer, one_writer_start),
                       "two": (102, direct_start)}
            for label, (writer, started) in writers.items():
                state_dir = root / f"{label}-state"
                state_dir.mkdir()
                (state_dir / f"{label}.state.json").write_text(json.dumps({
                    "ts": heartbeat_ts,
                    "supervisor_pid": str(writer),
                    "supervisor_pid_started_at": started,
                }))
                (agents / f"{label}.plist").write_bytes(plistlib.dumps({
                    "EnvironmentVariables": {
                        "HOME": str(root), "TARTCI_STATE_DIR": str(state_dir),
                    },
                }))
            gen = f"{root}/.local/share/tartci-generations/current"
            table = (
                f"101 1 {wrapper_start} bash {gen}/providers/tart-macos/runner.sh --loop\n"
                f"102 1 {direct_start} bash {gen}/providers/tart-macos/runner.sh --loop\n"
                f"201 101 {child_start} bash {gen}/providers/tart-macos/runner.sh --loop\n"
                f"202 102 {child_start} bash {gen}/providers/tart-macos/runner.sh --loop\n"
            )
            if release_managed is not None:
                # Same generations path and ppid 1 as a real release-lane
                # supervisor, rooted at THIS scenario's home so the orphan
                # predicate actually sees it.
                table += (
                    f"999 1 Mon Sep  1 00:00:03 2026 bash "
                    f"{gen}/providers/tart-macos/runner.sh --loop\n"
                )
            calls = [
                subprocess.CompletedProcess([], 0, "state = running\npid = 101\n", ""),
                subprocess.CompletedProcess([], 0, "state = running\npid = 102\n", ""),
                subprocess.CompletedProcess([], 0, table, ""),
            ]
            if release_managed is not None:
                listing = "PID\tStatus\tLabel\n"
                if release_managed:
                    listing += "999\t0\tcom.danielraffel.pulp.tart-runner-macos-release\n"
                calls.append(subprocess.CompletedProcess([], 0, listing, ""))
            calls.append(subprocess.CompletedProcess([], 0, "", ""))
            args = (Path("receipt"), Path("config"), agents, Path("support"))
            with mock.patch.object(fleet, "verify_receipt", return_value=receipt), \
                 mock.patch.object(fleet.subprocess, "run", side_effect=calls), \
                 mock.patch.object(fleet, "verify_loaded_snapshot", return_value={}):
                value = fleet.fleet_readiness(*args, participating=True, pool_state="on")
            td.cleanup()
            return value

        # 201 is a child of lane one's job pid 101; lane two writes from 102 itself.
        value = scenario(201, child_start)
        self.assertTrue(value["fleet_ready"])
        self.assertEqual(value["verified_running_supervisors"], 2)
        self.assertEqual(value["problems"], [])

        # NEGATIVE CONTROL: 202 is live and on the generations path, but it
        # descends from the OTHER lane's job. One lane must not be verified by
        # its neighbour's writer.
        value = scenario(202, child_start)
        self.assertFalse(value["fleet_ready"])
        self.assertIn("heartbeat_missing", {
            problem["code"] for problem in value["problems"]
        })

        # NEGATIVE CONTROL: the writer pid is a correct descendant, but the
        # recorded start time is not that writer's. This is the pid-reuse guard,
        # and it must compare against the writer rather than the job.
        value = scenario(201, "Mon Sep  1 09:09:09 2026")
        self.assertFalse(value["fleet_ready"])
        self.assertIn("heartbeat_missing", {
            problem["code"] for problem in value["problems"]
        })

        # POSITIVE CONTROL for the assertion below: with launchd reporting
        # nothing, the same process IS reported as an orphan. Without this the
        # next assertion could pass by never seeing the process at all.
        value = scenario(201, child_start, release_managed=False)
        self.assertIn("orphaned_supervisor", {
            problem["code"] for problem in value["problems"]
        })

        # A managed supervisor outside the fleet lanes -- the release lane lives
        # on the same host -- is not an orphan, whatever its ppid.
        value = scenario(201, child_start, release_managed=True)
        self.assertNotIn("orphaned_supervisor", {
            problem["code"] for problem in value["problems"]
        })
        self.assertTrue(value["fleet_ready"])

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
                self.assertIn(
                    f"host={host_id} lanes={5 if host_id == 'm5' else 4}",
                    valid.stdout,
                )
                data = tomllib.loads(config.read_text())
                self.assertEqual(data["host"]["id"], host_id)
                self.assertEqual(data["host"]["home"], "/Users/danielraffel")
                self.assertEqual(data["host"]["tart_home"], expected[host_id][0])
                self.assertEqual(
                    data["host"].get("github_api_timeout_seconds"),
                    30,
                    f"{host_id}: every fleet host pins the GitHub API timeout at 30s. "
                    "The 15s default was the dominant assignment-scan failure on the "
                    "hosts that had not pinned it (m3 88%, m5 81%, measured 2026-09-21).",
                )
                self.assertEqual(
                    data["host"].get("persistent_runner_labels", []),
                    [],
                    f"{host_id}: no host keeps a persistent runner; the preamble lane "
                    "runs GitHub-hosted.",
                )
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
                self.assertEqual(
                    [tier["runner_group_id"] for tier in pulp_lane["tier"]],
                    [1, 1],
                )
                self.assertEqual(pulp_lane["assignment_mode"], "event-class-v2")
                self.assertEqual(
                    pulp_lane.get("assignment_top_tier_receipt_max_age_seconds"),
                    180 if host_id == "m1" else None,
                )
                self.assertEqual(pulp_lane["registration_scope"], "repository")
                self.assertEqual(pulp_lane["assignment_omit_labels"], ["pulp-gate-fast"])
                self.assertNotIn("priority", pulp_lane)
                self.assertEqual(
                    pulp_lane.get("vm_cores"),
                    12 if host_id == "studio" else None,
                )
                self.assertEqual(
                    pulp_lane["supervisors"], 2
                )
                self.assertEqual(
                    {lane["repo"]: lane["runner_group_id"] for lane in data["lane"]},
                    RUNNER_GROUP_IDS,
                )
                spectr_lane = next(
                    lane for lane in data["lane"] if lane["id"] == "spectr-gate"
                )
                self.assertEqual(spectr_lane["registration_scope"], "repository")
                self.assertEqual(
                    spectr_lane["labels"],
                    ["self-hosted", "macOS", "ARM64", "spectr-build", "spectr-build-vm", "spectr-gate-fast"],
                )
                self.assertEqual(
                    spectr_lane["workflows"], ["Spectr M5 Product Acceptance"]
                )
                self.assertEqual(
                    spectr_lane["min_queued_age_seconds"],
                    600 if host_id == "m1" else 0,
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
                    self.assertTrue(all(
                        value["EnvironmentVariables"].get("TARTCI_GH_TIMEOUT_SECS")
                        == "30"
                        for value in values
                    ))
                    self.assertTrue(all(
                        value["EnvironmentVariables"].get(
                            "TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS"
                        ) == ("180" if host_id == "m1" else None)
                        for value in values
                        if value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]
                        == "Generous-Corp/pulp"
                    ))
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
                    github_app_keys = (
                        "SHIPYARD_GITHUB_APP_ID",
                        "SHIPYARD_GITHUB_APP_PRIVATE_KEY_PATH",
                        "SHIPYARD_GITHUB_APP_CACHE_DIR",
                    )
                    if host_id in {"m1", "m5"}:
                        expected_github_app = {
                            "SHIPYARD_GITHUB_APP_ID": "3878000",
                            "SHIPYARD_GITHUB_APP_PRIVATE_KEY_PATH":
                                "/Users/danielraffel/.config/shipyard/github-apps/shipyard-local.private-key.pem",
                            "SHIPYARD_GITHUB_APP_CACHE_DIR":
                                "/Users/danielraffel/.config/shipyard/ghapp-cache",
                        }
                        self.assertTrue(all(
                            {key: value["EnvironmentVariables"][key]
                             for key in github_app_keys} == expected_github_app
                            for value in values
                        ))
                    else:
                        self.assertTrue(all(
                            not any(key in value["EnvironmentVariables"]
                                    for key in github_app_keys)
                            for value in values
                        ))
                    self.assertTrue(all(
                        not any(
                            key.startswith("TARTCI_STACKED_")
                            or key.startswith("TARTCI_REGISTRY_")
                            or key == "TARTCI_FLAT_ROLLBACK_GOLDEN"
                            for key in value["EnvironmentVariables"]
                        )
                        for value in values
                    ))
                    self.assertEqual(
                        {
                            value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]:
                            value["EnvironmentVariables"].get("TARTCI_VM_LEASE_PRIORITY")
                            for value in values
                        },
                        {
                            "Generous-Corp/pulp": None,
                            "danielraffel/spectr": "vm" if host_id == "m1" else "gate",
                            "Generous-Corp/forge": "gate",
                            "Generous-Corp/vellum": "gate",
                        },
                    )
                    for value in values:
                        env = value["EnvironmentVariables"]
                        expected_vm_cores = (
                            "12"
                            if host_id == "studio"
                            and env["TARTCI_RUNNER_REPO"] == "Generous-Corp/pulp"
                            else None
                        )
                        self.assertEqual(
                            env.get("TARTCI_MACOS_VM_CORES"),
                            expected_vm_cores,
                        )
                    chrome_routes = {
                        value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]:
                        value["EnvironmentVariables"].get("TARTCI_RUNNER_CHROME_APP_DIR")
                        for value in values
                    }
                    self.assertEqual(
                        chrome_routes,
                        {
                            "Generous-Corp/pulp": None,
                            "danielraffel/spectr": None,
                            "Generous-Corp/forge": "/Applications/Google Chrome.app",
                            "Generous-Corp/vellum": None,
                        },
                    )
                    pulp_plists = [
                        value for value in values
                        if value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]
                        == "Generous-Corp/pulp"
                        and value["EnvironmentVariables"].get(
                            "TARTCI_RUNNER_ASSIGNMENT_MODE"
                        ) == "event-class-v2"
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
                        self.assertNotIn("TARTCI_VM_LEASE_PRIORITY", env)
                        self.assertEqual(env["TARTCI_ADMISSION_CLEAN_MODE"], "required")
                        self.assertEqual(env["TARTCI_RUNNER_ASSIGNMENT_MODE"], "event-class-v2")
                        self.assertEqual(env["TARTCI_ASSIGNMENT_V2_OMIT_LABELS"], "pulp-gate-fast")
                        self.assertEqual(env["TARTCI_ASSIGNMENT_V2_REQUIRED_OMIT_LABELS"], "pulp-gate-fast")
                        self.assertEqual(
                            env["TARTCI_ASSIGNMENT_V2_CLASS_LABELS"],
                            "pulp-build-merge-group,pulp-build-pr-head",
                        )
                        self.assertEqual(
                            env["TARTCI_RUNNER_WORKFLOW_TIER_GROUPS"],
                            "pulp-build-merge-group|1\npulp-build-pr-head|1",
                        )

    def _m5_with_release_yield_bound(self, td: str, value: str) -> Path:
        body = HOST_CONFIGS["m5"].read_text()
        marker = 'yield_to_workflow = "Build and Test"\n'
        self.assertEqual(body.count(marker), 1)
        path = Path(td) / "m5.toml"
        path.write_text(body.replace(
            marker, marker + f"yield_max_wait_seconds = {value}\n"
        ))
        return path

    def _release_env(self, data: dict) -> dict:
        rendered = fleet.rendered_plists(data)
        return plistlib.loads(rendered[
            "com.danielraffel.tartci.tart-runner-macos-fleet.m5.pulp-release.plist"
        ])["EnvironmentVariables"]

    def test_yield_bound_is_off_in_every_shipped_profile(self) -> None:
        # The bound is enabled deliberately per host, never by an upgrade.
        for host, profile in HOST_CONFIGS.items():
            with self.subTest(host=host):
                for body in fleet.rendered_plists(fleet.load(profile)).values():
                    env = plistlib.loads(body)["EnvironmentVariables"]
                    self.assertNotIn("TARTCI_YIELD_MAX_WAIT_SECONDS", env)

    def test_yield_bound_exports_only_a_positive_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = self._release_env(fleet.load(
                self._m5_with_release_yield_bound(td, "2700")
            ))
            self.assertEqual(env["TARTCI_YIELD_MAX_WAIT_SECONDS"], "2700")
            # Control for the absence assertions: the same lane still yields.
            self.assertEqual(env["TARTCI_YIELD_TO_WORKFLOW_NAME"], "Build and Test")
        with tempfile.TemporaryDirectory() as td:
            env = self._release_env(fleet.load(
                self._m5_with_release_yield_bound(td, "0")
            ))
            self.assertNotIn("TARTCI_YIELD_MAX_WAIT_SECONDS", env)
            self.assertEqual(env["TARTCI_YIELD_TO_WORKFLOW_NAME"], "Build and Test")

    def test_yield_bound_rejects_invalid_values(self) -> None:
        for value in ("-1", "45.5", '"2700"', "true"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as td:
                path = self._m5_with_release_yield_bound(td, value)
                with self.assertRaisesRegex(ValueError, "yield_max_wait_seconds"):
                    fleet.load(path)

    def test_yield_bound_requires_a_yield_target(self) -> None:
        body = HOST_CONFIGS["m5"].read_text()
        marker = 'id = "spectr-gate"\n'
        self.assertEqual(body.count(marker), 1)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "m5.toml"
            path.write_text(body.replace(
                marker, marker + "yield_max_wait_seconds = 2700\n"
            ))
            with self.assertRaisesRegex(ValueError, "yield_max_wait_seconds"):
                fleet.load(path)

    def test_m5_generated_release_lane_preserves_exact_contract(self) -> None:
        data = fleet.load(HOST_CONFIGS["m5"])
        release = next(lane for lane in data["lane"] if lane["id"] == "pulp-release")
        self.assertEqual(release["repo"], "Generous-Corp/pulp")
        self.assertEqual(release["runner_group_id"], 1)
        self.assertEqual(release["registration_scope"], "repository")
        self.assertEqual(release["supervisors"], 1)
        self.assertEqual(release["runner_idle_timeout_seconds"], 60)
        self.assertEqual(
            release["labels"],
            ["self-hosted", "macOS", "ARM64", "pulp-build-vm-release"],
        )
        self.assertEqual(
            [
                (tier["label"], tier["workflow"], tier["runner_group_id"])
                for tier in release["tier"]
            ],
            [
                ("pulp-release-tagged", "Release CLI", 1),
                ("pulp-release-tagged", "Sign and Release", 1),
                ("pulp-release-pr-gate", "Release-path PR gate", 1),
            ],
        )
        self.assertEqual(release["yield_to_workflow"], "Build and Test")
        self.assertEqual(
            release["yield_to_labels"],
            [
                "self-hosted", "macOS", "ARM64", "pulp-build",
                "pulp-build-vm", "pulp-gate-fast", "pulp-build-pr-head",
                "pulp-build-merge-group",
            ],
        )
        self.assertEqual(
            release["replaces_launchd_labels"],
            ["com.danielraffel.pulp.tart-runner-macos-release"],
        )
        self.assertNotIn("priority", release)
        rendered = fleet.rendered_plists(data)
        value = plistlib.loads(rendered[
            "com.danielraffel.tartci.tart-runner-macos-fleet.m5.pulp-release.plist"
        ])
        env = value["EnvironmentVariables"]
        self.assertEqual(env["TARTCI_RUNNER_GROUP_ID"], "1")
        self.assertEqual(env["TARTCI_RUNNER_IDLE_TIMEOUT_SECS"], "60")
        self.assertEqual(env["TARTCI_YIELD_TO_WORKFLOW_NAME"], "Build and Test")
        self.assertEqual(
            env["TARTCI_YIELD_TO_LABELS"],
            "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm,pulp-gate-fast,"
            "pulp-build-pr-head,pulp-build-merge-group",
        )
        self.assertEqual(
            env["TARTCI_RUNNER_WORKFLOW_TIERS"],
            "pulp-release-tagged|Release CLI\n"
            "pulp-release-tagged|Sign and Release\n"
            "pulp-release-pr-gate|Release-path PR gate",
        )
        self.assertEqual(
            env["TARTCI_RUNNER_WORKFLOW_TIER_GROUPS"],
            "pulp-release-tagged|1\npulp-release-pr-gate|1",
        )
        provider_env = os.environ.copy()
        provider_env.update(env)
        for tier_index in (0, 1):
            with self.subTest(tier_index=tier_index):
                result = subprocess.run(
                    [
                        "/bin/bash",
                        str(ROOT / "providers" / "tart-macos" / "runner.sh"),
                        "--print-runner-contract",
                        str(tier_index),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    env=provider_env,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.strip(),
                    "1\trepos/Generous-Corp/pulp/actions/runners",
                )

        duplicate_env = provider_env.copy()
        duplicate_env["TARTCI_RUNNER_WORKFLOW_TIER_GROUPS"] = (
            "pulp-release-tagged|1\n"
            "pulp-release-tagged|1\n"
            "pulp-release-pr-gate|1"
        )
        duplicate = subprocess.run(
            [
                "/bin/bash",
                str(ROOT / "providers" / "tart-macos" / "runner.sh"),
                "--print-runner-contract",
                "0",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=duplicate_env,
        )
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn(
            "workflow-tier runner groups must exactly match workflow tiers in priority order",
            duplicate.stderr,
        )
        self.assertNotIn("TARTCI_VM_LEASE_PRIORITY", env)
        self.assertNotIn("TARTCI_RUNNER_ASSIGNMENT_MODE", env)

    def test_workflows_sharing_a_tier_class_reject_conflicting_groups(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "conflicting-tier-groups.toml"
            path.write_text(HOST_CONFIGS["m5"].read_text().replace(
                'label = "pulp-release-tagged"\n'
                'workflow = "Sign and Release"\n'
                'runner_group_id = 1',
                'label = "pulp-release-tagged"\n'
                'workflow = "Sign and Release"\n'
                'runner_group_id = 3',
                1,
            ))
            with self.assertRaisesRegex(
                ValueError,
                "workflows sharing tier class label pulp-release-tagged must use "
                "the same runner_group_id",
            ):
                fleet.load(path)

    def test_m5_release_lane_rejects_org_scope_and_auxiliary_authority(self) -> None:
        base = HOST_CONFIGS["m5"].read_text()
        fixtures = {
            "group-3": base.replace(
                'id = "pulp-release"\nrepo = "Generous-Corp/pulp"\nrunner_group_id = 1',
                'id = "pulp-release"\nrepo = "Generous-Corp/pulp"\nrunner_group_id = 3',
                1,
            ),
            "organization-scope": base.replace(
                'id = "pulp-release"\nrepo = "Generous-Corp/pulp"\nrunner_group_id = 1\nregistration_scope = "repository"',
                'id = "pulp-release"\nrepo = "Generous-Corp/pulp"\nrunner_group_id = 1\nregistration_scope = "organization"',
                1,
            ),
            "tier-group-3": base.replace(
                'label = "pulp-release-tagged"\nworkflow = "Release CLI"\nrunner_group_id = 1',
                'label = "pulp-release-tagged"\nworkflow = "Release CLI"\nrunner_group_id = 3',
                1,
            ),
            "auxiliary-retirement": base.replace(
                '  "com.danielraffel.pulp.tart-runner-macos-release",\n]',
                '  "com.danielraffel.pulp.tart-runner-macos-release",\n'
                '  "com.danielraffel.pulp.tart-runner",\n]',
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
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertNotIn("Traceback", result.stderr)

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
            ["com.danielraffel.pulp.tart-runner-macos-release"],
            next(lane for lane in m5["lane"] if lane["id"] == "pulp-release")
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
        self.assertIn("lanes=4", valid.stdout)
        with tempfile.TemporaryDirectory() as td:
            rendered = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "render", str(CONFIG), "--output", td],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            files = sorted(Path(td).glob("*.plist"))
            self.assertEqual(len(files), 5)
            values = [plistlib.loads(path.read_bytes()) for path in files]
            self.assertTrue(all(value["RunAtLoad"] for value in values))
            self.assertTrue(all(value["ExitTimeOut"] == 30 for value in values))
            self.assertTrue(all(value["AbandonProcessGroup"] is False for value in values))
            self.assertTrue(all(".tart-runner-" in value["Label"] for value in values))
            self.assertTrue(all("--name" not in value["ProgramArguments"] for value in values))
            self.assertTrue(all(value["EnvironmentVariables"]["TARTCI_GH_CLI"] == "ghapp" for value in values))
            self.assertTrue(all(
                value["EnvironmentVariables"]["SHIPYARD_GH_APP_REPO"]
                == value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"]
                for value in values
            ))
            self.assertTrue(all(
                value["EnvironmentVariables"]["SHIPYARD_GITHUB_APP_ID"] == "3878000"
                and value["EnvironmentVariables"]["SHIPYARD_GITHUB_APP_PRIVATE_KEY_PATH"]
                == "/Users/danielraffel/.config/shipyard/github-apps/shipyard-local.private-key.pem"
                and value["EnvironmentVariables"]["SHIPYARD_GITHUB_APP_CACHE_DIR"]
                == "/Users/danielraffel/.config/shipyard/ghapp-cache"
                for value in values
            ))
            self.assertTrue(all(value["EnvironmentVariables"]["TARTCI_ADMISSION_CLEAN_MODE"] == "required" for value in values))
            pulp_values = [
                value for value in values
                if value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"] == "Generous-Corp/pulp"
            ]
            self.assertTrue(all(
                value["EnvironmentVariables"]["TARTCI_ASSIGNMENT_SCAN_TIMEOUT_SECS"] == "180"
                for value in pulp_values
            ))
            self.assertTrue(all(
                value["EnvironmentVariables"]["TARTCI_ASSIGNMENT_SCAN_MAX_WORKERS"] == "4"
                for value in pulp_values
            ))
            self.assertTrue(all(
                "TARTCI_ASSIGNMENT_SCAN_TIMEOUT_SECS" not in value["EnvironmentVariables"]
                for value in values if value not in pulp_values
            ))
            self.assertTrue(all(value["EnvironmentVariables"]["TART_HOME"] == "/Users/danielraffel/VMs" for value in values))
            self.assertTrue(all(Path(value["StandardOutPath"]).parent == Path("/Users/danielraffel/Library/Logs/tartci") for value in values))
            repos = {value["EnvironmentVariables"]["TARTCI_RUNNER_REPO"] for value in values}
            self.assertEqual(
                repos,
                {
                    "Generous-Corp/pulp",
                    "danielraffel/spectr",
                    "Generous-Corp/forge",
                    "Generous-Corp/vellum",
                },
            )
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
            self.assertNotIn("TARTCI_VM_LEASE_PRIORITY", pulp["EnvironmentVariables"])
            self.assertNotIn("TARTCI_MACOS_VM_CORES", pulp["EnvironmentVariables"])
            self.assertTrue(pulp["EnvironmentVariables"]["TARTCI_RUNNER_WORKFLOW_TIERS"].startswith("pulp-build-merge-group|"))
            self.assertIn("pulp-build-pr-head", pulp["EnvironmentVariables"]["TARTCI_RUNNER_WORKFLOW_TIERS"])
            self.assertEqual(pulp["EnvironmentVariables"]["TARTCI_RUNNER_ASSIGNMENT_MODE"], "event-class-v2")
            self.assertEqual(
                pulp["EnvironmentVariables"]["TARTCI_RUNNER_WORKFLOW_TIER_GROUPS"],
                "pulp-build-merge-group|1\npulp-build-pr-head|1",
            )
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
            home = root / "home"
            agents = root / "agents"
            agents.mkdir()
            config = root / "fleet.toml"
            config.write_text(CONFIG.read_text().replace("/Users/danielraffel", str(home)))
            render = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "render", str(config), "--output", str(agents)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(render.returncode, 0, render.stderr)
            receipt = root / "receipt.json"
            support_root = root / "support"
            copy_support_cohort(support_root)
            manifest = support_root / support_manifest.MANIFEST_NAME
            write_support_manifest(support_root, manifest)
            launch = support_root / support_manifest.LAUNCH_NAME
            launch.write_bytes(support_manifest.canonical_wrapper_bytes(support_root))
            launch.chmod(0o555)
            launch = launch.resolve()
            freeze_support_cohort(support_root, manifest)
            for plist_path in agents.glob("*.plist"):
                value = plistlib.loads(plist_path.read_bytes())
                value["ProgramArguments"][1] = str(launch)
                plist_path.write_bytes(plistlib.dumps(value, sort_keys=False))
            entrypoint = home / ".local/bin/tartci"
            support_manifest.write_wrapper(entrypoint, support_root)
            write = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "write-receipt", str(config),
                 "--agents-dir", str(agents), "--output", str(receipt),
                 "--support-root", str(support_root), "--support-manifest", str(manifest),
                 "--entrypoint", str(entrypoint),
                 "--entrypoint-source", str(entrypoint),
                 "--launch-entrypoint", str(launch),
                 "--source-authority-commit", "a" * 40],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(write.returncode, 0, write.stderr)
            self.assertEqual(json.loads(receipt.read_text())["schema"], 3)
            verify = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(config), "--agents-dir", str(agents),
                 "--support-root", str(support_root)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(verify.returncode, 0, verify.stderr)
            legacy_receipt = json.loads(receipt.read_text())
            legacy_receipt["schema"] = 2
            legacy_receipt.pop("launch_helper", None)
            receipt.write_text(json.dumps(legacy_receipt))
            verify_legacy = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(config), "--agents-dir", str(agents),
                 "--support-root", str(support_root)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(verify_legacy.returncode, 0, verify_legacy.stderr)
            legacy_receipt["schema"] = 1
            receipt.write_text(json.dumps(legacy_receipt))
            verify_obsolete = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(config), "--agents-dir", str(agents),
                 "--support-root", str(support_root)],
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(verify_obsolete.returncode, 0)
            self.assertIn("schema must be 2 or 3", verify_obsolete.stderr)
            legacy_receipt["schema"] = 3
            legacy_receipt["launch_helper"] = None
            receipt.write_text(json.dumps(legacy_receipt))
            target = next(agents.glob("*.plist"))
            base_value = plistlib.loads(target.read_bytes())
            profile = config.parent / "custom/network-profile.toml"
            profile.parent.mkdir()
            profile.write_text(
                "schema_version = 1\n[http_connect_relay]\n"
                "enabled = true\nrelay_hosts = [\"relay-a\", \"relay-b\"]\n"
                "github_cli = \"ghapp\"\n"
                "github_probe_repo = \"Generous-Corp/pulp\"\n"
                "probe_timeout_seconds = 15\n"
            )
            participation = config.parent / "participation"
            participation.write_text("0\n")
            with (
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(network, "_reload", return_value=True),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network.Path, "home", return_value=home),
                mock.patch.dict(os.environ, {
                    "TARTCI_POOL_TRANSITION_LOCK": str(config.parent / "pool.lock")
                }),
            ):
                reconciled = network.reconcile(
                    profile, agents, participation_path=participation
                )
            self.assertTrue(reconciled["ok"], reconciled)
            overlay_value = plistlib.loads(target.read_bytes())
            label = overlay_value["Label"]
            network_receipt_path = network.applied_receipt_path(profile)
            verify_env = {**os.environ, "TARTCI_NETWORK_PROFILE": str(profile)}
            composed = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(config), "--agents-dir", str(agents),
                 "--support-root", str(support_root)],
                text=True, capture_output=True, check=False, env=verify_env,
            )
            self.assertEqual(composed.returncode, 0, composed.stderr)
            stale_default = config.parent / "network-profile.applied.json"
            stale_default.write_bytes(network_receipt_path.read_bytes())
            absent_profile_env = {
                **os.environ,
                "TARTCI_NETWORK_PROFILE": str(config.parent / "absent-profile.toml"),
            }
            stale_refused = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(config), "--agents-dir", str(agents),
                 "--support-root", str(support_root)],
                text=True, capture_output=True, check=False, env=absent_profile_env,
            )
            self.assertEqual(stale_refused.returncode, 2)
            self.assertIn("failed receipt verification", stale_refused.stderr)
            symlink_target = config.parent / "overlay-target.plist"
            symlink_target.write_bytes(target.read_bytes())
            target.unlink()
            target.symlink_to(symlink_target)
            symlinked = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(config), "--agents-dir", str(agents),
                 "--support-root", str(support_root)],
                text=True, capture_output=True, check=False, env=verify_env,
            )
            self.assertEqual(symlinked.returncode, 2)
            self.assertIn("failed receipt verification", symlinked.stderr)
            target.unlink()
            target.write_bytes(symlink_target.read_bytes())
            overlay_value["EnvironmentVariables"]["FOREIGN_PROXY_DRIFT"] = "1"
            target.write_bytes(plistlib.dumps(overlay_value, sort_keys=False))
            network_receipt = json.loads(network_receipt_path.read_text())
            network_receipt["agents"][label]["digest"] = network._plist_digest(
                overlay_value
            )
            network_receipt_path.write_text(json.dumps(network_receipt))
            overlay_drift = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(config), "--agents-dir", str(agents),
                 "--support-root", str(support_root)],
                text=True, capture_output=True, check=False, env=verify_env,
            )
            self.assertEqual(overlay_drift.returncode, 2)
            self.assertIn("failed receipt verification", overlay_drift.stderr)
            target.write_bytes(plistlib.dumps(base_value, sort_keys=False))
            network_receipt_path.unlink()
            stale = agents / "com.danielraffel.tartci.tart-runner-macos-fleet.m1.removed.plist"
            stale.write_bytes(next(agents.glob("*.plist")).read_bytes())
            extra = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(config), "--agents-dir", str(agents),
                 "--support-root", str(support_root)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(extra.returncode, 2)
            self.assertIn("does not exactly match", extra.stderr)
            stale.unlink()
            target = next(agents.glob("*.plist"))
            target.write_bytes(target.read_bytes() + b"\n")
            rejected = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "verify-installed", str(receipt),
                 "--config", str(config), "--agents-dir", str(agents),
                 "--support-root", str(support_root)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(rejected.returncode, 2)
            self.assertIn("failed receipt verification", rejected.stderr)
            thaw_support_cohort(support_root)

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

    def test_github_app_references_are_complete_and_host_local(self) -> None:
        base = CONFIG.read_text()
        fixtures = {
            "partial": base.replace(
                'cache_dir = "/Users/danielraffel/.config/shipyard/ghapp-cache"\n',
                "",
                1,
            ),
            "bad-id": base.replace('id = "3878000"', 'id = "not-an-id"', 1),
            "key-outside-root": base.replace(
                'private_key_path = "/Users/danielraffel/.config/shipyard/github-apps/shipyard-local.private-key.pem"',
                'private_key_path = "/tmp/github-app.pem"',
                1,
            ),
            "cache-traversal": base.replace(
                'cache_dir = "/Users/danielraffel/.config/shipyard/ghapp-cache"',
                'cache_dir = "/Users/danielraffel/.config/shipyard/nested/../ghapp-cache"',
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
                    self.assertIn("github_app", result.stderr)

    def test_assignment_scan_timeout_is_bounded_and_v2_only(self) -> None:
        base = CONFIG.read_text()
        fixtures = {
            "too-small": base.replace(
                "assignment_scan_timeout_seconds = 180",
                "assignment_scan_timeout_seconds = 59",
                1,
            ),
            "wrong-type": base.replace(
                "assignment_scan_timeout_seconds = 180",
                'assignment_scan_timeout_seconds = "180"',
                1,
            ),
            "non-v2": base.replace(
                "min_queued_age_seconds = 0",
                "min_queued_age_seconds = 0\nassignment_scan_timeout_seconds = 180",
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
                    self.assertIn("assignment_scan_timeout_seconds", result.stderr)

    def test_host_agent_floor_is_validated(self) -> None:
        base = CONFIG.read_text()
        knob = "github_api_timeout_seconds = 30"
        self.assertIn(knob, base)
        cases = {
            "valid": (f"{knob}\nagent_floor_cores = 6\nagent_floor_pool_cores = 6", 0),
            "pool-below-floor": (
                f"{knob}\nagent_floor_cores = 6\nagent_floor_pool_cores = 4", 2
            ),
            "wrong-type": (f'{knob}\nagent_floor_cores = "6"', 2),
            "negative": (f"{knob}\nagent_floor_cores = -1", 2),
        }
        with tempfile.TemporaryDirectory() as td:
            for name, (replacement, expected) in cases.items():
                with self.subTest(name=name):
                    path = Path(td) / f"{name}.toml"
                    path.write_text(base.replace(knob, replacement, 1))
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                    if expected:
                        self.assertIn("host.agent_floor", result.stderr)

    def test_host_github_api_timeout_is_bounded(self) -> None:
        base = CONFIG.read_text()
        fixtures = {
            "too-small": base.replace(
                "github_api_timeout_seconds = 30",
                "github_api_timeout_seconds = 4",
                1,
            ),
            "too-large": base.replace(
                "github_api_timeout_seconds = 30",
                "github_api_timeout_seconds = 61",
                1,
            ),
            "wrong-type": base.replace(
                "github_api_timeout_seconds = 30",
                'github_api_timeout_seconds = "30"',
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
                    self.assertIn("host.github_api_timeout_seconds", result.stderr)

    def test_current_job_observation_budgets_are_bounded(self) -> None:
        base = CONFIG.read_text()
        pin = "github_api_timeout_seconds = 30"
        fixtures = {
            "attempt-too-small": (
                f"{pin}\ncurrent_job_attempt_timeout_seconds = 29",
                "host.current_job_attempt_timeout_seconds",
            ),
            "attempt-too-large": (
                f"{pin}\ncurrent_job_attempt_timeout_seconds = 601",
                "host.current_job_attempt_timeout_seconds",
            ),
            "attempt-wrong-type": (
                f'{pin}\ncurrent_job_attempt_timeout_seconds = "120"',
                "host.current_job_attempt_timeout_seconds",
            ),
            "budget-too-small": (
                f"{pin}\ncurrent_job_lifecycle_budget_seconds = 59",
                "host.current_job_lifecycle_budget_seconds",
            ),
            "budget-too-large": (
                f"{pin}\ncurrent_job_lifecycle_budget_seconds = 1801",
                "host.current_job_lifecycle_budget_seconds",
            ),
            # An attempt is lowered to whatever the budget has left, so a
            # budget under the attempt silently shortens every observation.
            "budget-under-attempt": (
                f"{pin}\ncurrent_job_attempt_timeout_seconds = 120"
                "\ncurrent_job_lifecycle_budget_seconds = 119",
                "at least",
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            for name, (block, expected) in fixtures.items():
                with self.subTest(name=name):
                    path = Path(td) / f"{name}.toml"
                    path.write_text(base.replace(pin, block, 1))
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(
                        result.returncode, 2, result.stdout + result.stderr
                    )
                    self.assertIn(expected, result.stderr)

    def test_current_job_observation_budget_renders_or_falls_back(self) -> None:
        """A declared budget reaches the lane; an omitted one leaves the default."""
        base = CONFIG.read_text()
        pin = "github_api_timeout_seconds = 30"
        declared = base.replace(
            pin,
            f"{pin}\ncurrent_job_attempt_timeout_seconds = 150"
            "\ncurrent_job_lifecycle_budget_seconds = 450",
            1,
        )
        attempt_key = "TARTCI_CAPTURE_CURRENT_JOB_ATTEMPT_TIMEOUT_SECS"
        budget_key = "TARTCI_CAPTURE_CURRENT_JOB_LIFECYCLE_BUDGET_SECS"
        with tempfile.TemporaryDirectory() as td:
            for name, body, expected in (
                ("declared", declared, ("150", "450")),
                ("omitted", base, (None, None)),
            ):
                with self.subTest(name=name):
                    config = Path(td) / f"{name}.toml"
                    config.write_text(body)
                    out = Path(td) / f"{name}-out"
                    rendered = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "render", str(config),
                         "--output", str(out)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(rendered.returncode, 0, rendered.stderr)
                    plists = [
                        plistlib.loads(path.read_bytes())
                        for path in out.glob("*.plist")
                    ]
                    self.assertTrue(plists, "render produced no lane plists")
                    self.assertEqual(
                        {
                            (
                                value["EnvironmentVariables"].get(attempt_key),
                                value["EnvironmentVariables"].get(budget_key),
                            )
                            for value in plists
                        },
                        {expected},
                    )
        # An omitted key leaves the provider default in force, so the default is
        # the value an unpinned host actually observes with.
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        self.assertIn(f"${{{attempt_key}-120}}", runner)
        self.assertIn(f"${{{budget_key}-360}}", runner)

    def test_assignment_scan_workers_are_bounded_and_v2_only(self) -> None:
        base = CONFIG.read_text()
        fixtures = {
            "zero": base.replace(
                "assignment_scan_max_workers = 4",
                "assignment_scan_max_workers = 0",
                1,
            ),
            "too-many": base.replace(
                "assignment_scan_max_workers = 4",
                "assignment_scan_max_workers = 5",
                1,
            ),
            "wrong-type": base.replace(
                "assignment_scan_max_workers = 4",
                'assignment_scan_max_workers = "4"',
                1,
            ),
            "non-v2": base.replace(
                "min_queued_age_seconds = 0",
                "min_queued_age_seconds = 0\nassignment_scan_max_workers = 4",
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
                    self.assertIn("assignment_scan_max_workers", result.stderr)

    def test_top_tier_receipt_age_is_bounded_and_v2_only(self) -> None:
        base = CONFIG.read_text()
        key = "assignment_top_tier_receipt_max_age_seconds"
        fixtures = {
            "negative": base.replace(f"{key} = 180", f"{key} = -1", 1),
            "too-large": base.replace(f"{key} = 180", f"{key} = 301", 1),
            "wrong-type": base.replace(f"{key} = 180", f'{key} = "180"', 1),
            "non-v2": base.replace(
                "min_queued_age_seconds = 0",
                f"min_queued_age_seconds = 0\n{key} = 180",
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
                    self.assertIn(key, result.stderr)

    @staticmethod
    def _profile_without_idle_retarget() -> str:
        """The m1 profile with its canary knob removed, so fixtures can inject
        their own value without a duplicate TOML key."""
        base, count = re.subn(
            r"^assignment_idle_retarget_seconds = \d+\n", "", CONFIG.read_text(), flags=re.M
        )
        assert count <= 1, count
        return base

    def test_idle_retarget_is_bounded_and_v2_only(self) -> None:
        base = self._profile_without_idle_retarget()
        key = "assignment_idle_retarget_seconds"
        anchor = "assignment_feed_rescue = true"
        self.assertEqual(base.count(anchor), 1)
        fixtures = {
            "negative": base.replace(anchor, f"{anchor}\n{key} = -1", 1),
            "below-floor": base.replace(anchor, f"{anchor}\n{key} = 59", 1),
            "too-large": base.replace(anchor, f"{anchor}\n{key} = 3601", 1),
            "wrong-type": base.replace(anchor, f'{anchor}\n{key} = "120"', 1),
            "bool": base.replace(anchor, f"{anchor}\n{key} = true", 1),
            # The spectr lane is not event-class-v2; the knob is meaningless there.
            "non-v2": base.replace('priority = "vm"', f'priority = "vm"\n{key} = 120', 1),
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
                    self.assertIn(key, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
            for value in (0, 60, 120, 3600):
                with self.subTest(accepted=value):
                    path = Path(td) / f"ok-{value}.toml"
                    path.write_text(base.replace(anchor, f"{anchor}\n{key} = {value}", 1))
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_idle_retarget_renders_only_when_a_profile_opts_in(self) -> None:
        """Only the canary host's profile declares the retarget, and only on
        its pulp-gate slots; every other shipped host is unaffected."""
        env_key = "TARTCI_ASSIGNMENT_V2_IDLE_RETARGET_SECS"
        canary = {"m1": "120"}
        for host_id, config in HOST_CONFIGS.items():
            with self.subTest(shipped=host_id):
                rendered = fleet.rendered_plists(fleet.load(config))
                self.assertGreater(len(rendered), 0)
                for body in rendered.values():
                    env = plistlib.loads(body)["EnvironmentVariables"]
                    on_pulp_gate = env["TARTCI_QUEUE_LANE_ID"].startswith(f"{host_id}-pulp-gate")
                    if host_id in canary and on_pulp_gate:
                        self.assertEqual(env.get(env_key), canary[host_id])
                    else:
                        self.assertNotIn(env_key, env)
                    # Control: the V2 mode itself IS rendered on the pulp slots,
                    # so an absent retarget key is a real absence.
                    if env["TARTCI_QUEUE_LANE_ID"].startswith(f"{host_id}-pulp-gate"):
                        self.assertEqual(env["TARTCI_RUNNER_ASSIGNMENT_MODE"], "event-class-v2")
        base = self._profile_without_idle_retarget()
        anchor = "assignment_feed_rescue = true"
        with tempfile.TemporaryDirectory() as td:
            for value, expected in ((120, "120"), (0, None)):
                with self.subTest(value=value):
                    path = Path(td) / f"m1-{value}.toml"
                    path.write_text(
                        base.replace(anchor, f"{anchor}\nassignment_idle_retarget_seconds = {value}", 1)
                    )
                    rendered = fleet.rendered_plists(fleet.load(path))
                    pulp_slots = 0
                    for body in rendered.values():
                        env = plistlib.loads(body)["EnvironmentVariables"]
                        if env["TARTCI_QUEUE_LANE_ID"].startswith("m1-pulp-gate"):
                            pulp_slots += 1
                            self.assertEqual(env.get("TARTCI_ASSIGNMENT_V2_IDLE_RETARGET_SECS"), expected)
                        else:
                            self.assertNotIn(env_key, env)
                    self.assertEqual(pulp_slots, 2)

    def test_slot_tier_order_renders_only_on_the_m3_pr_first_slot(self) -> None:
        """Only m3 (host id studio) pulp-gate slot 2 prefers PR-head; its slot 1
        and every m1/m5 slot keep the configured merge-group-first order."""
        env_key = "TARTCI_ASSIGNMENT_V2_TIER_ORDER"
        seen = 0
        for host_id, config in HOST_CONFIGS.items():
            rendered = fleet.rendered_plists(fleet.load(config))
            for name, body in rendered.items():
                env = plistlib.loads(body)["EnvironmentVariables"]
                with self.subTest(plist=name):
                    if name.endswith(".studio.pulp-gate.slot2.plist"):
                        seen += 1
                        self.assertEqual(
                            env.get(env_key),
                            "pulp-build-pr-head,pulp-build-merge-group",
                        )
                        # The slot still registers both classes in configured
                        # order, so runner groups and tier numbers are unchanged.
                        self.assertEqual(
                            env["TARTCI_RUNNER_WORKFLOW_TIERS"].splitlines()[0],
                            "pulp-build-merge-group|Build and Test",
                        )
                    else:
                        self.assertNotIn(env_key, env)
        self.assertEqual(seen, 1)

    def test_slot_tier_order_is_a_complete_permutation_on_a_real_slot(self) -> None:
        base = HOST_CONFIGS["studio"].read_text()
        key = "assignment_slot_tier_order"
        line = f'{key} = {{ 2 = ["pulp-build-pr-head", "pulp-build-merge-group"] }}'
        self.assertEqual(base.count(line), 1)
        rejected = {
            "missing-class": f'{key} = {{ 2 = ["pulp-build-pr-head"] }}',
            "duplicate": f'{key} = {{ 2 = ["pulp-build-pr-head", "pulp-build-pr-head"] }}',
            "unknown-class": f'{key} = {{ 2 = ["pulp-build-pr-head", "pulp-other"] }}',
            "no-such-slot": f'{key} = {{ 3 = ["pulp-build-pr-head", "pulp-build-merge-group"] }}',
            "zero-slot": f'{key} = {{ 0 = ["pulp-build-pr-head", "pulp-build-merge-group"] }}',
            "padded-slot": f'{key} = {{ 02 = ["pulp-build-pr-head", "pulp-build-merge-group"] }}',
            "not-a-table": f'{key} = ["pulp-build-pr-head", "pulp-build-merge-group"]',
            "wrong-type": f'{key} = {{ 2 = "pulp-build-pr-head" }}',
        }
        with tempfile.TemporaryDirectory() as td:
            for name, replacement in rejected.items():
                with self.subTest(name=name):
                    path = Path(td) / f"{name}.toml"
                    path.write_text(base.replace(line, replacement, 1))
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(key, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
            # A non-V2 lane (spectr) cannot carry a preference order.
            non_v2 = base.replace(line + "\n", "", 1).replace(
                'repo = "danielraffel/spectr"',
                'repo = "danielraffel/spectr"\n' + line, 1,
            )
            path = Path(td) / "non-v2.toml"
            path.write_text(non_v2)
            result = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn(key, result.stderr)
            # Control: the shipped line and the configured order both validate.
            for name, replacement in {
                "shipped": line,
                "configured": f'{key} = {{ 1 = ["pulp-build-merge-group", "pulp-build-pr-head"] }}',
            }.items():
                with self.subTest(accepted=name):
                    path = Path(td) / f"ok-{name}.toml"
                    path.write_text(base.replace(line, replacement, 1))
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_process_type_accepts_only_documented_launchd_values(self) -> None:
        base = CONFIG.read_text()
        self.assertIn('process_type = "Adaptive"', base)
        rejected = {
            "unknown": 'process_type = "Fast"',
            "lowercase": 'process_type = "adaptive"',
            "wrong-type": "process_type = 1",
            "empty": 'process_type = ""',
        }
        with tempfile.TemporaryDirectory() as td:
            for name, replacement in rejected.items():
                with self.subTest(name=name):
                    path = Path(td) / f"{name}.toml"
                    path.write_text(
                        base.replace('process_type = "Adaptive"', replacement, 1)
                    )
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(
                        result.returncode, 2, result.stdout + result.stderr
                    )
                    self.assertIn("process_type", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
            for value in fleet.PROCESS_TYPES:
                with self.subTest(accepted=value):
                    path = Path(td) / f"ok-{value}.toml"
                    path.write_text(
                        base.replace(
                            'process_type = "Adaptive"',
                            f'process_type = "{value}"',
                            1,
                        )
                    )
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(path)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_an_undeclared_lane_still_renders_the_background_default(self) -> None:
        body = CONFIG.read_text().replace('process_type = "Adaptive"\n', "", 1)
        self.assertNotIn("process_type", body)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "no-process-type.toml"
            path.write_text(body)
            output = Path(td) / "rendered"
            rendered = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "render",
                 str(path), "--output", str(output)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            values = [
                plistlib.loads(plist.read_bytes())
                for plist in sorted(output.glob("*.plist"))
            ]
            self.assertTrue(values)
            self.assertEqual(
                {value["ProcessType"] for value in values}, {"Background"}
            )

    def test_only_pulp_gate_supervisors_leave_the_background_band(self) -> None:
        for host_id, config in HOST_CONFIGS.items():
            with self.subTest(host=host_id):
                with tempfile.TemporaryDirectory() as td:
                    rendered = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "render",
                         str(config), "--output", td],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(rendered.returncode, 0, rendered.stderr)
                    by_label = {
                        value["Label"]: value["ProcessType"]
                        for value in (
                            plistlib.loads(plist.read_bytes())
                            for plist in sorted(Path(td).glob("*.plist"))
                        )
                    }
                promoted = {
                    label for label, kind in by_label.items() if kind == "Adaptive"
                }
                prefix = (
                    "com.danielraffel.tartci.tart-runner-macos-fleet."
                    f"{host_id}.pulp-gate"
                )
                # Both supervisors of the gate lane, and nothing else --
                # release and the other product gates stay Background.
                self.assertEqual(promoted, {prefix, f"{prefix}.slot2"})
                self.assertEqual(
                    {kind for label, kind in by_label.items()
                     if label not in promoted},
                    {"Background"},
                )

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
                'golden = "pulp-build-runner:latest"',
                'golden = "pulp-build-runner:latest"\nchrome_app_dir = "/Applications/Google Chrome.app"',
                1,
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            for name, body in fixtures.items():
                with self.subTest(name=name):
                    self.assertNotEqual(body, base)
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

    def test_repository_scoped_lane_requires_explicit_scope(self) -> None:
        base = '''schema=1
[host]
id="m5"
home="/h"
tart_home="/v"
cache_root="/c"
log_root="/l"
[[lane]]
id="product"
repo="owner/product"
runner_group_id=1
registration_scope="repository"
golden="g"
labels=["self-hosted","macOS","ARM64"]
workflows=["Product acceptance"]
'''
        fixtures = {
            "missing-scope": base.replace('registration_scope="repository"\n', ""),
            "wrong-scope": base.replace('registration_scope="repository"', 'registration_scope="organization"'),
            "scope-on-org-group": base.replace("runner_group_id=1", "runner_group_id=11"),
        }
        with tempfile.TemporaryDirectory() as td:
            good = Path(td) / "repository-scope.toml"
            good.write_text(base)
            result = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(good)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name, body in fixtures.items():
                with self.subTest(name=name):
                    bad = Path(td) / f"{name}.toml"
                    bad.write_text(body)
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertTrue(
                        "runner_group_id" in result.stderr
                        or "registration_scope" in result.stderr,
                        result.stderr,
                    )

    def test_pulp_pr_head_contract_cannot_omit_class_or_repository_scope(self) -> None:
        base = CONFIG.read_text()
        fixtures = {
            "missing-pr-head": base.replace(
                '\n[[lane.tier]]\nlabel = "pulp-build-pr-head"\nworkflow = "Build and Test"\nrunner_group_id = 1\n',
                "",
                1,
            ),
            "org-scoped-pr-head": base.replace(
                'label = "pulp-build-pr-head"\nworkflow = "Build and Test"\nrunner_group_id = 1',
                'label = "pulp-build-pr-head"\nworkflow = "Build and Test"\nrunner_group_id = 3',
                1,
            ),
            "org-scoped-merge-group": base.replace(
                'label = "pulp-build-merge-group"\nworkflow = "Build and Test"\nrunner_group_id = 1',
                'label = "pulp-build-merge-group"\nworkflow = "Build and Test"\nrunner_group_id = 3',
                1,
            ),
        }
        with tempfile.TemporaryDirectory() as td:
            for name, body in fixtures.items():
                with self.subTest(name=name):
                    bad = Path(td) / f"{name}.toml"
                    bad.write_text(body)
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn("event-class-v2", result.stderr)

    def test_event_class_v2_rejects_explicit_lease_priority(self) -> None:
        base = CONFIG.read_text()
        body = base.replace(
            'golden = "pulp-build-runner:latest"',
            'golden = "pulp-build-runner:latest"\npriority = "gate"',
            1,
        )
        self.assertNotEqual(body, base)
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "v2-explicit-priority.toml"
            bad.write_text(body)
            result = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("event-class-v2 must derive lease priority", result.stderr)

    def test_unconfigured_non_v2_priority_delegates_to_provider_labels(self) -> None:
        base = CONFIG.read_text()
        body = base.replace(
            'chrome_app_dir = "/Applications/Google Chrome.app"\npriority = "gate"',
            'chrome_app_dir = "/Applications/Google Chrome.app"',
            1,
        )
        self.assertNotEqual(body, base)
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "non-v2-derived-priority.toml"
            config.write_text(body)
            rendered = subprocess.run(
                [str(ROOT / "tartci"), "fleet-macos", "render", str(config),
                 "--output", td],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            forge = next(
                plistlib.loads(path.read_bytes())
                for path in Path(td).glob("*.plist")
                if "forge-gate" in path.name
            )
        self.assertNotIn(
            "TARTCI_VM_LEASE_PRIORITY", forge["EnvironmentVariables"]
        )

    def test_vm_cores_must_be_a_positive_integer(self) -> None:
        base = CONFIG.read_text()
        fixtures = {
            "zero": "0",
            "negative": "-1",
            "boolean": "true",
            "string": '"12"',
        }
        with tempfile.TemporaryDirectory() as td:
            for name, value in fixtures.items():
                with self.subTest(name=name):
                    body = base.replace(
                        'golden = "pulp-build-runner:latest"',
                        f'golden = "pulp-build-runner:latest"\nvm_cores = {value}',
                        1,
                    )
                    self.assertNotEqual(body, base)
                    bad = Path(td) / f"vm-cores-{name}.toml"
                    bad.write_text(body)
                    result = subprocess.run(
                        [str(ROOT / "tartci"), "fleet-macos", "validate", str(bad)],
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn("vm_cores must be a positive integer", result.stderr)

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



class ServingBlockedTests(unittest.TestCase):
    """A fresh heartbeat proves a supervisor is alive, never that it serves.

    A lane that keeps taking queued work and failing before assignment
    heartbeats on a normal cadence forever, so the age-only test cannot see it.
    These pin the separation in every direction: a short block is ordinary
    contention, a lane with no demand at all is the designed resting state of
    an ephemeral fleet, and only a long run of work entries that served nothing
    is a fault.
    """

    def _readiness(self, extra_state: dict, **kwargs) -> dict:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            receipt = {"plists": {"one.plist": "a"}, "retired_launchd_labels": []}
            start = "Mon Sep  1 00:00:00 2026"
            state_dir = root / "one-state"
            state_dir.mkdir()
            state = {
                "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "supervisor_pid": "101",
                "supervisor_pid_started_at": start,
            }
            state.update(extra_state)
            (state_dir / "one.state.json").write_text(json.dumps(state))
            (agents / "one.plist").write_bytes(plistlib.dumps({
                "EnvironmentVariables": {
                    "HOME": str(root), "TARTCI_STATE_DIR": str(state_dir),
                },
            }))
            running = subprocess.CompletedProcess(
                [], 0, "state = running\npid = 101\n", ""
            )
            domain = subprocess.CompletedProcess([], 0, "", "")
            process_table = subprocess.CompletedProcess(
                [], 0,
                f"101 1 {start} bash {root}/.local/share/tartci-generations/"
                "current/providers/tart-macos/runner.sh --loop\n",
                "",
            )
            args = (Path("receipt"), Path("config"), agents, Path("support"))
            with mock.patch.object(fleet, "verify_receipt", return_value=receipt), \
                 mock.patch.object(
                     fleet.subprocess, "run",
                     side_effect=[running, process_table, domain],
                 ), \
                 mock.patch.object(fleet, "verify_loaded_snapshot", return_value={}):
                return fleet.fleet_readiness(
                    *args, participating=True, pool_state="on", **kwargs
                )

    @staticmethod
    def _ago(seconds: int) -> str:
        moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _blocked(self, value: dict) -> bool:
        return value["serving"]["blocked"]

    # -- the fault itself ----------------------------------------------------

    def test_a_long_serve_less_streak_reports_the_lane_blocked(self) -> None:
        """The measured outage: three hours of taking work and serving none."""
        value = self._readiness({
            "serving_blocked_since": self._ago(10800),
            "serving_blocked_streak": 143,
            "serving_blocked_last_phase": "admission-error",
        })
        self.assertTrue(self._blocked(value))
        lanes = value["serving"]["blocked_lanes"]
        self.assertEqual([lane["label"] for lane in lanes], ["one"])
        self.assertEqual(lanes[0]["streak"], 143)
        self.assertEqual(lanes[0]["last_phase"], "admission-error")
        self.assertGreaterEqual(lanes[0]["blocked_seconds"], 10800)

    def test_a_blocked_lane_is_not_a_fleet_readiness_problem(self) -> None:
        """The design decision, stated as an assertion.

        The dominant cause of a blocked lane is upstream and hits every lane on
        every host at once, so folding it into `fleet_ready` would let one
        upstream refusal fail the gate that decides whether hosts stay in the
        fleet -- converting a serving outage into a control-plane outage, and
        destroying the warm supervisors that are the recovery path. The
        supervisor really is installed, loaded and running; that claim stays
        true and the contradicting fact gets its own name.
        """
        value = self._readiness({
            "serving_blocked_since": self._ago(10800),
            "serving_blocked_streak": 143,
        })
        self.assertTrue(self._blocked(value))
        self.assertNotIn(
            "serving_blocked", {item["code"] for item in value["problems"]}
        )
        self.assertEqual(value["verified_running_supervisors"], 1)
        self.assertTrue(value["fleet_ready"])

    # -- the false positives -------------------------------------------------

    def test_an_idle_lane_with_no_demand_is_not_blocked(self) -> None:
        """The critical false positive.

        Zero VMs at rest is the DESIGNED state of an ephemeral on-demand fleet,
        not a symptom. A lane polling an empty queue clears the streak on every
        pass, so it must read exactly like a lane that just finished a job.
        """
        value = self._readiness({
            "phase": "waiting",
            "vm": "",
            "serving_blocked_since": "",
            "serving_blocked_streak": 0,
        })
        self.assertFalse(self._blocked(value))
        self.assertEqual(value["serving"]["blocked_lanes"], [])
        self.assertEqual(value["verified_running_supervisors"], 1)
        self.assertTrue(value["fleet_ready"])

    def test_ordinary_contention_stays_verified(self) -> None:
        """A lane blocked for a minute is waiting its turn behind a live build,
        and flagging that would manufacture alarms on a healthy fleet."""
        value = self._readiness({
            "serving_blocked_since": self._ago(60),
            "serving_blocked_streak": 1,
        })
        self.assertFalse(self._blocked(value))
        self.assertEqual(value["verified_running_supervisors"], 1)
        self.assertTrue(value["fleet_ready"])

    def test_a_fast_retry_burst_below_the_duration_floor_is_not_blocked(self) -> None:
        """The transience gate. An upstream blip can drive the streak past the
        threshold in minutes; raising a fleet-wide alarm on that would be its
        own outage."""
        value = self._readiness({
            "serving_blocked_since": self._ago(120),
            "serving_blocked_streak": 200,
        })
        self.assertFalse(self._blocked(value))

    def test_a_long_block_below_the_streak_floor_is_not_blocked(self) -> None:
        """The shape gate. Duration alone cannot tell a lane that is failing
        from one that is merely slow, which is why the threshold is a streak
        and not an elapsed time or an error count."""
        value = self._readiness({
            "serving_blocked_since": self._ago(10800),
            "serving_blocked_streak": 2,
        })
        self.assertFalse(self._blocked(value))

    def test_healthy_and_failing_cadences_land_on_opposite_sides(self) -> None:
        """The threshold against the real numbers.

        Healthy: ~1.6 work entries/hour at job-per-mint ~1.00, so the streak is
        reset by nearly every entry and three hours of it reaches ~1. Failing:
        ~35 entries/hour at job-per-mint 0.00, so three hours reaches ~105. The
        threshold of 6 sits between them with an order of magnitude of margin
        on the failing side and 3.75 hours of consecutive total failure needed
        to reach it on the healthy side.
        """
        hours = 3
        healthy_streak = 1
        failing_streak = int(35 * hours)
        self.assertLess(healthy_streak, 6)
        self.assertGreater(failing_streak, 6 * 10)
        healthy = self._readiness({
            "serving_blocked_since": self._ago(3600 * hours),
            "serving_blocked_streak": healthy_streak,
        })
        failing = self._readiness({
            "serving_blocked_since": self._ago(3600 * hours),
            "serving_blocked_streak": failing_streak,
        })
        self.assertFalse(self._blocked(healthy))
        self.assertTrue(self._blocked(failing))

    # -- blindness is not health --------------------------------------------

    def test_a_generation_predating_the_streak_is_reported_unmeasurable(self) -> None:
        """Hosts run whatever generation was last installed. An older runner
        never writes the counter, and its absence is ignorance, not health."""
        value = self._readiness({})
        self.assertEqual(value["serving"]["unmeasurable_lanes"], ["one"])
        self.assertFalse(self._blocked(value))
        self.assertEqual(value["verified_running_supervisors"], 1)

    def test_a_generation_predating_the_streak_keeps_the_duration_only_test(self) -> None:
        """Do not regress the lease-denial path on a host that has not been
        redeployed: without a counter to read, the elapsed-time test is the
        only evidence there is, and it still fires."""
        value = self._readiness({"serving_blocked_since": self._ago(7200)})
        self.assertTrue(self._blocked(value))
        self.assertIsNone(value["serving"]["blocked_lanes"][0]["streak"])

    def test_an_unreadable_marker_is_reported_rather_than_skipped(self) -> None:
        value = self._readiness({"serving_blocked_since": "not-a-timestamp"})
        self.assertIn(
            "serving_blocked_invalid",
            {item["code"] for item in value["problems"]},
        )
        self.assertEqual(value["verified_running_supervisors"], 0)

    def test_an_unreadable_streak_is_reported_rather_than_skipped(self) -> None:
        value = self._readiness({
            "serving_blocked_since": self._ago(10800),
            "serving_blocked_streak": "many",
        })
        self.assertIn(
            "serving_blocked_streak_invalid",
            {item["code"] for item in value["problems"]},
        )
        self.assertFalse(self._blocked(value))

    def test_the_thresholds_are_configurable(self) -> None:
        value = self._readiness(
            {"serving_blocked_since": self._ago(600), "serving_blocked_streak": 3},
            blocked_serving_seconds=300, blocked_serving_streak=2,
        )
        self.assertTrue(self._blocked(value))


class ExecutedGenerationTests(unittest.TestCase):
    """The installed generation must be the one a LaunchAgent actually runs."""

    LANE = "com.danielraffel.tartci.tart-runner-macos-fleet.studio.pulp-gate.plist"
    COMMIT = "b" * 40
    MANIFEST = "c" * 64

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.agents = self.root / "LaunchAgents"
        self.agents.mkdir()
        self.generation = self.root / f"generations/{self.COMMIT}-{self.MANIFEST[:16]}"
        self.generation.mkdir(parents=True)
        self.launch = self.generation / support_manifest.LAUNCH_NAME
        self.launch.write_text("#!/bin/bash\nexit 0\n")
        self.launch.chmod(0o555)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_plist(self, arguments: list[str]) -> None:
        (self.agents / self.LANE).write_bytes(plistlib.dumps({
            "Label": self.LANE.removesuffix(".plist"),
            "ProgramArguments": arguments,
            "RunAtLoad": True,
        }, sort_keys=False))

    def seal_bundle(self, *, commit: str, manifest_sha256: str) -> Path:
        """Materialize a launcher bundle around a frozen copy of some cohort."""
        bundle = self.root / "libexec/TartCILauncher.app"
        executable = bundle / "Contents/MacOS/tartci-launcher"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/bash\nexit 0\n")
        executable.chmod(0o755)
        sealed = bundle / fleet.SEALED_SUPPORT
        sealed.mkdir(parents=True)
        sealed_launch = sealed / support_manifest.LAUNCH_NAME
        sealed_launch.write_text("#!/bin/bash\nexit 0\n")
        sealed_launch.chmod(0o555)
        (bundle / fleet.SEALED_METADATA).write_text(json.dumps({
            "schema": 1,
            "source_commit": commit,
            "support_manifest_sha256": manifest_sha256,
            "profile_policy_sha256": "d" * 64,
            "tart_home": "/Volumes/Workshop/VMs",
        }, sort_keys=True))
        self.write_plist([str(executable), "--lane", "studio-pulp-gate"])
        return sealed_launch

    def assert_executed(self) -> dict:
        return fleet.assert_executed_generation(
            self.agents, [self.LANE],
            launch_entrypoint=self.launch,
            source_commit=self.COMMIT,
            manifest_sha256=self.MANIFEST,
        )

    def test_sealed_launcher_around_a_stale_cohort_fails_the_install(self) -> None:
        sealed_launch = self.seal_bundle(commit="a" * 40, manifest_sha256="e" * 64)
        with self.assertRaises(ValueError) as caught:
            self.assert_executed()
        message = str(caught.exception)
        self.assertIn("install_ineffective", message)
        self.assertIn(str(sealed_launch), message)
        self.assertIn(str(self.launch.resolve()), message)
        self.assertIn(f"{'a' * 40}/{'e' * 64}", message)
        self.assertIn(f"{self.COMMIT}/{self.MANIFEST}", message)

    def test_sealed_launcher_around_the_installed_cohort_succeeds(self) -> None:
        sealed_launch = self.seal_bundle(
            commit=self.COMMIT, manifest_sha256=self.MANIFEST
        )
        self.assertEqual(self.assert_executed(), {self.LANE: {
            "kind": "sealed-bundle", "executes": str(sealed_launch),
        }})

    def test_launch_agent_running_the_generation_directly_succeeds(self) -> None:
        self.write_plist(["/bin/bash", str(self.launch), "serve", "macos", "--loop"])
        self.assertEqual(self.assert_executed(), {self.LANE: {
            "kind": "generation", "executes": str(self.launch.resolve()),
        }})

    def test_launch_agent_running_a_foreign_program_fails_the_install(self) -> None:
        self.write_plist(["/usr/bin/true", "--lane", "studio-pulp-gate"])
        with self.assertRaisesRegex(ValueError, "install_ineffective"):
            self.assert_executed()

    def test_sealed_cohort_without_a_launch_entrypoint_fails_the_install(self) -> None:
        sealed_launch = self.seal_bundle(
            commit=self.COMMIT, manifest_sha256=self.MANIFEST
        )
        sealed_launch.chmod(0o755)
        sealed_launch.unlink()
        with self.assertRaisesRegex(ValueError, "no launch entrypoint"):
            self.assert_executed()


class ShippedProfileGitHubTimeoutTests(unittest.TestCase):
    """Every production macOS fleet profile must pin the GitHub API timeout.

    The generic default is 15s. Measured 2026-09-21, a single GitHub call
    exceeding that default was the dominant assignment-scan failure on the
    hosts that had not pinned it: m3 184/209 (88%), m5 213/263 (81%). m1,
    which pinned 30, showed the inverse split. The calls are not slow in
    isolation; they exceed 15s under concurrent supervisors. Dropping the key
    silently reverts a host to 15s, so pin it here rather than rely on review.
    """

    def _fleet_profiles(self):
        paths = sorted((ROOT / "profiles").glob("*-macos-fleet.toml"))
        self.assertTrue(paths, "no *-macos-fleet.toml profiles found: the glob is wrong")
        return paths

    def test_every_fleet_profile_pins_the_github_api_timeout(self) -> None:
        missing = []
        for path in self._fleet_profiles():
            with path.open("rb") as handle:
                data = tomllib.load(handle)
            if "github_api_timeout_seconds" not in data.get("host", {}):
                missing.append(path.name)
        self.assertEqual(
            missing, [],
            "these fleet profiles do not pin host.github_api_timeout_seconds and "
            "so silently fall back to the 15s default: " + ", ".join(missing),
        )

    def test_the_pinned_timeout_is_within_the_validated_range(self) -> None:
        for path in self._fleet_profiles():
            with path.open("rb") as handle:
                value = tomllib.load(handle)["host"]["github_api_timeout_seconds"]
            self.assertIsInstance(value, int, f"{path.name}: must be an integer")
            self.assertGreaterEqual(value, 5, f"{path.name}: below the validated floor")
            self.assertLessEqual(value, 60, f"{path.name}: above the validated ceiling")


if __name__ == "__main__":
    unittest.main()
