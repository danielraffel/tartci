#!/usr/bin/env python3
"""Stop cloning VMs on a host whose VM DHCP server has stopped answering.

A booted VM gets its address from the host's macOS DHCP server (bootpd, a
socket-activated system daemon that Internet Sharing manages). On m5 it
stopped answering twice (2026-09-23 10:16-11:03Z, 13 VMs; 2026-10-04
21:05-21:45Z, 10 VMs). Every lane kept cloning, waiting 120 s for an address
(`boot_failed no_ip`) and discarding, while no boot could succeed: on 10-04
bootpd ran nothing from 20:53Z until smd re-enabled it at 21:48:51Z. Recovery
needs root, so tartci never attempts it; it stops spending VMs until an
address comes back, and says so.

One breaker per host, in BREAKER_DIR/breaker.json, shared by every lane:

* closed: lanes clone as usual. Each `no_ip` is recorded; K (2) of them
  within N (15 min) with no address in between opens the breaker. A single
  `no_ip` never has: both isolated ones on record cleared on the next boot.
* open: no lane clones (`check` answers `backoff`, an idle pass). Once per
  PROBE_SECS (300), at once when the VM-network chain has changed since the
  last look (bootpd loaded or its run count, /etc/bootpd.plist's mtime, or
  InternetSharing's pid: an operator acted), or at once after `probe-now`,
  exactly one lane is answered `probe` and clones one VM. Every probe is
  recorded `vm_dhcp_probe result=ip|no_ip`.
* closed again: the first address any VM on the host gets, probe or not
  (`record ip`), closes it and reports how long it was open, how many VMs it
  spent, and the recovery latency.
* verifying: after the host boots (kern.boottime differs from the one
  recorded), when no boot time is recorded yet (first run, or a breaker file
  from before this state existed), and after a self-update's `pool on`
  (`verify`), the VM network is proven before lanes clone freely. Exactly one
  lane is answered `probe`; the rest idle. Its `ip` closes the breaker
  (`vm_dhcp_verified`); its `no_ip` opens it at once with the layered cause
  (`streak=1 trigger=post_boot alert=now`), because a host that is broken
  at boot is not a one-off. A probe that reports nothing within
  VERIFY_REPORT_SECS opens it with `cause=probe_unreported` and frees the
  slot, so a crashed probe cannot hold every lane idle; a later `ip` still
  closes it. On m5 on 2026-10-07 the old rule (a reboot closes the breaker)
  let every lane clone again after the reboot, and about 56 VMs were spent.

  VERIFY_REPORT_SECS is twice the slowest probe report on current lane code,
  rounded up to the minute. A probe reports when its 120 s address wait
  ends, so clone_start -> `boot_failed no_ip` is the measure (read-only, every
  lane's events.jsonl, 2026-10-07):

    host  n    p50  p90  p99  max (s)
    m3    6    197  200  200  200
    m1    298  199  353  392  1129 (one 2026-07-09 event in the retired
                                    pre-fleet `macos` lane; next 400)
    m5    303  205  220  316  464
    m5s   0    (1032 clones, never a no_ip)

  The success report (`record ip`) comes earlier; clone_start -> boot_ok, an
  upper bound on it that also counts SSH and the JIT mint, passed 960 s in 5
  of 5215 boots over the 30 days before. TARTCI_VM_DHCP_VERIFY_SECS
  overrides it. The runner now logs `boot_ip clone_to_ip_s=` at the address;
  `boot-times` re-derives the bound from both reports once 30 days exist.

Someone is told: the launchd watchdog's 300 s pass (scripts/vm_boot_alert.py)
opens one GitHub issue per outage when `alert_due` here says so (a post-boot no_ip at once;
open PROBE_SECS with a failed probe or no probe at all; two consecutive
unreported probes), names the host, the doctor command and the remedy, and
closes it when a VM gets an address.

The trade is explicit: an outage now costs about one VM per PROBE_SECS
instead of one per lane every 2-4 min, and recovery is noticed within
PROBE_SECS plus a boot instead of within minutes.

Fail open: an unreadable or corrupt breaker reads as closed, so a lane never
refuses to boot over state it cannot read. Writes are atomic (tmp + rename)
under an exclusive lock, so a reader never sees a partial file.

Python 3.9-safe: every lane calls it with a bare `python3`, and a host whose
lane PATH falls through to /usr/bin/python3 must still stop cloning.

Every `no_ip`, read while that VM is still up, also records which layer
failed (`cause`), most fundamental first:
  pfd_crash_loop      no bridge100 and pfd (the packet-filter daemon that
                      InternetSharing waits on) keeps exiting non-zero: m5
                      on 2026-10-07, exit 3 every 10 s since boot
  vm_network_missing  no bridge100 exists: InternetSharing (vmnet shared
                      mode) never created the VM network, so bootpd has
                      nothing to serve
  bootpd_not_loaded   launchd has no bootpd job (`launchctl print` exit 113)
  dhcp_config_disabled  the network exists but /etc/bootpd.plist does not
                      enable DHCP on it
  dhcp_silent         everything is in place and bootpd still answers nothing
/etc/bootpd.plist saying dhcp_enabled=false is the NORMAL idle state: Internet
Sharing rewrites it when it creates bridge100, so it only means something
while a VM is up.

Usage: vm_dhcp_breaker.py check --lane L | record --outcome ip|no_ip --lane L
[--vm V] | probe-now | verify [--reason R] | boot-times [--days N] | status --json. `check` and `record` print {"action", "events"}; the
caller emits the events.
"""
from __future__ import annotations

