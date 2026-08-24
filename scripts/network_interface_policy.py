#!/usr/bin/env python3
"""Inspect and enforce the host network-interface preference used by CI."""

from __future__ import annotations

import argparse
import ipaddress
import json
import platform
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Callable, Sequence


RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class NetworkService:
    name: str
    hardware_port: str
    device: str
    kind: str
    healthy: bool
    ipv4: str | None


@dataclass(frozen=True)
class PolicyResult:
    platform: str
    state: str
    action: str
    preferred_service: str | None
    preferred_device: str | None
    fallback_service: str | None
    service_order: tuple[str, ...]
    detail: str


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, check=False)


def classify_port(hardware_port: str) -> str:
    normalized = hardware_port.lower().replace("-", "").replace(" ", "")
    if "ethernet" in normalized:
        return "ethernet"
    if "wifi" in normalized or "airport" in normalized:
        return "wifi"
    return "other"


def parse_service_order(output: str) -> list[tuple[str, str, str]]:
    services: list[tuple[str, str, str]] = []
    pending_name: str | None = None
    service_re = re.compile(r"^\(\d+\)\s+(.+?)\s*$")
    port_re = re.compile(r"^\(Hardware Port:\s*(.*?),\s*Device:\s*([^)]*)\)\s*$")
    for raw_line in output.splitlines():
        line = raw_line.strip()
        service_match = service_re.match(line)
        if service_match:
            pending_name = service_match.group(1).removeprefix("(*) ").strip()
            continue
        port_match = port_re.match(line)
        if pending_name is not None and port_match:
            services.append(
                (pending_name, port_match.group(1).strip(), port_match.group(2).strip())
            )
            pending_name = None
    return services


def parse_interface_health(output: str) -> tuple[bool, str | None]:
    active = re.search(r"^\s*status:\s*active\s*$", output, re.MULTILINE) is not None
    ipv4: str | None = None
    for match in re.finditer(r"^\s*inet\s+(\S+)", output, re.MULTILINE):
        try:
            address = ipaddress.ip_address(match.group(1))
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address) and not (
            address.is_loopback or address.is_link_local
        ):
            ipv4 = str(address)
            break
    return active and ipv4 is not None, ipv4


def macos_services(run: RunCommand) -> tuple[list[NetworkService], str | None]:
    listed = run(["/usr/sbin/networksetup", "-listnetworkserviceorder"])
    if listed.returncode != 0:
        detail = (listed.stderr or listed.stdout).strip() or "networksetup failed"
        return [], detail
    services: list[NetworkService] = []
    for name, hardware_port, device in parse_service_order(listed.stdout):
        healthy = False
        ipv4 = None
        if device:
            status = run(["/sbin/ifconfig", device])
            if status.returncode == 0:
                healthy, ipv4 = parse_interface_health(status.stdout)
            if healthy:
                scoped_route = run(["/sbin/route", "-n", "get", "-ifscope", device, "default"])
                healthy = scoped_route.returncode == 0 and re.search(
                    rf"^\s*interface:\s*{re.escape(device)}\s*$",
                    scoped_route.stdout,
                    re.MULTILINE,
                ) is not None
        services.append(
            NetworkService(
                name=name,
                hardware_port=hardware_port,
                device=device,
                kind=classify_port(hardware_port),
                healthy=healthy,
                ipv4=ipv4,
            )
        )
    return services, None


def desired_macos_order(
    services: Sequence[NetworkService],
) -> tuple[list[str], NetworkService | None, NetworkService | None]:
    current = [service.name for service in services]
    ethernet = next(
        (service for service in services if service.kind == "ethernet" and service.healthy),
        None,
    )
    wifi = next(
        (service for service in services if service.kind == "wifi" and service.healthy),
        None,
    )
    if ethernet is None or wifi is None:
        return current, ethernet, wifi
    ethernet_index = current.index(ethernet.name)
    wifi_index = current.index(wifi.name)
    if ethernet_index < wifi_index:
        return current, ethernet, wifi
    desired = list(current)
    desired.pop(ethernet_index)
    desired.insert(wifi_index, ethernet.name)
    return desired, ethernet, wifi


