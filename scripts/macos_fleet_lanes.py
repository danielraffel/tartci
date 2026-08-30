#!/usr/bin/env python3
"""Validate and render dormant, capability-driven macOS Tart fleet lanes.

The rendered LaunchAgents are staging artifacts. This command never installs,
loads, enables, drains, or otherwise mutates a host runner pool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import plistlib
import re
import sys
import tomllib
from pathlib import Path, PurePosixPath


SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED_BASE_LABELS = {"self-hosted", "macOS", "ARM64"}
LEASE_PRIORITIES = {"background", "build", "vm", "runner", "gate"}
TOP_KEYS = {"schema", "name", "host", "stacked_images", "lane"}
HOST_KEYS = {"id", "home", "tart_home", "cache_root", "log_root"}
STACKED_IMAGE_KEYS = {
    "enabled", "minimum_macos_major", "minimum_tart_version",
    "registry_username_file", "registry_token_file", "flat_rollback",
}
LANE_KEYS = {
    "id", "repo", "golden", "priority", "labels", "workflows", "tier",
    "runner_group_id", "min_queued_age_seconds", "replaces_launchd_labels",
    "jit_github_cli", "chrome_app_dir", "assignment_mode",
    "assignment_omit_labels", "supervisors",
}
TIER_KEYS = {"label", "workflow"}
LABEL = re.compile(r"^[A-Za-z0-9_.:-]+$")
REPLACED_AGENT = re.compile(
    r"^com[.]danielraffel[.](?:pulp[.]tart-runner|"
    r"[a-z0-9.-]+[.]tart-runner-[a-z0-9.-]+)$"
)


def fail(message: str) -> None:
    raise ValueError(message)


def load(path: Path) -> dict:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    if type(data.get("schema")) is not int or data["schema"] != 1:
        fail("schema must be 1")
    unknown = set(data) - TOP_KEYS
    if unknown:
        fail(f"unknown top-level keys: {sorted(unknown)}")
    if "name" in data and (
        not isinstance(data["name"], str) or not SAFE_ID.fullmatch(data["name"])
    ):
        fail("name must be a stable lowercase profile identifier")
    host = data.get("host") or {}
    if not isinstance(host, dict):
        fail("host must be a table")
    unknown = set(host) - HOST_KEYS
    if unknown:
        fail(f"unknown host keys: {sorted(unknown)}")
    lanes = data.get("lane") or []
    if not isinstance(lanes, list):
        fail("lane must be an array of tables")
    if not isinstance(host.get("id"), str) or not SAFE_ID.fullmatch(host["id"]):
        fail("host.id must be a stable lowercase fleet identifier")
    for key in ("home", "tart_home", "cache_root", "log_root"):
        value = host.get(key, "")
        if not isinstance(value, str):
            fail(f"host.{key} must be a string")
        if not value.startswith("/"):
            fail(f"host.{key} must be an absolute path")
    if host["tart_home"] == host["home"] or host["log_root"] == host["home"]:
        fail("Tart and log roots may not be the host home directory itself")
    stacked = data.get("stacked_images")
    if stacked is not None and not isinstance(stacked, dict):
        fail("stacked_images must be a table")
    if stacked is not None:
        unknown = set(stacked) - STACKED_IMAGE_KEYS
        if unknown:
            fail(f"unknown stacked_images keys: {sorted(unknown)}")
        if set(stacked) != STACKED_IMAGE_KEYS:
            fail("stacked_images must declare every rollout and rollback field")
        if type(stacked["enabled"]) is not bool:
            fail("stacked_images.enabled must be a boolean")
        if stacked["enabled"]:
            fail("stacked_images.enabled cannot be true before provider support and benchmark graduation")
        if type(stacked["minimum_macos_major"]) is not int or stacked["minimum_macos_major"] < 27:
            fail("stacked_images.minimum_macos_major must be an integer of at least 27")
        if (not isinstance(stacked["minimum_tart_version"], str)
                or not re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", stacked["minimum_tart_version"])):
            fail("stacked_images.minimum_tart_version must be a semantic version")
        secret_root = PurePosixPath(host["home"]) / ".config/pulp/secrets"
        secret_paths: list[PurePosixPath] = []
        for key in ("registry_username_file", "registry_token_file"):
            value = stacked[key]
            if not isinstance(value, str):
                fail(f"stacked_images.{key} must be an absolute host-local Pulp secret path")
            normalized = PurePosixPath(posixpath.normpath(value))
            try:
                relative = normalized.relative_to(secret_root)
            except ValueError:
                relative = None
            if (not normalized.is_absolute() or normalized.as_posix() != value
                    or relative is None or not relative.parts):
                fail(f"stacked_images.{key} must be an absolute host-local Pulp secret path")
            secret_paths.append(normalized)
        if secret_paths[0] == secret_paths[1]:
            fail("stacked_images registry username and token paths must be distinct")
        if not isinstance(stacked["flat_rollback"], str) or not stacked["flat_rollback"].strip():
            fail("stacked_images.flat_rollback must name the retained flat golden")
    if not lanes:
        fail("at least one [[lane]] is required")
    generated_labels: set[str] = set()
    for lane in lanes:
        if not isinstance(lane, dict):
            fail("each lane must be a table")
        supervisors = lane.get("supervisors", 1)
        if type(supervisors) is not int or supervisors not in (1, 2):
            fail(f"lane {lane.get('id', '')}: supervisors must be 1 or 2")
        for slot in range(1, supervisors + 1):
            generated_labels.add(
                f"com.danielraffel.tartci.tart-runner-macos-fleet."
                f"{host['id']}.{lane.get('id', '')}"
                f"{'' if slot == 1 else f'.slot{slot}'}"
            )
    seen: set[str] = set()
    replaced: set[str] = set()
    for lane in lanes:
        if not isinstance(lane, dict):
            fail("each lane must be a table")
        unknown = set(lane) - LANE_KEYS
        if unknown:
            fail(f"unknown lane keys: {sorted(unknown)}")
        lane_id = lane.get("id", "")
        if not isinstance(lane_id, str):
            fail("lane id must be a string")
        if not SAFE_ID.fullmatch(lane_id) or lane_id in seen:
            fail(f"lane id is invalid or duplicated: {lane_id!r}")
        seen.add(lane_id)
        if not isinstance(lane.get("repo"), str) or not REPO.fullmatch(lane["repo"]):
            fail(f"lane {lane_id}: repo must be OWNER/REPO")
        runner_group_id = lane.get("runner_group_id")
        if type(runner_group_id) is not int or runner_group_id <= 1:
            fail(
                f"lane {lane_id}: runner_group_id must be an explicit "
                "non-Default GitHub runner group integer"
            )
        labels = lane.get("labels") or []
        if (not isinstance(labels, list) or not labels
                or not all(isinstance(value, str) and value for value in labels)
                or not all(LABEL.fullmatch(value) for value in labels)
                or len(labels) != len(set(labels))
                or not REQUIRED_BASE_LABELS.issubset(labels)):
            fail(f"lane {lane_id}: labels must be unique and include {sorted(REQUIRED_BASE_LABELS)}")
        if not isinstance(lane.get("golden"), str) or not lane["golden"].strip():
            fail(f"lane {lane_id}: golden must be a nonempty string")
        age = lane.get("min_queued_age_seconds", 0)
        if type(age) is not int or age < 0:
            fail(f"lane {lane_id}: min_queued_age_seconds must be a nonnegative integer")
        priority = lane.get("priority", "gate")
        if not isinstance(priority, str) or priority not in LEASE_PRIORITIES:
            fail(f"lane {lane_id}: unsupported lease priority")
        supervisors = lane.get("supervisors", 1)
        if type(supervisors) is not int or supervisors not in (1, 2):
            fail(f"lane {lane_id}: supervisors must be 1 or 2")
        assignment_mode = lane.get("assignment_mode")
        if assignment_mode is not None and assignment_mode != "event-class-v2":
            fail(f"lane {lane_id}: unsupported assignment_mode")
        omit_labels = lane.get("assignment_omit_labels", [])
        if (not isinstance(omit_labels, list)
                or not all(isinstance(value, str) and LABEL.fullmatch(value)
                           for value in omit_labels)
                or len(omit_labels) != len(set(omit_labels))):
            fail(f"lane {lane_id}: assignment_omit_labels must contain unique labels")
        jit_github_cli = lane.get("jit_github_cli")
        if jit_github_cli is not None and (
                not isinstance(jit_github_cli, str)
                or not SAFE_ID.fullmatch(jit_github_cli)):
            fail(
                f"lane {lane_id}: jit_github_cli must be a secret-free executable name"
            )
        chrome_app_dir = lane.get("chrome_app_dir")
        if chrome_app_dir is not None:
            if lane["repo"] != "Generous-Corp/forge":
                fail(f"lane {lane_id}: chrome_app_dir is restricted to the Forge lane")
            if not isinstance(chrome_app_dir, str):
                fail(f"lane {lane_id}: chrome_app_dir must be an absolute Google Chrome.app path")
            normalized_chrome = PurePosixPath(posixpath.normpath(chrome_app_dir))
            if (not normalized_chrome.is_absolute()
                    or normalized_chrome.as_posix() != chrome_app_dir
                    or normalized_chrome.name != "Google Chrome.app"
                    or any(char in chrome_app_dir for char in ":\r\n")):
                fail(f"lane {lane_id}: chrome_app_dir must be an absolute Google Chrome.app path")
        workflows = lane.get("workflows") or []
        tiers = lane.get("tier") or []
        if workflows and (not isinstance(workflows, list)
                          or not all(isinstance(value, str) and value.strip()
                                     and not any(char in value for char in "\r\n|")
                                     for value in workflows)):
            fail(f"lane {lane_id}: workflows must be a nonempty string array")
        if tiers and not isinstance(tiers, list):
            fail(f"lane {lane_id}: tier must be an array of tables")
        if bool(workflows) == bool(tiers):
            fail(f"lane {lane_id}: declare exactly one of workflows or [[lane.tier]]")
        replacements = (
            lane["replaces_launchd_labels"]
            if "replaces_launchd_labels" in lane
            else []
        )
        if (not isinstance(replacements, list)
                or not all(isinstance(value, str)
                           and REPLACED_AGENT.fullmatch(value)
                           for value in replacements)
                or len(replacements) != len(set(replacements))):
            fail(f"lane {lane_id}: replaces_launchd_labels must contain unique owned LaunchAgent labels")
        overlap = replaced.intersection(replacements)
        if overlap:
            fail(f"replacement LaunchAgent labels must be unique across lanes: {sorted(overlap)}")
        generated_overlap = generated_labels.intersection(replacements)
        if generated_overlap:
            fail(f"replacement LaunchAgent labels may not name rendered fleet agents: {sorted(generated_overlap)}")
        replaced.update(replacements)
        for tier in tiers:
            if not isinstance(tier, dict):
                fail(f"lane {lane_id}: each tier must be a table")
            unknown = set(tier) - TIER_KEYS
            if unknown:
                fail(f"lane {lane_id}: unknown tier keys: {sorted(unknown)}")
            label = tier.get("label", "")
            workflow = tier.get("workflow", "")
            if (not isinstance(label, str) or not SAFE_ID.fullmatch(label)
                    or not isinstance(workflow, str) or not workflow.strip()
                    or any(char in workflow for char in "\r\n|")):
                fail(f"lane {lane_id}: invalid workflow tier")
            if label in labels:
                fail(f"lane {lane_id}: tier label must be exclusive, not a base label")
        if assignment_mode == "event-class-v2":
            class_labels = [tier["label"] for tier in tiers]
            if class_labels != ["pulp-build-merge-group", "pulp-build-pr-head"]:
                fail(
                    f"lane {lane_id}: event-class-v2 requires merge-group then PR-head tiers"
                )
            if "pulp-gate-fast" not in omit_labels:
                fail(f"lane {lane_id}: event-class-v2 must omit pulp-gate-fast")
    return data


def rendered_plists(data: dict) -> dict[str, bytes]:
    host_id = data["host"]["id"]
    result: dict[str, bytes] = {}
    for lane in data["lane"]:
        for slot in range(1, lane.get("supervisors", 1) + 1):
            suffix = "" if slot == 1 else f".slot{slot}"
            name = (
                f"com.danielraffel.tartci.tart-runner-macos-fleet."
                f"{host_id}.{lane['id']}{suffix}.plist"
            )
            result[name] = plistlib.dumps(
                lane_plist(data, lane, slot=slot), sort_keys=False
            )
    return result


def replacements(data: dict) -> list[str]:
    return [label for lane in data["lane"] for label in lane.get("replaces_launchd_labels", [])]


def write_receipt(config: Path, agents_dir: Path, output: Path) -> None:
    data = load(config)
    expected = rendered_plists(data)
    digests: dict[str, str] = {}
    for name, body in expected.items():
        installed = agents_dir / name
        if installed.is_symlink() or not installed.is_file() or installed.read_bytes() != body:
            fail(f"installed plist does not match rendered profile: {installed}")
        digests[name] = hashlib.sha256(body).hexdigest()
    installed_names = {
        path.name for path in agents_dir.glob(
            "com.danielraffel.tartci.tart-runner-macos-fleet.*.plist"
        )
    }
    if installed_names != set(expected):
        fail("installed fleet plist set does not exactly match the profile")
    for label in replacements(data):
        if (agents_dir / f"{label}.plist").exists():
            fail(f"declared legacy LaunchAgent is still installable: {label}")
    receipt = {
        "schema": 1,
        "profile": data.get("name", config.stem),
        "config_path": str(config.resolve()),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "agents_dir": str(agents_dir.resolve()),
        "plists": digests,
        "retired_launchd_labels": replacements(data),
    }
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def verify_receipt(path: Path, config: Path, agents_dir: Path) -> None:
    try:
        receipt = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"could not read install receipt {path}: {exc}")
    if not isinstance(receipt, dict) or receipt.get("schema") != 1:
        fail("install receipt schema must be 1")
    config = config.resolve()
    agents_dir = agents_dir.resolve()
    if (Path(receipt.get("config_path", "")).resolve() != config
            or Path(receipt.get("agents_dir", "")).resolve() != agents_dir):
        fail("install receipt paths do not match the canonical installed paths")
    if not config.is_file() or not agents_dir.is_dir():
        fail("install receipt config_path and agents_dir must exist")
    if hashlib.sha256(config.read_bytes()).hexdigest() != receipt.get("config_sha256"):
        fail("installed fleet profile digest does not match its receipt")
    data = load(config)
    expected = rendered_plists(data)
    recorded = receipt.get("plists")
    if not isinstance(recorded, dict) or set(recorded) != set(expected):
        fail("install receipt plist set does not match the profile")
    installed_names = {
        installed.name for installed in agents_dir.glob(
            "com.danielraffel.tartci.tart-runner-macos-fleet.*.plist"
        )
    }
    if installed_names != set(expected):
        fail("installed fleet plist set does not exactly match the profile")
    for name, body in expected.items():
        installed = agents_dir / name
        digest = hashlib.sha256(body).hexdigest()
        if (recorded.get(name) != digest or installed.is_symlink()
                or not installed.is_file() or installed.read_bytes() != body):
            fail(f"installed fleet plist failed receipt verification: {installed}")
    expected_retired = replacements(data)
    if receipt.get("retired_launchd_labels") != expected_retired:
        fail("install receipt retired-label set does not match the profile")
    for label in expected_retired:
        if (agents_dir / f"{label}.plist").exists():
            fail(f"declared legacy LaunchAgent became installable again: {label}")


def lane_plist(data: dict, lane: dict, *, slot: int = 1) -> dict:
    host = data["host"]
    lane_id = lane["id"]
    suffix = "" if slot == 1 else f".slot{slot}"
    identity = lane_id if slot == 1 else f"{lane_id}-slot{slot}"
    label = (
        f"com.danielraffel.tartci.tart-runner-macos-fleet."
        f"{host['id']}.{lane_id}{suffix}"
    )
    state = f"{host['home']}/.tartci/state/macos-fleet/{identity}"
    env = {
        "HOME": host["home"],
        "PATH": f"/opt/homebrew/bin:/usr/local/bin:{host['home']}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "TART_HOME": host["tart_home"],
        "TARTCI_HOME": f"{host['home']}/.tartci",
        "TARTCI_GH_CLI": "ghapp",
        "TARTCI_LAUNCHD_LABEL": label,
        "TARTCI_RUNNER_REPO": lane["repo"],
        "TARTCI_RUNNER_GROUP_ID": str(lane["runner_group_id"]),
        "TARTCI_RUNNER_LABELS": ",".join(lane["labels"]),
        "TARTCI_MACOS_GOLDEN": lane["golden"],
        "TARTCI_VM_LEASE_PRIORITY": lane.get("priority", "gate"),
        "TARTCI_CI_CACHE": host["cache_root"],
        "TARTCI_STATE_DIR": state,
        "TARTCI_EVENT_LOG": f"{state}/events.jsonl",
        "TARTCI_MACOS_LOGS": f"{host['log_root']}/macos-fleet-jobs/{identity}",
        "TARTCI_QUEUE_LANE_ID": f"{host['id']}-{identity}",
        "TARTCI_SHARED_QUEUE_CACHE": f"{host['home']}/.tartci/state/queue-discovery.json",
        "TARTCI_RUNNER_NAME_PREFIX": f"{host['id']}-{identity}",
        "TARTCI_RUNNER_SLOT": str(slot),
        "TARTCI_ADMISSION_CLEAN_MODE": "required",
        "TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS": str(lane.get("min_queued_age_seconds", 0)),
    }
    if lane.get("tier"):
        env["TARTCI_RUNNER_WORKFLOW_TIERS"] = "\n".join(
            f"{row['label']}|{row['workflow']}" for row in lane["tier"]
        )
    else:
        env["TARTCI_RUNNER_WORKFLOW_NAMES"] = "\n".join(lane["workflows"])
    if "jit_github_cli" in lane:
        env["TARTCI_JIT_GH_CLI"] = lane["jit_github_cli"]
    if "chrome_app_dir" in lane:
        env["TARTCI_RUNNER_CHROME_APP_DIR"] = lane["chrome_app_dir"]
    if "assignment_mode" in lane:
        omit = ",".join(lane.get("assignment_omit_labels", []))
        class_labels = ",".join(row["label"] for row in lane["tier"])
        env["TARTCI_RUNNER_ASSIGNMENT_MODE"] = lane["assignment_mode"]
        env["TARTCI_ASSIGNMENT_V2_OMIT_LABELS"] = omit
        env["TARTCI_ASSIGNMENT_V2_REQUIRED_OMIT_LABELS"] = omit
        env["TARTCI_ASSIGNMENT_V2_CLASS_LABELS"] = class_labels
    return {
        "Label": label,
        "ProgramArguments": [
            "/bin/bash", f"{host['home']}/.local/bin/tartci", "serve", "macos", "--loop"
        ],
        "WorkingDirectory": host["home"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": f"{host['log_root']}/macos-fleet-{identity}.log",
        "StandardErrorPath": f"{host['log_root']}/macos-fleet-{identity}.log",
        "ProcessType": "Background",
        "EnvironmentVariables": env,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci fleet-macos")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "render"):
        cmd = sub.add_parser(name)
        cmd.add_argument("config", type=Path)
        if name == "render":
            cmd.add_argument("--output", required=True, type=Path)
    receipt = sub.add_parser("write-receipt")
    receipt.add_argument("config", type=Path)
    receipt.add_argument("--agents-dir", required=True, type=Path)
    receipt.add_argument("--output", required=True, type=Path)
    verify = sub.add_parser("verify-installed")
    verify.add_argument("receipt", type=Path)
    verify.add_argument("--config", required=True, type=Path)
    verify.add_argument("--agents-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "verify-installed":
            verify_receipt(args.receipt, args.config, args.agents_dir)
            print(f"verified installed macOS fleet receipt: {args.receipt}")
            return 0
        data = load(args.config)
        if args.command == "validate":
            print(f"valid: host={data['host']['id']} lanes={len(data['lane'])} activation=unchanged")
            return 0
        if args.command == "write-receipt":
            write_receipt(args.config, args.agents_dir, args.output)
            print(args.output)
            return 0
        args.output.mkdir(parents=True, exist_ok=True)
        for name, body in rendered_plists(data).items():
            target = args.output / name
            target.write_bytes(body)
            print(target)
        print("rendered only: no LaunchAgent was installed, loaded, enabled, or activated", file=sys.stderr)
        return 0
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"fleet-macos: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
