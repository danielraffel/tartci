#!/usr/bin/env python3
"""Keep a failed gate VM for debugging without keeping its slot.

Off by default (`[debug_hold] enabled = false`); turning it on is a host
behaviour change, so it is on per host only with Daniel's OK.

When a served job fails, the lane (providers/tart-macos/debug-hold.lib.sh)
asks `admit`. On a hold it first proves the runner is gone (the guest's JIT
config and runner service removed, and GitHub listing no runner of that name;
otherwise the VM is deleted as usual, fail closed), then stops the VM
(`tart stop`, disk kept, no guest change), renames it `held-<...>` and
releases its lease and slot. `record` writes held/<name>.json.

A stopped VM uses no slot: Apple's two-running-guest limit counts running
guests, and tart_inventory counts only those. `inspect` boots a held VM
without shares (a restored rw share is ENOENT; plain boots have no shares to
remount), waits for SSH (never `tart ip`, which answers from a stale lease)
and prints the ssh command; it is refused when no slot is free and counts as
a slot only while running. `expire` (the watchdog pass) stops one that was
left running past `inspect_idle_minutes`, and deletes one at `ttl_hours` or
when its PR is merged or closed (one batched GraphQL read per repo, at most
every PR_READ_SECS).

`held-` is a declared prefix: vm_reap, tart_image_prune and the inventory
know it by name (HELD_PREFIX) instead of relying on it being outside the CI
prefixes. A new failure never evicts an older hold.

Suspend (`tart run --suspendable`) is not used: it changes every gate VM's
device set for every job. A lane may opt into it only after its parity canary
passes, and its restore path then needs the virtiofs remount and the cpu/mem
pin (planning 2026-10-08-vm-saved-state-trial.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None  # type: ignore[assignment]

TABLE = "debug_hold"
HELD_PREFIX = "held-"
SETTINGS_KEYS = {"enabled", "ttl_hours", "max_per_host", "min_free_gb", "inspect_idle_minutes"}
DEFAULTS = {"enabled": False, "ttl_hours": 24, "max_per_host": 3, "inspect_idle_minutes": 60}
PR_READ_SECS = 900
SSH_WAIT_SECS = 180
GIB = 1 << 30


def is_held(name: str) -> bool:
    return isinstance(name, str) and name.startswith(HELD_PREFIX)


def held_name(vm: str) -> str:
    return HELD_PREFIX + re.sub(r"[^A-Za-z0-9.-]", "-", vm)[:80]


# ── settings ────────────────────────────────────────────────────────────────

def validate_table(table: Any) -> List[str]:
    """Problems with a `[debug_hold]` table; empty when acceptable. Shared by
    the profile validator and the runtime reader."""
    if not isinstance(table, dict):
        return ["debug_hold must be a table"]
    problems = []
    unknown = set(table) - SETTINGS_KEYS
    if unknown:
        problems.append(f"unknown debug_hold keys: {sorted(unknown)}")
    enabled = table.get("enabled", False)
    if type(enabled) is not bool:
        problems.append("debug_hold.enabled must be a boolean")
    for key in ("ttl_hours", "max_per_host", "min_free_gb", "inspect_idle_minutes"):
        value = table.get(key)
        if value is not None and (type(value) is not int or value <= 0):
            problems.append(f"debug_hold.{key} must be a positive integer")
    if enabled is True and table.get("min_free_gb") is None:
        # No default: it is the host's disk floor plus the measured size of a
        # held gate VM (docs/runbook.md, "Holding a failed gate VM").
        problems.append("debug_hold.min_free_gb is required when enabled = true")
    return problems


def profile_path() -> Path:
    return Path(os.environ.get("TARTCI_FLEET_PROFILE", str(
        Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml")))


def load_settings(profile: Optional[Path] = None) -> Tuple[Optional[Dict[str, Any]], str]:
    """(settings, why). None means unreadable, which holds nothing."""
    profile = profile or profile_path()
    if not profile.exists():
        return dict(DEFAULTS), f"no fleet profile at {profile}"
    if tomllib is None:
        return None, "no tomllib (needs Python 3.11+); cannot read the fleet profile"
    try:
        data = tomllib.loads(profile.read_text())
    except (OSError, ValueError) as exc:
        return None, f"cannot read {profile}: {exc}"
    table = data.get(TABLE)
    if table is None:
        return dict(DEFAULTS), f"no [{TABLE}] table"
    problems = validate_table(table)
    if problems:
        return None, "; ".join(problems)
    return {**DEFAULTS, **table}, f"[{TABLE}] in {profile}"


# ── state ───────────────────────────────────────────────────────────────────

def state_dir() -> Path:
    override = os.environ.get("TARTCI_DEBUG_HOLD_DIR")
    if override:
        return Path(override)
    home = os.environ.get("TARTCI_HOME") or str(Path.home() / ".tartci")
    return Path(home) / "state" / "debug-hold"


def _read(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def records(directory: Optional[Path] = None) -> List[Dict[str, Any]]:
    directory = directory or state_dir()
    out = []
    for path in sorted((directory / "held").glob("*.json")) if (directory / "held").is_dir() else []:
        value = _read(path)
        if value and is_held(str(value.get("name"))):
            out.append(value)
    return out


def event(directory: Path, name: str, detail: str, fields: Optional[Dict[str, Any]] = None,
          now: Optional[float] = None) -> None:
    row: Dict[str, Any] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
        time.time() if now is None else now)), "event": name, "detail": detail}
    if fields:
        row["fields"] = fields
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError:
        pass


# ── effects ─────────────────────────────────────────────────────────────────

class System:
    """Every external effect, so tests run the logic against fakes."""

    def run(self, argv: List[str], timeout: float = 60) -> Tuple[int, str, str]:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                  check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return 255, "", str(exc)
        return proc.returncode, proc.stdout, proc.stderr

    def spawn(self, argv: List[str], log: Path) -> int:
        with log.open("a") as handle:
            proc = subprocess.Popen(argv, stdout=handle, stderr=handle, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
        return proc.pid

    def free_bytes(self, path: str) -> Optional[int]:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def tart_vms(sys_: System) -> Optional[Dict[str, str]]:
    """name -> state for local VMs, or None when tart cannot be read."""
    rc, out, _ = sys_.run(["tart", "list", "--format", "json"], timeout=30)
    try:
        rows = json.loads(out) if rc == 0 else None
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        return None
    return {str(r.get("Name")): str(r.get("State") or "").lower() for r in rows
            if isinstance(r, dict) and r.get("Source", "local") == "local"}


def slot_cap() -> int:
    path = Path(os.environ.get("TARTCI_MACOS_VM_CAP_FILE", str(
        Path.home() / ".config" / "tartci" / "macos-vm-cap")))
    try:
        value = int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 2
    return max(1, min(value, 2))


# ── admission ───────────────────────────────────────────────────────────────

def admit(settings: Optional[Dict[str, Any]], held: List[Dict[str, Any]],
          free: Optional[int]) -> Tuple[bool, str]:
    """Whether a failed VM may be held now. A new failure never evicts an
    older hold: at the cap it is refused."""
    if settings is None:
        return False, "settings_unreadable"
    if not settings.get("enabled"):
        return False, "disabled"
    if len(held) >= int(settings["max_per_host"]):
        return False, f"cap max_per_host={settings['max_per_host']} held={len(held)}"
    if free is None:
        return False, "free_disk_unknown"
    need = int(settings["min_free_gb"]) * GIB
    if free < need:
        return False, f"disk free_gb={free // GIB} min_free_gb={settings['min_free_gb']}"
    return True, "ok"


# ── PR state ────────────────────────────────────────────────────────────────

def pr_numbers(run: Dict[str, Any]) -> List[int]:
    """The PRs a workflow run belongs to: its pull_requests, or the PR a merge
    queue branch names (gh-readonly-queue/<base>/pr-<n>-<sha>)."""
    out = [int(p["number"]) for p in run.get("pull_requests") or []
           if isinstance(p, dict) and isinstance(p.get("number"), int)]
    match = re.search(r"/pr-(\d+)-", str(run.get("head_branch") or ""))
    if match:
        out.append(int(match.group(1)))
    return sorted(set(out))


def gh_cli() -> str:
    return os.environ.get("TARTCI_GH_CLI") or "gh"


def pr_states(sys_: System, repo: str, numbers: List[int]) -> Optional[Dict[int, str]]:
    """{number: OPEN|MERGED|CLOSED} in one GraphQL read, or None on failure."""
    if not numbers:
        return {}
    owner, _, name = repo.partition("/")
    fields = " ".join(f"p{n}: pullRequest(number: {n}) {{ state }}" for n in sorted(set(numbers)))
    query = f'query {{ repository(owner: "{owner}", name: "{name}") {{ {fields} }} }}'
    rc, out, _ = sys_.run([gh_cli(), "api", "graphql", "-f", f"query={query}"], timeout=60)
    try:
        repo_value = json.loads(out)["data"]["repository"] if rc == 0 else None
    except (ValueError, KeyError, TypeError):
        repo_value = None
    if not isinstance(repo_value, dict):
        return None
    return {int(k[1:]): str((v or {}).get("state") or "") for k, v in repo_value.items()
            if k.startswith("p") and k[1:].isdigit()}


# ── operations ──────────────────────────────────────────────────────────────

def cmd_admit(args: argparse.Namespace, sys_: System, directory: Path) -> int:
    settings, why = load_settings()
    ok, reason = admit(settings, records(directory), sys_.free_bytes(args.tart_home))
    print(f"hold {held_name(args.vm)}" if ok else f"refused {reason}")
    return 0 if ok else 1


def cmd_record(args: argparse.Namespace, sys_: System, directory: Path) -> int:
    now = sys_.now()
    settings, _ = load_settings()
    ttl = int((settings or DEFAULTS)["ttl_hours"])
    run: Dict[str, Any] = {}
    if args.repo and args.run_id:
        rc, out, _ = sys_.run([gh_cli(), "api", f"repos/{args.repo}/actions/runs/{args.run_id}"])
        try:
            run = json.loads(out) if rc == 0 else {}
        except ValueError:
            run = {}
    value = {"name": args.name, "vm": args.vm, "lane": args.lane, "repo": args.repo,
             "run_id": args.run_id, "job_id": args.job_id, "head_sha": run.get("head_sha"),
             "prs": pr_numbers(run), "created_at": now, "expires_at": now + ttl * 3600,
             "mechanism": "stop"}
    _write(directory / "held" / f"{args.name}.json", value)
    event(directory, "debug_hold_kept", f"{args.name} from {args.vm}",
          {k: value[k] for k in ("name", "vm", "lane", "run_id", "job_id", "prs")}, now)
    print(json.dumps(value, sort_keys=True))
    return 0


def delete_held(sys_: System, directory: Path, name: str, reason: str) -> bool:
    if not is_held(name):
        print(f"refused: {name} is not a held VM (no {HELD_PREFIX} prefix)", file=sys.stderr)
        return False
    vms = tart_vms(sys_)
    if vms is None:
        return False
    if name in vms:
        sys_.run(["tart", "stop", name], timeout=60)
        rc, _, err = sys_.run(["tart", "delete", name], timeout=120)
        if rc != 0:
            event(directory, "debug_hold_delete_failed", f"{name}: {err.strip()[:200]}",
                  {"name": name, "reason": reason}, sys_.now())
            return False
    (directory / "held" / f"{name}.json").unlink(missing_ok=True)
    event(directory, "debug_hold_deleted", f"{name}: {reason}", {"name": name, "reason": reason},
          sys_.now())
    return True


def stop_held(sys_: System, directory: Path, name: str) -> bool:
    if not is_held(name):
        return False
    rc, _, _ = sys_.run(["tart", "stop", name], timeout=60)
    path = directory / "held" / f"{name}.json"
    value = _read(path)
    if value is not None:
        value.pop("inspecting_since", None)
        _write(path, value)
    return rc == 0


def cmd_inspect(args: argparse.Namespace, sys_: System, directory: Path) -> int:
    name = args.name
    path = directory / "held" / f"{name}.json"
    value = _read(path)
    if not is_held(name) or value is None:
        print(f"refused: {name} is not a held VM", file=sys.stderr)
        return 2
    vms = tart_vms(sys_)
    if vms is None:
        print("refused: tart list unreadable, so slot use is unknown", file=sys.stderr)
        return 3
    running = sum(1 for state in vms.values() if state.startswith("run"))
    if not vms.get(name, "").startswith("run"):
        if running >= slot_cap():
            print(f"refused: no VM slot free ({running}/{slot_cap()} running); retry when a "
                  "lane is idle", file=sys.stderr)
            return 4
        # No --dir shares: the held VM inspects its own disk, and a plain boot
        # has no share to remount. It never registers: its runner service and
        # JIT config were removed before it was kept, and nothing mints one.
        sys_.spawn(["tart", "run", "--no-graphics", name], directory / f"{name}.run.log")
        value["inspecting_since"] = sys_.now()
        _write(path, value)
    user = os.environ.get("TARTCI_VM_USER", "admin")
    key = os.environ.get("TARTCI_VM_SSH_KEY", str(Path.home() / ".ssh" / "id_ed25519"))
    deadline = sys_.now() + SSH_WAIT_SECS
    while sys_.now() < deadline:
        # The address is only trusted once SSH answers on it: `tart ip` can
        # report the stale lease of the run that failed.
        rc, ip, _ = sys_.run(["tart", "ip", name], timeout=10)
        ip = ip.strip()
        if rc == 0 and ip:
            ok, _, _ = sys_.run(["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                                 "-o", "StrictHostKeyChecking=no", "-o",
                                 "UserKnownHostsFile=/dev/null", "-i", key, f"{user}@{ip}",
                                 "true"], timeout=15)
            if ok == 0:
                print(f"ssh -i {key} {user}@{ip}")
                print(f"stop it when done: tartci held stop {name} (stopped automatically "
                      f"after {(load_settings()[0] or DEFAULTS)['inspect_idle_minutes']} min)")
                return 0
        sys_.sleep(2)
    print(f"{name} did not answer SSH within {SSH_WAIT_SECS}s; stopping it", file=sys.stderr)
    stop_held(sys_, directory, name)
    return 5


def expire(sys_: System, directory: Path, settings: Optional[Dict[str, Any]] = None
           ) -> Dict[str, Any]:
    """The watchdog pass. Stops inspections left running, deletes at TTL or
    when every PR a hold belongs to is merged or closed."""
    now = sys_.now()
    settings = settings or load_settings()[0] or dict(DEFAULTS)
    held = records(directory)
    out: Dict[str, Any] = {"held": len(held), "stopped": [], "deleted": [], "pr_read": False}
    if not held:
        return out
    idle = int(settings.get("inspect_idle_minutes") or DEFAULTS["inspect_idle_minutes"]) * 60
    vms = tart_vms(sys_)
    states: Dict[Tuple[str, int], str] = {}
    marker = directory / "pr-read.json"
    last = _read(marker) or {}
    if now - float(last.get("ts") or 0) >= PR_READ_SECS:
        wanted: Dict[str, List[int]] = {}
        for value in held:
            if value.get("repo") and value.get("prs"):
                wanted.setdefault(str(value["repo"]), []).extend(int(n) for n in value["prs"])
        for repo, numbers in wanted.items():
            got = pr_states(sys_, repo, numbers)
            if got is not None:
                states.update({(repo, n): s for n, s in got.items()})
        out["pr_read"] = bool(wanted)
        _write(marker, {"ts": now, "states": {f"{r}#{n}": s for (r, n), s in states.items()}})
    else:
        for key, state in (last.get("states") or {}).items():
            repo, _, number = key.rpartition("#")
            if number.isdigit():
                states[(repo, int(number))] = state
    for value in held:
        name = str(value["name"])
        prs = [int(n) for n in value.get("prs") or []]
        closed = bool(prs) and all(states.get((str(value.get("repo")), n)) in ("MERGED", "CLOSED")
                                   for n in prs)
        if now >= float(value.get("expires_at") or 0):
            reason = "ttl"
        elif closed:
            reason = "pr_closed"
        elif vms is not None and name not in vms:
            reason = "vm_gone"
        else:
            reason = None
        if reason:
            if delete_held(sys_, directory, name, reason):
                out["deleted"].append({"name": name, "reason": reason})
            continue
        since = value.get("inspecting_since")
        if (vms is not None and vms.get(name, "").startswith("run")
                and isinstance(since, (int, float)) and now - since >= idle):
            if stop_held(sys_, directory, name):
                out["stopped"].append(name)
    return out


def cmd_list(args: argparse.Namespace, sys_: System, directory: Path) -> int:
    held = records(directory)
    vms = tart_vms(sys_) or {}
    if args.json:
        print(json.dumps([{**v, "state": vms.get(v["name"], "missing")} for v in held],
                         sort_keys=True))
        return 0
    if not held:
        print("no held VMs")
    for v in held:
        left = (float(v.get("expires_at") or 0) - sys_.now()) / 3600
        print(f"{v['name']}  {vms.get(v['name'], 'missing')}  run {v.get('run_id')} "
              f"job {v.get('job_id')}  PR {','.join(map(str, v.get('prs') or [])) or '-'}  "
              f"expires in {left:.1f} h")
    return 0


def main(argv: Optional[List[str]] = None, sys_: Optional[System] = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci held", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("admit")
    p.add_argument("--vm", required=True)
    p.add_argument("--tart-home", default=os.environ.get("TART_HOME", str(Path.home() / ".tart")))
    p = sub.add_parser("record")
    for key in ("name", "vm", "lane", "repo", "run-id", "job-id"):
        p.add_argument(f"--{key}", default="")
    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    for verb in ("inspect", "stop", "delete"):
        sub.add_parser(verb).add_argument("name")
    sub.add_parser("expire")
    args = parser.parse_args(argv)
    sys_ = sys_ or System()
    directory = state_dir()
    if args.cmd == "admit":
        return cmd_admit(args, sys_, directory)
    if args.cmd == "record":
        return cmd_record(args, sys_, directory)
    if args.cmd == "list":
        return cmd_list(args, sys_, directory)
    if args.cmd == "inspect":
        return cmd_inspect(args, sys_, directory)
    if args.cmd == "stop":
        return 0 if stop_held(sys_, directory, args.name) else 1
    if args.cmd == "delete":
        return 0 if delete_held(sys_, directory, args.name, "operator") else 1
    print(json.dumps(expire(sys_, directory), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