import argparse
import calendar
import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - tartci hosts are POSIX.
    fcntl = None  # type: ignore[assignment]

K = 2
WINDOW_S = 15 * 60
PROBE_SECS = 300
# Boot counts per UTC hour, kept this long; outages kept, newest last.
HOURLY_KEEP_SECS = 14 * 86400
OUTAGES_KEEP = 50
# What survives a change of state: the boot clock, the measurements, and an
# outage that is still running across a reboot.
CARRIED_KEYS = ("boot_time", "hourly", "outages", "outage")
VERIFY_REPORT_SECS = 960
STATES = ("open", "closed", "verifying")
MAX_STREAK = 50


def breaker_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "TARTCI_VM_DHCP_DIR", str(pathlib.Path.home() / ".tartci" / "state" / "vm-dhcp")))


def setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


@contextlib.contextmanager
def locked(directory: pathlib.Path) -> Iterator[pathlib.Path]:
    directory.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        raise OSError("breaker requires fcntl")
    with (directory / "breaker.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield directory / "breaker.json"
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load(path: pathlib.Path) -> dict[str, Any]:
    """The breaker, or a closed one when it is absent or unreadable.

    `_source` tells the two apart: an absent file is a host that has never
    proven its VM network (it verifies), a corrupt one fails open (it clones
    and is not rewritten here)."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"state": "closed", "streak": [], "_source": "absent"}
    except (OSError, ValueError):
        return {"state": "closed", "streak": [], "_source": "corrupt"}
    if not isinstance(value, dict) or value.get("state") not in STATES:
        return {"state": "closed", "streak": [], "_source": "corrupt"}
    if not isinstance(value.get("streak"), list):
        value["streak"] = []
    return value


def save(path: pathlib.Path, value: dict[str, Any]) -> None:
    value.pop("_source", None)
    tmp = path.with_name(f".breaker.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def boot_time() -> float | None:
    """The host's last boot, epoch seconds (`sysctl kern.boottime`)."""
    override = os.environ.get("TARTCI_VM_DHCP_BOOT_TIME")
    if override is not None:
        try:
            return float(override)
        except ValueError:
            return None
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                             text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"sec\s*=\s*(\d+)", out)
    return float(match.group(1)) if match else None


# `launchctl print` exits 113 ("Could not find service") for a job launchd
# has not loaded. bootpd's plist ships Disabled and only Internet Sharing
# (com.apple.NetworkSharing) loads it, so "not loaded" is its own failure: a
# kickstart of bootpd then fails, and the job must be loaded first.
LAUNCHCTL_NOT_FOUND = 113


