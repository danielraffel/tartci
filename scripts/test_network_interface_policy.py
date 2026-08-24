#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


PATH = Path(__file__).with_name("network_interface_policy.py")
ROOT = PATH.parents[1]
SPEC = importlib.util.spec_from_file_location("network_interface_policy", PATH)
assert SPEC and SPEC.loader
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


ORDER_WIFI_FIRST = """\
An asterisk (*) denotes that a network service is disabled.
(1) Wi-Fi
(Hardware Port: Wi-Fi, Device: en1)
(2) Ethernet
(Hardware Port: Ethernet, Device: en0)
(3) Tailscale
(Hardware Port: io.tailscale.ipn.macos, Device: )
"""

ORDER_ETHERNET_FIRST = """\
(1) Ethernet
(Hardware Port: Ethernet, Device: en0)
(2) Wi-Fi
(Hardware Port: Wi-Fi, Device: en1)
(3) Tailscale
(Hardware Port: io.tailscale.ipn.macos, Device: )
"""

ORDER_WIFI_ONLY = """\
(1) Wi-Fi
(Hardware Port: Wi-Fi, Device: en0)
"""

ACTIVE_ETHERNET = """\
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.168.86.175 netmask 0xffffff00 broadcast 192.168.86.255
    status: active
"""

ACTIVE_WIFI = """\
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.168.86.176 netmask 0xffffff00 broadcast 192.168.86.255
    status: active
"""

INACTIVE_ETHERNET = """\
en0: flags=8822<BROADCAST,SMART,SIMPLEX,MULTICAST> mtu 1500
    status: inactive
"""


class FakeRun:
    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        key = tuple(argv)
        self.calls.append(key)
        returncode, stdout, stderr = self.responses.get(
            key, (127, "", f"unexpected command: {key}")
        )
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def mac_responses(
    order: str, *, ethernet: str = ACTIVE_ETHERNET, wifi_device: str = "en1"
) -> dict[tuple[str, ...], tuple[int, str, str]]:
    responses = {
        ("/usr/sbin/networksetup", "-listnetworkserviceorder"): (0, order, ""),
        ("/sbin/ifconfig", "en0"): (0, ethernet, ""),
        ("/sbin/ifconfig", "en1"): (0, ACTIVE_WIFI, ""),
        ("/sbin/route", "-n", "get", "-ifscope", "en0", "default"): (
            0, "gateway: 192.168.86.1\ninterface: en0\n", "",
        ),
        ("/sbin/route", "-n", "get", "-ifscope", "en1", "default"): (
            0, "gateway: 192.168.86.1\ninterface: en1\n", "",
        ),
    }
    if wifi_device == "en0":
        responses[("/sbin/ifconfig", "en0")] = (0, ACTIVE_WIFI, "")
    return responses


class NetworkInterfacePolicyTests(unittest.TestCase):
    def test_corrects_only_confirmed_ethernet_wifi_drift(self) -> None:
        responses = mac_responses(ORDER_WIFI_FIRST)
        command = (
            "/usr/sbin/networksetup", "-ordernetworkservices",
            "Ethernet", "Wi-Fi", "Tailscale",
        )
        responses[command] = (0, "", "")
        run = FakeRun(responses)
        result, rc = policy.evaluate("Darwin", run, apply=True)
        self.assertEqual(rc, 0)
        self.assertEqual(result.state, "corrected")
        self.assertEqual(result.service_order, ("Ethernet", "Wi-Fi", "Tailscale"))
        self.assertEqual(run.calls.count(command), 1)

    def test_second_aligned_run_is_idempotent(self) -> None:
        run = FakeRun(mac_responses(ORDER_ETHERNET_FIRST))
        result, rc = policy.evaluate("Darwin", run, apply=True)
        self.assertEqual((result.state, rc), ("compliant", 0))
        self.assertFalse(any("-ordernetworkservices" in call for call in run.calls))

    def test_wifi_only_host_is_valid_and_never_mutated(self) -> None:
        run = FakeRun(mac_responses(ORDER_WIFI_ONLY, wifi_device="en0"))
        result, rc = policy.evaluate("Darwin", run, apply=True)
        self.assertEqual((result.state, rc), ("wifi_only", 0))
        self.assertEqual(result.fallback_service, "Wi-Fi")
        self.assertFalse(any("-ordernetworkservices" in call for call in run.calls))

    def test_unhealthy_ethernet_keeps_wifi_fallback(self) -> None:
        run = FakeRun(mac_responses(ORDER_WIFI_FIRST, ethernet=INACTIVE_ETHERNET))
        result, rc = policy.evaluate("Darwin", run, apply=True)
        self.assertEqual((result.state, rc), ("wifi_only", 0))
        self.assertFalse(any("-ordernetworkservices" in call for call in run.calls))

    def test_ethernet_without_scoped_default_keeps_wifi_fallback(self) -> None:
        responses = mac_responses(ORDER_WIFI_FIRST)
        responses[("/sbin/route", "-n", "get", "-ifscope", "en0", "default")] = (
            1, "", "not in table",
        )
        result, rc = policy.evaluate("Darwin", FakeRun(responses), apply=True)
        self.assertEqual((result.state, rc), ("wifi_only", 0))

    def test_confirmed_drift_blocks_when_correction_fails(self) -> None:
        responses = mac_responses(ORDER_WIFI_FIRST)
        responses[(
            "/usr/sbin/networksetup", "-ordernetworkservices",
            "Ethernet", "Wi-Fi", "Tailscale",
        )] = (1, "", "not authorized")
        result, rc = policy.evaluate("Darwin", FakeRun(responses), apply=True)
        self.assertEqual((result.state, result.action, rc), ("drift", "failed", 1))

    def test_report_mode_never_mutates(self) -> None:
        run = FakeRun(mac_responses(ORDER_WIFI_FIRST))
        result, rc = policy.evaluate("Darwin", run, apply=False)
        self.assertEqual((result.state, result.action, rc), ("drift", "report", 0))
        self.assertFalse(any("-ordernetworkservices" in call for call in run.calls))

    def test_linux_reports_default_route_without_mutation(self) -> None:
        run = FakeRun({
            ("ip", "-o", "route", "show", "default"): (
                0, "default via 192.168.86.1 dev vmbr0 proto kernel\n", "",
            )
        })
        result, rc = policy.evaluate("Linux", run, apply=True)
        self.assertEqual((result.state, result.preferred_device, rc), ("reported", "vmbr0", 0))
        self.assertEqual(run.calls, [("ip", "-o", "route", "show", "default")])

    def test_provider_entrypoints_call_shared_preflight(self) -> None:
        for relative in (
            "providers/tart-macos/runner.sh",
            "providers/tart-linux/runner.sh",
            "providers/qemu-windows/runner.sh",
        ):
            source = (ROOT / relative).read_text()
            self.assertIn("tartci_network_interface_preflight", source, relative)

    def test_shared_preflight_has_explicit_apply_report_and_off_modes(self) -> None:
        source = (ROOT / "providers/common/network-interface-policy.lib.sh").read_text()
        for mode in ("apply)", "report)", "off)"):
            self.assertIn(mode, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
