#!/usr/bin/env python3
"""tartci governor: show, set and explain this host's build governor.

One file holds every tuning knob: ~/.config/tartci/governor.toml (override
with TARTCI_GOVERNOR_FILE), a flat [governor] table. tartci host-profile
and the lease store derive every number from it (see host_profile.py,
"governor: one per-host config surface", and leases.py, "build classes and
dynamic gate lending").

    tartci governor show [--json]          resolved knobs, sources, derived budget
    tartci governor set KEY=VALUE ...      edit the file (validated), then show
    tartci governor unset KEY ...          drop keys back to their defaults
    tartci governor explain [--json]       what each class would get right now
    tartci governor fleet [--hosts a,b]    one read-only SSH show per host
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import host_profile
import leases

HEADER = """\
# tartci build governor for this host. Edit with tartci governor set KEY=VALUE.
# Every key is optional; an absent key (or "auto") takes the role default.
# tartci governor show prints the resolved values and where each came from.
"""

PROFILE_KEYS = (
    "role",
    "ncpu",
    "headroom_cores",
    "lease_capacity_cores",
    "reserved_gate_cores",
    "non_gate_capacity_cores",
    "serves_gate",
    "dynamic_lending",
    "gate_prompt_reserve_cores",
    "interactive_share_cores",
    "interactive_min_cores",
    "interactive_wait_secs",
    "background_share_cores",
    "qos",
    "agent_floor_cores",
    "agent_floor_pool_cores",
)


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def write_governor_file(path: Path, values: dict[str, Any]) -> None:
    lines = [HEADER, "[governor]"]
    for key in host_profile.GOVERNOR_KEYS:
        if key in values:
            lines.append(f"# {host_profile.GOVERNOR_HELP[key]}")
            lines.append(f"{key} = {_format_value(values[key])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def file_values(path: Path) -> dict[str, Any]:
    """The keys currently in the file, validated (invalid ones are dropped)."""
    try:
        table = host_profile._parse_governor_table(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    table.pop("__invalid__", None)
    values: dict[str, Any] = {}
    for key, raw in table.items():
        try:
            values[key] = host_profile.parse_governor_value(key, raw)
        except ValueError:
            continue
    return values


def show_payload() -> dict[str, Any]:
    governor = host_profile.governor_settings()
    profile = host_profile.build_profile()
    knobs = {}
    for key in host_profile.GOVERNOR_KEYS:
        knobs[key] = {
            "value": governor.values.get(key, "default"),
            "source": governor.sources.get(key, "role default"),
            "help": host_profile.GOVERNOR_HELP[key],
        }
    knobs["role"]["value"] = profile["role"]
    knobs["role"]["source"] = profile["role_source"]
    for key in host_profile.AGENT_FLOOR_KEYS:
        if key not in governor.values:
            knobs[key]["value"] = profile[key]
            knobs[key]["source"] = profile["agent_floor_source"]
    return {
        "schema": 1,
        "hostname": profile["host"]["hostname"],
        "governor_file": str(governor.path),
        "knobs": knobs,
        "derived": {key: profile.get(key) for key in PROFILE_KEYS},
        "problems": governor.problems,
    }


def show_text(payload: dict[str, Any]) -> str:
    derived = payload["derived"]
    lines = [
        f"host {payload['hostname']}  governor file {payload['governor_file']}",
        "",
        "knobs:",
    ]
    for key, row in payload["knobs"].items():
        value = row["value"]
        shown = _format_value(value) if value != "default" else "(default)"
        lines.append(f"  {key:28} {shown:16} {row['source']}")
    lines += [
        "",
        "derived budget (cores):",
        f"  ncpu {derived['ncpu']} - human {derived['headroom_cores']} = lease capacity T {derived['lease_capacity_cores']}",
        f"  static gate guarantee S {derived['reserved_gate_cores']}  ->  guaranteed non-gate N {derived['non_gate_capacity_cores']}",
        f"  serves gate lanes: {'yes' if derived['serves_gate'] else 'no'}  "
        f"dynamic lending: {'on' if derived['dynamic_lending'] else 'off'}  "
        f"prompt reserve P {derived['gate_prompt_reserve_cores']}",
        f"  interactive: share {derived['interactive_share_cores']}, min {derived['interactive_min_cores']}, "
        f"wait {derived['interactive_wait_secs']}s, normal QoS",
        f"  background: share {derived['background_share_cores']}, QoS {derived['qos']}, "
        f"floor {derived['agent_floor_cores']} (pool {derived['agent_floor_pool_cores']})",
    ]
    for problem in payload["problems"]:
        lines.append(f"  problem: {problem}")
    return "\n".join(lines)


def explain_payload() -> dict[str, Any]:
    """What an interactive, a background and a gate request would get now."""
    args = leases.parse_args(["status"])
    cfg = leases.capacity_config(args)
    store_dir = Path(args.store_dir).expanduser()
    with leases.locked_store(store_dir):
        records = leases.load_records(store_dir)
        active, _, _ = leases.reclaim(records, int(args.stale_secs))
        reserve, reserve_source = leases.prompt_reserve(store_dir, cfg)
    current = leases.usage(active, cfg)
    profile = host_profile.build_profile()

    def best(priority: int, build_class: str | None, want: int) -> int:
        for size in range(want, 0, -1):
            verdict = leases.core_and_memory_verdict(
                cfg, current, priority, size, size * cfg["per_job_mem_mb"],
                build_class=build_class, reserve=reserve,
            )
            if not (verdict["total_exceeded"] or verdict["class_exceeded"] or verdict["mem_exceeded"]):
                return size
        return 0

    gate_size = leases.read_gate_hint(store_dir) or int(profile["vm_pool_cores"])
    return {
        "capacity": current,
        "gate_prompt_reserve_cores": reserve,
        "gate_prompt_reserve_source": reserve_source,
        "interactive_limit_cores": leases.interactive_limit(active, cfg, reserve),
        "would_grant": {
            "interactive": best(leases.PRIORITY_CLASSES["build"], "interactive",
                                int(profile["interactive_build_jobs"])),
            "background": best(leases.PRIORITY_CLASSES["build"], "background",
                               int(profile["pulp_build_jobs"])),
            "classless": best(leases.PRIORITY_CLASSES["build"], None,
                              int(profile["pulp_build_jobs"])),
            "gate_vm": best(leases.PRIORITY_CLASSES["gate"], None, gate_size),
        },
        "requests": {
            "interactive": int(profile["interactive_build_jobs"]),
            "background": int(profile["pulp_build_jobs"]),
            "gate_vm": gate_size,
        },
    }


def explain_text(payload: dict[str, Any]) -> str:
    cap = payload["capacity"]
    grant = payload["would_grant"]
    req = payload["requests"]
    lines = [
        f"now: {cap['used_cores']}/{cap['total_cores']} cores leased "
        f"(gate {cap.get('gate_used_cores', '?')}, non-gate {cap['non_gate_used_cores']}, "
        f"lent {cap.get('lent_cores', 0)}, interactive {cap.get('interactive_used_cores', 0)}, "
        f"background {cap.get('background_used_cores', 0)})",
        f"prompt reserve for the next gate job: {payload['gate_prompt_reserve_cores']} "
        f"({payload['gate_prompt_reserve_source']})",
        f"interactive builds may grow non-gate use to {payload['interactive_limit_cores']}",
        "",
        f"an interactive build asking {req['interactive']} would get {grant['interactive']} (normal QoS)",
        f"a background build asking {req['background']} would get {grant['background']}"
        + (" (else the agent-floor lease)" if grant["background"] == 0 else ""),
        f"a class-less build asking {req['background']} would get {grant['classless']}",
        f"a gate VM asking {req['gate_vm']} would get {grant['gate_vm']}"
        + ("" if grant["gate_vm"] >= req["gate_vm"] else "  <-- gate would be DENIED"),
    ]
    if cap.get("preempted_lease_ids"):
        lines.append(f"preempted borrowers (background QoS until the gate finishes): "
                     f"{', '.join(cap['preempted_lease_ids'])}")
    return "\n".join(lines)


def cmd_set(pairs: list[str], unset: bool = False) -> int:
    path = host_profile.governor_file_path()
    try:
        unparseable = "__invalid__" in host_profile._parse_governor_table(
            path.read_text(encoding="utf-8")
        )
    except OSError:
        unparseable = False
    if unparseable:
        # Rewriting would silently drop every key in the broken file.
        print(f"governor: {path} is not valid TOML; fix or remove it first "
              "(the host is running on role defaults until then)", file=sys.stderr)
        return 2
    values = file_values(path)
    for item in pairs:
        if unset:
            if item not in host_profile.GOVERNOR_KEYS:
                print(f"governor: unknown key {item!r}", file=sys.stderr)
                return 2
            values.pop(item, None)
            continue
        key, sep, raw = item.partition("=")
        if not sep:
            print(f"governor: expected KEY=VALUE, got {item!r}", file=sys.stderr)
            return 2
        try:
            values[key.strip()] = host_profile.parse_governor_value(key.strip(), raw)
        except ValueError as exc:
            print(f"governor: {exc}", file=sys.stderr)
            return 2
    write_governor_file(path, values)
    print(show_text(show_payload()))
    return 0


def cmd_fleet(hosts: list[str], as_json: bool) -> int:
    rows = []
    for host in hosts:
        try:
            proc = subprocess.run(
                ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                 "PATH=$HOME/.local/bin:/opt/homebrew/bin:$PATH tartci governor show --json"],
                text=True, capture_output=True, timeout=45, check=False,
            )
            payload = json.loads(proc.stdout) if proc.returncode == 0 else None
            error = None if payload else (proc.stderr.strip().splitlines() or ["no output"])[-1]
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            payload, error = None, str(exc)
        rows.append({"host": host, "show": payload, "error": error})
    if as_json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    print(f"{'host':12} {'role':18} {'T':>3} {'S':>3} {'N':>3} {'gate':>4} {'lend':>4} "
          f"{'P':>3} {'int':>4} {'bg':>3} bgQoS")
    for row in rows:
        if row["show"] is None:
            print(f"{row['host']:12} unavailable: {row['error']}")
            continue
        d = row["show"]["derived"]
        print(f"{row['host']:12} {d['role']:18} {d['lease_capacity_cores']:>3} "
              f"{d['reserved_gate_cores']:>3} {d['non_gate_capacity_cores']:>3} "
              f"{'yes' if d['serves_gate'] else 'no':>4} {'on' if d['dynamic_lending'] else 'off':>4} "
              f"{d['gate_prompt_reserve_cores']:>3} {d['interactive_share_cores']:>4} "
              f"{d['background_share_cores']:>3} {d['qos']}")
    return 0 if all(row["show"] for row in rows) else 1


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="tartci governor")
    sub = parser.add_subparsers(dest="command")
    show = sub.add_parser("show", help="resolved knobs, sources and derived budget")
    show.add_argument("--json", action="store_true")
    setter = sub.add_parser("set", help="set KEY=VALUE pairs in the governor file")
    setter.add_argument("pairs", nargs="+")
    unsetter = sub.add_parser("unset", help="remove keys from the governor file")
    unsetter.add_argument("keys", nargs="+")
    explain = sub.add_parser("explain", help="what each build class would get now")
    explain.add_argument("--json", action="store_true")
    fleet = sub.add_parser("fleet", help="read governor show from fleet hosts over SSH")
    fleet.add_argument("--hosts", help="comma-separated SSH aliases (default: fleet_hosts knob)")
    fleet.add_argument("--json", action="store_true")
    sub.add_parser("keys", help="list every knob with its meaning")
    args = parser.parse_args(argv)
    if args.command is None:
        args.command, args.json = "show", False
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "show":
        payload = show_payload()
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else show_text(payload))
        return 0
    if args.command == "set":
        return cmd_set(args.pairs)
    if args.command == "unset":
        return cmd_set(args.keys, unset=True)
    if args.command == "explain":
        payload = explain_payload()
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else explain_text(payload))
        return 0
    if args.command == "fleet":
        raw = args.hosts or host_profile.governor_settings().get("fleet_hosts") or os.environ.get(
            "TARTCI_FLEET_HOSTS", ""
        )
        hosts = [host.strip() for host in str(raw).split(",") if host.strip()]
        if not hosts:
            print("governor fleet: pass --hosts a,b or set fleet_hosts", file=sys.stderr)
            return 2
        return cmd_fleet(hosts, args.json)
    if args.command == "keys":
        for key, kind in host_profile.GOVERNOR_KEYS.items():
            print(f"{key:28} {kind:6} {host_profile.GOVERNOR_HELP[key]}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
