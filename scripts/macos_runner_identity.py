#!/usr/bin/env python3
"""Canonical Tart macOS runner/state identity derivation."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_LABELS = "self-hosted,macOS,ARM64,pulp-build-vm"


@dataclass(frozen=True)
class RunnerIdentity:
    runner_name: str
    state_dir: str
    state_file: str


def argument(args: Sequence[str], flag: str) -> str:
    try:
        return args[args.index(flag) + 1]
    except (ValueError, IndexError):
        return ""


def derive_identity(
    *,
    name: str = "",
    name_prefix: str = "",
    slot: str = "1",
    labels: str = "",
    state_dir: str = "",
    home: str = "",
    hostname: str,
) -> RunnerIdentity:
    """Resolve exactly the identity used by the macOS supervisor."""
    if not re.fullmatch(r"[0-9]+", slot) or int(slot) < 1:
        raise ValueError("runner slot must be a positive integer")
    resolved_name = name
    prefix = name_prefix
    labels = labels or DEFAULT_LABELS
    if not resolved_name:
        if not prefix:
            classes = [
                label.removeprefix("pulp-build-")
                for label in labels.split(",")
                if label.startswith("pulp-build-") and len(label) > 11
            ]
            if classes:
                prefix = f"pulp-{classes[-1]}"
            else:
                normalized = re.sub(
                    r"[^a-z0-9]+", "-", hostname.lower()
                ).strip("-")
                prefix = f"pulp-{normalized}"
        resolved_name = f"{prefix}-{int(slot):02d}"
    resolved_home = home or str(Path.home())
    resolved_state_dir = state_dir or os.path.join(
        resolved_home, ".tartci/state/macos"
    )
    resolved_state_dir = os.path.abspath(
        os.path.expanduser(resolved_state_dir.replace("$HOME", resolved_home))
    )
    return RunnerIdentity(
        runner_name=resolved_name,
        state_dir=resolved_state_dir,
        state_file=os.path.join(
            resolved_state_dir, f"{resolved_name}.state.json"
        ),
    )


def resolve_plist_identity(
    plist: Mapping[str, Any], *, hostname: str
) -> RunnerIdentity:
    args = [str(value) for value in plist.get("ProgramArguments", [])]
    raw_env = plist.get("EnvironmentVariables", {})
    env = (
        {str(key): str(value) for key, value in raw_env.items()}
        if isinstance(raw_env, Mapping)
        else {}
    )
    home = env.get("HOME", str(Path.home()))
    return derive_identity(
        name=(
            argument(args, "--name")
            or env.get("TARTCI_RUNNER_NAME")
            or env.get("PULP_RUNNER_NAME")
            or ""
        ),
        name_prefix=(
            argument(args, "--name-prefix")
            or env.get("TARTCI_RUNNER_NAME_PREFIX")
            or env.get("PULP_RUNNER_NAME_PREFIX")
            or ""
        ),
        slot=(
            argument(args, "--slot")
            or env.get("TARTCI_RUNNER_SLOT")
            or env.get("PULP_RUNNER_SLOT")
            or "1"
        ),
        labels=(
            argument(args, "--labels")
            or env.get("TARTCI_RUNNER_LABELS")
            or env.get("PULP_RUNNER_LABELS")
            or ""
        ),
        state_dir=(
            argument(args, "--state-dir")
            or env.get("TARTCI_STATE_DIR")
            or ""
        ),
        home=home,
        hostname=hostname,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="")
    parser.add_argument("--name-prefix", default="")
    parser.add_argument("--slot", default="1")
    parser.add_argument("--labels", default="")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--home", default="")
    parser.add_argument("--hostname", required=True)
    args = parser.parse_args(argv)
    identity = derive_identity(
        name=args.name,
        name_prefix=args.name_prefix,
        slot=args.slot,
        labels=args.labels,
        state_dir=args.state_dir,
        home=args.home,
        hostname=args.hostname,
    )
    print(json.dumps(asdict(identity), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
