#!/usr/bin/env python3
"""Tell someone, once per outage, that a host cannot boot VMs.

The VM-DHCP breaker (vm_dhcp_breaker.py) stops a host's lanes cloning into a
VM network that gives no address; on m5 on 2026-10-07 that lasted about 6 h
and nobody was told. The launchd watchdog runs `alert_pass` every 300 s: when
the breaker's `alert_due` says so, it opens one GitHub issue per outage
through host_off's once-per-episode path and closes it when a VM gets an
address. The title leads with the host, and the body's first three lines are
a plain statement, the doctor command to run on that host, and the remedy
read from fleet_reasons, so it is usable from a phone notification.

It runs wherever the watchdog does, including a bare python3: without
tomllib the host is named by the node name. It imports only host_off and the
breaker, never fleet_doctor, so the doctor's dependencies stay off that path.
"""
from __future__ import annotations

import calendar
import os
import pathlib
import tempfile
import time
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

import json

import host_off
import vm_dhcp_breaker as vb

REASONS_PATH = pathlib.Path(__file__).resolve().parent / "fleet_reasons.json"


def reasons() -> dict[str, dict]:
    """fleet_reasons.json, the same table `tartci doctor fleet` cites."""
    try:
        value = json.loads(REASONS_PATH.read_text())
    except (OSError, ValueError):
        return {}
    table = value.get("reasons") if isinstance(value, dict) else None
    return table if isinstance(table, dict) else {}


def _alert_host() -> tuple[str, str]:
    """(host id, ssh target others reach it by): the profile's host.id and
    host.ssh, else the fleet's `tartci-<id>` alias convention."""
    host: dict[str, Any] = {}
    try:
        if tomllib is None:
            raise ModuleNotFoundError("tomllib")
        path = pathlib.Path(os.environ.get(
            "TARTCI_FLEET_PROFILE",
            str(pathlib.Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml")))
        host = tomllib.loads(path.read_text()).get("host") or {}
    except Exception:  # noqa: BLE001 - fall back to the node name
        host = {}
    name = str(host.get("id") or os.uname().nodename.split(".")[0])
    return name, str(host.get("ssh") or f"tartci-{name}")


def alert_text(value: dict[str, Any], host: str, target: str) -> tuple[str, str]:
    """A title and body readable from a phone notification: host first, then
    the command to run and the remedy, then the facts."""
    _, code, detail = vb.doctor_code(value)
    reason = reasons().get(code) or {}
    since = vb.iso(float(value.get("opened_at") or time.time()))
    title = f"[tartci] {host}: cannot boot VMs since {since} ({code})"
    body = "\n".join([
        f"{host} has booted no VM since {since}: {int(value.get('vms_spent') or 0)} VMs spent, "
        f"lanes idle except one probe every {vb.PROBE_SECS} s.",
        f"Run: ssh {target} 'tartci doctor fleet'",
        f"Fix: {reason.get('remedy', 'see the runbook (vm_dhcp breaker)')}",
        "",
        f"Finding: {detail}",
        f"Why: {reason.get('why', '')}",
        f"Breaker: cause={value.get('cause')} probes={value.get('probes')} "
        f"failed_probes={value.get('failed_probes')} "
        f"consecutive_unreported={value.get('consecutive_unreported')}",
        "",
        "This issue closes itself when a VM on the host gets an address.",
    ])
    return title, body


def _scratch(directory: pathlib.Path) -> bool:
    """A breaker under the temp dir is a test's: it never reaches GitHub
    unless the test passes its own issue opener."""
    scratch = os.path.realpath(tempfile.gettempdir())
    return os.path.realpath(str(directory)).startswith(scratch + os.sep)


def alert_pass(now: float | None = None, directory: pathlib.Path | None = None,
               issue: Any = None, close: Any = None, host: str | None = None,
               target: str | None = None) -> dict[str, Any]:
    """The watchdog's pass: one GitHub issue per outage, closed on recovery."""
    now = time.time() if now is None else now
    directory = directory or vb.breaker_dir()
    value = vb.status(directory)
    due, why = vb.alert_due(value, now)
    since = vb.iso(float(value["opened_at"])) if value.get("opened_at") else None
    if host is None or target is None:
        host, target = _alert_host()

    def raise_event() -> None:
        host_off.event(directory, "host_vm_boot_down", f"{host}: {why}",
                       {"cause": value.get("cause"), "since": since,
                        "vms_spent": value.get("vms_spent")}, now)

    state_path = directory / "alert.json"
    before = host_off._read_json(state_path) or {}
    out = host_off.episode_alert(
        state_path, active=due, resolved=value.get("state") == "closed", since=since,
        raise_event=raise_event, render=lambda: alert_text(value, host, target),
        issue=issue, close=close,
        issues_enabled=(os.environ.get("TARTCI_VM_BOOT_ISSUE", "1") != "0"
                        and (issue is not None or not _scratch(directory))))
    if out.get("closed"):
        opened = before.get("since")
        down = (int(now - calendar.timegm(time.strptime(opened, "%Y-%m-%dT%H:%M:%SZ")))
                if opened else None)
        host_off.event(directory, "host_vm_boot_up", f"{host}: a VM got an address",
                       {"down_s": down, "issue": before.get("issue")}, now)
    return {"due": due, "why": why, **out}
