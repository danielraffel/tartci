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
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

import tartci_support_manifest as support_manifest
import macos_launcher_identity
import macos_launcher_probe
import network_profile


SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REQUIRED_BASE_LABELS = {"self-hosted", "macOS", "ARM64"}
LEASE_PRIORITIES = {"background", "build", "vm", "runner", "gate"}
# launchd.plist(5) documents exactly these ProcessType values. A supervisor
# left Background is throttled by the system for latency-insensitive work,
# which lengthens every GitHub API call its queue scan makes.
PROCESS_TYPES = ("Background", "Standard", "Adaptive", "Interactive")
DEFAULT_PROCESS_TYPE = "Background"
TOP_KEYS = {
    "schema", "name", "host", "github_app", "stacked_images",
    "launch_helper", "worktree_cleanup", "lane",
}
HOST_KEYS = {
    "id", "home", "tart_home", "cache_root", "log_root",
    "github_api_timeout_seconds", "persistent_runner_labels",
    "current_job_attempt_timeout_seconds",
    "current_job_lifecycle_budget_seconds",
}
GITHUB_APP_KEYS = {"id", "private_key_path", "cache_dir"}
STACKED_IMAGE_KEYS = {
    "enabled", "minimum_macos_major", "minimum_tart_version",
    "registry_username_file", "registry_token_file", "flat_rollback",
}
LAUNCH_HELPER_KEYS = {"path", "approval_sha256_path", "identifier", "team_id"}
# A signed launcher bundle execs its own frozen copy of the support cohort.
SEALED_SUPPORT = "Contents/Resources/support"
SEALED_METADATA = "Contents/Resources/bundle.json"
WORKTREE_CLEANUP_KEYS = {
    "provider", "repo", "primary", "prefix", "main_ref",
    "apply", "max_trees", "max_gib", "timeout_seconds", "cooldown_seconds",
}
LANE_KEYS = {
    "id", "repo", "golden", "priority", "vm_cores", "labels", "workflows", "tier",
    "runner_group_id", "registration_scope", "min_queued_age_seconds", "replaces_launchd_labels",
    "jit_github_cli", "chrome_app_dir", "assignment_mode",
    "assignment_omit_labels", "supervisors", "process_type",
    "assignment_scan_timeout_seconds", "assignment_scan_max_workers",
    "assignment_top_tier_receipt_max_age_seconds", "assignment_feed_rescue",
    "runner_idle_timeout_seconds", "yield_to_workflow", "yield_to_labels",
}
TIER_KEYS = {"label", "workflow", "runner_group_id"}
LABEL = re.compile(r"^[A-Za-z0-9_.:-]+$")
REPLACED_AGENT = re.compile(
    r"^com[.]danielraffel[.](?:pulp[.]tart-runner|"
    r"[a-z0-9.-]+[.]tart-runner-[a-z0-9.-]+)$"
)
PERSISTENT_AGENT = re.compile(r"^actions[.]runner[.][A-Za-z0-9_.-]+$")
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
    attempt_timeout = host.get("current_job_attempt_timeout_seconds")
    if attempt_timeout is not None and (
            type(attempt_timeout) is not int
            or not 30 <= attempt_timeout <= 600):
        fail(
            "host.current_job_attempt_timeout_seconds must be an integer "
            "from 30 through 600"
        )
    lifecycle_budget = host.get("current_job_lifecycle_budget_seconds")
    if lifecycle_budget is not None and (
            type(lifecycle_budget) is not int
            or not 60 <= lifecycle_budget <= 1800):
        fail(
            "host.current_job_lifecycle_budget_seconds must be an integer "
            "from 60 through 1800"
        )
    # An attempt is lowered to whatever the lifecycle budget has left, so a
    # budget below the attempt silently shortens every observation.
    if (
        attempt_timeout is not None
        and lifecycle_budget is not None
        and lifecycle_budget < attempt_timeout
    ):
        fail(
            "host.current_job_lifecycle_budget_seconds must be at least "
            "host.current_job_attempt_timeout_seconds"
        )
    persistent_labels = host.get("persistent_runner_labels", [])
    if (
        not isinstance(persistent_labels, list)
        or not all(
            isinstance(label, str) and PERSISTENT_AGENT.fullmatch(label)
            for label in persistent_labels
        )
        or len(persistent_labels) != len(set(persistent_labels))
    ):
        fail(
            "host.persistent_runner_labels must contain unique actions.runner.* labels"
        )
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
            "apply": False, "max_trees": 8,
            "max_gib": 512, "timeout_seconds": 300, "cooldown_seconds": 3600,
        }
        if not isinstance(cleanup, dict) or set(cleanup) != WORKTREE_CLEANUP_KEYS:
            fail("worktree_cleanup must declare the complete strict contract")
        expected["apply"]=cleanup.get("apply")
        if cleanup != expected or type(cleanup.get("apply")) is not bool:
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
        process_type = lane.get("process_type")
        if process_type is not None and process_type not in PROCESS_TYPES:
            fail(
                f"lane {lane_id}: process_type must be one of "
                f"{list(PROCESS_TYPES)}"
            )
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
        feed_rescue = lane.get("assignment_feed_rescue")
        if feed_rescue is not None and (
                assignment_mode != "event-class-v2"
                or type(feed_rescue) is not bool):
            fail(
                f"lane {lane_id}: assignment_feed_rescue must be a boolean on "
                "an event-class-v2 lane"
            )
        idle_timeout = lane.get("runner_idle_timeout_seconds")
        if idle_timeout is not None and (
                type(idle_timeout) is not int or not 1 <= idle_timeout <= 3600):
            fail(
                f"lane {lane_id}: runner_idle_timeout_seconds must be an "
                "integer from 1 through 3600"
            )
        yield_workflow = lane.get("yield_to_workflow")
        yield_labels = lane.get("yield_to_labels")
        if (yield_workflow is None) != (yield_labels is None):
            fail(
                f"lane {lane_id}: yield_to_workflow and yield_to_labels must "
                "be declared together"
            )
        if yield_workflow is not None and (
                not isinstance(yield_workflow, str)
                or not yield_workflow.strip()
                or any(char in yield_workflow for char in "\r\n|")):
            fail(f"lane {lane_id}: yield_to_workflow must be one workflow name")
        if yield_labels is not None and (
                not isinstance(yield_labels, list)
                or not yield_labels
                or not all(isinstance(value, str) and LABEL.fullmatch(value)
                           for value in yield_labels)
                or len(yield_labels) != len(set(yield_labels))
                or not REQUIRED_BASE_LABELS.issubset(yield_labels)):
            fail(
                f"lane {lane_id}: yield_to_labels must be unique and include "
                f"{sorted(REQUIRED_BASE_LABELS)}"
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
        tier_group_by_label = {}
        for tier in tiers:
            label = tier["label"]
            group_id = tier.get("runner_group_id")
            if (
                label in tier_group_by_label
                and tier_group_by_label[label] != group_id
            ):
                fail(
                    f"lane {lane_id}: workflows sharing tier class label {label} "
                    "must use the same runner_group_id"
                )
            tier_group_by_label[label] = group_id
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
        release_tiers = [
            (tier["label"], tier["workflow"], tier.get("runner_group_id"))
            for tier in tiers
            if tier["label"].startswith("pulp-release-")
        ]
        if lane_id == "pulp-release":
            expected_release_tiers = [
                ("pulp-release-tagged", "Release CLI", 1),
                ("pulp-release-tagged", "Sign and Release", 1),
                ("pulp-release-pr-gate", "Release-path PR gate", 1),
            ]
            expected_yield_labels = [
                "self-hosted", "macOS", "ARM64", "pulp-build",
                "pulp-build-vm", "pulp-gate-fast", "pulp-build-pr-head",
                "pulp-build-merge-group",
            ]
            if (
                lane["repo"] != "Generous-Corp/pulp"
                or runner_group_id != 1
                or registration_scope != "repository"
                or lane["labels"] != [
                    "self-hosted", "macOS", "ARM64", "pulp-build-vm-release"
                ]
                or release_tiers != expected_release_tiers
                or len(release_tiers) != len(tiers)
                or supervisors != 1
                or idle_timeout != 60
                or yield_workflow != "Build and Test"
                or yield_labels != expected_yield_labels
                or priority is not None
                or assignment_mode is not None
                or replacements != [
                    "com.danielraffel.pulp.tart-runner-macos-release"
                ]
            ):
                fail(
                    "lane pulp-release must preserve the repository-scoped "
                    "group-1 M5 release controller contract"
                )
        elif release_tiers:
            fail("pulp release workflow tiers are reserved for lane pulp-release")
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


def persistent_plist_records(data: dict, agents_dir: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for label in data["host"].get("persistent_runner_labels", []):
        name = f"{label}.plist"
        installed = agents_dir / name
        if installed.is_symlink() or not installed.is_file():
            fail(f"declared persistent runner plist is unavailable: {installed}")
        try:
            value = plistlib.loads(installed.read_bytes())
        except plistlib.InvalidFileException as exc:
            fail(f"declared persistent runner plist is malformed: {installed}: {exc}")
        if value.get("Label") != label:
            fail(f"declared persistent runner plist label does not match: {installed}")
        info = installed.stat()
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode):
            fail(f"declared persistent runner plist is not a regular file: {installed}")
        if info.st_uid != os.getuid():
            fail(f"declared persistent runner plist is not owned by the caller: {installed}")
        if not (mode & stat.S_IRUSR) or mode & (stat.S_IWGRP | stat.S_IWOTH):
            fail(
                f"declared persistent runner plist must be owner-readable and "
                f"not group/world-writable: "
                f"{installed} has {mode:04o}"
            )
        records[name] = {
            "sha256": hashlib.sha256(installed.read_bytes()).hexdigest(),
            "mode": mode,
            "owner_uid": info.st_uid,
        }
    return records


def _sealed_bundle_root(program: Path) -> Path | None:
    """Return the app bundle whose sealed cohort `program` would execute."""
    parents = program.parents
    if len(parents) < 3:
        return None
    bundle = parents[2]
    if parents[0].name != "MacOS" or parents[1].name != "Contents":
        return None
    return bundle if bundle.suffix == ".app" else None


def assert_executed_generation(
    agents_dir: Path,
    names: Iterable[str],
    *,
    launch_entrypoint: Path,
    source_commit: str,
    manifest_sha256: str,
) -> dict[str, dict]:
    """Prove every installed LaunchAgent executes the support generation it records.

    Staging a generation no LaunchAgent can reach is a no-op that still writes a
    receipt, so the evidence is the exec path in the installed plist's
    ProgramArguments rather than any exit code. A signed launcher bundle execs
    its own sealed copy of the cohort, and that copy counts as the generation
    only while it carries the exact installed commit and manifest digest; a
    bundle sealed around an older cohort keeps running the older code no matter
    what a newer generation put on disk.
    """
    launch_entrypoint = launch_entrypoint.resolve()
    records: dict[str, dict] = {}
    for name in sorted(names):
        installed = agents_dir / name
        if installed.is_symlink() or not installed.is_file():
            fail(f"install_ineffective: fleet LaunchAgent is unavailable: {installed}")
        try:
            value = plistlib.loads(installed.read_bytes())
        except (plistlib.InvalidFileException, ValueError) as exc:
            fail(f"install_ineffective: fleet LaunchAgent is malformed: {installed}: {exc}")
        arguments = value.get("ProgramArguments")
        if (not isinstance(arguments, list) or not arguments
                or not all(isinstance(argument, str) for argument in arguments)):
            fail(
                "install_ineffective: fleet LaunchAgent declares no executable "
                f"program: {installed}"
            )
        if any(
            argument == str(launch_entrypoint)
            or (argument.startswith("/") and Path(argument).resolve() == launch_entrypoint)
            for argument in arguments
        ):
            records[name] = {
                "kind": "generation", "executes": str(launch_entrypoint),
            }
            continue
        program = Path(arguments[0])
        bundle = _sealed_bundle_root(program)
        if bundle is None:
            fail(
                f"install_ineffective: launcher execs {program} which is not the "
                f"installed generation {launch_entrypoint} ({name})"
            )
        sealed_launch = bundle / SEALED_SUPPORT / support_manifest.LAUNCH_NAME
        if sealed_launch.is_symlink() or not sealed_launch.is_file():
            fail(
                f"install_ineffective: launcher execs {sealed_launch} which is not "
                f"the installed generation {launch_entrypoint}: the sealed cohort "
                f"has no launch entrypoint ({name})"
            )
        try:
            metadata = json.loads((bundle / SEALED_METADATA).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            fail(
                f"install_ineffective: launcher execs {sealed_launch} which is not "
                f"the installed generation {launch_entrypoint}: sealed cohort "
                f"metadata is unreadable: {exc} ({name})"
            )
        if not isinstance(metadata, dict):
            fail(
                f"install_ineffective: launcher execs {sealed_launch} which is not "
                f"the installed generation {launch_entrypoint}: sealed cohort "
                f"metadata is malformed ({name})"
            )
        sealed_commit = str(metadata.get("source_commit", "")) or "<missing>"
        sealed_manifest = str(metadata.get("support_manifest_sha256", "")) or "<missing>"
        if sealed_commit != source_commit or sealed_manifest != manifest_sha256:
            fail(
                f"install_ineffective: launcher execs {sealed_launch} which is not "
                f"the installed generation {launch_entrypoint}: sealed cohort "
                f"{sealed_commit}/{sealed_manifest} is not installed cohort "
                f"{source_commit}/{manifest_sha256} ({name})"
            )
        records[name] = {
            "kind": "sealed-bundle", "executes": str(sealed_launch),
        }
    if not records:
        fail(
            "install_ineffective: no fleet LaunchAgent executes the installed "
            f"generation {launch_entrypoint}"
        )
    return records


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
    persistent_records = persistent_plist_records(data, agents_dir)
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert_executed_generation(
        agents_dir, expected,
        launch_entrypoint=launch_entrypoint,
        source_commit=support["source_commit"],
        manifest_sha256=manifest_sha256,
    )
    receipt = {
        "schema": 4 if persistent_records else 3,
        "profile": data.get("name", config.stem),
        "config_path": str(config.resolve()),
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "agents_dir": str(agents_dir.resolve()),
        "plists": digests,
        "persistent_plists": persistent_records,
        "retired_launchd_labels": replacements(data),
        "launch_helper": helper_record,
        "support": {
            "root": str(support_root),
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha256,
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
    persistent_expected = bool(data["host"].get("persistent_runner_labels"))
    supported_schemas = ({4} if persistent_expected else ({3} if helper is not None else {2, 3}))
    if receipt.get("schema") not in supported_schemas:
        expected = "4" if persistent_expected else ("3" if helper is not None else "2 or 3")
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
    expected_persistent = persistent_plist_records(data, agents_dir)
    recorded_persistent = receipt.get("persistent_plists", {})
    if recorded_persistent != expected_persistent:
        fail("installed persistent runner plist set does not match the profile receipt")
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
    assert_executed_generation(
        agents_dir, expected,
        launch_entrypoint=launch_entrypoint,
        source_commit=verified_support["source_commit"],
        manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    )
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


def _verify_persistent_loaded_output(
    name: str, payload: bytes, output: str, agents_dir: Path
) -> str:
    label = name.removesuffix(".plist")
    value = plistlib.loads(payload)
    if not re.search(r"^\s*state = running\s*$", output, re.MULTILINE):
        fail(f"loaded persistent LaunchAgent {label} is not running")
    if not re.search(r"^\s*pid = [0-9]+\s*$", output, re.MULTILINE):
        fail(f"loaded persistent LaunchAgent {label} has no numeric pid")
    loaded_path = re.search(r"^\tpath = (.+)$", output, re.MULTILINE)
    if (
        loaded_path is None
        or Path(loaded_path.group(1)).resolve() != (agents_dir / name).resolve()
    ):
        fail(f"loaded persistent LaunchAgent {label} path does not match its receipt")
    arguments = value.get("ProgramArguments")
    if (
        not isinstance(arguments, list)
        or not arguments
        or not all(isinstance(argument, str) and argument for argument in arguments)
    ):
        fail(f"persistent LaunchAgent {label} has invalid ProgramArguments")
    _loaded_has(output, f"\tprogram = {arguments[0]}\n", f"{label} program")
    rendered_arguments = "\targuments = {\n" + "".join(
        f"\t\t{argument}\n" for argument in arguments
    ) + "\t}\n"
    _loaded_has(output, rendered_arguments, f"{label} arguments")
    for key, plist_key in (
        ("working directory", "WorkingDirectory"),
        ("stdout path", "StandardOutPath"),
        ("stderr path", "StandardErrorPath"),
    ):
        expected = value.get(plist_key)
        if not isinstance(expected, str) or not expected:
            fail(f"persistent LaunchAgent {label} has invalid {plist_key}")
        _loaded_has(output, f"\t{key} = {expected}\n", f"{label} {key}")
    environment = value.get("EnvironmentVariables", {})
    if not isinstance(environment, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in environment.items()
    ):
        fail(f"persistent LaunchAgent {label} has invalid EnvironmentVariables")
    environment_start = output.find("\n\tenvironment = {\n")
    environment_end = output.find("\n\t}\n", environment_start + 2)
    if environment_start < 0 or environment_end < 0:
        fail(f"loaded persistent LaunchAgent {label} has no readable environment")
    loaded_environment = output[environment_start:environment_end]
    loaded_keys = set(re.findall(
        r"^\t\t([^\s=]+) =>", loaded_environment, re.MULTILINE
    ))
    allowed_launchd_environment = {"OSLogRateLimit", "XPC_SERVICE_NAME"}
    unexpected_loaded = loaded_keys - set(environment) - allowed_launchd_environment
    missing_loaded = set(environment) - loaded_keys
    if unexpected_loaded or missing_loaded:
        fail(
            f"loaded persistent LaunchAgent {label} environment does not match "
            f"its receipt: missing={sorted(missing_loaded)} "
            f"unexpected={sorted(unexpected_loaded)}"
        )
    for key, expected in environment.items():
        _loaded_has(output, f"\t\t{key} => {expected}\n", f"{label} environment {key}")
    properties = next(
        (line for line in output.splitlines() if line.startswith("\tproperties = ")),
        "",
    )
    if value.get("RunAtLoad") is not True or "runatload" not in properties:
        fail(f"loaded persistent LaunchAgent {label} lost runatload")
    keep_alive = value.get("KeepAlive", False)
    if type(keep_alive) is not bool or ("keepalive" in properties) != keep_alive:
        fail(f"loaded persistent LaunchAgent {label} keepalive does not match")
    session_create = value.get("SessionCreate", False)
    if type(session_create) is not bool or (
        "creates session" in properties
    ) != session_create:
        fail(f"loaded persistent LaunchAgent {label} session creation does not match")
    process_type = value.get("ProcessType")
    if process_type != "Interactive" or not re.search(
        r"^\tspawn type = interactive(?: \([0-9]+\))?$", output, re.MULTILINE
    ):
        fail(f"loaded persistent LaunchAgent {label} process type does not match")
    exit_timeout = value.get("ExitTimeOut", 5)
    if type(exit_timeout) is not int or not _loaded_exit_timeout_matches(output, exit_timeout):
        fail(f"loaded persistent LaunchAgent {label} exit timeout does not match")
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
    persistent_loaded: dict[str, str] = {}
    for name in receipt.get("persistent_plists", {}):
        label = name.removesuffix(".plist")
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                text=True, capture_output=True, check=False, timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            fail(f"timed out reading loaded persistent LaunchAgent {label}: {exc}")
        if result.returncode != 0:
            fail(
                f"could not read loaded persistent LaunchAgent {label}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        persistent_loaded[name] = _verify_persistent_loaded_output(
            name, (agents_dir / name).read_bytes(), result.stdout, agents_dir
        )
    return {
        "schema": 1,
        "verified_at_unix": int(time.time()),
        "install_receipt_path": str(receipt_path.resolve()),
        "install_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "support_root": str(support_root.resolve()),
        "loaded_services": {**loaded, **persistent_loaded},
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


ANCESTRY_WALK_LIMIT = 64


def pid_is_self_or_descendant(
    writer_pid: int, job_pid: int, parents: dict[int, int]
) -> bool:
    """Is writer_pid the launchd job itself, or a process it forked?

    A lane whose Tart home is on an external volume runs through a signed
    launch_helper, so launchd's job pid is the helper and the heartbeat is
    written by a child of it. A lane on the internal disk writes from the job
    pid directly. Both are correct, so identity is ancestry rather than
    equality -- but it stays scoped to THIS lane's job pid, so a wedged lane
    cannot be verified by a neighbour's writer. The walk is bounded because a
    reparenting cycle would otherwise loop forever.
    """
    if writer_pid == job_pid:
        return True
    seen: set[int] = set()
    current = writer_pid
    for _ in range(ANCESTRY_WALK_LIMIT):
        if current in seen:
            return False
        seen.add(current)
        parent = parents.get(current)
        if parent is None or parent <= 1:
            return False
        if parent == job_pid:
            return True
        current = parent
    return False


def launchd_managed_pids() -> set[int]:
    """Pids launchd is currently running, under any label.

    Reparenting to init is normal for a launchd job, so ppid == 1 is not
    evidence of abandonment. Without this, every managed supervisor outside
    the fleet lanes -- the release lane in particular -- reports as orphaned.
    """
    try:
        listed = subprocess.run(
            ["launchctl", "list"],
            text=True, capture_output=True, check=False, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return set()
    if listed.returncode != 0:
        return set()
    managed: set[int] = set()
    for line in listed.stdout.splitlines()[1:]:
        field = line.split("\t", 1)[0].strip()
        if field.isdigit():
            managed.add(int(field))
    return managed


def fleet_readiness(
    receipt_path: Path,
    config: Path,
    agents_dir: Path,
    support_root: Path,
    participating: bool,
    pool_state: str,
    stale_heartbeat_seconds: int = 300,
    blocked_serving_seconds: int = 5400,
    blocked_serving_streak: int = 6,
) -> dict:
    """Report realized receipt-backed capacity separately from pool intent."""
    problems: list[dict[str, str]] = []
    serving_blocked_lanes: list[dict[str, object]] = []
    serving_unmeasurable_lanes: list[str] = []
    try:
        receipt = verify_receipt(receipt_path, config, agents_dir, support_root)
    except (OSError, ValueError) as exc:
        # An unverifiable receipt is a fact about the observer, not the fleet.
        # Reporting 0 here renders identically to "no supervisors are running",
        # which is the one reading that provokes an operator to bounce a pool.
        # None means "not checked from here" and is rendered as unknown.
        return {
            "managed": True,
            "fleet_ready": False,
            "verified_running_supervisors": None,
            "expected_supervisors": None,
            "serving": {
                "blocked": None,
                "blocked_lanes": [],
                "unmeasurable_lanes": [],
                "streak_threshold": blocked_serving_streak,
                "blocked_seconds_threshold": blocked_serving_seconds,
            },
            "problems": [{"code": "receipt_mismatch", "detail": str(exc)}],
        }

    labels = sorted(name.removesuffix(".plist") for name in receipt["plists"])
    persistent_labels = sorted(
        name.removesuffix(".plist")
        for name in receipt.get("persistent_plists", {})
    )
    required_labels = labels + persistent_labels
    admission_open = participating and pool_state == "on"
    if participating != (pool_state == "on"):
        problems.append({
            "code": "admission_state_mismatch",
            "detail": f"state={pool_state} participating={int(participating)}",
        })
    loaded_outputs: dict[str, str] = {}
    persistent_loaded_outputs: dict[str, str] = {}
    for label in required_labels:
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                text=True, capture_output=True, check=False, timeout=5,
            )
        except subprocess.TimeoutExpired:
            problems.append({"code": "launchctl_probe_timeout", "label": label})
            continue
        if result.returncode == 0:
            if label in persistent_labels:
                persistent_loaded_outputs[label] = result.stdout
            else:
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
    process_parents: dict[int, int] = {}
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
                process_parents[pid] = ppid
                command = match.group(4)
                if (
                    ppid == 1
                    and generation_prefix in command
                    and provider_suffix in command
                ):
                    managed_supervisor_pids.add(pid)
            candidates = managed_supervisor_pids - expected_pids
            if candidates:
                candidates -= launchd_managed_pids()
            for orphan_pid in sorted(candidates):
                problems.append({
                    "code": "orphaned_supervisor", "label": str(orphan_pid),
                    "detail": process_starts[orphan_pid],
                })

    verified_running = 0
    persistent_verified = 0
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
                    try:
                        writer_pid = int(str(candidate.get("supervisor_pid")))
                    except (TypeError, ValueError):
                        continue
                    if (
                        pid_is_self_or_descendant(writer_pid, pid, process_parents)
                        and " ".join(str(candidate.get(
                            "supervisor_pid_started_at", ""
                        )).split()) == process_starts.get(writer_pid, "")
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
                # A fresh heartbeat proves the supervisor is alive, not that it
                # is serving. One that keeps taking a slot against real queued
                # demand and then failing before assignment heartbeats normally
                # forever, so age alone cannot see it. Absent field = a
                # generation that predates it; treat that as "not measurable"
                # rather than "not blocked".
                #
                # This does NOT decrement verified_running and does NOT become a
                # `problems` entry, so it cannot clear `fleet_ready`. The two
                # answer different questions: `fleet_ready` is host-local and
                # host-fixable, while the dominant cause of a blocked lane is
                # upstream and hits every lane on every host at once. Gating the
                # fleet on an upstream condition it cannot fix converts a
                # serving outage into a control-plane outage, and destroys the
                # warm supervisors that are the recovery path. It is reported
                # instead as its own named state, which is loud on its own.
                streak_raw = matching_state.get("serving_blocked_streak")
                if streak_raw is None:
                    serving_unmeasurable_lanes.append(label)
                blocked_since = str(matching_state.get("serving_blocked_since", "") or "")
                if blocked_since:
                    try:
                        blocked_at = dt.datetime.fromisoformat(
                            blocked_since.replace("Z", "+00:00")
                        )
                    except (TypeError, ValueError):
                        problems.append({
                            "code": "serving_blocked_invalid", "label": label,
                            "detail": blocked_since,
                        })
                        continue
                    streak: int | None
                    if streak_raw is None:
                        streak = None
                    else:
                        try:
                            streak = int(streak_raw)
                        except (TypeError, ValueError):
                            problems.append({
                                "code": "serving_blocked_streak_invalid",
                                "label": label, "detail": str(streak_raw),
                            })
                            continue
                    blocked_for = (now - blocked_at).total_seconds()
                    # Two gates doing two jobs. The streak is the SHAPE gate: it
                    # counts consecutive work entries that served nothing, so a
                    # lane whose failures are interleaved with served jobs never
                    # reaches it however many errors it logs. The duration is the
                    # TRANSIENCE gate, so an upstream blip cannot raise a
                    # fleet-wide alarm. A pre-streak generation has only the
                    # duration gate, which is the behaviour it already had.
                    if blocked_for > blocked_serving_seconds and (
                        streak is None or streak >= blocked_serving_streak
                    ):
                        serving_blocked_lanes.append({
                            "label": label,
                            "blocked_seconds": int(blocked_for),
                            "streak": streak,
                            "last_phase": str(
                                matching_state.get("serving_blocked_last_phase", "") or ""
                            ),
                        })
                verified_running += 1

    if admission_open and len(persistent_loaded_outputs) == len(persistent_labels):
        for label, output in persistent_loaded_outputs.items():
            name = f"{label}.plist"
            try:
                _verify_persistent_loaded_output(
                    name, (agents_dir / name).read_bytes(), output, agents_dir
                )
            except (OSError, ValueError) as exc:
                problems.append({
                    "code": "persistent_loaded_receipt_mismatch",
                    "label": label,
                    "detail": str(exc),
                })
            else:
                persistent_verified += 1

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
        "fleet_ready": (
            admission_open
            and verified_running == len(labels)
            and persistent_verified == len(persistent_labels)
            and not problems
        ),
        "verified_running_supervisors": verified_running,
        "expected_supervisors": len(labels),
        "serving": {
            "blocked": bool(serving_blocked_lanes),
            "blocked_lanes": serving_blocked_lanes,
            "unmeasurable_lanes": sorted(serving_unmeasurable_lanes),
            "streak_threshold": blocked_serving_streak,
            "blocked_seconds_threshold": blocked_serving_seconds,
        },
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
    if cleanup is not None and cleanup["apply"] and lane["repo"] == cleanup["repo"] and lane["id"] == "pulp-gate":
        env.update({
            "TARTCI_WORKTREE_CLEANUP_PROVIDER": cleanup["provider"],
            "TARTCI_WORKTREE_CLEANUP_REPO": cleanup["repo"],
            "TARTCI_WORKTREE_CLEANUP_PRIMARY": cleanup["primary"],
            "TARTCI_WORKTREE_CLEANUP_PREFIX": cleanup["prefix"],
            "TARTCI_WORKTREE_CLEANUP_MAIN_REF": cleanup["main_ref"],
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
    if "current_job_attempt_timeout_seconds" in host:
        env["TARTCI_CAPTURE_CURRENT_JOB_ATTEMPT_TIMEOUT_SECS"] = str(
            host["current_job_attempt_timeout_seconds"]
        )
    if "current_job_lifecycle_budget_seconds" in host:
        env["TARTCI_CAPTURE_CURRENT_JOB_LIFECYCLE_BUDGET_SECS"] = str(
            host["current_job_lifecycle_budget_seconds"]
        )
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
            tier_group_by_label = {}
            for row in lane["tier"]:
                tier_group_by_label.setdefault(
                    row["label"], row["runner_group_id"]
                )
            env["TARTCI_RUNNER_WORKFLOW_TIER_GROUPS"] = "\n".join(
                f"{label}|{group_id}"
                for label, group_id in tier_group_by_label.items()
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
    if lane.get("assignment_feed_rescue"):
        env["TARTCI_ASSIGNMENT_FEED_RESCUE"] = "1"
    if "runner_idle_timeout_seconds" in lane:
        env["TARTCI_RUNNER_IDLE_TIMEOUT_SECS"] = str(
            lane["runner_idle_timeout_seconds"]
        )
    if "yield_to_workflow" in lane:
        env["TARTCI_YIELD_TO_WORKFLOW_NAME"] = lane["yield_to_workflow"]
        env["TARTCI_YIELD_TO_LABELS"] = ",".join(lane["yield_to_labels"])
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
        "ProcessType": lane.get("process_type", DEFAULT_PROCESS_TYPE),
        # Give the supervisor's TERM trap a deterministic cleanup window and
        # retain launchd ownership of ordinary provider descendants.
        "ExitTimeOut": 30,
        "AbandonProcessGroup": False,
        "EnvironmentVariables": env,
    }


# ── Advertised labels (offline) ─────────────────────────────────────────────
#
# What GitHub sees at generate-jitconfig is decided by the provider supervisor,
# not by the profile: providers/tart-macos/runner.sh `select_work` and
# assignment-v2.lib.sh turn the rendered lane environment into the label set of
# each JIT registration. The snapshot below replays that exact rule over the
# environment `lane_plist` renders, so a renderer change and a profile change
# are both reflected, and no host or GitHub call is needed.

ADVERTISED_LABELS_SCHEMA = "tartci.advertised-labels/v1"
ADVERTISED_LABELS_REPO = "danielraffel/tartci"
# runner.sh defaults for a variable the rendered environment leaves unset.
_RUNNER_DEFAULT_LABELS = "self-hosted,macOS,ARM64,pulp-build-vm"
_RUNNER_DEFAULT_WORKFLOW = "Build and Test"
_RUNNER_DEFAULT_OMIT = "pulp-gate-fast"
_RUNNER_DEFAULT_CLASSES = "pulp-build-merge-group,pulp-build-pr-head"


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _dedupe_labels(labels: Iterable[str]) -> list[str]:
    """GitHub label sets are case-insensitive; keep the first spelling."""
    seen: set[str] = set()
    out: list[str] = []
    for label in labels:
        key = label.lower()
        if key not in seen:
            seen.add(key)
            out.append(label)
    return out


def registrations_from_env(env: dict[str, str]) -> list[dict]:
    """Replay runner.sh's registration-label rule over a lane environment.

    Returns one row per distinct registration: its assignment mode, class
    label, the exact label set passed to generate-jitconfig, and the workflow
    names that registration mints for.
    """
    labels = _csv(env.get("TARTCI_RUNNER_LABELS") or _RUNNER_DEFAULT_LABELS)
    mode = env.get("TARTCI_RUNNER_ASSIGNMENT_MODE") or "legacy"
    if mode not in ("legacy", "observe", "event-class-v2"):
        raise ValueError(f"unsupported assignment mode: {mode}")
    tiers: list[tuple[str, str]] = []
    for raw in (env.get("TARTCI_RUNNER_WORKFLOW_TIERS") or "").splitlines():
        entry = raw.rstrip("\r")
        if not entry:
            continue
        if "|" not in entry:
            raise ValueError(f"invalid workflow tier entry: {entry}")
        tier_label, workflow = entry.split("|", 1)
        if not tier_label or not workflow or "," in tier_label:
            raise ValueError(f"invalid workflow tier entry: {entry}")
        tiers.append((tier_label, workflow))
    if not tiers:
        if mode == "event-class-v2":
            raise ValueError("event-class-v2 requires workflow tiers")
        names = [
            line.rstrip("\r")
            for line in (env.get("TARTCI_RUNNER_WORKFLOW_NAMES") or "").splitlines()
            if line.rstrip("\r")
        ] or [env.get("TARTCI_RUNNER_WORKFLOW_NAME") or _RUNNER_DEFAULT_WORKFLOW]
        return [{
            "assignment_mode": "legacy",
            "class_label": None,
            "labels": _dedupe_labels(labels),
            "workflows": list(dict.fromkeys(names)),
        }]
    ordered: list[str] = []
    workflows: dict[str, list[str]] = {}
    for tier_label, workflow in tiers:
        if tier_label not in workflows:
            ordered.append(tier_label)
            workflows[tier_label] = []
        if workflow not in workflows[tier_label]:
            workflows[tier_label].append(workflow)
    if mode == "event-class-v2":
        omitted = {item.lower() for item in _csv(
            env.get("TARTCI_ASSIGNMENT_V2_OMIT_LABELS", _RUNNER_DEFAULT_OMIT))}
        classes = {item.lower() for item in _csv(
            env.get("TARTCI_ASSIGNMENT_V2_CLASS_LABELS", _RUNNER_DEFAULT_CLASSES))}
        base = [label for label in labels if label.lower() not in omitted | classes]
        if not base:
            raise ValueError("V2 assignment omitted every configured runner label")
        effective = "event-class-v2"
    else:
        # `observe` mints the legacy selection; only its log line differs.
        base = labels
        effective = "legacy"
    return [{
        "assignment_mode": effective,
        "class_label": tier_label,
        "labels": _dedupe_labels([*base, tier_label]),
        "workflows": workflows[tier_label],
    } for tier_label in ordered]


def advertised_registrations(data: dict) -> list[dict]:
    """Every JIT registration a loaded profile's lanes can mint."""
    host_id = data["host"]["id"]
    profile = data.get("name") or host_id
    rows: list[dict] = []
    for lane in data["lane"]:
        env = lane_plist(data, lane)["EnvironmentVariables"]
        for registration in registrations_from_env(env):
            rows.append({
                "profile": profile,
                "host_id": host_id,
                "lane": lane["id"],
                "repo": lane["repo"],
                **registration,
            })
    return rows


def git_head(root: Path) -> str | None:
    """HEAD of the checkout holding ROOT, or None outside a git checkout."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


def _display_path(path: Path) -> str:
    root = Path(__file__).resolve().parents[1]
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return str(path)


TARTCI_ROOT = Path(__file__).resolve().parents[1]
PUBLISHED_SUPPLY = TARTCI_ROOT / "fleet" / "advertised-labels.json"
PUBLISHED_SUPPLY_URL = (
    "https://raw.githubusercontent.com/danielraffel/tartci/main/fleet/advertised-labels.json"
)
FLEET_PROFILE_GLOB = "*-macos-fleet.toml"
_PERSISTENT_RUNNER_NAME = re.compile(
    r"^actions\.runner\.[A-Za-z0-9_.-]+\.(?P<name>[A-Za-z0-9_.-]+)$")


def fleet_profiles(root: Path = TARTCI_ROOT) -> list[Path]:
    """Every checked-in macOS fleet profile. Discovery is the glob: no host list."""
    return sorted((root / "profiles").glob(FLEET_PROFILE_GLOB))


def persistent_runners(data: dict) -> list[dict]:
    """Host-owned persistent Actions services a profile declares.

    Their registered name is the launchd label's last component; their labels
    are set at registration outside tartci, so only the name is declared.
    """
    host_id = data["host"]["id"]
    rows = []
    for label in data["host"].get("persistent_runner_labels", []) or []:
        match = _PERSISTENT_RUNNER_NAME.fullmatch(label)
        rows.append({
            "profile": data.get("name") or host_id,
            "host_id": host_id,
            "launchd_label": label,
            "runner_name": match.group("name") if match else None,
        })
    return rows


def advertised_labels_snapshot(paths: list[Path], commit: str | None) -> dict:
    registrations: list[dict] = []
    persistent: list[dict] = []
    for path in paths:
        data = load(path)
        registrations.extend(advertised_registrations(data))
        persistent.extend(persistent_runners(data))
    return {
        "schema": ADVERTISED_LABELS_SCHEMA,
        "generated_from": {
            "repo": ADVERTISED_LABELS_REPO,
            "commit": commit,
            "profiles": [_display_path(path) for path in paths],
        },
        "registrations": registrations,
        # Additive to v1: runners a host owns whose labels tartci does not set.
        "persistent_runners": persistent,
    }


def published_snapshot(root: Path = TARTCI_ROOT) -> dict:
    """The committed form: every fleet profile, commit null.

    A file cannot name the commit that contains it, so the published copy
    carries no commit; its provenance is the git ref it was read from.
    """
    return advertised_labels_snapshot(fleet_profiles(root), None)


def render_published(snapshot: dict) -> str:
    return json.dumps(snapshot, indent=2) + "\n"


def read_published(source: str | Path, timeout: float = 15.0) -> dict:
    text: str
    if isinstance(source, str) and source.startswith(("https://", "http://")):
        import urllib.request
        with urllib.request.urlopen(source, timeout=timeout) as response:  # noqa: S310
            text = response.read().decode("utf-8")
    else:
        text = Path(source).read_text()
    value = json.loads(text)
    if not isinstance(value, dict) or value.get("schema") != ADVERTISED_LABELS_SCHEMA:
        raise ValueError(f"not a {ADVERTISED_LABELS_SCHEMA} document")
    if not isinstance(value.get("registrations"), list):
        raise ValueError("published snapshot has no registrations array")
    return value


# ── Supply verification: installed vs declared ──────────────────────────────

MATCH = "MATCH"
INSTALLED_ONLY = "INSTALLED_ONLY"
DECLARED_ONLY = "DECLARED_ONLY"
LABELS_DIFFER = "LABELS_DIFFER"
SUPPLY_UNKNOWN = "UNKNOWN"


def _registration_key(row: dict) -> tuple[str, str]:
    return row["lane"], row.get("class_label") or ""


def _registration_facts(row: dict) -> dict:
    return {
        "repo": row["repo"].lower(),
        "labels": sorted(label.lower() for label in row["labels"]),
        "workflows": sorted(row["workflows"]),
        "assignment_mode": row["assignment_mode"],
    }


def verify_supply(installed: Path, published: dict | None,
                  published_error: str = "") -> dict:
    """Compare this host's installed registrations with the declared ones.

    `state` is `match`, `mismatch`, or `unknown`; unknown is never a match.
    """
    result: dict = {"schema": "tartci.supply-verify/v1", "installed": str(installed),
                    "host_id": None, "state": "unknown", "reason": None, "lanes": []}
    if published is None:
        result["reason"] = f"published supply unreadable: {published_error}"
        return result
    try:
        data = load(installed)
    except FileNotFoundError:
        result["reason"] = "installed profile does not exist"
        return result
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        result["reason"] = f"installed profile invalid: {exc}"
        return result
    host_id = data["host"]["id"]
    result["host_id"] = host_id
    declared = [row for row in published["registrations"] if row.get("host_id") == host_id]
    if not declared:
        result["reason"] = (f"host_id {host_id!r} declares no registrations in the "
                            "published supply, so nothing can be fact-checked")
        return result
    have = {_registration_key(row): row for row in advertised_registrations(data)}
    want = {_registration_key(row): row for row in declared}
    rows = []
    for key in sorted(set(have) | set(want)):
        lane, class_label = key
        entry: dict = {"lane": lane, "class_label": class_label or None}
        if key not in want:
            entry.update(verdict=INSTALLED_ONLY, installed=have[key]["labels"])
        elif key not in have:
            entry.update(verdict=DECLARED_ONLY, declared=want[key]["labels"])
        else:
            h, w = _registration_facts(have[key]), _registration_facts(want[key])
            if h == w:
                entry.update(verdict=MATCH, labels=have[key]["labels"])
            else:
                entry.update(verdict=LABELS_DIFFER, differs={
                    field: {"installed": h[field], "declared": w[field]}
                    for field in h if h[field] != w[field]})
        rows.append(entry)
    result["lanes"] = rows
    result["state"] = ("match" if all(row["verdict"] == MATCH for row in rows)
                       else "mismatch")
    return result


def render_supply(result: dict) -> str:
    lines = [f"supply verify: {result['state'].upper()} host_id={result['host_id'] or '-'}",
             f"  installed: {result['installed']}"]
    if result["reason"]:
        lines.append(f"  reason: {result['reason']}")
    for row in result["lanes"]:
        name = row["lane"] + (f" [{row['class_label']}]" if row["class_label"] else "")
        detail = ""
        if row["verdict"] == MATCH:
            detail = ",".join(row["labels"])
        elif row["verdict"] == INSTALLED_ONLY:
            detail = "installed registers " + ",".join(row["installed"]) + "; git declares nothing"
        elif row["verdict"] == DECLARED_ONLY:
            detail = "git declares " + ",".join(row["declared"]) + "; not installed here"
        else:
            detail = "; ".join(f"{k}: installed={v['installed']} declared={v['declared']}"
                               for k, v in row["differs"].items())
        lines.append(f"  {row['verdict']:<15} {name}: {detail}")
    return "\n".join(lines)


def reachable(registration: dict, repo: str, workflow: str,
              job_labels: Iterable[str]) -> bool:
    """GitHub semantics: a job's labels must be a case-insensitive subset."""
    have = {label.lower() for label in registration["labels"]}
    return (registration["repo"].lower() == repo.lower()
            and workflow in registration["workflows"]
            and all(label.lower() in have for label in job_labels))


# ── Installed-profile drift ─────────────────────────────────────────────────
#
# The installer copies a checked-in profile to
# ~/.config/tartci/macos-fleet-profile.toml. A later edit on either side is
# invisible to every receipt check, because the receipt binds the installed
# copy to itself. This compares the two semantically, by key path.

DEFAULT_INSTALLED_PROFILE = Path.home() / ".config" / "tartci" / "macos-fleet-profile.toml"
DEFAULT_PROFILES_DIR = Path(__file__).resolve().parents[1] / "profiles"


def _flatten(value: object, prefix: str = "") -> dict[str, object]:
    """Key-path view of a profile. Lanes are keyed by id, not position."""
    out: dict[str, object] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "lane" and prefix == "" and isinstance(child, list):
                for index, lane in enumerate(child):
                    lane_id = lane.get("id") if isinstance(lane, dict) else None
                    name = f"lane[{lane_id if isinstance(lane_id, str) else '#' + str(index)}]"
                    out.update(_flatten(lane, name))
                continue
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(child, dict) or (
                    isinstance(child, list) and child
                    and all(isinstance(item, dict) for item in child)):
                out.update(_flatten(child, path))
            else:
                out[path] = child
    elif isinstance(value, list):
        for index, item in enumerate(value):
            out.update(_flatten(item, f"{prefix}[{index}]"))
    else:
        out[prefix] = value
    return out


def profile_drift(installed: Path, profiles_dir: Path) -> dict:
    """Semantic key diff between an installed profile and its checked-in source.

    `state` is `in_sync`, `drift`, or `unknown`; unknown carries a reason and
    is never reported as in sync.
    """
    result: dict = {
        "schema": "tartci.profile-drift/v1",
        "installed": str(installed),
        "checked_in": None,
        "name": None,
        "state": "unknown",
        "reason": None,
        "missing_in_installed": {},
        "extra_in_installed": {},
        "changed": {},
    }
    try:
        with installed.open("rb") as handle:
            installed_data = tomllib.load(handle)
    except FileNotFoundError:
        result["reason"] = "installed profile does not exist"
        return result
    except (OSError, tomllib.TOMLDecodeError) as exc:
        result["reason"] = f"installed profile unreadable: {exc}"
        return result
    name = installed_data.get("name")
    if not isinstance(name, str) or not name:
        result["reason"] = "installed profile has no `name`, so no checked-in source can be matched"
        return result
    result["name"] = name
    matches: list[tuple[Path, dict]] = []
    for candidate in sorted(profiles_dir.glob("*.toml")):
        try:
            with candidate.open("rb") as handle:
                parsed = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if parsed.get("name") == name:
            matches.append((candidate, parsed))
    if len(matches) != 1:
        result["reason"] = (
            f"{len(matches)} checked-in profiles in {profiles_dir} declare name={name!r}; "
            "exactly one is required"
        )
        return result
    source_path, source_data = matches[0]
    result["checked_in"] = str(source_path)
    have = _flatten(installed_data)
    want = _flatten(source_data)
    result["missing_in_installed"] = {k: want[k] for k in sorted(set(want) - set(have))}
    result["extra_in_installed"] = {k: have[k] for k in sorted(set(have) - set(want))}
    result["changed"] = {
        k: {"installed": have[k], "checked_in": want[k]}
        for k in sorted(set(have) & set(want)) if have[k] != want[k]
    }
    drifted = any(result[k] for k in ("missing_in_installed", "extra_in_installed", "changed"))
    result["state"] = "drift" if drifted else "in_sync"
    return result


def render_profile_drift(result: dict) -> str:
    lines = [f"profile drift: {result['state'].upper()}",
             f"  installed:  {result['installed']}",
             f"  checked-in: {result['checked_in'] or '-'} (name={result['name'] or '-'})"]
    if result["reason"]:
        lines.append(f"  reason: {result['reason']}")
    for key, value in result["missing_in_installed"].items():
        lines.append(f"  - {key} = {value!r}   (checked in, missing from installed)")
    for key, value in result["extra_in_installed"].items():
        lines.append(f"  + {key} = {value!r}   (installed only)")
    for key, pair in result["changed"].items():
        lines.append(f"  ~ {key}: installed={pair['installed']!r} checked_in={pair['checked_in']!r}")
    if result["state"] == "drift":
        lines.append("  remedy: reinstall from the checked-in profile "
                     "(tartci fleet-macos install <profile> --apply), or commit the host edit")
    return "\n".join(lines)


def render_advertised(snapshot: dict) -> str:
    lines = [f"{snapshot['schema']} commit={snapshot['generated_from']['commit'] or '-'}"]
    for row in snapshot["registrations"]:
        lines.append(
            f"{row['profile']}/{row['lane']} [{row['assignment_mode']}] {row['repo']}: "
            f"{','.join(row['labels'])}  <- {' | '.join(row['workflows'])}"
        )
    return "\n".join(lines)


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
    readiness.add_argument("--blocked-serving-seconds", type=int, default=5400)
    readiness.add_argument("--blocked-serving-streak", type=int, default=6)
    advertised = sub.add_parser(
        "advertised-labels",
        help="offline: the exact label set each lane registers with GitHub")
    advertised.add_argument("profiles", nargs="*", type=Path)
    advertised.add_argument("--all", action="store_true",
                            help="every profiles/*-macos-fleet.toml under the tartci root")
    advertised.add_argument("--publish", action="store_true",
                            help="the committed form: --all, commit null, JSON")
    advertised.add_argument("--check", type=Path, metavar="FILE",
                            help="exit 1 unless FILE equals the --publish output")
    advertised.add_argument("--json", action="store_true")
    supply = sub.add_parser(
        "verify-supply",
        help="read-only: installed profile's registrations vs the published supply")
    supply.add_argument("--installed", type=Path, default=None)
    supply.add_argument("--published", default=str(PUBLISHED_SUPPLY),
                        help=f"file or URL (default: repo copy; main: {PUBLISHED_SUPPLY_URL})")
    supply.add_argument("--json", action="store_true")
    drift = sub.add_parser(
        "profile-drift",
        help="read-only: installed profile vs its checked-in source (by name)")
    drift.add_argument("--installed", type=Path, default=DEFAULT_INSTALLED_PROFILE)
    drift.add_argument("--profiles-dir", type=Path, default=DEFAULT_PROFILES_DIR)
    drift.add_argument("--strict", action="store_true",
                       help="exit 1 on drift (exit 2 on unknown in every mode)")
    drift.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "advertised-labels":
            if args.publish or args.check:
                if args.profiles:
                    parser.error("--publish/--check take no profile arguments")
                body = render_published(published_snapshot())
                if args.check:
                    try:
                        current = args.check.read_text()
                    except OSError as exc:
                        print(f"fleet-macos: {args.check}: {exc}", file=sys.stderr)
                        return 1
                    if current != body:
                        print(f"fleet-macos: {args.check} is stale; regenerate with "
                              "`tartci fleet-macos advertised-labels --publish > "
                              "fleet/advertised-labels.json`", file=sys.stderr)
                        return 1
                    print(f"up to date: {args.check}")
                    return 0
                sys.stdout.write(body)
                return 0
            paths = fleet_profiles() if args.all else args.profiles
            if args.all and args.profiles:
                parser.error("--all takes no profile arguments")
            if not paths:
                parser.error("name profile files or pass --all")
            snapshot = advertised_labels_snapshot(paths, git_head(TARTCI_ROOT))
            print(json.dumps(snapshot, indent=2) if args.json else render_advertised(snapshot))
            return 0
        if args.command == "verify-supply":
            installed = args.installed or DEFAULT_INSTALLED_PROFILE
            try:
                published, error = read_published(args.published), ""
            except Exception as exc:  # noqa: BLE001 - any unreadable source is UNKNOWN
                published, error = None, f"{args.published}: {exc}"
            result = verify_supply(installed, published, error)
            print(json.dumps(result, indent=2) if args.json else render_supply(result))
            return {"match": 0, "mismatch": 1}.get(result["state"], 2)
        if args.command == "profile-drift":
            result = profile_drift(args.installed, args.profiles_dir)
            print(json.dumps(result, indent=2, sort_keys=True, default=str)
                  if args.json else render_profile_drift(result))
            if result["state"] == "unknown":
                return 2
            return 1 if args.strict and result["state"] == "drift" else 0
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
                args.blocked_serving_seconds,
                args.blocked_serving_streak,
            ), sort_keys=True))
            return 0
        if args.command == "verify-installed":
            receipt_value = verify_receipt(
                args.receipt, args.config, args.agents_dir, args.support_root
            )
            if args.print_services:
                for name in sorted(receipt_value["plists"]):
                    print(name.removesuffix(".plist"))
                for name in sorted(receipt_value.get("persistent_plists", {})):
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
