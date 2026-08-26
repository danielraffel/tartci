#!/usr/bin/env python3
"""Validate and render dormant, capability-driven macOS Tart fleet lanes.

The rendered LaunchAgents are staging artifacts. This command never installs,
loads, enables, drains, or otherwise mutates a host runner pool.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import sys
import tomllib
from pathlib import Path


SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED_BASE_LABELS = {"self-hosted", "macOS", "ARM64"}
LEASE_PRIORITIES = {"background", "build", "vm", "runner", "gate"}
TOP_KEYS = {"schema", "name", "host", "lane"}
HOST_KEYS = {"id", "home", "tart_home", "cache_root", "log_root"}
LANE_KEYS = {
    "id", "repo", "golden", "priority", "labels", "workflows", "tier",
    "min_queued_age_seconds",
}
TIER_KEYS = {"label", "workflow"}
LABEL = re.compile(r"^[A-Za-z0-9_.:-]+$")


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
    if not lanes:
        fail("at least one [[lane]] is required")
    seen: set[str] = set()
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
    return data


def lane_plist(data: dict, lane: dict) -> dict:
    host = data["host"]
    lane_id = lane["id"]
    label = f"com.danielraffel.tartci.tart-runner-macos-fleet.{host['id']}.{lane_id}"
    state = f"{host['home']}/.tartci/state/macos-fleet/{lane_id}"
    env = {
        "HOME": host["home"],
        "PATH": f"/opt/homebrew/bin:/usr/local/bin:{host['home']}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "TART_HOME": host["tart_home"],
        "TARTCI_HOME": f"{host['home']}/.tartci",
        "TARTCI_GH_CLI": "ghapp",
        "TARTCI_LAUNCHD_LABEL": label,
        "TARTCI_RUNNER_REPO": lane["repo"],
        "TARTCI_RUNNER_LABELS": ",".join(lane["labels"]),
        "TARTCI_MACOS_GOLDEN": lane["golden"],
        "TARTCI_VM_LEASE_PRIORITY": lane.get("priority", "gate"),
        "TARTCI_CI_CACHE": host["cache_root"],
        "TARTCI_STATE_DIR": state,
        "TARTCI_EVENT_LOG": f"{state}/events.jsonl",
        "TARTCI_MACOS_LOGS": f"{host['log_root']}/macos-fleet-jobs/{lane_id}",
        "TARTCI_QUEUE_LANE_ID": f"{host['id']}-{lane_id}",
        "TARTCI_SHARED_QUEUE_CACHE": f"{host['home']}/.tartci/state/queue-discovery.json",
        "TARTCI_RUNNER_NAME_PREFIX": f"{host['id']}-{lane_id}",
        "TARTCI_ADMISSION_CLEAN_MODE": "required",
        "TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS": str(lane.get("min_queued_age_seconds", 0)),
    }
    if lane.get("tier"):
        env["TARTCI_RUNNER_WORKFLOW_TIERS"] = "\n".join(
            f"{row['label']}|{row['workflow']}" for row in lane["tier"]
        )
    else:
        env["TARTCI_RUNNER_WORKFLOW_NAMES"] = "\n".join(lane["workflows"])
    return {
        "Label": label,
        "ProgramArguments": [
            "/bin/bash", f"{host['home']}/.local/bin/tartci", "serve", "macos", "--loop"
        ],
        "WorkingDirectory": host["home"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": f"{host['log_root']}/macos-fleet-{lane_id}.log",
        "StandardErrorPath": f"{host['log_root']}/macos-fleet-{lane_id}.log",
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
    args = parser.parse_args(argv)
    try:
        data = load(args.config)
        if args.command == "validate":
            print(f"valid: host={data['host']['id']} lanes={len(data['lane'])} activation=unchanged")
            return 0
        args.output.mkdir(parents=True, exist_ok=True)
        for lane in data["lane"]:
            target = args.output / f"com.danielraffel.tartci.tart-runner-macos-fleet.{data['host']['id']}.{lane['id']}.plist"
            with target.open("wb") as handle:
                plistlib.dump(lane_plist(data, lane), handle, sort_keys=False)
            print(target)
        print("rendered only: no LaunchAgent was installed, loaded, enabled, or activated", file=sys.stderr)
        return 0
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"fleet-macos: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