def evaluate_macos(run: RunCommand, *, apply: bool) -> tuple[PolicyResult, int]:
    services, error = macos_services(run)
    if error is not None:
        return PolicyResult(
            "Darwin", "unknown", "none", None, None, None, (),
            f"could not inspect network service order: {error}",
        ), 0

    current = [service.name for service in services]
    desired, ethernet, wifi = desired_macos_order(services)
    fallback = wifi.name if wifi else None
    if ethernet is None:
        state = "wifi_only" if wifi else "no_healthy_primary"
        detail = (
            "no healthy Ethernet service; retaining healthy Wi-Fi fallback"
            if wifi
            else "no healthy Ethernet or Wi-Fi service detected"
        )
        return PolicyResult(
            "Darwin", state, "none", None, None, fallback, tuple(current), detail
        ), 0 if wifi else 1

    if wifi is None:
        return PolicyResult(
            "Darwin", "ethernet_only", "none", ethernet.name, ethernet.device,
            None, tuple(current), "healthy Ethernet present; no healthy Wi-Fi fallback detected",
        ), 0

    if desired == current:
        return PolicyResult(
            "Darwin", "compliant", "none", ethernet.name, ethernet.device,
            fallback, tuple(current), "healthy Ethernet already precedes Wi-Fi",
        ), 0

    if not apply:
        return PolicyResult(
            "Darwin", "drift", "report", ethernet.name, ethernet.device, fallback,
            tuple(current), f"healthy Ethernet should precede Wi-Fi; desired order: {', '.join(desired)}",
        ), 0

    corrected = run(["/usr/sbin/networksetup", "-ordernetworkservices", *desired])
    if corrected.returncode != 0:
        detail = (corrected.stderr or corrected.stdout).strip() or "networksetup correction failed"
        return PolicyResult(
            "Darwin", "drift", "failed", ethernet.name, ethernet.device, fallback,
            tuple(current), f"could not put healthy Ethernet before Wi-Fi: {detail}",
        ), 1
    return PolicyResult(
        "Darwin", "corrected", "reordered", ethernet.name, ethernet.device, fallback,
        tuple(desired), "moved healthy Ethernet before Wi-Fi while preserving all other service order",
    ), 0


def evaluate_linux(run: RunCommand) -> tuple[PolicyResult, int]:
    route = run(["ip", "-o", "route", "show", "default"])
    device = None
    gateway = None
    if route.returncode == 0:
        match = re.search(r"\bdefault\s+via\s+(\S+)\s+dev\s+(\S+)", route.stdout)
        if match:
            gateway, device = match.groups()
    state = "reported" if device else "unknown"
    detail = (
        f"Linux report-only default route uses {device} via {gateway}; manage order in Linux/Proxmox networking"
        if device
        else "Linux report-only mode could not identify a default route"
    )
    return PolicyResult(
        "Linux", state, "report", None, device, None, (), detail
    ), 0


def evaluate(system: str, run: RunCommand, *, apply: bool) -> tuple[PolicyResult, int]:
    if system == "Darwin":
        return evaluate_macos(run, apply=apply)
    if system == "Linux":
        return evaluate_linux(run)
    return PolicyResult(
        system, "unsupported", "none", None, None, None, (),
        "network interface policy is informational on this platform",
    ), 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="correct confirmed macOS service-order drift"
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result, exit_code = evaluate(platform.system(), run_command, apply=args.apply)
    if args.json:
        print(json.dumps(asdict(result), sort_keys=True))
    else:
        print(
            "network-policy: "
            f"platform={result.platform} state={result.state} action={result.action} "
            f"preferred={result.preferred_device or '-'} fallback={result.fallback_service or '-'}; "
            f"{result.detail}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
