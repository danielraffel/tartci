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

An episode is keyed by the host and the time it began, never by the breaker's
current `opened_at` or cause. A reboot and a self-update re-verify both put
the breaker through `verifying` and reopen it with a fresh `opened_at`, and the
cause is often reclassified once a probe reads a deeper layer. None of those
ends the outage: the episode's start is kept in alert.json, a cause change is
posted as a comment on the open issue, and the issue closes only when a VM got
an address after the episode began (the `host_vm_boot_up` event).

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


def alert_text(value: dict[str, Any], host: str, target: str,
               since: str | None = None) -> tuple[str, str]:
    """A title and body readable from a phone notification: host first, then
    the command to run and the remedy, then the facts.

    The title names the host and the episode's start only. The cause can be
    reclassified while the issue is open, so it lives in the body and in the
    comments, never in the title."""
    _, code, detail = vb.doctor_code(value)
    reason = reasons().get(code) or {}
    since = since or vb.iso(float(value.get("opened_at") or time.time()))
    title = f"[tartci] {host}: cannot boot VMs since {since}"
    body = "\n".join([
        f"{host} has booted no VM since {since}: {int(value.get('vms_spent') or 0)} VMs spent, "
        f"lanes idle except one probe every {vb.PROBE_SECS} s.",
        f"Run: ssh {target} 'tartci doctor fleet'",
        f"Fix: {reason.get('remedy', 'see the runbook (vm_dhcp breaker)')}",
        "",
        f"Cause: {code}",
        f"Finding: {detail}",
        f"Why: {reason.get('why', '')}",
        f"Breaker: cause={value.get('cause')} probes={value.get('probes')} "
        f"failed_probes={value.get('failed_probes')} "
        f"consecutive_unreported={value.get('consecutive_unreported')}",
        "",
        "This issue stays open across a reboot, a self-update re-verify and a cause "
        "change (each change is posted as a comment). It closes itself when a VM on "
        "the host gets an address.",
    ])
    return title, body


def cause_comment(old: str, value: dict[str, Any]) -> str:
    """The comment posted when the open episode's cause is reclassified."""
    _, code, detail = vb.doctor_code(value)
    reason = reasons().get(code) or {}
    return "\n".join([
        f"Cause changed: {old} -> {code}.",
        f"Fix: {reason.get('remedy', 'see the runbook (vm_dhcp breaker)')}",
        "",
        f"Finding: {detail}",
    ])


def _epoch(stamp: str | None) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def _comment_issue(number: str, body: str) -> tuple[int, str]:
    if host_off._scratch_home():
        return 1, "refused: issue writes from a scratch TARTCI_HOME (a test run) never reach GitHub"
    return host_off._ghapp(["api", "-X", "POST",
                            f"repos/{host_off.ISSUE_REPO}/issues/{number}/comments",
                            "-f", f"body={body}", "--jq", ".id"], host_off._update_checkout())


def _scratch(directory: pathlib.Path) -> bool:
    """A breaker under the temp dir is a test's: it never reaches GitHub
    unless the test passes its own issue opener."""
    scratch = os.path.realpath(tempfile.gettempdir())
    return os.path.realpath(str(directory)).startswith(scratch + os.sep)


def alert_pass(now: float | None = None, directory: pathlib.Path | None = None,
               issue: Any = None, close: Any = None, host: str | None = None,
               target: str | None = None, comment: Any = None) -> dict[str, Any]:
    """The watchdog's pass: one GitHub issue per outage episode, closed on
    recovery.

    The episode's start is the one alert.json already holds while it is open;
    a new episode starts from the breaker's running outage (which a reboot
    carries), so a reopened breaker never opens a second issue."""
    now = time.time() if now is None else now
    directory = directory or vb.breaker_dir()
    value = vb.status(directory)
    due, why = vb.alert_due(value, now)
    if host is None or target is None:
        host, target = _alert_host()
    state_path = directory / "alert.json"
    before = host_off._read_json(state_path) or {}
    if before.get("since"):
        since: str | None = str(before["since"])
    elif value.get("state") == "open":
        since = vb.iso(float(vb.outage_so_far(value, now)["opened_at"]))
    else:
        since = None
    # Recovered means a VM got an address after the episode began. A closed
    # breaker without one (deleted or rebuilt state) is not a recovery.
    began = _epoch(since)
    last_ip = value.get("last_ip_at")
    recovered = (value.get("state") == "closed" and isinstance(last_ip, (int, float))
                 and (began is None or float(last_ip) >= began))

    def raise_event() -> None:
        host_off.event(directory, "host_vm_boot_down", f"{host}: {why}",
                       {"cause": value.get("cause"), "since": since,
                        "vms_spent": value.get("vms_spent")}, now)

    out = host_off.episode_alert(
        state_path, active=due or (bool(before.get("since")) and not recovered),
        resolved=recovered, since=since,
        raise_event=raise_event, render=lambda: alert_text(value, host, target, since),
        issue=issue, close=close,
        issues_enabled=(os.environ.get("TARTCI_VM_BOOT_ISSUE", "1") != "0"
                        and (issue is not None or not _scratch(directory))))
    if out.get("closed"):
        down = int(now - began) if began is not None else None
        host_off.event(directory, "host_vm_boot_up", f"{host}: a VM got an address",
                       {"down_s": down, "issue": before.get("issue")}, now)
    else:
        out["commented"] = _note_cause(state_path, value, comment)
    return {"due": due, "why": why, **out}


def _note_cause(state_path: pathlib.Path, value: dict[str, Any], comment: Any) -> bool:
    """Comment on the open issue when the open breaker's cause changed.

    Only an open breaker has a cause worth naming (verifying is a probe in
    flight). The first cause seen for an issue is recorded without a comment;
    a failed comment keeps the old cause so the next pass retries it."""
    state = host_off._read_json(state_path) or {}
    if not state.get("since") or value.get("state") != "open":
        return False
    code = vb.doctor_code(value)[1]
    if not state.get("issue"):
        return False
    if not state.get("cause"):
        state["cause"] = code
        host_off._write_json(state_path, state)
        return False
    if state["cause"] == code:
        return False
    rc, text = (comment or _comment_issue)(str(state["issue"]),
                                           cause_comment(str(state["cause"]), value))
    if rc != 0:
        state["comment_error"] = text[:300]
        host_off._write_json(state_path, state)
        return False
    state["cause"] = code
    state.pop("comment_error", None)
    host_off._write_json(state_path, state)
    return True
