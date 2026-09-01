#!/usr/bin/env python3
"""Validate and render dormant, capability-driven macOS Tart fleet lanes.

The rendered LaunchAgents are staging artifacts. This command never installs,
loads, enables, drains, or otherwise mutates a host runner pool.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import posixpath
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path, PurePosixPath

import tartci_support_manifest as support_manifest
import macos_launcher_identity
import macos_launcher_probe
import network_profile


SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED_BASE_LABELS = {"self-hosted", "macOS", "ARM64"}
LEASE_PRIORITIES = {"background", "build", "vm", "runner", "gate"}
TOP_KEYS = {
    "schema", "name", "host", "github_app", "stacked_images",
    "launch_helper", "worktree_cleanup", "lane",
}
HOST_KEYS = {
    "id", "home", "tart_home", "cache_root", "log_root",
    "github_api_timeout_seconds",
}
GITHUB_APP_KEYS = {"id", "private_key_path", "cache_dir"}
STACKED_IMAGE_KEYS = {
    "enabled", "minimum_macos_major", "minimum_tart_version",
    "registry_username_file", "registry_token_file", "flat_rollback",
}
LAUNCH_HELPER_KEYS = {"path", "approval_sha256_path", "identifier", "team_id"}
WORKTREE_CLEANUP_KEYS = {
    "provider", "repo", "primary", "prefix", "main_ref", "github_cli",
    "apply", "max_trees", "max_gib", "timeout_seconds", "cooldown_seconds",
}
LANE_KEYS = {
    "id", "repo", "golden", "priority", "vm_cores", "labels", "workflows", "tier",
    "runner_group_id", "registration_scope", "min_queued_age_seconds", "replaces_launchd_labels",
    "jit_github_cli", "chrome_app_dir", "assignment_mode",
    "assignment_omit_labels", "supervisors",
    "assignment_scan_timeout_seconds", "assignment_scan_max_workers",
    "assignment_top_tier_receipt_max_age_seconds",
}
TIER_KEYS = {"label", "workflow", "runner_group_id"}
LABEL = re.compile(r"^[A-Za-z0-9_.:-]+$")
REPLACED_AGENT = re.compile(
    r"^com[.]danielraffel[.](?:pulp[.]tart-runner|"
    r"[a-z0-9.-]+[.]tart-runner-[a-z0-9.-]+)$"
)
NETWORK_PROXY_ENV = {
    "HTTP_PROXY": "http://127.0.0.1:49125",
    "HTTPS_PROXY": "http://127.0.0.1:49125",
    "http_proxy": "http://127.0.0.1:49125",
    "https_proxy": "http://127.0.0.1:49125",
    "NO_PROXY": "127.0.0.1,localhost,::1",
    "no_proxy": "127.0.0.1,localhost,::1",
    "TARTCI_GUEST_HTTP_PROXY": "http://192.168.64.1:49125",
}


def fail(message: str) -> None:
    raise ValueError(message)


def _verified_network_overlay(
    installed: Path, base_body: bytes, config: Path
) -> bytes | None:
    """Return a receipted host-network overlay, or None when it is not exact."""
    if installed.is_symlink() or not installed.is_file():
        return None
    receipt_path = network_profile.applied_receipt_path(
        network_profile.default_profile_path()
    )
    try:
        receipt = json.loads(receipt_path.read_text())
        body = installed.read_bytes()
        value = plistlib.loads(body)
        base = plistlib.loads(base_body)
    except (OSError, json.JSONDecodeError, plistlib.InvalidFileException):
        return None
    if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
        return None
    label = value.get("Label")
    agent = (receipt.get("agents") or {}).get(label)
    owner = ((receipt.get("ownership") or {}).get("controllers") or {}).get(label)
    if not isinstance(agent, dict) or set(agent) != {"digest", "path", "state"}:
        return None
    if (
        Path(str(agent.get("path", ""))).resolve() != installed.resolve()
        or agent.get("state") not in {"staged", "loaded"}
        or agent.get("digest") != hashlib.sha256(
            plistlib.dumps(value, sort_keys=True)
        ).hexdigest()
    ):
        return None
    if not isinstance(owner, dict) or set(owner) != {"environment", "path"}:
        return None
    original = owner.get("environment")
    if (
        Path(str(owner.get("path", ""))).resolve() != installed.resolve()
        or not isinstance(original, dict)
    ):
        return None
    if set(original) != set(NETWORK_PROXY_ENV):
        return None
    environment = value.get("EnvironmentVariables")
    if not isinstance(environment, dict):
        return None
    if any(environment.get(key) != wanted for key, wanted in NETWORK_PROXY_ENV.items()):
        return None
    restored = dict(environment)
    for key, state in original.items():
        if not isinstance(state, dict) or set(state) not in (
            {"present"}, {"present", "value"}
        ) or not isinstance(state.get("present"), bool):
            return None
        if state["present"]:
            if set(state) != {"present", "value"} or not isinstance(state["value"], str):
                return None
            restored[key] = state["value"]
        else:
            if set(state) != {"present"}:
                return None
            restored.pop(key, None)
    value["EnvironmentVariables"] = restored
    if value != base or plistlib.dumps(value, sort_keys=False) != base_body:
        return None
    return body


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
    github_api_timeout = host.get("github_api_timeout_seconds")
    if github_api_timeout is not None and (
            type(github_api_timeout) is not int
            or not 5 <= github_api_timeout <= 60):
        fail("host.github_api_timeout_seconds must be an integer from 5 through 60")
    if host["tart_home"] == host["home"] or host["log_root"] == host["home"]:
        fail("Tart and log roots may not be the host home directory itself")
    helper = data.get("launch_helper")
    external_tart_home = PurePosixPath(host["tart_home"]).parts[:2] == ("/", "Volumes")
    if helper is not None:
        if not isinstance(helper, dict) or set(helper) != LAUNCH_HELPER_KEYS:
            fail("launch_helper must declare path, approval_sha256_path, identifier, and team_id")
        expected_path = PurePosixPath(host["home"]) / ".local/libexec/TartCILauncher.app"
        if helper.get("path") != str(expected_path):
            fail("launch_helper.path must be the stable host-local TartCI launcher path")
        expected_approval = PurePosixPath(host["home"]) / ".config/tartci/m3-launcher-approved.sha256"
        if helper.get("approval_sha256_path") != str(expected_approval):
            fail("launch_helper.approval_sha256_path must be the stable private M3 approval path")
        if helper.get("identifier") != "com.danielraffel.tartci.launcher":
            fail("launch_helper.identifier must be com.danielraffel.tartci.launcher")
        if (not isinstance(helper.get("team_id"), str)
                or not re.fullmatch(r"[A-Z0-9]{10}", helper["team_id"])):
            fail("launch_helper.team_id must be a ten-character Apple Team ID")
        if host["tart_home"] != "/Volumes/Workshop/VMs":
            fail("launch_helper is restricted to the private M3 /Volumes/Workshop/VMs store")
    if external_tart_home and helper is None:
        fail("an external-volume Tart home requires a verified signed launch_helper")
    if helper is not None and not external_tart_home:
        fail("launch_helper is reserved for an external-volume Tart home")
    cleanup = data.get("worktree_cleanup")
    if cleanup is not None:
        expected = {
            "provider": "merged-main-v1", "repo": "Generous-Corp/pulp",
            "primary": "/Volumes/Workshop/Code/pulp",
            "prefix": "/Volumes/Workshop/Code", "main_ref": "origin/main",
            "github_cli": "ghapp", "apply": False, "max_trees": 8,
            "max_gib": 512, "timeout_seconds": 300, "cooldown_seconds": 3600,
        }
        if not isinstance(cleanup, dict) or set(cleanup) != WORKTREE_CLEANUP_KEYS:
            fail("worktree_cleanup must declare the complete strict contract")
        if cleanup != expected:
            fail("worktree_cleanup is restricted to the reviewed M3 merged-main-v1 contract")
        if host.get("id") != "studio" or host.get("tart_home") != "/Volumes/Workshop/VMs":
            fail("worktree_cleanup is restricted to the private M3 profile")
    github_app = data.get("github_app")
    if github_app is not None:
        if not isinstance(github_app, dict):
            fail("github_app must be a table")
        unknown = set(github_app) - GITHUB_APP_KEYS
        if unknown:
            fail(f"unknown github_app keys: {sorted(unknown)}")
        if set(github_app) != GITHUB_APP_KEYS:
            fail("github_app must declare id, private_key_path, and cache_dir together")
        if (not isinstance(github_app["id"], str)
                or not re.fullmatch(r"[1-9][0-9]*", github_app["id"])):
            fail("github_app.id must be a positive decimal string")
        home = PurePosixPath(host["home"])
        for key, required_root in (
                ("private_key_path", home / ".config/shipyard/github-apps"),
                ("cache_dir", home / ".config/shipyard")):
            value = github_app[key]
            if not isinstance(value, str):
                fail(f"github_app.{key} must be an absolute host-local Shipyard path")
            normalized = PurePosixPath(posixpath.normpath(value))
            try:
                relative = normalized.relative_to(required_root)
            except ValueError:
                relative = None
            if (not normalized.is_absolute() or normalized.as_posix() != value
                    or relative is None or not relative.parts):
                fail(f"github_app.{key} must be an absolute host-local Shipyard path")
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
        registration_scope = lane.get("registration_scope")
        if registration_scope not in (None, "repository"):
            fail(
                f"lane {lane_id}: registration_scope must be repository when declared"
            )
        repository_scoped = runner_group_id == 1 and registration_scope == "repository"
        if (type(runner_group_id) is not int or runner_group_id < 1
                or (runner_group_id == 1 and not repository_scoped)
                or (runner_group_id > 1 and registration_scope is not None)):
            fail(
                f"lane {lane_id}: runner_group_id must be an explicit "
                "non-Default GitHub runner group integer, or group 1 with "
                "registration_scope = repository"
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
        vm_cores = lane.get("vm_cores")
        if vm_cores is not None and (type(vm_cores) is not int or vm_cores < 1):
            fail(f"lane {lane_id}: vm_cores must be a positive integer")
        priority = lane.get("priority")
        if priority is not None and (
                not isinstance(priority, str) or priority not in LEASE_PRIORITIES):
            fail(f"lane {lane_id}: unsupported lease priority")
        supervisors = lane.get("supervisors", 1)
        if type(supervisors) is not int or supervisors not in (1, 2):
            fail(f"lane {lane_id}: supervisors must be 1 or 2")
        assignment_mode = lane.get("assignment_mode")
        if assignment_mode is not None and assignment_mode != "event-class-v2":
            fail(f"lane {lane_id}: unsupported assignment_mode")
        if assignment_mode == "event-class-v2" and "priority" in lane:
            fail(
                f"lane {lane_id}: event-class-v2 must derive lease priority "
                "from the selected event class"
            )
        scan_timeout = lane.get("assignment_scan_timeout_seconds")
        if scan_timeout is not None and (
                assignment_mode != "event-class-v2"
                or type(scan_timeout) is not int
                or not 60 <= scan_timeout <= 300):
            fail(
                f"lane {lane_id}: assignment_scan_timeout_seconds must be an "
                "integer from 60 through 300 on an event-class-v2 lane"
            )
        scan_workers = lane.get("assignment_scan_max_workers")
        if scan_workers is not None and (
                assignment_mode != "event-class-v2"
                or type(scan_workers) is not int
                or not 1 <= scan_workers <= 4):
            fail(
                f"lane {lane_id}: assignment_scan_max_workers must be an "
                "integer from 1 through 4 on an event-class-v2 lane"
            )
        top_tier_receipt_age = lane.get(
            "assignment_top_tier_receipt_max_age_seconds"
        )
        if top_tier_receipt_age is not None and (
                assignment_mode != "event-class-v2"
                or type(top_tier_receipt_age) is not int
                or not 0 <= top_tier_receipt_age <= 300):
            fail(
                f"lane {lane_id}: assignment_top_tier_receipt_max_age_seconds "
                "must be an integer from 0 through 300 on an event-class-v2 lane"
            )
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
            tier_group_id = tier.get("runner_group_id")
            if tier_group_id is not None and (
                    type(tier_group_id) is not int or tier_group_id < 1):
                fail(
                    f"lane {lane_id}: tier runner_group_id must be a positive integer"
                )
        tier_groups = [tier.get("runner_group_id") for tier in tiers]
        if any(group_id is not None for group_id in tier_groups) and any(
                group_id is None for group_id in tier_groups):
            fail(
                f"lane {lane_id}: every tier must declare runner_group_id when any tier does"
            )
        if assignment_mode == "event-class-v2":
            class_labels = [tier["label"] for tier in tiers]
            if class_labels != ["pulp-build-merge-group", "pulp-build-pr-head"]:
                fail(
                    f"lane {lane_id}: event-class-v2 requires merge-group then PR-head tiers"
                )
            if "pulp-gate-fast" not in omit_labels:
                fail(f"lane {lane_id}: event-class-v2 must omit pulp-gate-fast")
            if tier_groups != [1, 1]:
                fail(
                    f"lane {lane_id}: event-class-v2 requires repository-scoped "
                    "merge-group and PR-head registration"
                )
    return data


def rendered_plists(
    data: dict, *, launch_entrypoint: Path | None = None
) -> dict[str, bytes]:
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
                lane_plist(
                    data, lane, slot=slot, launch_entrypoint=launch_entrypoint
                ),
                sort_keys=False,
            )
    return result


def replacements(data: dict) -> list[str]:
    return [label for lane in data["lane"] for label in lane.get("replaces_launchd_labels", [])]


def write_receipt(
    config: Path,
    agents_dir: Path,
    output: Path,
    support_root: Path,
    manifest_path: Path,
    entrypoint: Path,
    entrypoint_source: Path,
    launch_entrypoint: Path,
    source_authority_commit: str,
) -> None:
    data = load(config)
    support_root = support_root.resolve()
    launch_entrypoint = launch_entrypoint.resolve()
    expected = rendered_plists(data, launch_entrypoint=launch_entrypoint)
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
    manifest_path = manifest_path.resolve()
    if manifest_path != support_root / support_manifest.MANIFEST_NAME:
        fail("support manifest must be installed inside the immutable support root")
    support = support_manifest.verify(
        support_root, manifest_path, immutable=True
    )
    if source_authority_commit != support["source_commit"]:
        fail("authenticated source authority does not match the support commit")
    wrapper = support_manifest.staged_wrapper_record(
        entrypoint_source, entrypoint, support_root
    )
    launch = support_manifest.launch_record(launch_entrypoint, support_root)
    interpreter = Path("/usr/bin/python3")
    interpreter_info = interpreter.lstat()
    if interpreter.is_symlink() or not interpreter.is_file():
        fail("fleet launch interpreter must be a regular non-symlink file")
    interpreter_record = {
        "path": str(interpreter),
        "mode": stat.S_IMODE(interpreter_info.st_mode),
        "sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        "owner_uid": interpreter_info.st_uid,
    }
    helper = data.get("launch_helper")
    helper_record = None
    if helper is not None:
        helper_record = macos_launcher_identity.verify(
            Path(helper["path"]), identifier=helper["identifier"],
            team_id=helper["team_id"],
            profile_policy_sha256=macos_launcher_identity.profile_policy_digest(config),
            source_commit=source_authority_commit,
        )
    receipt = {
        "schema": 3,
        "profile": data.get("name", config.stem),
        "config_path": str(config.resolve()),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "agents_dir": str(agents_dir.resolve()),
        "plists": digests,
        "retired_launchd_labels": replacements(data),
        "launch_helper": helper_record,
        "support": {
            "root": str(support_root),
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "repository": support["repository"],
            "source_commit": support["source_commit"],
            "members": support["members"],
            "entrypoint": wrapper,
            "launch_entrypoint": launch,
            "interpreter": interpreter_record,
            "source_authority": {
                "kind": "github_app_commit_read",
                "repository": support["repository"],
                "commit": source_authority_commit,
            },
        },
    }
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")


def verify_receipt(
    path: Path, config: Path, agents_dir: Path, support_root: Path
) -> dict:
    try:
        receipt = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"could not read install receipt {path}: {exc}")
    if not isinstance(receipt, dict):
        fail("install receipt must be an object")
    data = load(config)
    helper = data.get("launch_helper")
    supported_schemas = {3} if helper is not None else {2, 3}
    if receipt.get("schema") not in supported_schemas:
        expected = "3" if helper is not None else "2 or 3"
        fail(f"install receipt schema must be {expected} for this profile")
    config = config.resolve()
    agents_dir = agents_dir.resolve()
    support_root = support_root.resolve()
    if (Path(receipt.get("config_path", "")).resolve() != config
            or Path(receipt.get("agents_dir", "")).resolve() != agents_dir):
        fail("install receipt paths do not match the canonical installed paths")
    if not config.is_file() or not agents_dir.is_dir():
        fail("install receipt config_path and agents_dir must exist")
    support = receipt.get("support")
    if not isinstance(support, dict) or set(support) != {
        "root", "manifest_path", "manifest_sha256", "repository", "source_commit",
        "members", "entrypoint", "launch_entrypoint", "interpreter",
        "source_authority",
    }:
        fail("install receipt support cohort is missing or malformed")
    if Path(str(support.get("root", ""))).resolve() != support_root:
        fail("install receipt support root does not match the active TartCI root")
    manifest_path = Path(str(support.get("manifest_path", ""))).resolve()
    if manifest_path != support_root / support_manifest.MANIFEST_NAME:
        fail("install receipt manifest is outside the immutable support root")
    verified_support = support_manifest.verify(
        support_root, manifest_path, immutable=True
    )
    if (
        hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        != support.get("manifest_sha256")
        or verified_support.get("repository") != support.get("repository")
        or verified_support.get("source_commit") != support.get("source_commit")
        or verified_support.get("members") != support.get("members")
    ):
        fail("installed TartCI support cohort does not match its receipt")
    if support.get("source_authority") != {
        "kind": "github_app_commit_read",
        "repository": verified_support["repository"],
        "commit": verified_support["source_commit"],
    }:
        fail("installed TartCI source authority does not match its receipt")
    if helper is None:
        if receipt.get("launch_helper") is not None:
            fail("install receipt unexpectedly records a launch helper")
    else:
        helper_record = macos_launcher_identity.verify(
            Path(helper["path"]), identifier=helper["identifier"],
            team_id=helper["team_id"],
            profile_policy_sha256=macos_launcher_identity.profile_policy_digest(config),
            source_commit=verified_support["source_commit"],
        )
        if helper_record != receipt.get("launch_helper"):
            fail("installed launch helper does not match its receipt")
    expected_entrypoint = Path(data["host"]["home"]) / ".local/bin/tartci"
    verified_entrypoint = support_manifest.wrapper_record(
        expected_entrypoint, support_root
    )
    if verified_entrypoint != support.get("entrypoint"):
        fail("installed TartCI entrypoint does not match its receipt")
    launch_entrypoint = support_root / support_manifest.LAUNCH_NAME
    if (
        support_manifest.launch_record(launch_entrypoint, support_root)
        != support.get("launch_entrypoint")
    ):
        fail("installed TartCI launch entrypoint does not match its receipt")
    interpreter = Path("/usr/bin/python3")
    interpreter_info = interpreter.lstat()
    interpreter_record = {
        "path": str(interpreter),
        "mode": stat.S_IMODE(interpreter_info.st_mode),
        "sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        "owner_uid": interpreter_info.st_uid,
    }
    if interpreter.is_symlink() or not interpreter.is_file() \
            or interpreter_record != support.get("interpreter"):
        fail("fleet launch interpreter does not match its receipt")
    if hashlib.sha256(config.read_bytes()).hexdigest() != receipt.get("config_sha256"):
        fail("installed fleet profile digest does not match its receipt")
    expected = rendered_plists(data, launch_entrypoint=launch_entrypoint)
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
        installed_body = None
        if not installed.is_symlink() and installed.is_file():
            installed_body = installed.read_bytes()
        if installed_body is not None and installed_body != body:
            installed_body = _verified_network_overlay(installed, body, config)
        if recorded.get(name) != digest or installed_body is None:
            fail(f"installed fleet plist failed receipt verification: {installed}")
    expected_retired = replacements(data)
    if receipt.get("retired_launchd_labels") != expected_retired:
        fail("install receipt retired-label set does not match the profile")
    for label in expected_retired:
        if (agents_dir / f"{label}.plist").exists():
            fail(f"declared legacy LaunchAgent became installable again: {label}")
    return receipt


def _loaded_has(output: str, value: str, description: str) -> None:
    if value not in output:
        fail(
            f"loaded LaunchAgent {description} does not match its receipt "
            f"(missing {value!r})"
        )


def _loaded_exit_timeout_matches(output: str, seconds: int) -> bool:
    """Accept launchd's macOS 26 (`seconds`) and macOS 27 (bare) renderings."""
    return re.search(
        rf"^\texit timeout = {seconds}(?: seconds)?$", output, re.MULTILINE
    ) is not None


