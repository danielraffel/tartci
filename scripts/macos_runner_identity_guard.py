#!/usr/bin/env python3
"""Fail closed when two loaded Tart macOS agents resolve to one identity."""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path


LABEL_PREFIX = "com.danielraffel.pulp.tart-runner"


def _argument(args: list[str], flag: str) -> str:
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return ""


def resolve_identity(plist: dict, *, hostname: str) -> tuple[str, str]:
    args = [str(value) for value in plist.get("ProgramArguments", [])]
    env = {str(k): str(v) for k, v in plist.get("EnvironmentVariables", {}).items()}
    home = env.get("HOME", str(Path.home()))
    name = _argument(args, "--name") or env.get("TARTCI_RUNNER_NAME") or env.get("PULP_RUNNER_NAME")
    slot = _argument(args, "--slot") or env.get("TARTCI_RUNNER_SLOT") or env.get("PULP_RUNNER_SLOT") or "1"
    if not name:
        prefix = (
            _argument(args, "--name-prefix")
            or env.get("TARTCI_RUNNER_NAME_PREFIX")
            or env.get("PULP_RUNNER_NAME_PREFIX")
        )
        labels = _argument(args, "--labels") or env.get("TARTCI_RUNNER_LABELS") or env.get("PULP_RUNNER_LABELS", "")
        if not prefix:
            classes = [label.removeprefix("pulp-build-") for label in labels.split(",") if label.startswith("pulp-build-") and len(label) > 11]
            if classes:
                prefix = f"pulp-{classes[-1]}"
            else:
                normalized = re.sub(r"[^a-z0-9]+", "-", hostname.lower()).strip("-")
                prefix = f"pulp-{normalized}"
        name = f"{prefix}-{int(slot):02d}"
    state_dir = _argument(args, "--state-dir") or env.get("TARTCI_STATE_DIR") or os.path.join(home, ".tartci/state/macos")
    state_dir = os.path.abspath(os.path.expanduser(state_dir.replace("$HOME", home)))
    return name, os.path.join(state_dir, f"{name}.state.json")


def _cached_spec(text: str) -> dict:
    args: list[str] = []
    env: dict[str, str] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line == "arguments = {":
            section = "args"
        elif line == "environment = {":
            section = "env"
        elif line == "}":
            section = ""
        elif section == "args":
            match = re.fullmatch(r"\d+\s*=\s*(.*)", line)
            if match:
                args.append(match.group(1))
            elif line and "=" not in line:
                args.append(line)
        elif section == "env":
            match = re.fullmatch(r"([^=]+?)\s*=>\s*(.*)", line)
            if match:
                env[match.group(1).strip()] = match.group(2).strip()
    return {"ProgramArguments": args, "EnvironmentVariables": env}


def _service_labels(text: str) -> set[str]:
    labels: set[str] = set()
    in_services = False
    base_indent = 0
    for raw in text.splitlines():
        if not in_services and raw.strip() == "services = {":
            in_services = True
            base_indent = len(raw) - len(raw.lstrip())
            continue
        if in_services:
            indent = len(raw) - len(raw.lstrip())
            if raw.strip() == "}" and indent == base_indent:
                break
            match = re.match(r'\s*"?([A-Za-z0-9_.-]+)"?\s*=>\s*\{', raw)
            if match:
                labels.add(match.group(1))
                continue
            match = re.match(
                r"\s*(?:-|\d+)\s+\S+\s+([A-Za-z0-9_.-]+)\s*$",
                raw,
            )
            if match:
                labels.add(match.group(1))
    return labels


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current-label", required=True)
    parser.add_argument("--runner-name", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--agents-dir", default=str(Path.home() / "Library/LaunchAgents"))
    parser.add_argument("--hostname", default=os.uname().nodename.split(".")[0])
    args = parser.parse_args()
    launchctl = os.environ.get("TARTCI_LAUNCHCTL", "launchctl")
    domain = f"gui/{os.getuid()}"
    expected = (
        args.runner_name,
        os.path.join(os.path.abspath(os.path.expanduser(args.state_dir)), f"{args.runner_name}.state.json"),
    )
    domain_print = subprocess.run(
        [launchctl, "print", domain], capture_output=True, text=True, check=False
    )
    if domain_print.returncode != 0:
        print(f"identity guard cannot enumerate loaded services in {domain}", file=sys.stderr)
        return 2
    loaded_labels = _service_labels(domain_print.stdout)
    disk_specs: dict[str, dict] = {}
    for path in sorted(Path(args.agents_dir).glob("*.plist")):
        try:
            with path.open("rb") as source:
                plist = plistlib.load(source)
        except Exception as error:
            if path.name.startswith(LABEL_PREFIX):
                print(f"identity guard cannot parse candidate {path}: {error}", file=sys.stderr)
                return 2
            continue
        label = plist.get("Label")
        if not isinstance(label, str):
            continue
        disk_specs[label] = plist
        loaded = subprocess.run([launchctl, "print", f"{domain}/{label}"], capture_output=True, text=True, check=False)
        if loaded.returncode == 0:
            loaded_labels.add(label)
    conflicts: list[tuple[str, tuple[str, str]]] = []
    for label in sorted(loaded_labels - {args.current_label}):
        detail = subprocess.run(
            [launchctl, "print", f"{domain}/{label}"], capture_output=True, text=True, check=False
        )
        if detail.returncode != 0:
            continue
        cached = _cached_spec(detail.stdout)
        if cached["ProgramArguments"] or cached["EnvironmentVariables"]:
            plist = cached
        elif label in disk_specs:
            plist = disk_specs[label]
        else:
            print(f"identity guard cannot resolve cached specification for {label}", file=sys.stderr)
            return 2
        candidate_args = [str(value) for value in plist.get("ProgramArguments", [])]
        candidate_env = plist.get("EnvironmentVariables", {})
        direct_runner = any(
            re.search(r"(?:tart-macos/runner\.sh|tools/ci/tart-runner\.sh)$", value)
            for value in candidate_args
        )
        is_macos_runner = (
            "serve" in candidate_args
            and "macos" in candidate_args
            and any("tartci" in value for value in candidate_args)
        ) or direct_runner or "TARTCI_LAUNCHD_LABEL" in candidate_env
        if not is_macos_runner:
            continue
        try:
            identity = resolve_identity(plist, hostname=args.hostname)
        except Exception as error:
            print(f"identity guard cannot resolve loaded agent {label}: {error}", file=sys.stderr)
            return 2
        if identity[0] == expected[0] or identity[1] == expected[1]:
            conflicts.append((label, identity))
    if conflicts:
        for label, identity in conflicts:
            print(
                f"refusing duplicate loaded macOS runner: {args.current_label} and {label} "
                f"resolve to runner={identity[0]} state={identity[1]}",
                file=sys.stderr,
            )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
