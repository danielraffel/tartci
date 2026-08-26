#!/usr/bin/env python3
"""Render and validate Pulp's second event-class macOS gate supervisor."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import sys
from pathlib import Path
from typing import Any

from macos_runner_identity import resolve_plist_identity


PRIMARY_LABEL = "com.danielraffel.pulp.tart-runner-macos-gate"
SLOT2_LABEL = "com.danielraffel.pulp.tart-runner-macos-gate-slot2"
BASE_LABELS = (
    "self-hosted",
    "macOS",
    "ARM64",
    "pulp-build",
    "pulp-build-vm",
)
WORKFLOW_TIERS = (
    "pulp-build-merge-group|Build and Test\n"
    "pulp-build-pr-head|Build and Test"
)
CLASS_LABELS = "pulp-build-merge-group,pulp-build-pr-head"
LEGACY_LABEL = "pulp-gate-fast"
ABSOLUTE_PATH = (
    "/opt/homebrew/bin:/usr/local/bin:{home}/.local/bin:"
    "/usr/bin:/bin:/usr/sbin:/sbin"
)


def slot2_profile(home: str, tart_home: str, ccache_max_size: str = "40G") -> dict[str, Any]:
    state_dir = f"{home}/.tartci/state/macos-gate-slot2"
    log_path = f"{home}/Library/Logs/tartci/tart-runner-macos-gate-slot2.log"
    labels = ",".join(BASE_LABELS)
    return {
        "Label": SLOT2_LABEL,
        "ProgramArguments": [
            "/bin/bash",
            f"{home}/.local/bin/tartci",
            "serve",
            "macos",
            "--loop",
            "--slot",
            "2",
            "--labels",
            labels,
        ],
        "WorkingDirectory": home,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": {
            "HOME": home,
            "PATH": ABSOLUTE_PATH.format(home=home),
            "TARTCI_GH_CLI": "ghapp",
            "TARTCI_ADMISSION_CLEAN_MODE": "disabled",
            "TARTCI_LAUNCHD_LABEL": SLOT2_LABEL,
            "TART_HOME": tart_home,
            "TARTCI_HOME": f"{home}/.tartci",
            "TARTCI_CI_CACHE": f"{home}/.cache/pulp-ci",
            "TARTCI_CCACHE_MAX_SIZE": ccache_max_size,
            "TARTCI_RUNNER_REPO": "Generous-Corp/pulp",
            "TARTCI_MACOS_GOLDEN": "pulp-build-runner:latest",
            "TARTCI_RUNNER_LABELS": labels,
            "TARTCI_RUNNER_WORKFLOW_TIERS": WORKFLOW_TIERS,
            "TARTCI_RUNNER_ASSIGNMENT_MODE": "event-class-v2",
            "TARTCI_ASSIGNMENT_V2_OMIT_LABELS": LEGACY_LABEL,
            "TARTCI_ASSIGNMENT_V2_REQUIRED_OMIT_LABELS": LEGACY_LABEL,
            "TARTCI_ASSIGNMENT_V2_CLASS_LABELS": CLASS_LABELS,
            "TARTCI_RUNNER_SLOT": "2",
            "TARTCI_RUNNER_NAME_PREFIX": "pulp-macos-gate-slot2",
            "TARTCI_STATE_DIR": state_dir,
            "TARTCI_EVENT_LOG": f"{state_dir}/events.jsonl",
            "TARTCI_MACOS_LOGS": f"{home}/Library/Logs/tartci/macos-gate-slot2-jobs",
            "TARTCI_QUEUE_LANE_ID": "pulp-macos-gate-slot2",
            "TARTCI_MACOS_VM_CAP": "2",
            "TARTCI_MACOS_VM_CORES": "6",
            "TARTCI_MACOS_VM_MEM_MB": "8192",
        },
    }


def load_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        value = plistlib.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: plist root must be a dictionary")
    return value


def _expect(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def _normalize_path(value: Any, home: str) -> str:
    expanded = str(value).replace("$HOME", home)
    return os.path.abspath(os.path.expanduser(expanded))


def _effective_path(env: dict[str, Any], key: str, fallback: str) -> str:
    value = env.get(key) or fallback
    return _normalize_path(value, str(env.get("HOME", "")))


def validate_slot2(value: dict[str, Any], sibling: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    env = value.get("EnvironmentVariables")
    args = value.get("ProgramArguments")
    _expect(errors, isinstance(env, dict), "EnvironmentVariables must be a dictionary")
    _expect(errors, isinstance(args, list), "ProgramArguments must be an array")
    if not isinstance(env, dict) or not isinstance(args, list):
        return errors
    args = [str(item) for item in args]
    labels = ",".join(BASE_LABELS)
    required_env = {
        "TARTCI_LAUNCHD_LABEL": SLOT2_LABEL,
        "TARTCI_GH_CLI": "ghapp",
        "TARTCI_RUNNER_LABELS": labels,
        "TARTCI_RUNNER_WORKFLOW_TIERS": WORKFLOW_TIERS,
        "TARTCI_RUNNER_ASSIGNMENT_MODE": "event-class-v2",
        "TARTCI_ASSIGNMENT_V2_OMIT_LABELS": LEGACY_LABEL,
        "TARTCI_ASSIGNMENT_V2_REQUIRED_OMIT_LABELS": LEGACY_LABEL,
        "TARTCI_ASSIGNMENT_V2_CLASS_LABELS": CLASS_LABELS,
        "TARTCI_RUNNER_SLOT": "2",
        "TARTCI_MACOS_VM_CAP": "2",
        "TARTCI_MACOS_VM_CORES": "6",
        "TARTCI_MACOS_VM_MEM_MB": "8192",
    }
    _expect(errors, value.get("Label") == SLOT2_LABEL, f"Label must be {SLOT2_LABEL}")
    for key, expected in required_env.items():
        _expect(errors, env.get(key) == expected, f"{key} must be {expected!r}")
    _expect(errors, args[-4:] == ["--slot", "2", "--labels", labels],
            "ProgramArguments must end with the canonical slot and base labels")
    _expect(errors, LEGACY_LABEL not in labels, "legacy generic label leaked into runner labels")
    _expect(errors, all(LEGACY_LABEL not in str(item) for item in args),
            "legacy generic label leaked into ProgramArguments")
    _expect(errors, env.get("PATH") == ABSOLUTE_PATH.format(home=env.get("HOME", "")),
            "PATH must include absolute Homebrew, local-wrapper, and system paths")
    ccache_max_size = str(env.get("TARTCI_CCACHE_MAX_SIZE", ""))
    _expect(errors, re.fullmatch(r"[1-9][0-9]*[KMGT]", ccache_max_size) is not None,
            "TARTCI_CCACHE_MAX_SIZE must be a positive ccache size such as 40G")
    for key in ("TARTCI_STATE_DIR", "TARTCI_EVENT_LOG", "TARTCI_MACOS_LOGS", "TARTCI_QUEUE_LANE_ID", "TARTCI_RUNNER_NAME_PREFIX"):
        _expect(errors, bool(env.get(key)), f"{key} must be explicit")
    _expect(errors, value.get("StandardOutPath") == value.get("StandardErrorPath"),
            "stdout and stderr must share the canonical lane log")
    if sibling is not None:
        sibling_env = sibling.get("EnvironmentVariables")
        if not isinstance(sibling_env, dict):
            errors.append("primary sibling EnvironmentVariables must be a dictionary")
        else:
            _expect(errors, sibling.get("Label") == PRIMARY_LABEL,
                    f"primary sibling Label must be {PRIMARY_LABEL}")
            primary_tart_home = _effective_path(sibling_env, "TART_HOME", "")
            slot2_tart_home = _effective_path(env, "TART_HOME", "")
            _expect(errors, primary_tart_home == slot2_tart_home,
                    "both gate supervisors must share the same TART_HOME")
            try:
                primary_identity = resolve_plist_identity(sibling, hostname="profile-host")
                slot2_identity = resolve_plist_identity(value, hostname="profile-host")
                _expect(errors, primary_identity.runner_name != slot2_identity.runner_name,
                        "gate supervisors resolve to the same runner name")
                _expect(errors, primary_identity.state_file != slot2_identity.state_file,
                        "gate supervisors resolve to the same state file")
                primary_home = str(sibling_env.get("HOME", ""))
                slot2_home = str(env.get("HOME", ""))
                primary_cache = _effective_path(
                    sibling_env, "TARTCI_CI_CACHE", f"{primary_home}/.cache/pulp-ci"
                )
                slot2_cache = _effective_path(
                    env, "TARTCI_CI_CACHE", f"{slot2_home}/.cache/pulp-ci"
                )
                _expect(errors, primary_cache == slot2_cache,
                        "both gate supervisors must share TARTCI_CI_CACHE")
                primary_ccache_max = str(
                    sibling_env.get("TARTCI_CCACHE_MAX_SIZE") or "40G"
                )
                _expect(errors, primary_ccache_max == ccache_max_size,
                        "both gate supervisors must share TARTCI_CCACHE_MAX_SIZE")
                primary_queue = str(sibling_env.get(
                    "TARTCI_QUEUE_LANE_ID", f"{primary_identity.runner_name}-1"
                ))
                slot2_queue = str(env.get(
                    "TARTCI_QUEUE_LANE_ID", f"{slot2_identity.runner_name}-2"
                ))
                _expect(errors, primary_queue != slot2_queue,
                        "gate supervisors must not share TARTCI_QUEUE_LANE_ID")
                primary_event = _effective_path(
                    sibling_env, "TARTCI_EVENT_LOG",
                    f"{primary_identity.state_dir}/events.jsonl",
                )
                slot2_event = _effective_path(
                    env, "TARTCI_EVENT_LOG", f"{slot2_identity.state_dir}/events.jsonl"
                )
                _expect(errors, primary_event != slot2_event,
                        "gate supervisors must not share TARTCI_EVENT_LOG")
                primary_jobs = _effective_path(
                    sibling_env, "TARTCI_MACOS_LOGS",
                    f"{primary_home}/VMs/logs/tartci-macos",
                )
                slot2_jobs = _effective_path(
                    env, "TARTCI_MACOS_LOGS", f"{slot2_home}/VMs/logs/tartci-macos"
                )
                _expect(errors, primary_jobs != slot2_jobs,
                        "gate supervisors must not share TARTCI_MACOS_LOGS")
            except (TypeError, ValueError) as error:
                errors.append(f"could not resolve gate identities: {error}")
            primary_stdout = _normalize_path(sibling.get("StandardOutPath", ""), primary_home)
            slot2_stdout = _normalize_path(value.get("StandardOutPath", ""), slot2_home)
            primary_stderr = _normalize_path(sibling.get("StandardErrorPath", ""), primary_home)
            slot2_stderr = _normalize_path(value.get("StandardErrorPath", ""), slot2_home)
            _expect(errors, primary_stdout != slot2_stdout,
                    "gate supervisors must not share a launchd log")
            _expect(errors, primary_stderr != slot2_stderr,
                    "gate supervisors must not share a launchd error log")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci gate-slot2")
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--home", required=True)
    render.add_argument("--tart-home", required=True)
    render.add_argument("--ccache-max-size", default="40G")
    render.add_argument("--output", type=Path)
    validate = sub.add_parser("validate")
    validate.add_argument("plist", type=Path)
    validate.add_argument("--sibling", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            value = slot2_profile(args.home, args.tart_home, args.ccache_max_size)
            errors = validate_slot2(value)
            if errors:
                raise ValueError("; ".join(errors))
            if args.output:
                with args.output.open("wb") as destination:
                    plistlib.dump(value, destination, sort_keys=False)
            else:
                plistlib.dump(value, sys.stdout.buffer, sort_keys=False)
            return 0
        value = load_plist(args.plist)
        sibling = load_plist(args.sibling) if args.sibling else None
        errors = validate_slot2(value, sibling)
        if errors:
            for error in errors:
                print(f"gate-slot2: {error}", file=sys.stderr)
            return 2
        print(f"valid: label={SLOT2_LABEL} slot=2 assignment=event-class-v2")
        return 0
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        print(f"gate-slot2: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