def bootpd_readout() -> dict[str, Any]:
    """bootpd's launchd state, readable without root.

    {"loaded": True, "state", "runs", "last_exit"} for a loaded job,
    {"loaded": False, "state": "not_loaded"} when launchd does not have it,
    and {} when it could not be read.
    """
    launchctl = os.environ.get("TARTCI_VM_DHCP_LAUNCHCTL", "launchctl")
    try:
        proc = subprocess.run([launchctl, "print", "system/com.apple.bootpd"],
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        text = f"{proc.stdout}\n{proc.stderr}"
        if proc.returncode == LAUNCHCTL_NOT_FOUND or "Could not find service" in text:
            return {"loaded": False, "state": "not_loaded"}
        return {}
    out: dict[str, Any] = {"loaded": True}
    for line in proc.stdout.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if not sep:
            continue
        if key == "state" and "state" not in out:
            out["state"] = value.strip()
        elif key == "runs":
            try:
                out["runs"] = int(value.strip())
            except ValueError:
                pass
        elif key == "last exit code":
            out["last_exit"] = value.strip()
    return out


def vm_network_readout() -> dict[str, Any]:
    """The host's vmnet shared-mode interfaces (bridge1NN), readable without
    root; {} when unreadable."""
    ifconfig = os.environ.get("TARTCI_VM_DHCP_IFCONFIG", "ifconfig")
    try:
        proc = subprocess.run([ifconfig, "-l"], capture_output=True, text=True, timeout=5,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    names = proc.stdout.split()
    return {"bridges": sorted(n for n in names if re.fullmatch(r"bridge1\d\d", n)),
            "vmenet": sum(1 for n in names if n.startswith("vmenet"))}


def bootpd_config_readout() -> dict[str, Any]:
    """/etc/bootpd.plist (world-readable): which interfaces get DHCP, and its
    mtime. {"present": False} when absent; {} when unreadable."""
    import plistlib
    path = pathlib.Path(os.environ.get("TARTCI_VM_DHCP_BOOTPD_PLIST", "/etc/bootpd.plist"))
    try:
        raw = path.read_bytes()
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return {"present": False}
    except OSError:
        return {}
    try:
        value = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 - any malformed plist is unreadable
        return {}
    enabled = value.get("dhcp_enabled") if isinstance(value, dict) else None
    return {"present": True, "mtime": mtime,
            "dhcp_enabled": [str(x) for x in enabled] if isinstance(enabled, list) else []}


def sharing_pid() -> int | None:
    """InternetSharing's pid (com.apple.NetworkSharing), readable without root."""
    launchctl = os.environ.get("TARTCI_VM_DHCP_LAUNCHCTL", "launchctl")
    try:
        proc = subprocess.run([launchctl, "print", "system/com.apple.NetworkSharing"],
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
        key, sep, value = line.strip().partition(" = ")
        if sep and key == "pid":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def pfd_readout() -> dict[str, Any]:
    """pfd's launchd state (com.apple.pfd), readable without root; {} when unreadable."""
    launchctl = os.environ.get("TARTCI_VM_DHCP_LAUNCHCTL", "launchctl")
    try:
        proc = subprocess.run([launchctl, "print", "system/com.apple.pfd"],
                              capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    out: dict[str, Any] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if not sep:
            continue
        if key == "state" and "state" not in out:
            out["state"] = value.strip()
        elif key == "runs":
            try:
                out["runs"] = int(value.strip())
            except ValueError:
                pass
        elif key == "last exit code":
            out["last_exit"] = value.strip()
    return out


def pfd_crash_looping(pfd: dict[str, Any]) -> bool:
    """Not running, and its last run ended non-zero. A healthy pfd is running
    or last exited 0 (its idle exit); "(never exited)" is not an exit."""
    last = str(pfd.get("last_exit") or "")
    return (bool(pfd) and pfd.get("state") != "running"
            and last.lstrip("-").isdigit() and int(last) != 0)


def diagnose(net: dict[str, Any], bootpd: dict[str, Any],
             config: dict[str, Any], pfd: dict[str, Any] | None = None) -> str:
    """Which layer failed, read while a VM that got no address is still up."""
    if not net:
        return "unknown"
    bridges = net.get("bridges") or []
    if not bridges:
        return "pfd_crash_loop" if pfd_crash_looping(pfd or {}) else "vm_network_missing"
    if bootpd.get("loaded") is False:
        return "bootpd_not_loaded"
    if config.get("present") is False or (
            config.get("present") and not set(bridges) & set(config.get("dhcp_enabled") or [])):
        return "dhcp_config_disabled"
    return "dhcp_silent"


def chain() -> dict[str, Any]:
    """What an operator's fix changes: compared between looks, a difference
    triggers a probe at once. Every part is readable without root. pfd's run
    count is left out on purpose: a crash-looping pfd bumps it every 10 s,
    which would make every look a probe; its state and last exit change
    exactly when it is fixed."""
    bootpd = bootpd_readout()
    config = bootpd_config_readout()
    pfd = pfd_readout()
    return {"bootpd_loaded": bootpd.get("loaded"), "bootpd_runs": bootpd.get("runs"),
            "config_mtime": config.get("mtime"), "sharing_pid": sharing_pid(),
            "pfd_state": pfd.get("state"), "pfd_last_exit": pfd.get("last_exit")}


def fmt(fields: dict[str, Any]) -> str:
    return " ".join(f"{k}={v}" for k, v in fields.items() if v is not None and v != "")


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def carried(value: dict[str, Any]) -> dict[str, Any]:
    """The keys every state change keeps (CARRIED_KEYS). close(), verified()
    and enter_verifying() all rebuild the breaker from this, never from a
    hand-copied list."""
    return {key: value[key] for key in CARRIED_KEYS if key in value}


def count_boot(value: dict[str, Any], outcome: str, now: float) -> None:
    """One boot per record() call, probe or lane, in its UTC hour."""
    hour = time.strftime("%Y-%m-%dT%H", time.gmtime(now))
    cutoff = time.strftime("%Y-%m-%dT%H", time.gmtime(now - HOURLY_KEEP_SECS))
    hourly = {h: c for h, c in (value.get("hourly") or {}).items()
              if isinstance(c, dict) and h > cutoff}
    bucket = hourly.setdefault(hour, {"ip": 0, "no_ip": 0})
    bucket[outcome] = int(bucket.get(outcome) or 0) + 1
    value["hourly"] = hourly


def outage_so_far(value: dict[str, Any], now: float) -> dict[str, Any]:
    """The running outage: begun at the earliest open, across any reboot."""
    earlier = value.get("outage") or {}
    opened = earlier.get("opened_at") or value.get("opened_at") or now
    return {"opened_at": opened,
            "vms_spent": int(earlier.get("vms_spent") or 0) + int(value.get("vms_spent") or 0),
            "cause": value.get("cause") or earlier.get("cause")}


def end_outage(value: dict[str, Any], now: float, closed_by: str) -> None:
    """Append the outage that just ended to the bounded history."""
    outage = outage_so_far(value, now)
    outage.update({"closed_at": now, "duration_s": int(now - float(outage["opened_at"])),
                   "closed_by": closed_by})
    value["outages"] = (list(value.get("outages") or []) + [outage])[-OUTAGES_KEEP:]
    value.pop("outage", None)


def close(value: dict[str, Any], now: float, reason: str) -> list[list[str]]:
    opened = float(value.get("opened_at") or now)
    possible = max(float(value.get("last_probe_at") or opened),
                   float(value.get("bootpd_moved_at") or 0))
    event = ["vm_dhcp_recovered", fmt({
        "reason": reason, "open_s": int(now - opened),
        "vms_spent": int(value.get("vms_spent") or 0),
        "probes": int(value.get("probes") or 0),
        "latency_s": int(now - possible) if reason != "host_reboot" else None,
    })]
    end_outage(value, now, reason)
    kept = carried(value)
    value.clear()
    value.update({"state": "closed", "streak": [], "last_ip_at": now, **kept})
    return [event]


def enter_verifying(value: dict[str, Any], now: float, reason: str,
                    booted: float | None) -> list[list[str]]:
    """Prove the VM network with one probe before lanes clone freely."""
    previous = value.get("cause") if value.get("state") == "open" else None
    if value.get("state") == "open":
        # The outage runs on through the reboot until a VM gets an address.
        value["outage"] = outage_so_far(value, now)
    kept = carried(value)
    value.clear()
    value.update({**kept, "state": "verifying", "streak": [], "verify_reason": reason,
                  "verifying_since": now, "boot_time": booted, "previous_cause": previous,
                  "probe_lane": None, "probe_started_at": None})
    return [["vm_dhcp_verifying", fmt({"reason": reason, "previous_cause": previous})]]


def verified(value: dict[str, Any], now: float, lane: str, *, late: bool) -> list[str]:
    """Close a post-boot verification on an address; the `vm_dhcp_verified` event."""
    event = ["vm_dhcp_verified", fmt({
        "reason": value.get("verify_reason"), "lane": lane, "late": "true" if late else None,
        "latency_s": int(now - float(value.get("verifying_since") or now))})]
    if late or value.get("outage"):
        end_outage(value, now, "late" if late else "verified")
    kept = carried(value)
    value.clear()
    value.update({"state": "closed", "streak": [], "last_ip_at": now, **kept})
    return event


def open_breaker(value: dict[str, Any], now: float, *, streak: list[dict[str, Any]],
                 readout: dict[str, Any], cause: str,
                 extra: dict[str, Any] | None = None) -> list[str]:
    """Open it; the `vm_dhcp_unanswered` event."""
    first = float(streak[0]["ts"]) if streak else now
    value.update({"state": "open", "opened_at": now, "vms_spent": len(streak),
                  "probes": 0, "last_probe_at": now, "chain": chain(),
                  "bootpd_runs": readout.get("runs"), "bootpd_moved_at": None,
                  "probe_requested_at": None, "probe_lane": None, "streak": streak,
                  "cause": cause, "failed_probes": 0,
                  "consecutive_unreported": 1 if cause == "probe_unreported" else 0,
                  "alert_now": bool(extra and extra.get("alert") == "now")})
    return ["vm_dhcp_unanswered", fmt({
        "streak": len(streak), "window_s": int(now - first),
        "lanes": ",".join(sorted({str(r.get("lane")) for r in streak})),
        "last_no_ip": ",".join(iso(float(r["ts"])) for r in streak),
        "bootpd_state": readout.get("state", "unreadable"),
        "bootpd_runs": readout.get("runs"),
        "bootpd_last_exit": readout.get("last_exit"),
        "cause": cause, **(extra or {}),
    })]


def verifying_check(value: dict[str, Any], lane: str, now: float) -> tuple[str, list[list[str]]]:
    """One probe at a time; a probe that never reports frees the slot."""
    bound = setting("TARTCI_VM_DHCP_VERIFY_SECS", VERIFY_REPORT_SECS)
    started = value.get("probe_started_at")
    if value.get("probe_lane") and started is not None:
        if now - float(started) < bound:
            return "backoff", []
        event = open_breaker(value, now, streak=[], readout=bootpd_readout(),
                             cause="probe_unreported",
                             extra={"trigger": "post_boot", "lane": value.get("probe_lane")})
        return "backoff", [event]
    value["probe_lane"], value["probe_started_at"] = lane, now
    return "probe", [["vm_dhcp_probe_start", fmt({
        "lane": lane, "trigger": "post_boot", "reason": value.get("verify_reason")})]]


def check(args: argparse.Namespace, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    probe_secs = setting("TARTCI_VM_DHCP_PROBE_SECS", PROBE_SECS)
    with locked(breaker_dir()) as path:
        value = load(path)
        events: list[list[str]] = []
        if value.get("_source") == "corrupt":
            return {"action": "clone", "events": events}
        booted = boot_time()
        recorded = value.get("boot_time")
        if booted is not None and (recorded is None or abs(float(recorded) - booted) > 1):
            events += enter_verifying(value, now, "first_run" if recorded is None
                                      else "host_reboot", booted)
        if value["state"] == "verifying":
            action, more = verifying_check(value, args.lane, now)
            save(path, value)
            return {"action": action, "events": events + more}
        if value["state"] != "open":
            if events:
                save(path, value)
            return {"action": "clone", "events": events}
        last = value.get("last_probe_at")
        if last is None:
            last = value.get("opened_at")
        due = now - float(now if last is None else last) >= probe_secs
        current = chain()
        seen = value.get("chain")
        moved = isinstance(seen, dict) and current != seen
        value["chain"] = current
        if moved:
            value["bootpd_moved_at"] = now
        requested = value.get("probe_requested_at")
        asked = requested is not None and float(requested) > float(last or 0)
        if not (due or moved or asked):
            if not isinstance(seen, dict) or moved:
                save(path, value)
            return {"action": "backoff", "events": events}
        if value.get("probe_lane"):
            value["consecutive_unreported"] = int(value.get("consecutive_unreported") or 0) + 1
        value["last_probe_at"] = now
        value["probe_lane"] = args.lane
        value["probes"] = int(value.get("probes") or 0) + 1
        save(path, value)
        events.append(["vm_dhcp_probe_start", fmt({
            "lane": args.lane,
            "trigger": "operator" if asked else "chain_changed" if moved else "cadence",
            "probe": value["probes"]})])
        return {"action": "probe", "events": events}


def record(args: argparse.Namespace, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    k = setting("TARTCI_VM_DHCP_K", K)
    window = setting("TARTCI_VM_DHCP_WINDOW_SECS", WINDOW_S)
    with locked(breaker_dir()) as path:
        value = load(path)
        events: list[list[str]] = []
        count_boot(value, args.outcome, now)
        verifying = value.get("state") == "verifying"
        probe = (value.get("state") in ("open", "verifying")
                 and value.get("probe_lane") == args.lane)
        if probe:
            events.append(["vm_dhcp_probe", fmt({"lane": args.lane, "vm": args.vm,
                                                 "result": args.outcome})])
            value["probe_lane"] = None
            value["consecutive_unreported"] = 0
        if args.outcome == "ip":
            if verifying:
                events.append(verified(value, now, args.lane, late=False))
            elif value.get("state") == "open" and value.get("cause") == "probe_unreported":
                # The post-boot probe was slow, not broken: it still proves
                # the VM network, so this is the verification that was owed.
                events.append(verified(value, now, args.lane, late=True))
            elif value.get("state") == "open":
                events += close(value, now, "probe" if probe else "boot_ok")
            else:
                value["streak"] = []
                value["last_ip_at"] = now
            save(path, value)
            return {"action": "recorded", "events": events}
        # no_ip: read which layer failed while this VM is still up.
        readout = bootpd_readout()
        cause = diagnose(vm_network_readout(), readout, bootpd_config_readout(), pfd_readout())
        value["cause"], value["cause_at"] = cause, now
        if probe:
            events[-1][1] += f" cause={cause}"
        if verifying:
            events.append(open_breaker(value, now, streak=[{
                "ts": now, "lane": args.lane, "vm": args.vm}], readout=readout, cause=cause,
                extra={"trigger": "post_boot", "alert": "now"}))
            save(path, value)
            return {"action": "recorded", "events": events}
        if value.get("state") == "open":
            value["vms_spent"] = int(value.get("vms_spent") or 0) + 1
            if probe:
                value["failed_probes"] = int(value.get("failed_probes") or 0) + 1
            save(path, value)
            return {"action": "recorded", "events": events}
        streak = [row for row in value.get("streak") or []
                  if isinstance(row, dict) and now - float(row.get("ts") or 0) <= window]
        streak.append({"ts": now, "lane": args.lane, "vm": args.vm})
        value["streak"] = streak[-MAX_STREAK:]
        if len(streak) >= k:
            events.append(open_breaker(value, now, streak=value["streak"], readout=readout,
                                       cause=cause))
        save(path, value)
        return {"action": "recorded", "events": events}


def verify(reason: str, now: float | None = None) -> dict[str, Any]:
    """Prove the VM network with one probe now (after a self-update's pool on)."""
    now = time.time() if now is None else now
    with locked(breaker_dir()) as path:
        value = load(path)
        if value.get("_source") == "corrupt":
            return {"action": "unreadable", "events": []}
        events = enter_verifying(value, now, reason, boot_time())
        save(path, value)
        return {"action": "verifying", "events": events}


def probe_now(now: float | None = None) -> dict[str, Any]:
    """After a human fix: the next `check` probes at once instead of waiting
    out PROBE_SECS. A no-op on a closed breaker."""
    now = time.time() if now is None else now
    with locked(breaker_dir()) as path:
        value = load(path)
        if value.get("state") != "open":
            return {"action": "closed", "events": []}
        value["probe_requested_at"] = now
        save(path, value)
        return {"action": "requested", "events": [["vm_dhcp_probe_requested", fmt({
            "open_s": int(now - float(value.get("opened_at") or now)),
            "cause": value.get("cause")})]]}


def doctor_code(value: dict[str, Any]) -> tuple[str, str, str]:
    """(state, code, detail) for `tartci doctor fleet` and the outage alert:
    "ok" | "problem" | "unknown", most fundamental failing layer first."""
    if value.get("state") == "open":
        opened = value.get("opened_at")
        spent = (f"no lane clones except one probe every 300 s (open since {opened}, "
                 f"{value.get('vms_spent')} VMs spent, {value.get('probes')} probes)")
        # Most fundamental layer first: the cause read while the last VM that
        # got no address was still up, then bootpd's live launchd state.
        cause = value.get("cause")
        pfd = value.get("pfd") or {}
        if cause == "pfd_crash_loop" or (cause == "vm_network_missing"
                                         and pfd_crash_looping(pfd)):
            return ("problem", "vm_dhcp_pfd_crash_loop",
                    "VM DHCP is not answering because pfd keeps exiting (state "
                    f"{pfd.get('state')}, last exit {pfd.get('last_exit')}, "
                    f"{pfd.get('runs')} runs), so InternetSharing never creates the VM "
                    "network: " + spent)
        if cause == "vm_network_missing":
            return ("problem", "vm_dhcp_vm_network_missing",
                    "VM DHCP is not answering because the VM network was never created "
                    "(no bridge100 while a VM ran: InternetSharing is not answering): "
                    + spent)
        if (value.get("bootpd") or {}).get("loaded") is False or cause == "bootpd_not_loaded":
            return ("problem", "vm_dhcp_bootpd_not_loaded",
                    "VM DHCP is not answering because launchd has no bootpd job loaded "
                    "(a bootpd kickstart cannot work until it is loaded): " + spent)
        if cause == "dhcp_config_disabled":
            return ("problem", "vm_dhcp_config_disabled",
                    "VM DHCP is not answering because /etc/bootpd.plist does not enable "
                    "DHCP on the VM network while a VM ran: " + spent)
        return ("problem", "vm_dhcp_unanswered",
                "VM DHCP is not answering on this host: no lane clones except one probe "
                f"every 300 s (open since {opened}, {value.get('vms_spent')} VMs spent, "
                f"{value.get('probes')} probes)")
    if value.get("state") == "verifying":
        since = value.get("verifying_since")
        held = (f"{int(time.time() - float(since))}s" if isinstance(since, (int, float))
                else "an unknown time")
        lane = value.get("probe_lane")
        probing = (f"a probe is in flight on {lane}" if lane
                   else "no lane has probed yet")
        was = value.get("previous_cause")
        return ("ok", "vm_dhcp_verifying",
                f"proving the VM network after {value.get('verify_reason')} for {held}; "
                f"{probing}; other lanes wait" + (f"; was open with {was}" if was else ""))
    if value.get("state") == "closed":
        return ("ok", "vm_dhcp_ok", "VM DHCP breaker closed")
    return ("unknown", "vm_dhcp_unreadable",
            f"VM DHCP breaker unreadable: {value.get('error')}")


def boot_health(value: dict[str, Any], now: float, lanes: int) -> tuple[str, str, str]:
    """(state, code, detail) for the host's VM boot record, from the hourly
    counts and the outage history.

    Degraded when the last 24 h hold at least max(2, lanes) boots that got no
    address: one failed boot per lane in a day. On 30 days of the fleet's own
    lane logs (2026-10-07) m3, m1 and m5studio had no day with any no_ip;
    m5's outage days had 13, 10, 172 and 122, and its one isolated day had 2
    in 53 boots. A count, not a rate: at these volumes a single failure moves
    the rate, so the rate is reported and never judged. The max(2, ...) keeps
    a one- or two-lane host from reading degraded on one isolated failure.
    """
    if value.get("state") == "unreadable":
        return ("unknown", "vm_boot_unreadable",
                f"VM boot record unreadable: {value.get('error')}")
    hourly = value.get("hourly") or {}
    if not hourly:
        return ("not_applicable", "vm_boot_unmeasured",
                "no VM boot has been counted on this host yet")

    def window(secs: float) -> tuple[int, int]:
        since = time.strftime("%Y-%m-%dT%H", time.gmtime(now - secs))
        rows = [c for h, c in hourly.items() if h >= since and isinstance(c, dict)]
        return (sum(int(c.get("ip") or 0) for c in rows),
                sum(int(c.get("no_ip") or 0) for c in rows))

    def line(label: str, ip: int, no_ip: int) -> str:
        total = ip + no_ip
        rate = f" ({100 * ip / total:.1f}%)" if total else ""
        return f"{label}: {ip}/{total} boots got an address{rate}"

    day_ip, day_no = window(86400)
    week_ip, week_no = window(7 * 86400)
    recent = [o for o in value.get("outages") or []
              if isinstance(o, dict) and float(o.get("closed_at") or 0) >= now - 7 * 86400]
    down = sum(int(o.get("duration_s") or 0) for o in recent)
    detail = (f"{line('24 h', day_ip, day_no)}; {line('7 d', week_ip, week_no)}; "
              f"outages in 7 d: {len(recent)}, {down // 3600}h{down % 3600 // 60:02d}m down")
    threshold = max(2, int(lanes))
    if day_no >= threshold:
        return ("problem", "vm_boot_degraded",
                f"{day_no} boots got no address in 24 h (threshold {threshold}, "
                f"one per lane): {detail}")
    return ("ok", "vm_boot_ok", detail)


def alert_due(value: dict[str, Any], now: float,
              probe_secs: int = PROBE_SECS) -> tuple[bool, str]:
    """Whether an open breaker is an outage someone must be told about now.

    Pure: no I/O. Due when the breaker is open and
      (a) a post-boot probe got no address (`alert_now`); or
      (b) it has been open PROBE_SECS and a probe has failed since, or no
          probe has been granted at all (an idle host: the two no_ips that
          opened it are the evidence, and silence would last forever); a
          probe still in flight is waited for; or
      (c) two consecutive probes have gone unreported, whatever opened it:
          a probe counts as unreported when the next one is granted before it
          reported, so the third grant is the earliest this can hold. With
          PROBE_SECS at 300 that alerts about 10-15 min after the breaker
          opens. One unreported probe alone is a slow boot.
    Closed and verifying never are.
    """
    if value.get("state") != "open":
        return False, ""
    if value.get("alert_now"):
        return True, "a post-boot probe got no address"
    if int(value.get("consecutive_unreported") or 0) >= 2:
        return True, "two consecutive probes never reported"
    if value.get("cause") == "probe_unreported":
        return False, ""
    if now - float(value.get("opened_at") or now) < probe_secs:
        return False, ""
    if int(value.get("failed_probes") or 0) >= 1:
        return True, "a probe got no address"
    if not int(value.get("probes") or 0) and not value.get("probe_lane"):
        return True, "no lane has probed since it opened"
    return False, ""


def lane_logs(root: pathlib.Path) -> list[pathlib.Path]:
    """Every lane's events.jsonl under root, at any depth: m5studio keeps its
    lanes at state/macos-fleet/<lane>/, and a one-level search reads none."""
    return sorted(root.glob("**/events.jsonl"))


def _quantiles(values: list[float]) -> dict[str, Any]:
    values = sorted(values)
    if not values:
        return {"n": 0}
    at = lambda q: int(values[min(len(values) - 1, int(q * len(values)))])  # noqa: E731
    return {"n": len(values), "p50": at(0.5), "p90": at(0.9), "p99": at(0.99),
            "max": int(values[-1])}


def boot_times(root: pathlib.Path, since: float) -> dict[str, Any]:
    """How long a probe takes to report: clone_start to the address (`boot_ip`)
    or to the end of the address wait (`boot_failed no_ip`), per lane log, for
    events at or after `since`. The verify bound is twice the slowest report."""
    reports: dict[str, list[float]] = {"boot_ip": [], "no_ip": []}
    clones = 0
    logs = lane_logs(root)
    for log in logs:
        started: dict[str, float] = {}
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                at = calendar.timegm(time.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ"))
            except (ValueError, KeyError, TypeError):
                continue
            if at < since:
                continue
            runner, name = row.get("runner"), row.get("event")
            if name == "clone_start":
                started[runner] = at
                clones += 1
            elif runner in started and name == "boot_ip":
                reports["boot_ip"].append(at - started.pop(runner))
            elif runner in started and name == "boot_failed" and "no_ip" in str(row.get("detail")):
                reports["no_ip"].append(at - started.pop(runner))
    slowest = max([*reports["boot_ip"], *reports["no_ip"]], default=None)
    return {"lane_logs": len(logs), "clones": clones,
            "boot_ip": _quantiles(reports["boot_ip"]), "no_ip": _quantiles(reports["no_ip"]),
            "suggested_verify_secs": (None if slowest is None
                                      else int(-(-2 * slowest // 60) * 60))}


def status(directory: pathlib.Path | None = None) -> dict[str, Any]:
    """For `tartci doctor fleet`: never a write, never a lock.

    An open breaker also carries bootpd's launchd state read now, so the
    doctor can tell a job that is not loaded from one that is loaded but
    silent: the two need different remedies.
    """
    path = (directory or breaker_dir()) / "breaker.json"
    if not path.exists():
        return {"state": "closed", "source": "absent"}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"state": "unreadable", "error": str(exc)}
    if not isinstance(value, dict) or value.get("state") not in STATES:
        return {"state": "unreadable", "error": "unexpected breaker shape"}
    if value.get("state") == "open":
        value["bootpd"] = bootpd_readout()
        value["pfd"] = pfd_readout()
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--lane", required=True)
    r = sub.add_parser("record")
    r.add_argument("--outcome", choices=["ip", "no_ip"], required=True)
    r.add_argument("--lane", required=True)
    r.add_argument("--vm", default="")
    sub.add_parser("probe-now")
    v = sub.add_parser("verify")
    v.add_argument("--reason", default="operator")
    b = sub.add_parser("boot-times")
    b.add_argument("--days", type=float, default=30)
    b.add_argument("--root", default=str(pathlib.Path.home() / ".tartci" / "state"))
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            print(json.dumps(status(), sort_keys=True))
            return 0
        if args.command == "boot-times":
            print(json.dumps(boot_times(pathlib.Path(args.root),
                                        time.time() - args.days * 86400), sort_keys=True))
            return 0
        if args.command == "probe-now":
            result = probe_now()
        elif args.command == "verify":
            result = verify(args.reason)
        else:
            result = check(args) if args.command == "check" else record(args)
    except Exception as exc:  # noqa: BLE001 - the caller fails open
        print(json.dumps({"action": "error", "error": str(exc), "events": []}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
