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

from macos_runner_identity import resolve_plist_identity


LABEL_PREFIX = "com.danielraffel.pulp.tart-runner"
KNOWN_NON_MACOS_RUNNERS = (
    re.compile(r"(?:^|/)providers/tart-linux/runner\.sh$"),
    re.compile(r"(?:^|/)providers/qemu-windows/runner\.sh$"),
)


def _cached_spec(text: str) -> dict:
    args: list[str] = []
    env: dict[str, str] = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("program = "):
            args = [line.removeprefix("program = ").strip()]
        elif line == "arguments = {":
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


def _plausible_runner_label(label: str) -> bool:
    normalized = label.lower()
    return (
        normalized.startswith(LABEL_PREFIX.lower())
        or "tart-runner" in normalized
        or "tart-macos" in normalized
        or "tartci.macos" in normalized
    )


def _known_non_macos_provider(program_arguments: list[str]) -> bool:
    invocation = list(program_arguments)
    while invocation and invocation[0] in ("/bin/bash", "/bin/sh", "bash", "sh"):
        invocation.pop(0)
    if not invocation:
        return False
    if any(pattern.search(invocation[0]) for pattern in KNOWN_NON_MACOS_RUNNERS):
        return True
    return (
        len(invocation) >= 3
        and re.search(r"(?:^|/)tartci$", invocation[0]) is not None
        and invocation[1] == "serve"
        and invocation[2] in ("linux", "windows")
    )


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
        elif _plausible_runner_label(label):
            print(f"identity guard cannot resolve cached specification for {label}", file=sys.stderr)
            return 2
        else:
            continue
        candidate_args = [str(value) for value in plist.get("ProgramArguments", [])]
        candidate_env = plist.get("EnvironmentVariables", {})
        if _known_non_macos_provider(candidate_args):
            continue
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
            if _plausible_runner_label(label):
                print(
                    f"identity guard cannot prove plausible Tart runner {label} is unrelated",
                    file=sys.stderr,
                )
                return 2
            continue
        try:
            identity = resolve_plist_identity(plist, hostname=args.hostname)
        except Exception as error:
            print(f"identity guard cannot resolve loaded agent {label}: {error}", file=sys.stderr)
            return 2
        resolved = (identity.runner_name, identity.state_file)
        if resolved[0] == expected[0] or resolved[1] == expected[1]:
            conflicts.append((label, resolved))
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