def _verify_loaded_output(
    name: str, payload: bytes, output: str, agents_dir: Path
) -> str:
        label = name.removesuffix(".plist")
        value = plistlib.loads(payload)
        loaded_path = re.search(r"^\tpath = (.+)$", output, re.MULTILINE)
        if (
            loaded_path is None
            or Path(loaded_path.group(1)).resolve() != (agents_dir / name).resolve()
        ):
            fail(f"loaded LaunchAgent {label} path does not match its receipt")
        _loaded_has(output, f"\tprogram = {value['ProgramArguments'][0]}\n", f"{label} program")
        arguments = "\targuments = {\n" + "".join(
            f"\t\t{argument}\n" for argument in value["ProgramArguments"]
        ) + "\t}\n"
        _loaded_has(output, arguments, f"{label} arguments")
        for key, plist_key in (
            ("working directory", "WorkingDirectory"),
            ("stdout path", "StandardOutPath"),
            ("stderr path", "StandardErrorPath"),
        ):
            _loaded_has(output, f"\t{key} = {value[plist_key]}\n", f"{label} {key}")
        environment = value["EnvironmentVariables"]
        for key, expected_value in environment.items():
            _loaded_has(
                output,
                f"\t\t{key} => {expected_value}\n",
                f"{label} environment {key}",
            )
        environment_start = output.find("\n\tenvironment = {\n")
        environment_end = output.find("\n\t}\n", environment_start + 2)
        if environment_start < 0 or environment_end < 0:
            fail(f"loaded LaunchAgent {label} has no readable environment")
        loaded_environment = output[environment_start:environment_end]
        loaded_keys = set(re.findall(
            r"^\t\t([A-Z][A-Z0-9_]*) =>", loaded_environment, re.MULTILINE
        ))
        unexpected_governed = {
            key for key in loaded_keys - set(environment)
            if key.startswith(("TARTCI_", "SHIPYARD_", "TART_HOME"))
        }
        if unexpected_governed:
            fail(
                f"loaded LaunchAgent {label} retains obsolete governed environment: "
                f"{sorted(unexpected_governed)}"
            )
        properties = next(
            (line for line in output.splitlines() if line.startswith("\tproperties = ")),
            "",
        )
        if "keepalive" not in properties or "runatload" not in properties:
            fail(f"loaded LaunchAgent {label} lost keepalive/runatload properties")
        if not _loaded_exit_timeout_matches(output, 30):
            fail(
                f"loaded LaunchAgent {label} exit timeout does not match its receipt "
                "(expected 30 seconds)"
            )
        return hashlib.sha256(output.encode()).hexdigest()


