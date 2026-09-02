#!/usr/bin/env python3
"""Hermetic tests for opt-in host network profile convergence."""

from __future__ import annotations

import importlib.util
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
PATH = Path(__file__).with_name("network_profile.py")
SPEC = importlib.util.spec_from_file_location("network_profile", PATH)
assert SPEC and SPEC.loader
network = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = network
SPEC.loader.exec_module(network)


def write_profile(path: Path) -> None:
    path.write_text(
        """schema_version = 1
[http_connect_relay]
enabled = true
relay_hosts = ["relay-a", "relay-b"]
github_cli = "ghapp"
github_probe_repo = "Generous-Corp/pulp"
probe_timeout_seconds = 12
""",
        encoding="utf-8",
    )


def write_controller(path: Path, label: str, os_name: str = "macos") -> None:
    value = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", "tartci", "serve", os_name],
        "EnvironmentVariables": {
            "TARTCI_MACOS_GOLDEN": "pulp-build-runner:latest"
        } if os_name == "macos" else {},
    }
    with path.open("wb") as fh:
        plistlib.dump(value, fh)


class NetworkProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._pool_lock_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._pool_lock_dir.cleanup)
        self._pool_lock_env = mock.patch.dict(
            os.environ,
            {
                "TARTCI_POOL_TRANSITION_LOCK": str(
                    Path(self._pool_lock_dir.name) / "pool-transition.lock"
                ),
                # Tests that do not pass an explicit participation path model a
                # legacy/default-on host, never the developer machine's live
                # pool state.
                "TARTCI_POOL_PARTICIPATION_FILE": str(
                    Path(self._pool_lock_dir.name) / "absent-participation"
                ),
            },
        )
        self._pool_lock_env.start()
        self.addCleanup(self._pool_lock_env.stop)

    def test_stock_python_fallback_keeps_absent_profile_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td, mock.patch.object(network, "tomllib", None):
            root = Path(td)
            self.assertFalse(network.load_profile(root / "absent.toml").enabled)
            profile = root / "profile.toml"
            write_profile(profile)
            parsed = network.load_profile(profile)
            self.assertTrue(parsed.enabled)
            self.assertEqual(parsed.relay_hosts, ("relay-a", "relay-b"))

    def test_invalid_existing_receipt_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "network-profile.applied.json"
            path.write_text("{truncated", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid network profile receipt"):
                network._load_receipt(path)

    def test_launchctl_error_is_not_treated_as_absent(self) -> None:
        failed = subprocess.CompletedProcess([], 1, "", "permission denied")
        with mock.patch.object(network.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(OSError, "could not determine state"):
                network._loaded_path("controller")

    def test_launchctl_specific_absence_is_unloaded(self) -> None:
        absent = subprocess.CompletedProcess([], 113, "", "Could not find service")
        with mock.patch.object(network.subprocess, "run", return_value=absent):
            self.assertIsNone(network._loaded_path("controller"))

    def test_launchd_heal_json_preserves_network_failure_document(self) -> None:
        body = (ROOT / "tartci").read_text(encoding="utf-8")
        self.assertIn('network_args=(reconcile --json)', body)
        self.assertIn("printf '%s\\n' \"$network_output\"", body)

    def test_controller_scope_matches_pool_controlled_namespaces(self) -> None:
        pulp = {"Label": "com.danielraffel.pulp.tart-runner-macos-gate", "ProgramArguments": ["tartci", "serve", "macos"]}
        generic = {"Label": "com.danielraffel.tartci.tart-runner-macos-fleet.m1.gate", "ProgramArguments": ["tartci", "serve", "macos"]}
        forge = {"Label": "com.danielraffel.forge.tart-runner-macos", "ProgramArguments": ["tartci", "serve", "macos"]}
        self.assertTrue(network.is_macos_controller(pulp))
        self.assertTrue(network.is_macos_controller(generic))
        self.assertFalse(network.is_macos_controller(forge))

    def test_absent_profile_is_strict_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = root / "missing-config" / "absent.toml"
            result = network.reconcile(profile, root / "agents")
            self.assertTrue(result["ok"])
            self.assertFalse(result["enabled"])
            self.assertEqual(result["changes"], [])
            self.assertFalse(profile.parent.exists())

    def test_profile_derives_fixed_host_and_guest_addresses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "profile.toml"
            write_profile(path)
            profile = network.load_profile(path)
            relay = network.desired_relay_plist(Path("/Users/tester"), profile)
            self.assertEqual(relay["Label"], network.RELAY_LABEL)
            args = relay["ProgramArguments"]
            self.assertIn("relay-a", args)
            self.assertIn("relay-b", args)
            self.assertEqual(network.HOST_PROXY, "http://127.0.0.1:49125")
            self.assertEqual(network.GUEST_PROXY, "http://192.168.64.1:49125")

    def test_only_macos_controllers_receive_proxy_environment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "profile.toml"
            write_profile(profile)
            mac = agents / "mac.plist"
            linux = agents / "linux.plist"
            write_controller(mac, "com.danielraffel.pulp.tart-runner-macos-gate")
            write_controller(linux, "com.danielraffel.pulp.tart-runner-linux", "linux")
            reloaded: list[str] = []
            with (
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(network, "_reload", side_effect=lambda label, path, dry: reloaded.append(label) or True),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                result = network.reconcile(profile, agents)
            self.assertTrue(result["ok"], result)
            mac_env = network._read_plist(mac)["EnvironmentVariables"]
            self.assertEqual(mac_env["HTTP_PROXY"], network.HOST_PROXY)
            self.assertEqual(mac_env["HTTPS_PROXY"], network.HOST_PROXY)
            self.assertEqual(mac_env["http_proxy"], network.HOST_PROXY)
            self.assertEqual(mac_env["https_proxy"], network.HOST_PROXY)
            self.assertEqual(mac_env["NO_PROXY"], "127.0.0.1,localhost,::1")
            self.assertEqual(mac_env["no_proxy"], "127.0.0.1,localhost,::1")
            self.assertEqual(mac_env["TARTCI_GUEST_HTTP_PROXY"], network.GUEST_PROXY)
            self.assertNotIn("HTTP_PROXY", network._read_plist(linux)["EnvironmentVariables"])
            self.assertIn(network.RELAY_LABEL, reloaded)
            self.assertIn("com.danielraffel.pulp.tart-runner-macos-gate", reloaded)

    def test_controller_drift_defers_without_writing_during_running_vm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "profile.toml"
            write_profile(profile)
            mac = agents / "mac.plist"
            write_controller(mac, "com.danielraffel.pulp.tart-runner-macos-gate")
            before = mac.read_bytes()
            desired_relay = network.desired_relay_plist(Path("/Users/tester"), network.load_profile(profile))
            relay = agents / f"{network.RELAY_LABEL}.plist"
            with relay.open("wb") as fh:
                plistlib.dump(desired_relay, fh, sort_keys=False)
            with (
                mock.patch.object(network, "_loaded_path", side_effect=lambda label: relay if label == network.RELAY_LABEL else mac),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network, "_any_tart_vm_running", return_value=True),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                result = network.reconcile(profile, agents)
            self.assertFalse(result["ok"])
            self.assertIn("deferred", result["reason"])
            self.assertEqual(mac.read_bytes(), before)

    def test_unavailable_inventory_is_not_reported_as_a_running_vm(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "profile.toml"
            write_profile(profile)
            controller = agents / "mac.plist"
            write_controller(controller, "com.danielraffel.pulp.tart-runner-macos-gate")
            with (
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(network, "_any_tart_vm_running", return_value=None),
                mock.patch.object(
                    network,
                    "_tart_vm_probe_reason",
                    return_value="Tart executable unavailable; set TARTCI_TART_CLI",
                ),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                result = network.reconcile(profile, agents)
            self.assertFalse(result["ok"])
            self.assertIn("Tart VM probe unavailable", result["reason"])
            self.assertIn("TARTCI_TART_CLI", result["reason"])
            self.assertNotIn("while a Tart VM is running", result["reason"])

    def test_removed_applied_profile_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = root / "network-profile.toml"
            network._write_receipt(
                network.applied_receipt_path(profile),
                {"schema_version": 1, "agents": {network.RELAY_LABEL: {"digest": "old", "path": "/old"}}},
            )
            result = network.reconcile(profile, root / "agents")
            self.assertFalse(result["ok"])
            self.assertIn("cannot be removed", result["reason"])

    def test_failed_reload_is_retried_from_missing_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            write_profile(profile)
            calls: list[str] = []

            def reload(label: str, path: Path, dry: bool) -> bool:
                calls.append(label)
                return len(calls) > 1

            with (
                mock.patch.object(network, "_loaded_path", return_value=agents / f"{network.RELAY_LABEL}.plist"),
                mock.patch.object(network, "_reload", side_effect=reload),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                first = network.reconcile(profile, agents)
                second = network.reconcile(profile, agents)
            self.assertFalse(first["ok"])
            self.assertTrue(second["ok"], second)
            self.assertEqual(calls, [network.RELAY_LABEL, network.RELAY_LABEL])

    def test_relay_drift_does_not_reload_while_vm_runs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            write_profile(profile)
            with (
                mock.patch.object(network, "_loaded_path", return_value=agents / f"{network.RELAY_LABEL}.plist"),
                mock.patch.object(network, "_reload") as reload,
                mock.patch.object(network, "_any_tart_vm_running", return_value=True),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                result = network.reconcile(profile, agents)
            self.assertFalse(result["ok"])
            self.assertIn("deferred", result["reason"])
            reload.assert_not_called()

    def test_pool_off_stages_controllers_without_loading_them(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            participation = root / "participation"
            participation.write_text("0\n")
            write_profile(profile)
            controller = agents / "mac.plist"
            label = "com.danielraffel.pulp.tart-runner-macos-gate"
            write_controller(controller, label)
            reloads: list[str] = []
            with (
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(network, "_reload", side_effect=lambda item, path, dry: reloads.append(item) or True),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                result = network.reconcile(profile, agents, participation_path=participation)
            self.assertTrue(result["ok"], result)
            self.assertEqual(reloads, [network.RELAY_LABEL])
            receipt = network._load_receipt(network.applied_receipt_path(profile))
            self.assertEqual(receipt["agents"][label]["state"], "staged")
            self.assertEqual(network._read_plist(controller)["EnvironmentVariables"]["HTTP_PROXY"], network.HOST_PROXY)

    def test_closed_pool_stages_then_on_promotes_exact_loaded_controller(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            participation = root / "participation"
            participation.write_text("0\n")
            write_profile(profile)
            controller = agents / "mac.plist"
            label = "com.danielraffel.pulp.tart-runner-macos-gate"
            write_controller(controller, label)
            relay = agents / f"{network.RELAY_LABEL}.plist"
            reloads: list[str] = []
            with (
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(
                    network,
                    "_reload",
                    side_effect=lambda item, path, dry: reloads.append(item) or True,
                ),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                staged = network.reconcile(profile, agents, participation_path=participation)
            self.assertTrue(staged["ok"], staged)
            receipt_path = network.applied_receipt_path(profile)
            self.assertEqual(network._load_receipt(receipt_path)["agents"][label]["state"], "staged")
            self.assertEqual(reloads, [network.RELAY_LABEL])

            # Drain/off share the closed participation boundary. Pool-on loads
            # the exact staged controller; reconcile must promote that receipt
            # without bootout/bootstrap churn before admission opens.
            participation.write_text("1\n")
            reloads.clear()
            with (
                mock.patch.object(
                    network,
                    "_loaded_path",
                    side_effect=lambda item: relay if item == network.RELAY_LABEL else controller,
                ),
                mock.patch.object(
                    network,
                    "_reload",
                    side_effect=lambda item, path, dry: reloads.append(item) or True,
                ),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                promoted = network.reconcile(profile, agents, participation_path=participation)
            self.assertTrue(promoted["ok"], promoted)
            self.assertEqual(network._load_receipt(receipt_path)["agents"][label]["state"], "loaded")
            self.assertEqual(reloads, [])

    def test_emergency_pool_off_during_reconcile_never_reloads_controller(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            participation = root / "participation"
            participation.write_text("1\n")
            write_profile(profile)
            controller = agents / "mac.plist"
            label = "com.danielraffel.pulp.tart-runner-macos-gate"
            write_controller(controller, label)
            reads = iter((True, False))
            with (
                mock.patch.object(network, "pool_participating", side_effect=lambda _: next(reads)),
                mock.patch.object(network, "_loaded_path", side_effect=lambda item: controller if item == label else None),
                mock.patch.object(network, "_reload", return_value=True) as reload,
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                result = network.reconcile(profile, agents, participation_path=participation)
            self.assertFalse(result["ok"])
            self.assertIn("transitioned off", result["reason"])
            self.assertNotIn(label, [call.args[0] for call in reload.call_args_list])

    def test_emergency_pool_off_crossing_reload_unloads_controller(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            participation = root / "participation"
            participation.write_text("1\n")
            write_profile(profile)
            controller = agents / "mac.plist"
            label = "com.danielraffel.pulp.tart-runner-macos-gate"
            write_controller(controller, label)
            reads = iter((True, True, True, False))
            with (
                mock.patch.object(network, "pool_participating", side_effect=lambda _: next(reads)),
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(network, "_reload", return_value=True),
                mock.patch.object(network, "_unload", return_value=True) as unload,
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                result = network.reconcile(profile, agents, participation_path=participation)
            self.assertFalse(result["ok"])
            self.assertIn("controller unloaded", result["reason"])
            unload.assert_called_once_with(label)

    def test_probe_failure_leaves_controller_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            write_profile(profile)
            controller = agents / "mac.plist"
            write_controller(controller, "com.danielraffel.pulp.tart-runner-macos-gate")
            before = controller.read_bytes()
            with (
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(network, "_reload", return_value=True),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network, "authenticated_probe", return_value=(False, "relay unhealthy")),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                result = network.reconcile(profile, agents)
            self.assertFalse(result["ok"])
            self.assertEqual(controller.read_bytes(), before)

    def test_failed_controller_reload_leaves_pending_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            write_profile(profile)
            controller = agents / "mac.plist"
            label = "com.danielraffel.pulp.tart-runner-macos-gate"
            write_controller(controller, label)

            def reload(item: str, path: Path, dry: bool) -> bool:
                return item == network.RELAY_LABEL

            with (
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(network, "_reload", side_effect=reload),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                result = network.reconcile(profile, agents)
            self.assertFalse(result["ok"])
            receipt = network._load_receipt(network.applied_receipt_path(profile))
            self.assertEqual(receipt["agents"][label]["state"], "pending")

    def test_pool_off_refuses_to_stage_still_loaded_controller(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            participation = root / "participation"
            participation.write_text("0\n")
            write_profile(profile)
            controller = agents / "mac.plist"
            label = "com.danielraffel.pulp.tart-runner-macos-gate"
            write_controller(controller, label)
            with (
                mock.patch.object(network, "_loaded_path", side_effect=lambda item: controller if item == label else None),
                mock.patch.object(network, "_reload") as reload,
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                result = network.reconcile(profile, agents, participation_path=participation)
            self.assertFalse(result["ok"])
            self.assertIn("still loaded", result["reason"])
            reload.assert_not_called()

    def test_idle_pool_off_rollback_removes_owned_proxy_and_relay(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            participation = root / "participation"
            participation.write_text("1\n")
            write_profile(profile)
            controller = agents / "mac.plist"
            label = "com.danielraffel.pulp.tart-runner-macos-gate"
            write_controller(controller, label)
            with (
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(network, "_reload", return_value=True),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
                mock.patch.object(network, "authenticated_probe", return_value=(True, "authenticated")),
                mock.patch.object(network.Path, "home", return_value=Path("/Users/tester")),
            ):
                applied = network.reconcile(profile, agents, participation_path=participation)
            self.assertTrue(applied["ok"], applied)
            participation.write_text("0\n")
            profile.unlink()
            with (
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(network, "_unload", return_value=True),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
            ):
                rolled_back = network.rollback(profile, agents, participation)
            self.assertTrue(rolled_back["ok"], rolled_back)
            environment = network._read_plist(controller)["EnvironmentVariables"]
            self.assertNotIn("HTTP_PROXY", environment)
            self.assertNotIn("TARTCI_GUEST_HTTP_PROXY", environment)
            self.assertFalse((agents / f"{network.RELAY_LABEL}.plist").exists())
            self.assertFalse(network.applied_receipt_path(profile).exists())

    def test_rollback_skips_owned_controller_removed_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents = root / "agents"
            agents.mkdir()
            profile = root / "network-profile.toml"
            participation = root / "participation"
            participation.write_text("0\n")
            receipt = {
                "schema_version": 1,
                "agents": {},
                "ownership": {
                    "controllers": {
                        "com.danielraffel.pulp.tart-runner-gone": {
                            "path": str(agents / "gone.plist"),
                            "environment": {},
                        }
                    }
                },
            }
            network._write_receipt(network.applied_receipt_path(profile), receipt)
            with (
                mock.patch.object(network, "_loaded_path", return_value=None),
                mock.patch.object(network, "_any_tart_vm_running", return_value=False),
            ):
                result = network.rollback(profile, agents, participation)
            self.assertTrue(result["ok"], result)
            self.assertIn("skip-removed-controller", [item["action"] for item in result["changes"]])

    def test_controller_path_move_updates_rollback_target_without_losing_snapshot(self) -> None:
        receipt = {
            "ownership": {
                "controllers": {
                    "controller": {
                        "path": "/old/controller.plist",
                        "environment": {"HTTP_PROXY": {"present": False}},
                    }
                }
            }
        }
        network._capture_controller_ownership(
            receipt,
            "controller",
            Path("/new/controller.plist"),
            {"EnvironmentVariables": {"HTTP_PROXY": network.HOST_PROXY}},
        )
        owned = receipt["ownership"]["controllers"]["controller"]
        self.assertEqual(owned["path"], "/new/controller.plist")
        self.assertEqual(owned["environment"], {"HTTP_PROXY": {"present": False}})

    def test_authenticated_probe_forces_loopback_proxy_and_rejects_anonymous(self) -> None:
        profile = network.RelayProfile(True, ("a", "b"), "ghapp", "Generous-Corp/pulp", 9)
        calls: list[tuple[list[str], dict[str, str]]] = []

        def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((args, kwargs["env"]))  # type: ignore[index]
            stdout = "5000\n" if "rate_limit" in args else "Generous-Corp/pulp\n"
            return subprocess.CompletedProcess(args, 0, stdout, "")

        with mock.patch.object(network.shutil, "which", return_value="/usr/local/bin/ghapp"), mock.patch.object(network.subprocess, "run", side_effect=run):
            ok, _ = network.authenticated_probe(profile)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        for _, env in calls:
            self.assertEqual(env["HTTPS_PROXY"], network.HOST_PROXY)
            self.assertNotIn("api.github.com", env["NO_PROXY"])
            self.assertEqual(env["SHIPYARD_GH_APP_REPO"], "Generous-Corp/pulp")
            self.assertEqual(env["GH_REPO"], "Generous-Corp/pulp")

        with mock.patch.object(network.shutil, "which", return_value="/usr/local/bin/ghapp"), mock.patch.object(
            network.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "60\n", ""),
        ):
            ok, reason = network.authenticated_probe(profile)
        self.assertFalse(ok)
        self.assertIn("anonymous", reason)

    def test_authenticated_probe_overrides_ambient_repo_with_profile_authority(self) -> None:
        profile = network.RelayProfile(True, ("a", "b"), "ghapp", "Generous-Corp/forge", 9)
        seen: list[dict[str, str]] = []

        def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            env = kwargs["env"]  # type: ignore[index]
            seen.append(env)
            stdout = "5000\n" if "rate_limit" in args else "Generous-Corp/forge\n"
            return subprocess.CompletedProcess(args, 0, stdout, "")

        with mock.patch.dict(os.environ, {
            "SHIPYARD_GH_APP_REPO": "wrong/ambient",
            "GH_REPO": "wrong/ambient",
        }), mock.patch.object(network.shutil, "which", return_value="/usr/local/bin/ghapp"), mock.patch.object(
            network.subprocess, "run", side_effect=run,
        ):
            ok, _ = network.authenticated_probe(profile)
        self.assertTrue(ok)
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(env["SHIPYARD_GH_APP_REPO"] == "Generous-Corp/forge" for env in seen))
        self.assertTrue(all(env["GH_REPO"] == "Generous-Corp/forge" for env in seen))

    def test_enabled_profile_requires_explicit_probe_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            profile = Path(td) / "network-profile.toml"
            profile.write_text(
                """schema_version = 1
[http_connect_relay]
enabled = true
relay_hosts = ["relay-a", "relay-b"]
github_cli = "ghapp"
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "requires github_probe_repo"):
                network.load_profile(profile)

    def test_pool_on_converges_before_opening_participation(self) -> None:
        body = (ROOT / "tartci").read_text(encoding="utf-8")
        reconcile = body.index('network_profile.py" reconcile --pool-lock-held')
        state_on = body.index("tartci_pool_write_state on", reconcile)
        participation_on = body.index("tartci_pool_write_participation 1", state_on)
        self.assertLess(reconcile, state_on)
        self.assertLess(state_on, participation_on)


if __name__ == "__main__":
    unittest.main(verbosity=2)
