#!/usr/bin/env python3
"""Behavioral contract for the opt-in Tart proxy-only network boundary."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"


class MacosSoftnetProxyPolicyTests(unittest.TestCase):
    def invoke(self, **overrides: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            softnet = Path(tmp) / "softnet"
            softnet.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            softnet.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{tmp}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "TARTCI_TART_SOFTNET_PROXY_ONLY": "1",
                    "TARTCI_GUEST_HTTP_PROXY": "http://192.168.64.1:49125",
                    "TARTCI_SOFTNET_BIN": str(softnet),
                }
            )
            env.update(overrides)
            return subprocess.run(
                ["/bin/bash", str(RUNNER), "--print-network-policy"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_proxy_only_policy_blocks_every_ipv4_destination(self) -> None:
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = json.loads(result.stdout)
        self.assertTrue(policy["enforced"])
        self.assertEqual(policy["default"], "deny")
        self.assertEqual(policy["gateway_allow"], [])
        self.assertTrue(policy["host_initiated_stateful_allow"])
        self.assertEqual(policy["implicit_bootstrap"], ["dhcp_v4_client"])
        self.assertEqual(policy["non_ipv4_egress"], "drop_by_softnet_0.23.0")
        self.assertEqual(
            policy["proxy_transport"], "host-initiated-ssh-reverse-forward"
        )
        self.assertEqual(
            policy["tart_run_args"],
            [
                "--no-graphics",
                "--net-softnet-allow=in @host",
                "--net-softnet-block=0.0.0.0/0",
            ],
        )
        self.assertEqual(policy["guest_proxy"], "http://127.0.0.1:49125")
        self.assertEqual(policy["writable_host_mounts"], ["ccache"])

    def test_proxy_only_policy_requires_the_guest_proxy(self) -> None:
        result = self.invoke(TARTCI_GUEST_HTTP_PROXY="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires TARTCI_GUEST_HTTP_PROXY", result.stderr)

    def test_invalid_opt_in_fails_closed(self) -> None:
        result = self.invoke(TARTCI_TART_SOFTNET_PROXY_ONLY="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected 0 or 1", result.stderr)

    def test_disabled_policy_adds_no_tart_network_arguments(self) -> None:
        result = self.invoke(
            TARTCI_TART_SOFTNET_PROXY_ONLY="0",
            TARTCI_GUEST_HTTP_PROXY="",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = json.loads(result.stdout)
        self.assertFalse(policy["enforced"])
        self.assertEqual(policy["default"], "allow")
        self.assertEqual(policy["tart_run_args"], ["--no-graphics"])

    def test_effective_arguments_are_forwarded_to_every_ephemeral_boot(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn(
            'tartci_vm_lease_guard_run tart run "${TART_NETWORK_ARGS[@]}"',
            source,
        )

    def test_live_canary_covers_positive_negative_and_ipv6_controls(self) -> None:
        source = (ROOT / "scripts" / "canary_macos_softnet_proxy.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("https://github.com/robots.txt", source)
        self.assertIn("https://1.1.1/", source)
        self.assertIn("192.168.64.1 '$PROXY_PORT'", source)
        self.assertIn("route -n get -inet6 default", source)
        self.assertIn("2606:4700:4700::1111", source)
        self.assertIn("Positive controls use the same guest", source)
        self.assertGreaterEqual(source.count("https://github.com/robots.txt"), 3)
        self.assertGreaterEqual(source.count("https://1.1.1/"), 2)
        self.assertIn("BASELINE_IPV6", source)
        self.assertIn("tartci_acquire_vm_lease", source)
        self.assertIn("tart-macos-vm gate softnet-negative-control", source)
        self.assertIn('export TART_HOME="${TART_HOME:-$HOME/VMs}"', source)
        self.assertIn('tartci_vm_lease_guard_run "$TART_BIN" clone', source)
        self.assertIn('tartci_vm_lease_guard_run "$TART_BIN" run', source)
        self.assertIn("tartci_release_vm_lease", source)
        self.assertIn("canary_lease_absence_proved", source)
        self.assertIn("capture_command_bounded_allow_warning", source)
        self.assertIn('TARTCI_ACTIVE_VM_LEASE_ID="$lease_id"', source)
        self.assertIn("canary teardown authority remains held", source)
        self.assertIn("vm_teardown=PASS", source)
        self.assertIn("ipv6_escape=ABSENT", source)
        self.assertNotIn(" teardown=PASS", source)

    def test_runner_uses_host_initiated_reverse_forward_not_gateway_allow(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn('"proxy_transport": "host-initiated-ssh-reverse-forward"', source)
        self.assertIn('-R "127.0.0.1:$GUEST_PROXY_PORT:127.0.0.1:$GUEST_PROXY_PORT"', source)
        self.assertIn('--net-softnet-allow="in @host"', source)
        self.assertNotIn("--net-softnet-allow=192.168.64.1/32", source)

    def test_runner_proves_boundary_before_jit_and_cancels_on_tunnel_loss(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertLess(source.index('verify_proxy_boundary "$ip"'), source.index('event mint_jit'))
        self.assertIn('event proxy_tunnel_lost', source)
        self.assertIn("host_proxy_endpoint_healthy_bounded", source)
        self.assertIn("proxy_connect_probe github.com || proxy_connect_probe crates.io", source)
        self.assertIn('request = f"CONNECT {target}:443 HTTP/1.1', source)
        self.assertIn('event proxy_endpoint_lost', source)
        self.assertIn("now - last_proxy_probe", source)
        self.assertIn('cancel_current_run_bounded', source)
        self.assertIn('tart stop "$CURRENT_VM"', source)
        self.assertIn("api --paginate --slurp", source)
        self.assertIn("terminate_process_bounded", source)
        self.assertIn("wait_process_bounded", source)
        self.assertIn("capture_command_bounded 10 tart list --format json", source)
        self.assertIn("TEARDOWN_BLOCKED", source)
        self.assertGreaterEqual(
            source.count("tartci_vm_lease_guard_run tart delete"), 2
        )
        self.assertIn("release_current_vm_lease_proved", source)
        self.assertIn("vm_lease_absence_proved", source)
        self.assertIn("capture_command_bounded_allow_warning", source)
        self.assertIn('problem.rsplit(":", 1)[-1] == lease_id', source)
        self.assertIn("runner inventory is indeterminate", source)
        self.assertIn("runner_absence_unconfirmed=true", source)
        self.assertIn("pre-boot JIT runner absence is unconfirmed", source)
        self.assertIn('if ! reclaim_runner_name "$vm"; then', source)
        self.assertNotIn("network_positive_baseline", source)
        self.assertNotIn("network_baseline_pass", source)
        self.assertNotIn("network_baseline_start", source)
        self.assertIn("Softnet currently enforces IPv4 rules only", source)
        self.assertIn("if [ '$SOFTNET_PROXY_ONLY' != 1 ]", source)
        self.assertIn('[ -z "$teardown_vm" ] || PENDING_STATIC_RUNNER_RECLAIM=1', source)
        self.assertIn("PENDING_STATIC_RUNNER_RECLAIM=1", source)
        self.assertIn('if [ "$PENDING_STATIC_RUNNER_RECLAIM" = 1 ]; then', source)
        self.assertIn("hold_teardown_until_proved", source)
        self.assertIn('while [ "$TEARDOWN_BLOCKED" = 1 ]', source)
        self.assertIn("handle_supervisor_signal(){\n  trap '' INT TERM", source)
        self.assertIn("handle_supervisor_exit(){\n  local rc=$?\n  trap - EXIT\n  trap '' INT TERM", source)
        self.assertGreaterEqual(source.count('CURRENT_RPID=""'), 3)
        self.assertNotIn("supervisor halted with lease preserved", source)
        self.assertLess(source.index('event teardown_start'), source.index('event teardown "rc='))

    def test_negative_controls_reject_ssh_transport_failure(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("guest_probe_must_be_blocked", source)
        self.assertIn('output="$(ssh', source)
        self.assertIn('[ "$output" = "$marker" ]', source)
        canary = (ROOT / "scripts" / "canary_macos_softnet_proxy.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("blocked_probe", canary)
        self.assertIn("'$PROXY_PORT'", canary)
        self.assertIn("terminate_process_tree", canary)
        self.assertIn("run_cleanup_command_bounded", canary)
        self.assertIn("canary_vm_exists_or_unknown", canary)
        self.assertIn('capture_command_bounded 10 "$TART_BIN" list --format json', canary)
        self.assertIn("CANARY_UUID", canary)
        self.assertIn("LEASE_ACQUIRED=1", canary)
        self.assertIn("VM_OWNED=1", canary)
        final_cleanup = canary.rindex("cleanup || exit 1")
        self.assertLess(canary.rfind("trap '' INT TERM", 0, final_cleanup), final_cleanup)
        self.assertLess(final_cleanup, canary.index("softnet_canary=PASS"))

    def test_runner_requires_pinned_softnet_install_verifier(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("verify_macos_softnet_install.py", source)
        verifier = (ROOT / "scripts" / "verify_macos_softnet_install.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("PINNED_VERSION", verifier)
        self.assertIn("PINNED_SHA256", verifier)
        self.assertIn("stat.S_ISUID", verifier)
        self.assertIn("stat.S_IWGRP | stat.S_IWOTH", verifier)
        self.assertIn("pinned Softnet path must not contain symlink components", verifier)
        self.assertIn("if original != resolved", verifier)
        self.assertIn("parent directory is not root-owned", verifier)

    def test_hardened_lane_can_disable_the_writable_host_ccache_mount(self) -> None:
        result = self.invoke(TARTCI_MACOS_HOST_CCACHE="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = json.loads(result.stdout)
        self.assertEqual(policy["writable_host_mounts"], [])
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn('[ "$HOST_CCACHE" = 0 ] || tart_dirs+=', source)
        self.assertIn("if [ -L ~/Library/Caches/ccache ]; then rm -f ~/Library/Caches/ccache; fi; mkdir -p ~/Library/Caches/ccache", source)
        admission_start = source.index('tartci_check_disk_floor "$TART_HOME"')
        admission = source[admission_start : source.index("CLEANED_UP=0", admission_start)]
        self.assertIn('if [ "$HOST_CCACHE" = 1 ]; then', admission)
        self.assertIn('tartci_prepare_disk_root "$CACHE_ROOT"', admission)
        self.assertIn('tartci_check_disk_floor "$CACHE_ROOT"', admission)


if __name__ == "__main__":
    unittest.main(verbosity=2)