def verify_loaded_snapshot(
    receipt_path: Path,
    config: Path,
    agents_dir: Path,
    support_root: Path,
    outputs: dict[str, str],
) -> dict[str, str]:
    receipt = verify_receipt(receipt_path, config, agents_dir, support_root)
    expected = {
        name: (agents_dir / name).read_bytes()
        for name in receipt["plists"]
    }
    if set(outputs) != {name.removesuffix(".plist") for name in expected}:
        fail("loaded LaunchAgent snapshot does not match the receipt service set")
    return {
        name: _verify_loaded_output(
            name, payload, outputs[name.removesuffix(".plist")], agents_dir
        )
        for name, payload in expected.items()
    }


def verify_loaded(
    receipt_path: Path,
    config: Path,
    agents_dir: Path,
    support_root: Path,
) -> dict:
    receipt = verify_receipt(receipt_path, config, agents_dir, support_root)
    outputs: dict[str, str] = {}
    for name in receipt["plists"]:
        label = name.removesuffix(".plist")
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                text=True, capture_output=True, check=False, timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            fail(f"timed out reading loaded LaunchAgent {label}: {exc}")
        if result.returncode != 0:
            fail(
                f"could not read loaded LaunchAgent {label}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        outputs[label] = result.stdout
    loaded = verify_loaded_snapshot(
        receipt_path, config, agents_dir, support_root, outputs
    )
    return {
        "schema": 1,
        "verified_at_unix": int(time.time()),
        "install_receipt_path": str(receipt_path.resolve()),
        "install_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "support_root": str(support_root.resolve()),
        "loaded_services": loaded,
    }


def probe_launch_helper(
    receipt_path: Path,
    config: Path,
    agents_dir: Path,
    support_root: Path,
    timeout_seconds: float = 10.0,
) -> dict:
    """Prove external-volume access with the receipted launchd identity."""
    receipt = verify_receipt(receipt_path, config, agents_dir, support_root)
    helper = receipt.get("launch_helper")
    if helper is None:
        return {"schema": 1, "required": False, "passed": True}
    return macos_launcher_probe.run(helper, load(config), timeout_seconds)


def fleet_readiness(
    receipt_path: Path,
    config: Path,
    agents_dir: Path,
    support_root: Path,
    participating: bool,
    pool_state: str,
    stale_heartbeat_seconds: int = 300,
) -> dict:
    """Report realized receipt-backed capacity separately from pool intent."""
    problems: list[dict[str, str]] = []
    try:
        receipt = verify_receipt(receipt_path, config, agents_dir, support_root)
    except (OSError, ValueError) as exc:
        return {
            "managed": True,
            "fleet_ready": False,
            "verified_running_supervisors": 0,
            "expected_supervisors": None,
            "problems": [{"code": "receipt_mismatch", "detail": str(exc)}],
        }

    labels = sorted(name.removesuffix(".plist") for name in receipt["plists"])
    admission_open = participating and pool_state == "on"
    if participating != (pool_state == "on"):
        problems.append({
            "code": "admission_state_mismatch",
            "detail": f"state={pool_state} participating={int(participating)}",
        })
    loaded_outputs: dict[str, str] = {}
    for label in labels:
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                text=True, capture_output=True, check=False, timeout=5,
            )
        except subprocess.TimeoutExpired:
            problems.append({"code": "launchctl_probe_timeout", "label": label})
            continue
        if result.returncode == 0:
            loaded_outputs[label] = result.stdout
            if pool_state == "off":
                problems.append({"code": "unexpected_loaded_service", "label": label})
        elif result.returncode == 113 and "Could not find service" in result.stderr:
            if admission_open:
                problems.append({"code": "unloaded_service", "label": label})
        else:
            problems.append({
                "code": "launchctl_probe_error", "label": label,
                "detail": result.stderr.strip() or f"exit={result.returncode}",
            })

    state_dirs: set[Path] = set()
    fleet_homes: set[str] = set()
    for label in labels:
        plist = plistlib.loads((agents_dir / f"{label}.plist").read_bytes())
        environment = plist.get("EnvironmentVariables") or {}
        raw_state_dir = environment.get("TARTCI_STATE_DIR")
        raw_home = environment.get("HOME")
        if isinstance(raw_home, str) and raw_home:
            fleet_homes.add(raw_home)
        if isinstance(raw_state_dir, str) and raw_state_dir:
            state_dirs.add(Path(raw_state_dir))
        else:
            problems.append({"code": "state_dir_missing", "label": label})
    expected_pids = {
        int(match.group(1))
        for output in loaded_outputs.values()
        if (match := re.search(r"^\s*pid = ([0-9]+)\s*$", output, re.MULTILINE))
    }
    process_starts: dict[int, str] = {}
    managed_supervisor_pids: set[int] = set()
    try:
        process_table = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,lstart=,command="],
            text=True, capture_output=True, check=False, timeout=5,
        )
    except subprocess.TimeoutExpired:
        problems.append({"code": "process_table_probe_timeout"})
    else:
        if process_table.returncode != 0:
            problems.append({"code": "process_table_probe_error"})
        else:
            home = next(iter(fleet_homes), "")
            generation_prefix = f"{home}/.local/share/tartci-generations/"
            provider_suffix = "/providers/tart-macos/runner.sh --loop"
            for line in process_table.stdout.splitlines():
                match = re.match(
                    r"^\s*([0-9]+)\s+([0-9]+)\s+(.{24})\s+(.+)$", line
                )
                if match is None:
                    continue
                pid = int(match.group(1))
                ppid = int(match.group(2))
                process_starts[pid] = " ".join(match.group(3).split())
                command = match.group(4)
                if (
                    ppid == 1
                    and generation_prefix in command
                    and provider_suffix in command
                ):
                    managed_supervisor_pids.add(pid)
            for orphan_pid in sorted(managed_supervisor_pids - expected_pids):
                problems.append({
                    "code": "orphaned_supervisor", "label": str(orphan_pid),
                    "detail": process_starts[orphan_pid],
                })

    verified_running = 0
    if admission_open and len(loaded_outputs) == len(labels):
        try:
            verify_loaded_snapshot(
                receipt_path, config, agents_dir, support_root, loaded_outputs
            )
        except (OSError, ValueError) as exc:
            problems.append({"code": "loaded_receipt_mismatch", "detail": str(exc)})
        else:
            now = dt.datetime.now(dt.timezone.utc)
            for label, output in loaded_outputs.items():
                if not re.search(r"^\s*state = running\s*$", output, re.MULTILINE):
                    problems.append({"code": "supervisor_not_running", "label": label})
                    continue
                pid_match = re.search(r"^\s*pid = ([0-9]+)\s*$", output, re.MULTILINE)
                if pid_match is None:
                    problems.append({"code": "supervisor_pid_missing", "label": label})
                    continue
                pid = int(pid_match.group(1))
                plist = plistlib.loads((agents_dir / f"{label}.plist").read_bytes())
                state_dir = Path(
                    (plist.get("EnvironmentVariables") or {}).get("TARTCI_STATE_DIR", "")
                )
                matching_state = None
                for state_path in sorted(state_dir.glob("*.state.json")):
                    try:
                        candidate = json.loads(state_path.read_text())
                    except (OSError, json.JSONDecodeError):
                        continue
                    if (
                        candidate.get("supervisor_pid") == str(pid)
                        and " ".join(str(candidate.get(
                            "supervisor_pid_started_at", ""
                        )).split()) == process_starts.get(pid, "")
                    ):
                        matching_state = candidate
                        break
                if matching_state is None:
                    problems.append({"code": "heartbeat_missing", "label": label})
                    continue
                try:
                    heartbeat_at = dt.datetime.fromisoformat(
                        str(matching_state["ts"]).replace("Z", "+00:00")
                    )
                except (KeyError, TypeError, ValueError):
                    problems.append({"code": "heartbeat_invalid", "label": label})
                    continue
                age = (now - heartbeat_at).total_seconds()
                if age < -30:
                    problems.append({
                        "code": "heartbeat_from_future", "label": label,
                        "detail": f"skew_seconds={int(-age)}",
                    })
                    continue
                age = max(0.0, age)
                if age > stale_heartbeat_seconds:
                    problems.append({
                        "code": "heartbeat_stale", "label": label,
                        "detail": f"age_seconds={int(age)}",
                    })
                    continue
                verified_running += 1

    try:
        domain = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}"],
            text=True, capture_output=True, check=False, timeout=5,
        )
    except subprocess.TimeoutExpired:
        problems.append({"code": "launchctl_domain_probe_timeout"})
    else:
        if domain.returncode != 0:
            problems.append({"code": "launchctl_domain_probe_error"})
        else:
            prefix = "com.danielraffel.tartci.tart-runner-macos-fleet."
            loaded_managed = {
                match.group(1)
                for line in domain.stdout.splitlines()
                if (match := re.search(
                    rf"({re.escape(prefix)}[A-Za-z0-9_.-]+)\s*$", line
                ))
                and re.match(r"^\s*[0-9-]+\s+[0-9-]+\s+", line)
            }
            for unexpected in sorted(loaded_managed - set(labels)):
                problems.append({
                    "code": "unexpected_managed_service", "label": unexpected,
                })

    for retired in receipt.get("retired_launchd_labels", []):
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{retired}"],
                text=True, capture_output=True, check=False, timeout=5,
            )
        except subprocess.TimeoutExpired:
            problems.append({"code": "launchctl_probe_timeout", "label": retired})
            continue
        if result.returncode == 0:
            problems.append({"code": "retired_service_loaded", "label": retired})
        elif not (result.returncode == 113 and "Could not find service" in result.stderr):
            problems.append({"code": "launchctl_probe_error", "label": retired})

    return {
        "managed": True,
        "fleet_ready": admission_open and verified_running == len(labels) and not problems,
        "verified_running_supervisors": verified_running,
        "expected_supervisors": len(labels),
        "problems": problems,
    }


def atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(0o644)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def lane_plist(
    data: dict,
    lane: dict,
    *,
    slot: int = 1,
    launch_entrypoint: Path | None = None,
) -> dict:
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
        # launchd starts from the host home rather than a repository checkout.
        # Bind the App wrapper to the same exact repository as this lane so
        # every API call, including generate-jitconfig, has unambiguous
        # authority without relying on cwd discovery.
        "SHIPYARD_GH_APP_REPO": lane["repo"],
        "TARTCI_LAUNCHD_LABEL": label,
        "TARTCI_RUNNER_REPO": lane["repo"],
        "TARTCI_RUNNER_GROUP_ID": str(lane["runner_group_id"]),
        "TARTCI_RUNNER_LABELS": ",".join(lane["labels"]),
        "TARTCI_MACOS_GOLDEN": lane["golden"],
        "TARTCI_CI_CACHE": host["cache_root"],
        "TARTCI_STATE_DIR": state,
        "TARTCI_DISK_DENIAL_RECEIPT_DIR": f"{host['home']}/.tartci/state/disk-admission",
        "TARTCI_RECEIPT_HOST_ID": host["id"],
        "TARTCI_EVENT_LOG": f"{state}/events.jsonl",
        "TARTCI_MACOS_LOGS": f"{host['log_root']}/macos-fleet-jobs/{identity}",
        "TARTCI_QUEUE_LANE_ID": f"{host['id']}-{identity}",
        "TARTCI_SHARED_QUEUE_CACHE": f"{host['home']}/.tartci/state/queue-discovery.json",
        "TARTCI_RUNNER_NAME_PREFIX": f"{host['id']}-{identity}",
        "TARTCI_RUNNER_SLOT": str(slot),
        "TARTCI_ADMISSION_CLEAN_MODE": "required",
        "TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS": str(lane.get("min_queued_age_seconds", 0)),
    }
    cleanup = data.get("worktree_cleanup")
    if cleanup is not None and lane["repo"] == cleanup["repo"]:
        env.update({
            "TARTCI_WORKTREE_CLEANUP_PROVIDER": cleanup["provider"],
            "TARTCI_WORKTREE_CLEANUP_REPO": cleanup["repo"],
            "TARTCI_WORKTREE_CLEANUP_PRIMARY": cleanup["primary"],
            "TARTCI_WORKTREE_CLEANUP_PREFIX": cleanup["prefix"],
            "TARTCI_WORKTREE_CLEANUP_MAIN_REF": cleanup["main_ref"],
            "TARTCI_WORKTREE_CLEANUP_GITHUB_CLI": cleanup["github_cli"],
            "TARTCI_WORKTREE_CLEANUP_APPLY": "1" if cleanup["apply"] else "0",
            "TARTCI_WORKTREE_CLEANUP_MAX_TREES": str(cleanup["max_trees"]),
            "TARTCI_WORKTREE_CLEANUP_MAX_GIB": str(cleanup["max_gib"]),
            "TARTCI_WORKTREE_CLEANUP_TIMEOUT_SECS": str(cleanup["timeout_seconds"]),
            "TARTCI_WORKTREE_CLEANUP_COOLDOWN_SECS": str(cleanup["cooldown_seconds"]),
        })
    github_app = data.get("github_app")
    if github_app is not None:
        # References only: key/token contents remain in private host-local files.
        env.update({
            "SHIPYARD_GITHUB_APP_ID": github_app["id"],
            "SHIPYARD_GITHUB_APP_PRIVATE_KEY_PATH": github_app["private_key_path"],
            "SHIPYARD_GITHUB_APP_CACHE_DIR": github_app["cache_dir"],
        })
    if "github_api_timeout_seconds" in host:
        env["TARTCI_GH_TIMEOUT_SECS"] = str(host["github_api_timeout_seconds"])
    # An omitted priority delegates to the provider's exact-label policy.
    # Checked-in non-V2 lanes declare their fixed class; Pulp V2 must derive it.
    if "priority" in lane:
        env["TARTCI_VM_LEASE_PRIORITY"] = lane["priority"]
    if "vm_cores" in lane:
        env["TARTCI_MACOS_VM_CORES"] = str(lane["vm_cores"])
    if lane.get("tier"):
        env["TARTCI_RUNNER_WORKFLOW_TIERS"] = "\n".join(
            f"{row['label']}|{row['workflow']}" for row in lane["tier"]
        )
        tier_groups = [row.get("runner_group_id") for row in lane["tier"]]
        if any(group_id is not None for group_id in tier_groups):
            env["TARTCI_RUNNER_WORKFLOW_TIER_GROUPS"] = "\n".join(
                f"{row['label']}|{row['runner_group_id']}" for row in lane["tier"]
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
    if "assignment_scan_timeout_seconds" in lane:
        env["TARTCI_ASSIGNMENT_SCAN_TIMEOUT_SECS"] = str(
            lane["assignment_scan_timeout_seconds"]
        )
    if "assignment_scan_max_workers" in lane:
        env["TARTCI_ASSIGNMENT_SCAN_MAX_WORKERS"] = str(
            lane["assignment_scan_max_workers"]
        )
    if "assignment_top_tier_receipt_max_age_seconds" in lane:
        env["TARTCI_ASSIGNMENT_V2_TOP_TIER_RECEIPT_MAX_AGE_SECS"] = str(
            lane["assignment_top_tier_receipt_max_age_seconds"]
        )
    launch = str(launch_entrypoint or Path(host["home"]) / ".local/bin/tartci")
    helper = data.get("launch_helper")
    program_arguments = (
        [f"{helper['path']}/Contents/MacOS/tartci-launcher", "--lane",
         env["TARTCI_QUEUE_LANE_ID"]]
        if helper is not None else
        ["/bin/bash", launch, "serve", "macos", "--loop"]
    )
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": host["home"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": f"{host['log_root']}/macos-fleet-{identity}.log",
        "StandardErrorPath": f"{host['log_root']}/macos-fleet-{identity}.log",
        "ProcessType": "Background",
        # Give the supervisor's TERM trap a deterministic cleanup window and
        # retain launchd ownership of ordinary provider descendants.
        "ExitTimeOut": 30,
        "AbandonProcessGroup": False,
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
    receipt.add_argument("--support-root", required=True, type=Path)
    receipt.add_argument("--support-manifest", required=True, type=Path)
    receipt.add_argument("--entrypoint", required=True, type=Path)
    receipt.add_argument("--entrypoint-source", required=True, type=Path)
    receipt.add_argument("--launch-entrypoint", required=True, type=Path)
    receipt.add_argument("--source-authority-commit", required=True)
    verify = sub.add_parser("verify-installed")
    verify.add_argument("receipt", type=Path)
    verify.add_argument("--config", required=True, type=Path)
    verify.add_argument("--agents-dir", required=True, type=Path)
    verify.add_argument("--support-root", required=True, type=Path)
    verify.add_argument("--print-services", action="store_true")
    loaded = sub.add_parser("verify-loaded")
    loaded.add_argument("receipt", type=Path)
    loaded.add_argument("--config", required=True, type=Path)
    loaded.add_argument("--agents-dir", required=True, type=Path)
    loaded.add_argument("--support-root", required=True, type=Path)
    loaded.add_argument("--output", required=True, type=Path)
    probe = sub.add_parser("probe-launch-helper")
    probe.add_argument("receipt", type=Path)
    probe.add_argument("--config", required=True, type=Path)
    probe.add_argument("--agents-dir", required=True, type=Path)
    probe.add_argument("--support-root", required=True, type=Path)
    probe.add_argument("--output", type=Path)
    readiness = sub.add_parser("fleet-readiness")
    readiness.add_argument("receipt", type=Path)
    readiness.add_argument("--config", required=True, type=Path)
    readiness.add_argument("--agents-dir", required=True, type=Path)
    readiness.add_argument("--support-root", required=True, type=Path)
    readiness.add_argument("--participating", choices=("0", "1"), required=True)
    readiness.add_argument("--pool-state", choices=("on", "off", "draining"), required=True)
    readiness.add_argument("--stale-heartbeat-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    try:
        if args.command == "probe-launch-helper":
            value = probe_launch_helper(
                args.receipt, args.config, args.agents_dir, args.support_root,
            )
            if args.output is not None:
                atomic_write_json(args.output, value)
            print(json.dumps(value, sort_keys=True))
            return 0
        if args.command == "verify-loaded":
            value = verify_loaded(
                args.receipt, args.config, args.agents_dir, args.support_root
            )
            atomic_write_json(args.output, value)
            print(f"verified loaded macOS fleet generation: {args.output}")
            return 0
        if args.command == "fleet-readiness":
            print(json.dumps(fleet_readiness(
                args.receipt, args.config, args.agents_dir, args.support_root,
                args.participating == "1", args.pool_state,
                args.stale_heartbeat_seconds,
            ), sort_keys=True))
            return 0
        if args.command == "verify-installed":
            receipt_value = verify_receipt(
                args.receipt, args.config, args.agents_dir, args.support_root
            )
            if args.print_services:
                for name in sorted(receipt_value["plists"]):
                    print(name.removesuffix(".plist"))
            else:
                print(f"verified installed macOS fleet receipt: {args.receipt}")
            return 0
        data = load(args.config)
        if args.command == "validate":
            print(f"valid: host={data['host']['id']} lanes={len(data['lane'])} activation=unchanged")
            return 0
        if args.command == "write-receipt":
            write_receipt(
                args.config,
                args.agents_dir,
                args.output,
                args.support_root,
                args.support_manifest,
                args.entrypoint,
                args.entrypoint_source,
                args.launch_entrypoint,
                args.source_authority_commit,
            )
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
