#!/usr/bin/env python3
"""Whether pf holds an enable reference on a host that shares its network with VMs.

Tart's shared VM network is InternetSharing's: it creates bridge100 and runs
bootpd for the VMs, and before it does it asks pfd for packet-filter state.
When pf holds no enable reference, pfd logs "no pf starter references held"
and exits 3, so the VM network never appears and every VM boots without an
address. m5 lost its VM network that way after reboots on 2026-10-07 and
2026-10-09; on 10-09 Daniel installed a LaunchDaemon that takes a reference
at boot (`pfctl -E`), and the outage did not recur.

pf's own state (`pfctl -s info`, `pfctl -s References`) needs root. pfd's
launchd record does not, and it is the direct readout of this fault: pfd is
healthy (running, or idle-exited 0) while pf has a reference, and exits 3
while it has none. The breaker reads the same record when a VM gets no
address (vm_dhcp_breaker.pfd_readout); this reads it on every doctor run, so
a host with no reference is named before a lane spends a VM on it.

States:
  ok              pfd is healthy (running, or idle-exited 0)
  no_reference    pfd exits 3: pf holds no enable reference
  pfd_exiting     pfd keeps exiting with another code
  holder_missing  pfd is healthy now, but this host has lost pf's reference before
                  (the breaker's outage history records a pfd_crash_loop outage)
                  and no LaunchDaemon takes one at boot: the fault is proven to
                  recur at the next reboot here
  not_applicable  the host has no VM lanes, so there is no VM network to lose
  unknown         pfd's launchd record could not be read

Applicability is the host's VM lanes, never whether InternetSharing is
running: InternetSharing is launched on demand and idles when no VM is up (m5
read "not running" between jobs on 2026-10-09), so its absence says nothing
about the next VM. Its pid is kept as a fact.

Every state also says whether a LaunchDaemon takes a pf reference at boot
(`boot_holders`). On a host that has never lost the reference that is a fact,
not a problem: m3, m5s and m1 hold it some other way. On a host that has, it
is `holder_missing`. The remedy is scripts/install_pf_enable_ref.sh (sudo).
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path
from typing import Any

import vm_dhcp_breaker as vb

DAEMONS_DIR = Path("/Library/LaunchDaemons")


def boot_holders(directory: Path | None = None) -> list[str]:
    """LaunchDaemons that run `pfctl -E` at load: the labels that take a pf
    enable reference at boot. Unreadable plists are skipped."""
    directory = directory or Path(os.environ.get("TARTCI_PF_LAUNCHDAEMONS_DIR", str(DAEMONS_DIR)))
    found: list[str] = []
    try:
        paths = sorted(directory.glob("*.plist"))
    except OSError:
        return found
    for path in paths:
        try:
            value = plistlib.loads(path.read_bytes())
        except Exception:  # noqa: BLE001 - not ours to judge
            continue
        if not isinstance(value, dict) or not value.get("RunAtLoad"):
            continue
        argv = value.get("ProgramArguments") or ([value["Program"]] if value.get("Program") else [])
        words = " ".join(str(a) for a in argv).split()
        if any(w.endswith("pfctl") for w in words) and "-E" in words:
            found.append(str(value.get("Label") or path.stem))
    return found


def prior_episodes(breaker: dict[str, Any] | None) -> list[str]:
    """When this host's VM network was lost to a missing pf reference: the start
    of each pfd_crash_loop outage in the breaker's history (newest last), plus
    an open breaker with that cause."""
    breaker = breaker or {}
    rows = [row for row in breaker.get("outages") or [] if isinstance(row, dict)]
    if breaker.get("state") == "open":
        rows.append({"cause": breaker.get("cause"), "opened_at": breaker.get("opened_at")})
    out = []
    for row in rows:
        at = row.get("opened_at")
        if row.get("cause") == "pfd_crash_loop" and isinstance(at, (int, float)):
            out.append(vb.iso(float(at)))
    return out


def status(lanes: int, breaker: dict[str, Any] | None = None) -> dict[str, Any]:
    """The pf-reference state of a host with `lanes` VM lanes (see the module
    docstring). `breaker` is the VM-DHCP breaker's status, read when omitted."""
    holders = boot_holders()
    pfd = vb.pfd_readout()
    if breaker is None:
        try:
            breaker = vb.status(vb.breaker_dir())
        except Exception:  # noqa: BLE001 - no history is no evidence of a recurrence
            breaker = {}
    episodes = prior_episodes(breaker)
    out: dict[str, Any] = {"lanes": lanes, "sharing_pid": vb.sharing_pid(), "pfd": pfd,
                           "boot_holders": holders, "prior_episodes": episodes}
    if lanes <= 0:
        out["state"] = "not_applicable"
    elif not pfd:
        out["state"] = "unknown"
    elif vb.pfd_crash_looping(pfd):
        out["state"] = "no_reference" if str(pfd.get("last_exit")) == "3" else "pfd_exiting"
    elif episodes and not holders:
        out["state"] = "holder_missing"
    else:
        out["state"] = "ok"
    return out


def describe(value: dict[str, Any]) -> str:
    pfd = value.get("pfd") or {}
    holders = value.get("boot_holders") or []
    boot = (f"a pf reference is taken at boot by {', '.join(holders)}" if holders
            else "no LaunchDaemon takes a pf reference at boot")
    seen = (f"pfd state {pfd.get('state')}, last exit {pfd.get('last_exit')}, "
            f"{pfd.get('runs')} runs")
    state = value.get("state")
    if state == "not_applicable":
        return f"this host has no VM lanes, so there is no VM network to lose; {boot}"
    if state == "unknown":
        return f"pfd's launchd record (system/com.apple.pfd) could not be read; {boot}"
    if state == "no_reference":
        return (f"pf holds no enable reference: pfd exits 3 ({seen}), so InternetSharing "
                f"never creates the VM network and VMs boot without an address; {boot}")
    if state == "holder_missing":
        episodes = value.get("prior_episodes") or []
        return (f"pf holds an enable reference now ({seen}), but this host lost it before "
                f"({len(episodes)} pfd_crash_loop outage(s), latest {episodes[-1]}) and no "
                "LaunchDaemon takes one at boot, so the next reboot loses the VM network again")
    if state == "pfd_exiting":
        return (f"pfd keeps exiting ({seen}), so InternetSharing may not create the VM "
                f"network; {boot}")
    return f"pf holds an enable reference ({seen}); {boot}"
