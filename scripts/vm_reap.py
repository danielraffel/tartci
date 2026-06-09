#!/usr/bin/env python3
"""Safe local janitor for tartci macOS Tart runners.

The janitor is intentionally conservative:
  * VM deletion requires a positive state-file ownership marker.
  * The VM or runner name must match an allowed CI prefix.
  * Protected names and golden/tagged images are never deleted.
  * GitHub runner deletion only touches stale offline registrations whose
    names match an allowed CI prefix and are not backed by a fresh live
    supervisor heartbeat.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
from typing import Any


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S %z"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            pass
    return None


def run(
    argv: list[str],
    *,
    check: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=check,
        capture_output=True,
        text=text,
    )


def run_json(argv: list[str]) -> Any:
    proc = run(argv)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{' '.join(argv)} returned invalid JSON: {exc}") from exc


def starts_with_any(name: str, prefixes: list[str]) -> bool:
    return any(name.startswith(prefix) for prefix in prefixes if prefix)


def is_protected_name(name: str, protected: list[str]) -> bool:
    if not name:
        return True
    if ":" in name:
        return True
    if name in protected:
        return True
    if name.startswith("bench-") or name.endswith("-bench") or "-bench-" in name:
        return True
    if name.endswith(":latest"):
        return True
    return False


def pid_start(pid: int) -> str:
    proc = run(["ps", "-p", str(pid), "-o", "lstart="])
    if proc.returncode != 0:
        return ""
    return " ".join(proc.stdout.strip().split())


def pid_alive_same(pid_value: Any, expected_start: Any) -> bool:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    current = pid_start(pid)
    if not current:
        return False
    if isinstance(expected_start, str) and expected_start.strip():
        return current == " ".join(expected_start.strip().split())
    return True


def state_files(state_dir: pathlib.Path) -> list[pathlib.Path]:
    if not state_dir.exists():
        return []
    return sorted(state_dir.glob("*.state.json"))


def load_states(state_dir: pathlib.Path, now: dt.datetime) -> tuple[list[dict[str, Any]], list[str]]:
    states: list[dict[str, Any]] = []
    problems: list[str] = []
    for path in state_files(state_dir):
        try:
            with path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"state_unreadable:{path}:{exc}")
            continue
        state["_path"] = str(path)
        parsed_ts = parse_ts(state.get("ts"))
        if parsed_ts is None:
            try:
                parsed_ts = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
            except OSError:
                parsed_ts = now
        state["_age_secs"] = max(0, int((now - parsed_ts).total_seconds()))
        state["_owner_pid_alive"] = pid_alive_same(
            state.get("supervisor_pid"),
            state.get("supervisor_pid_started_at"),
        )
        states.append(state)
    return states, problems


def tart_vms() -> list[dict[str, Any]]:
    data = run_json(["tart", "list", "--format", "json", "--source", "local"])
    if not isinstance(data, list):
        return []
    return data


def vm_name(vm: dict[str, Any]) -> str:
    return str(vm.get("Name") or vm.get("name") or "")


def vm_state(vm: dict[str, Any]) -> str:
    return str(vm.get("State") or vm.get("state") or "")


def is_running_state(state: str) -> bool:
    return state.lower().startswith("run")


def macos_running_count(vms: list[dict[str, Any]]) -> int:
    count = 0
    for vm in vms:
        state = vm_state(vm)
        if not is_running_state(state):
            continue
        name = vm_name(vm)
        os_name = ""
        if name:
            try:
                detail = run_json(["tart", "get", name, "--format", "json"])
                os_name = str(detail.get("OS", "")).lower()
            except Exception:
                os_name = ""
        if os_name in ("", "darwin", "macos"):
            count += 1
    return count


def github_runners(repo: str) -> list[dict[str, Any]]:
    data = run_json(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/runners?per_page=100",
            "--paginate",
            "--slurp",
        ]
    )
    runners: list[dict[str, Any]] = []
    if isinstance(data, dict):
        runners = data.get("runners") or []
    elif isinstance(data, list):
        for page in data:
            if isinstance(page, dict):
                runners.extend(page.get("runners") or [])
    return runners


def delete_vm(name: str, running: bool) -> list[str]:
    fixed: list[str] = []
    if running:
        run(["tart", "stop", name])
        fixed.append(f"vm_stopped:{name}")
    run(["tart", "delete", name], check=True)
    fixed.append(f"vm_deleted:{name}")
    return fixed


def delete_runner(repo: str, runner_id: Any, runner_name: str) -> str:
    run(["gh", "api", "-X", "DELETE", f"repos/{repo}/actions/runners/{runner_id}"], check=True)
    return f"github_runner_deleted:{runner_name}:{runner_id}"


def unlink_state(path_text: str) -> str:
    pathlib.Path(path_text).unlink(missing_ok=True)
    return f"state_deleted:{path_text}"


def build_digest(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    now = utcnow()
    problems: list[str] = []
    fixed: list[str] = []
    unreadable: list[str] = []

    state_dir = pathlib.Path(args.state_dir).expanduser()
    prefixes = [p for p in args.prefixes.split(",") if p]
    protected = [p for p in args.protected_names.split(",") if p]

    for tool in ("tart", "gh"):
        if shutil.which(tool) is None:
            unreadable.append(f"missing_tool:{tool}")

    states, state_problems = load_states(state_dir, now)
    problems.extend(state_problems)
    states_by_vm = {
        str(state.get("vm") or ""): state
        for state in states
        if str(state.get("vm") or "")
    }
    states_by_runner = {
        str(state.get("runner") or ""): state
        for state in states
        if str(state.get("runner") or "")
    }
    vms: list[dict[str, Any]] = []
    runners: list[dict[str, Any]] = []
    capacity = {"macos_cap": args.macos_cap, "running_macos_vms": None, "free": None}

    if not unreadable:
        try:
            raw_vms = tart_vms()
        except Exception as exc:  # noqa: BLE001
            raw_vms = []
            unreadable.append(f"tart_unreadable:{exc}")
        else:
            running_macos = macos_running_count(raw_vms)
            capacity = {
                "macos_cap": args.macos_cap,
                "running_macos_vms": running_macos,
                "free": max(0, args.macos_cap - running_macos),
            }
            seen_vms: set[str] = set()
            for vm in raw_vms:
                name = vm_name(vm)
                if not name:
                    continue
                seen_vms.add(name)
                state_text = vm_state(vm)
                state = states_by_vm.get(name)
                protected_name = is_protected_name(name, protected)
                prefix_ok = starts_with_any(name, prefixes)
                state_runner = str((state or {}).get("runner") or "")
                state_vm = str((state or {}).get("vm") or "")
                state_provider = str((state or {}).get("provider") or "")
                marker_ok = bool(state) and state_vm == name
                owned = (
                    marker_ok
                    and prefix_ok
                    and not protected_name
                    and (state_provider in ("", "tart-macos"))
                    and (not state_runner or starts_with_any(state_runner, prefixes))
                )
                age = int((state or {}).get("_age_secs") or 0)
                owner_pid_alive = bool((state or {}).get("_owner_pid_alive")) if state else None
                action = ""
                stale = False
                running = is_running_state(state_text)
                if owned and not running and age >= args.stopped_age_secs:
                    stale = True
                    action = "delete_stopped_vm"
                elif owned and running and not owner_pid_alive and age >= args.running_age_secs:
                    stale = True
                    action = "stop_delete_ownerless_running_vm"
                elif owned and running and owner_pid_alive and age >= args.heartbeat_stale_secs:
                    stale = True
                    action = "suspect_live_owner_stale_heartbeat"
                    problems.append(f"suspect_live_owner_stale_heartbeat:{name}")
                elif marker_ok and not owned and prefix_ok and protected_name:
                    problems.append(f"owned_marker_protected_name:{name}")
                row = {
                    "name": name,
                    "state": state_text,
                    "owned": owned,
                    "protected": protected_name,
                    "owner_pid_alive": owner_pid_alive,
                    "age_secs": age,
                    "stale": stale,
                    "action": action,
                    "state_file": (state or {}).get("_path"),
                }
                vms.append(row)
                if args.fix and action in ("delete_stopped_vm", "stop_delete_ownerless_running_vm"):
                    try:
                        fixed.extend(delete_vm(name, running))
                        if state and state.get("_path"):
                            fixed.append(unlink_state(str(state.get("_path"))))
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"fix_failed:{action}:{name}:{exc}")
            for state in states:
                state_vm = str(state.get("vm") or "")
                path_text = str(state.get("_path") or "")
                if not state_vm or state_vm in seen_vms:
                    continue
                prefix_ok = starts_with_any(state_vm, prefixes)
                protected_name = is_protected_name(state_vm, protected)
                marker_ok = prefix_ok and not protected_name
                owner_pid_alive = bool(state.get("_owner_pid_alive"))
                action = ""
                stale = False
                if marker_ok and not owner_pid_alive:
                    stale = True
                    action = "delete_stale_state"
                    if args.fix:
                        try:
                            fixed.append(unlink_state(path_text))
                        except Exception as exc:  # noqa: BLE001
                            problems.append(f"fix_failed:delete_stale_state:{path_text}:{exc}")
                vms.append(
                    {
                        "name": state_vm,
                        "state": "missing",
                        "owned": marker_ok,
                        "protected": protected_name,
                        "owner_pid_alive": owner_pid_alive,
                        "age_secs": int(state.get("_age_secs") or 0),
                        "stale": stale,
                        "action": action,
                        "state_file": path_text,
                    }
                )
        if not unreadable:
            try:
                runners = github_runners(args.repo)
            except Exception as exc:  # noqa: BLE001
                unreadable.append(f"github_unreadable:{exc}")
            else:
                for runner in runners:
                    name = str(runner.get("name") or "")
                    if not starts_with_any(name, prefixes):
                        continue
                    state = states_by_runner.get(name)
                    state_age = int((state or {}).get("_age_secs") or 0) if state else None
                    state_provider = str((state or {}).get("provider") or "")
                    owner_pid_alive = bool((state or {}).get("_owner_pid_alive")) if state else None
                    state_file = (state or {}).get("_path")
                    live_fresh_supervisor = (
                        bool(state)
                        and state_provider in ("", "tart-macos")
                        and owner_pid_alive is True
                        and state_age is not None
                        and state_age < args.heartbeat_stale_secs
                    )
                    status = str(runner.get("status") or "")
                    busy = bool(runner.get("busy"))
                    action = ""
                    stale = False
                    if status == "offline" and not busy:
                        if live_fresh_supervisor:
                            action = "wait_for_live_supervisor"
                        elif state and owner_pid_alive is True:
                            stale = True
                            action = "suspect_live_owner_stale_offline_runner"
                            problems.append(f"suspect_live_owner_stale_offline_runner:{name}")
                        else:
                            stale = True
                            action = "delete_offline_runner"
                        if args.fix:
                            if action == "delete_offline_runner":
                                try:
                                    fixed.append(delete_runner(args.repo, runner.get("id"), name))
                                except Exception as exc:  # noqa: BLE001
                                    problems.append(f"fix_failed:delete_offline_runner:{name}:{exc}")
                    elif status == "offline" and busy:
                        stale = True
                        action = "offline_busy_wait_for_github"
                        problems.append(f"offline_busy_runner:{name}")
                    runner["owned"] = True
                    runner["stale"] = stale
                    runner["action"] = action
                    runner["heartbeat_age_secs"] = state_age
                    runner["owner_pid_alive"] = owner_pid_alive
                    runner["state_file"] = state_file

    digest = {
        "ts": iso(now),
        "host": socket.gethostname(),
        "config": {
            "repo": args.repo,
            "state_dir": str(state_dir),
            "prefixes": prefixes,
            "protected_names": protected,
            "fix": args.fix,
            "stopped_age_secs": args.stopped_age_secs,
            "running_age_secs": args.running_age_secs,
            "heartbeat_stale_secs": args.heartbeat_stale_secs,
        },
        "capacity": capacity,
        "supervisors": [
            {
                "runner": state.get("runner"),
                "vm": state.get("vm"),
                "phase": state.get("phase"),
                "ts": state.get("ts"),
                "heartbeat_age_secs": state.get("_age_secs"),
                "supervisor_pid": state.get("supervisor_pid"),
                "supervisor_pid_started_at": state.get("supervisor_pid_started_at"),
                "owner_pid_alive": state.get("_owner_pid_alive"),
                "state_file": state.get("_path"),
            }
            for state in states
        ],
        "vms": vms,
        "github_runners": [
            {
                "id": runner.get("id"),
                "name": runner.get("name"),
                "status": runner.get("status"),
                "busy": runner.get("busy"),
                "labels": [label.get("name") for label in runner.get("labels", []) if isinstance(label, dict)],
                "owned": runner.get("owned", False),
                "stale": runner.get("stale", False),
                "action": runner.get("action", ""),
                "heartbeat_age_secs": runner.get("heartbeat_age_secs"),
                "owner_pid_alive": runner.get("owner_pid_alive"),
                "state_file": runner.get("state_file"),
            }
            for runner in runners
            if runner.get("owned")
        ],
        "problems": sorted(set(problems + unreadable)),
        "fixed": fixed,
    }
    if unreadable:
        return digest, 2
    if digest["problems"]:
        return digest, 1
    if any(row.get("stale") and row.get("action") for row in vms) and not args.fix:
        return digest, 1
    if any(row.get("stale") and row.get("action") for row in digest["github_runners"]) and not args.fix:
        return digest, 1
    return digest, 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report or safely reap tartci macOS CI residue")
    parser.add_argument("--json", action="store_true", help="emit JSON digest")
    parser.add_argument("--fix", action="store_true", help="perform safe fixes")
    parser.add_argument("--repo", default=os.environ.get("TARTCI_RUNNER_REPO", "danielraffel/pulp"))
    parser.add_argument("--state-dir", default=os.environ.get("TARTCI_STATE_DIR", str(pathlib.Path.home() / ".tartci/state/macos")))
    parser.add_argument("--prefixes", default=os.environ.get("TARTCI_REAP_PREFIXES", "pulp-vm-,tartci-"))
    parser.add_argument("--protected-names", default=os.environ.get("TARTCI_REAP_PROTECTED_NAMES", "pulp-vm,rosetta-probe"))
    parser.add_argument("--macos-cap", type=int, default=int(os.environ.get("TARTCI_MACOS_VM_CAP", "2")))
    parser.add_argument("--stopped-age-secs", type=int, default=int(os.environ.get("TARTCI_REAP_STOPPED_AGE_SECS", "900")))
    parser.add_argument("--running-age-secs", type=int, default=int(os.environ.get("TARTCI_REAP_RUNNING_AGE_SECS", "10800")))
    parser.add_argument("--heartbeat-stale-secs", type=int, default=int(os.environ.get("TARTCI_REAP_HEARTBEAT_STALE_SECS", "900")))
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    digest, rc = build_digest(args)
    if args.json:
        print(json.dumps(digest, indent=2, sort_keys=True))
    else:
        print(f"tartci reap — host={digest['host']} problems={len(digest['problems'])} fixed={len(digest['fixed'])}")
        for problem in digest["problems"]:
            print(f"  problem: {problem}")
        for item in digest["fixed"]:
            print(f"  fixed: {item}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
