#!/usr/bin/env python3
"""Report a fleet host that is filling up or carrying VMs tartci did not make.

Two watchdog checks, both report-only (they never delete or stop anything):

disk floor
    The host's data volumes (the one holding $HOME and the one holding the
    VM store) must keep FLOOR_PCT of their size free and at least FLOOR_GB.
    Below either, one issue opens listing the top consumers with their sizes
    and the exact command to reclaim each. Agent directories are listed, never
    deleted: they hold other agents' work. m1 on 2026-10-16 was at 92% with
    ~/agent-scratch alone at 432 GB.

foreign VMs
    Every VM in the VM store and in Tart's default ~/.tart that tartci did not
    make, which is either running longer than FOREIGN_RUNNING_HOURS or stopped
    longer than FOREIGN_STOPPED_DAYS, goes into one issue with its name, age
    and size. On 2026-10-16 an agent's manual repro VM ran on m1 for 11 h
    unnoticed, and three two-month-old images sat in the stores.

Ownership is tart_image_prune.plan()'s, the one rule the image pruner already
tests: an image is reported only when that plan would delete it (so every
`:latest` tag, golden, Pulp vm-image manifest image, image a tartci file
names, and newest rollback image is kept), and a non-image VM only when it is
not this host's lane clone and nothing tartci installed names it. Anything
reported is listed, never touched.

Off unless ~/.config/tartci/host-hygiene.enabled exists, so it rolls out one
host at a time.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_off  # noqa: E402 - sibling module
import tartci_incident  # noqa: E402 - sibling module

GB = 1000 ** 3
FLOOR_PCT = 20.0
FLOOR_GB = 200.0
FOREIGN_RUNNING_HOURS = 4
FOREIGN_STOPPED_DAYS = 14
FOREIGN_INTERVAL_SECONDS = 3600
CONSUMERS_TTL_SECONDS = 6 * 3600
DU_TIMEOUT_SECONDS = 180
CLONE_RE = re.compile(r"^(?P<runner>.+)-(?P<pid>[0-9]+)-(?P<n>[0-9]+)$")
DISK_CLASS = "host_disk_floor"
FOREIGN_CLASS = "host_foreign_vms"


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _tartci_home() -> Path:
    return Path(os.environ.get("TARTCI_HOME") or os.path.expanduser("~/.tartci"))


def enabled(user_home: Path) -> bool:
    flag = os.environ.get("TARTCI_HOST_HYGIENE_ENABLE_FILE") or str(
        user_home / ".config" / "tartci" / "host-hygiene.enabled")
    return Path(flag).is_file()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _gb(n: float) -> str:
    return f"{n / GB:.0f} GB"


# ── disk floor ───────────────────────────────────────────────────────────────

def volumes(paths: list[Path], stat: Callable[[str], Any] = os.statvfs,
            device: Callable[[str], int] = lambda p: os.stat(p).st_dev) -> list[dict]:
    """Free/total for each distinct volume holding one of `paths`."""
    seen: dict[int, dict] = {}
    for path in paths:
        try:
            dev = device(str(path))
            st = stat(str(path))
        except OSError:
            continue
        if dev in seen:
            seen[dev]["paths"].append(str(path))
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        seen[dev] = {"paths": [str(path)], "total": total, "free": free,
                     "free_pct": (100.0 * free / total) if total else 0.0}
    return list(seen.values())


def breaches(vols: list[dict], floor_pct: float = FLOOR_PCT,
             floor_gb: float = FLOOR_GB) -> list[dict]:
    return [v for v in vols if v["free_pct"] < floor_pct or v["free"] < floor_gb * GB]


def default_consumers(user_home: Path, stores: list[Path]) -> list[tuple[Path, str]]:
    """(path, how to reclaim) for what usually fills a fleet host."""
    rows = [(user_home / "agent-scratch",
             f"du -sh {user_home}/agent-scratch/* | sort -h | tail -20   "
             "# remove only what its owner confirms is done"),
            (user_home / "agent-worktrees",
             f"du -sh {user_home}/agent-worktrees/* | sort -h | tail -20   "
             "# then `git worktree remove <path>` from its repository, never rm -rf"),
            (user_home / "Code" / "agent-worktrees",
             f"du -sh {user_home}/Code/agent-worktrees/* | sort -h | tail -20   "
             "# then `git worktree remove <path>` from its repository")]
    for store in stores:
        rows.append((store, f"TART_HOME={store} tart list   "
                            "# then `TART_HOME=" + str(store) + " tart delete <name>` "
                            "for a VM the foreign-VM issue lists"))
    rows.append((user_home / "Code", "tartci reclaim            # plan: regenerable "
                                      "build/ and build-cov* dirs; then `tartci reclaim --fix`"))
    return rows


TASKPOLICY = "/usr/sbin/taskpolicy"
MEASURE_DEADLINE_SECONDS = 300


def du_bytes(path: Path, timeout: float = DU_TIMEOUT_SECONDS) -> Optional[int]:
    """Bytes under `path`, or None when absent, unreadable or too slow to count.

    Run as a background task (`taskpolicy -b`: throttled CPU and disk I/O);
    `nice` alone does not lower disk I/O priority on macOS."""
    if not path.exists():
        return None
    argv = ["du", "-sk", str(path)]
    argv = [TASKPOLICY, "-b", *argv] if os.path.exists(TASKPOLICY) else ["nice", "-n", "20", *argv]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        return int(out.stdout.split()[0]) * 1024
    except (IndexError, ValueError):
        return None


def consumers(rows: list[tuple[Path, str]], measure: Callable[..., Optional[int]],
              cache: Path, now: float, deadline_seconds: float = MEASURE_DEADLINE_SECONDS,
              clock: Callable[[], float] = time.monotonic) -> list[dict]:
    """Sizes of `rows`, at most every CONSUMERS_TTL_SECONDS, within one overall
    deadline: each path gets what is left of it (and at most DU_TIMEOUT_SECONDS),
    so the heal pass is never held longer than the deadline."""
    cached = _read_json(cache)
    if cached.get("measured_at", 0) and 0 <= now - cached["measured_at"] < CONSUMERS_TTL_SECONDS:
        return cached.get("rows", [])
    out = []
    unmeasured = []
    end = clock() + deadline_seconds
    for path, how in rows:
        left = end - clock()
        if left <= 1:
            unmeasured.append(str(path))
            continue
        size = measure(path, timeout=min(DU_TIMEOUT_SECONDS, left))
        if size is not None:
            out.append({"path": str(path), "bytes": size, "reclaim": how})
    out.sort(key=lambda row: -row["bytes"])
    _write_json(cache, {"measured_at": now, "rows": out, "unmeasured": unmeasured})
    return out


def render_disk(host: str, since: str, low: list[dict], top: list[dict],
                detected: str) -> tuple[str, str]:
    worst = min(low, key=lambda v: v["free_pct"])
    lines = ["## What happened",
             f"`{host}` is below its disk floor ({FLOOR_PCT:g}% free and at least "
             f"{FLOOR_GB:g} GB free) since {since}:"]
    lines += [f"- `{', '.join(v['paths'])}`: {_gb(v['free'])} free of {_gb(v['total'])} "
              f"({v['free_pct']:.0f}%)" for v in low]
    lines += ["", "A full data volume denies every VM lease (`disk_capacity_exceeded`) and "
              "the host stops serving.", "", "## Top consumers", ""]
    lines += [f"- `{row['path']}`: {_gb(row['bytes'])}" for row in top] or ["- (none measured)"]
    lines += ["", "## Likely cause",
              "Agent scratch and worktree directories, VM images nobody deleted, and build "
              "trees. Nothing here deletes agent directories: they hold other agents' work.", "",
              "## Fix", "```sh"]
    lines += [row["reclaim"] for row in top] or ["df -h ~"]
    lines += ["```", "", "## Safe to auto-apply",
              "Only `tartci reclaim --fix` (regenerable build trees). Agent directories and "
              "VM images need their owner.", "", "## How to verify",
              f"`df -h {worst['paths'][0]}` shows at least {FLOOR_PCT:g}% and {FLOOR_GB:g} GB "
              "free; this issue then closes itself."]
    marker = tartci_incident.marker(host, DISK_CLASS, since, detected)
    return (f"[tartci] {host} is below its disk floor: {_gb(worst['free'])} free "
            f"({worst['free_pct']:.0f}%)", tartci_incident.with_marker("\n".join(lines), marker))


# ── foreign VMs ──────────────────────────────────────────────────────────────

def tart_list(store: Path) -> Optional[list[dict]]:
    tart = shutil.which("tart") or "/opt/homebrew/bin/tart"
    try:
        out = subprocess.run([tart, "list", "--format", "json"], capture_output=True, text=True,
                             timeout=30, check=False, env={**os.environ, "TART_HOME": str(store)})
        rows = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return rows if isinstance(rows, list) else None


def running_seconds(name: str, ps_out: str) -> Optional[float]:
    """How long `tart run ... <name>` has run, from `ps -axo etime=,command=`."""
    for line in ps_out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and re.search(r"(^|/)tart run\b", parts[1]) \
                and re.search(rf"(\s|^){re.escape(name)}(\s|$)", parts[1]):
            return _etime(parts[0])
    return None


def _etime(value: str) -> Optional[float]:
    days, _, clock = value.rpartition("-")
    parts = clock.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.insert(0, 0)
    return (int(days or 0) * 86400) + nums[0] * 3600 + nums[1] * 60 + nums[2]


def own_clone(name: str, host: str) -> bool:
    """This host's lane clone `<host>-<lane>-<pid>-<n>`: vm_reap.py's to judge."""
    match = CLONE_RE.match(name)
    return bool(match and match.group("runner").startswith(f"{host}-"))


def ownership_inputs(user_home: Path, profile_path: Path) -> Optional[dict]:
    """What tart_image_prune.plan() judges ownership by, read once; None when
    the fleet profile cannot be read (then nothing is judged foreign)."""
    import tart_image_prune as prune  # noqa: PLC0415 - sibling module; one ownership rule
    try:
        values, data = prune.profile_strings(profile_path)
    except (OSError, ValueError, RuntimeError):
        return None
    repo = (data.get("reclaim") or {}).get("repo")
    return {"profile_values": values,
            "manifest": prune.manifest_bases(Path(repo) if isinstance(repo, str) else None),
            "text": prune.reference_text([user_home / ".config/tartci", _tartci_home() / "state",
                                          user_home / "Library/LaunchAgents"])}


def foreign_vms(stores: list[Path], host: str, inputs: Optional[dict], now: float,
                listing: Callable[[Path], Optional[list[dict]]] = tart_list,
                ps_out: str = "",
                opened: Callable[[], Optional[list[str]]] = lambda: None
                ) -> tuple[list[dict], list[str]]:
    """(reportable VMs, stores that could not be judged).

    One ownership rule, tart_image_prune.plan()'s: an IMAGE (a tagged name, or
    one under a golden's base) is reported only when the plan would delete it,
    so every `:latest` tag, golden, manifest image, named or newest-rollback
    image is tartci's. A non-image VM is reported when it is not this host's
    lane clone, nothing tartci installed names it, and it has run longer than
    FOREIGN_RUNNING_HOURS or been stopped longer than FOREIGN_STOPPED_DAYS.
    """
    import tart_image_prune as prune  # noqa: PLC0415
    found, blind = [], []
    if inputs is None:
        return [], [f"{s} (fleet profile unreadable)" for s in stores]
    for store in stores:
        rows = listing(store)
        if rows is None:
            blind.append(str(store))
            continue

        def judged(open_list: Optional[list[str]]) -> dict:
            return prune.plan(rows, vms_dir=store / "vms", profile_values=inputs["profile_values"],
                              manifest=inputs["manifest"], text=inputs["text"],
                              opened=open_list, now=now, min_idle_days=FOREIGN_STOPPED_DAYS)

        report = judged([])
        if any(i["verdict"] == "delete" for i in report["images"]):
            # Confirm with the real open-file list only when something would be named.
            report = judged(opened())
        for image in report["images"]:
            if image["verdict"] != "delete":
                continue
            ts = prune.accessed_ts(image.get("accessed"))
            found.append({"name": image["name"], "store": str(store), "state": "stopped",
                          "size_gb": round(image["size_bytes"] / prune.GIB),
                          "age_hours": round((now - ts) / 3600, 1) if ts else 0.0,
                          "why": image["reason"]})
        for slot in report["slot_vms"]:
            name = slot["name"]
            if own_clone(name, host) or name in inputs["profile_values"] \
                    or prune.named_in(name, inputs["text"]):
                continue
            running = str(slot.get("state") or "").lower() == "running"
            if running:
                age = running_seconds(name, ps_out)
                if age is None or age < FOREIGN_RUNNING_HOURS * 3600:
                    continue
            else:
                try:
                    age = now - (store / "vms" / name).stat().st_mtime
                except OSError:
                    continue
                if age < FOREIGN_STOPPED_DAYS * 86400:
                    continue
            found.append({"name": name, "store": str(store),
                          "state": "running" if running else "stopped",
                          "size_gb": round(slot["size_bytes"] / prune.GIB),
                          "age_hours": round(age / 3600, 1), "why": "not tartci's"})
    found.sort(key=lambda e: (e["state"] != "running", -e["age_hours"]))
    return found, blind


def _age(hours: float) -> str:
    return f"{hours / 24:.0f} d" if hours >= 48 else f"{hours:.1f} h"


def render_foreign(host: str, since: str, vms: list[dict], detected: str) -> tuple[str, str]:
    lines = ["## What happened",
             f"`{host}` carries VMs nothing tartci uses: images the image pruner would "
             f"delete (unreferenced, idle over {FOREIGN_STOPPED_DAYS} days), and VMs tartci did "
             f"not make that have run over {FOREIGN_RUNNING_HOURS} h or been stopped over "
             f"{FOREIGN_STOPPED_DAYS} days. A running one takes cores and memory the lanes lease; "
             "a stopped one takes disk.",
             "", "## Evidence", "", "| VM | store | state | age | size | why |",
             "|---|---|---|---|---|---|"]
    lines += [f"| `{v['name']}` | `{v['store']}` | {v['state']} | {_age(v['age_hours'])} | "
              f"{v.get('size_gb', '?')} GB | {v.get('why', '')} |" for v in vms]
    lines += ["", "## Likely cause",
              "A manual or agent test VM that was never deleted (on m1, 2026-10-16, an agent's "
              "repro VM ran 11 h), or an image kept for a rollback that is no longer needed. "
              "Agents should use `tartci scratch-vm`, which deletes on a TTL.", "",
              "## Fix", "```sh"]
    for v in vms:
        stop = f"TART_HOME={v['store']} tart stop {v['name']}; " if v["state"] == "running" else ""
        lines.append(f"{stop}TART_HOME={v['store']} tart delete {v['name']}   # after its owner agrees")
    lines += ["```", "", "## Safe to auto-apply",
              "No. tartci cannot prove whose these are; it only reports them.", "",
              "## How to verify",
              "The next hourly check lists none of them; this issue then closes itself."]
    marker = tartci_incident.marker(host, FOREIGN_CLASS, since, detected)
    return (f"[tartci] {host} has {len(vms)} VM(s) nothing tartci uses",
            tartci_incident.with_marker("\n".join(lines), marker))


# ── the pass ─────────────────────────────────────────────────────────────────

def _host_id() -> str:
    import interpreter_alert  # noqa: PLC0415 - owns this host's id
    return interpreter_alert.host_id()


def _profile_path() -> Path:
    return Path(os.environ.get("TARTCI_MACOS_FLEET_PROFILE") or os.path.expanduser(
        "~/.config/tartci/macos-fleet-profile.toml"))


def _profile() -> Optional[dict]:
    try:
        import tomllib  # noqa: PLC0415 - the watchdog runs the pinned 3.11+
        with _profile_path().open("rb") as handle:
            return tomllib.load(handle)
    except Exception:  # noqa: BLE001 - no profile is no store from it
        return None


def stores(user_home: Path, profile: Optional[dict]) -> list[Path]:
    found = []
    declared = os.environ.get("TART_HOME") or ((profile or {}).get("host") or {}).get("tart_home")
    if isinstance(declared, str) and declared.startswith("/"):
        found.append(Path(declared))
    default = user_home / ".tart"
    if default not in found:
        found.append(default)
    return [s for s in found if s.is_dir()]


def alert_pass(now: Optional[float] = None, *, user_home: Optional[Path] = None,
               vols: Optional[Callable[[list[Path]], list[dict]]] = None,
               measure: Callable[..., Optional[int]] = du_bytes,
               listing: Callable[[Path], Optional[list[dict]]] = tart_list,
               ps: Optional[Callable[[], str]] = None,
               lsof: Optional[Callable[[], Optional[list[str]]]] = None,
               issue: Optional[Callable[..., Any]] = None,
               close: Optional[Callable[..., Any]] = None) -> list[str]:
    """One watchdog pass: the WARN lines to log."""
    now = time.time() if now is None else now
    user_home = user_home or Path(os.path.expanduser("~"))
    if not enabled(user_home):
        return []
    sdir = _tartci_home() / "state" / "host-hygiene"
    profile = _profile()
    host = _host_id()
    detected = _iso(now)
    out: list[str] = []
    store_paths = stores(user_home, profile)

    vol_rows = (vols or volumes)([user_home, *store_paths])
    low = breaches(vol_rows)
    disk_state = sdir / "disk-floor-alert.json"
    since = (host_off._read_json(disk_state) or {}).get("since") or detected
    top: list[dict] = []
    if low:
        top = consumers(default_consumers(user_home, store_paths), measure,
                        sdir / "consumers.json", now)[:8]
    host_off.episode_alert(
        disk_state, active=bool(low), resolved=not low, since=since if low else None,
        raise_event=lambda: host_off.event(sdir, DISK_CLASS, "below disk floor",
                                           {"volumes": low}, now),
        render=lambda: render_disk(host, since, low, top, detected), issue=issue, close=close)
    for v in low:
        out.append(f"{detected} launchd-watchdog: WARN host-disk: {', '.join(v['paths'])} "
                   f"{_gb(v['free'])} free ({v['free_pct']:.0f}%), below the floor "
                   f"({FLOOR_PCT:g}% and {FLOOR_GB:g} GB)")

    scan_state = sdir / "foreign-vms.json"
    last = _read_json(scan_state)
    if not (0 <= now - float(last.get("checked_at", 0)) < FOREIGN_INTERVAL_SECONDS):
        ps_text = (ps or _ps)()
        vms, blind = foreign_vms(store_paths, host, ownership_inputs(user_home, _profile_path()),
                                 now, listing, ps_text, opened=lsof or _open_paths)
        last = {"checked_at": now, "vms": vms, "blind": blind}
        _write_json(scan_state, last)
    vms = last.get("vms") or []
    fstate = sdir / "foreign-vms-alert.json"
    fsince = (host_off._read_json(fstate) or {}).get("since") or detected
    host_off.episode_alert(
        fstate, active=bool(vms), resolved=not vms, since=fsince if vms else None,
        raise_event=lambda: host_off.event(sdir, FOREIGN_CLASS, f"{len(vms)} foreign VM(s)",
                                           {"vms": [v["name"] for v in vms]}, now),
        render=lambda: render_foreign(host, fsince, vms, detected), issue=issue, close=close)
    if vms:
        out.append(f"{detected} launchd-watchdog: WARN foreign-vms: "
                   + "; ".join(f"{v['name']} {v['state']} {_age(v['age_hours'])}" for v in vms))
    for store in last.get("blind") or []:
        out.append(f"{detected} launchd-watchdog: WARN foreign-vms: could not list {store}")
    return out


def _open_paths() -> Optional[list[str]]:
    import tart_image_prune as prune  # noqa: PLC0415
    return prune.open_paths(subprocess.run)


def _ps() -> str:
    try:
        return subprocess.run(["/bin/ps", "-axo", "etime=,command="], capture_output=True,
                              text=True, timeout=15, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def main() -> int:
    for line in alert_pass():
        print(line, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
