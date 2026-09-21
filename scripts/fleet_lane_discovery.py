#!/usr/bin/env python3
"""Discover the macOS fleet lanes a host is actually running, from launchd.

Why this exists
---------------
A fleet lane writes its supervisor heartbeat to its OWN state directory,
``~/.tartci/state/macos-fleet/<identity>/``, named by the lane's installed
plist (``TARTCI_STATE_DIR``). Two observability tools looked somewhere else:
``vm_reap.py`` globbed a single legacy directory (``~/.tartci/state/macos``)
and ``macos_observe.py`` defaulted ``--state-dir`` to that same legacy path.
Neither had ever been taught about the per-lane layout, so on a host running
five fleet lanes they matched zero supervisors and said so as
``no matching macOS supervisors``, next to ``problems=0``.

The fix is not a second hard-coded list of directories -- that is the drift
that caused this. ``fleet_readiness`` already derives each lane's state
directory from the loaded plist, so the lane set has exactly one source of
truth: **launchd's loaded jobs and the plists behind them**. This module
extracts that walk so the readiness check and the observability tools read the
same thing and cannot disagree about which lanes exist.

The corollary, and the reason this module also computes coverage: once the
expected lane count comes from launchd rather than from a glob, "I matched
nothing" and "there is nothing here" stop being the same sentence. A tool can
state its own blindness instead of rendering it as health.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

# A loaded LaunchAgent is a macOS fleet lane when its label carries this
# prefix. Installed by macos_fleet_lanes.py as
# `<prefix><host_id>.<identity>` (e.g. ...macos-fleet.studio.pulp-gate).
FLEET_LABEL_PREFIX = "com.danielraffel.tartci.tart-runner-macos-fleet."


class Lane(NamedTuple):
    """One installed, loaded fleet lane."""

    label: str
    identity: str          # the lane name, e.g. "pulp-gate"
    state_dir: Path | None  # from the plist's TARTCI_STATE_DIR
    runner_name: str        # TARTCI_RUNNER_NAME when the plist declares one


class Coverage(NamedTuple):
    """How much of the expected lane set an observation actually saw."""

    matched: int
    expected: int | None    # None == launchd could not be read; NOT zero
    source: str

    @property
    def shortfall(self) -> bool:
        """True only when we KNOW we saw less than exists.

        An unknown expectation is never a shortfall: reporting one would be
        the same defect this module exists to remove, pointed the other way.
        """
        return self.expected is not None and self.matched < self.expected

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "expected": self.expected,
            "source": self.source,
        }


# ── pure core (unit-tested; no launchctl, no filesystem) ─────────────────────

def fleet_labels(launchctl_list_output: str, prefix: str = FLEET_LABEL_PREFIX) -> list[str]:
    """Extract loaded fleet-lane labels from `launchctl list` output.

    `launchctl list` prints `PID\tSTATUS\tLABEL`. A lane that is installed but
    not loaded does not appear, which is correct: this reports what the host is
    RUNNING, not what it has on disk.
    """
    labels: list[str] = []
    for raw in launchctl_list_output.splitlines():
        parts = raw.rstrip("\n").split("\t")
        if len(parts) < 3:
            continue
        label = parts[2].strip()
        if label.startswith(prefix) and label not in labels:
            labels.append(label)
    return labels


def lane_from_plist(label: str, plist: dict[str, Any], prefix: str = FLEET_LABEL_PREFIX) -> Lane:
    """Build a Lane from one loaded label and its parsed plist.

    A missing TARTCI_STATE_DIR yields state_dir=None rather than a guess. The
    caller reports that as a problem; inventing a default path here is how the
    legacy glob came to be believed in the first place.
    """
    env = plist.get("EnvironmentVariables") or {}
    raw_state_dir = env.get("TARTCI_STATE_DIR")
    state_dir = (
        Path(raw_state_dir).expanduser()
        if isinstance(raw_state_dir, str) and raw_state_dir.strip()
        else None
    )
    runner_name = env.get("TARTCI_RUNNER_NAME")
    identity = label[len(prefix):] if label.startswith(prefix) else label
    # `<host_id>.<identity>` -- the identity is everything after the first dot.
    if "." in identity:
        identity = identity.split(".", 1)[1]
    return Lane(
        label=label,
        identity=identity,
        state_dir=state_dir,
        runner_name=runner_name if isinstance(runner_name, str) else "",
    )


def runner_name_prefixes(lanes: Iterable[Lane]) -> list[str]:
    """Ownership prefixes implied by the discovered lanes.

    Fleet VMs are named `<host_id>-<identity>-...`, which the legacy default
    prefix list (`pulp-,linux-ephr-,win-ephr-,tartci-`) does not cover. Derive
    them from the lanes rather than adding another literal that can drift.
    """
    prefixes: list[str] = []
    for lane in lanes:
        for candidate in (lane.runner_name, f"{lane.identity}-"):
            if not candidate:
                continue
            # A runner name is `<host_id>-<identity>-<pid>-<boot>`; the stable
            # ownership prefix is everything through the identity.
            stem = re.sub(r"-\d+(-\d+)?$", "", candidate)
            stem = stem if stem.endswith("-") else stem + "-"
            if stem and stem not in prefixes:
                prefixes.append(stem)
    return prefixes


def coverage(matched: int, lanes: list[Lane] | None, source: str) -> Coverage:
    """Compare what an observation matched against what launchd says exists."""
    return Coverage(
        matched=matched,
        expected=None if lanes is None else len(lanes),
        source=source,
    )


def coverage_problem(cov: Coverage) -> str | None:
    """The problem string a digest must carry when it saw less than exists.

    This is the poka-yoke. It is not a convention a future tool must remember:
    the expected count is computed from a DIFFERENT source (launchd) than the
    matched count (state files), so a tool that goes blind against the state
    files cannot also silence its own denominator.
    """
    if not cov.shortfall:
        return None
    return (
        f"supervisor_coverage:{cov.matched}/{cov.expected}:"
        f"matched fewer macOS fleet supervisors than {cov.source} reports loaded"
    )


# ── I/O layer ────────────────────────────────────────────────────────────────

def _launchctl_list(timeout: float = 5.0) -> str | None:
    try:
        result = subprocess.run(
            ["launchctl", "list"], text=True, capture_output=True,
            check=False, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def discover_lanes(
    agents_dir: Path | None = None,
    *,
    list_reader: Callable[[], str | None] = _launchctl_list,
    prefix: str = FLEET_LABEL_PREFIX,
) -> tuple[list[Lane] | None, list[str]]:
    """Every loaded macOS fleet lane on this host, with its state directory.

    Returns (lanes, problems). ``lanes is None`` means launchd could not be
    read at all -- an ABSENCE of observation, never an observation of absence,
    so callers must not render it as "no lanes".
    """
    problems: list[str] = []
    listing = list_reader()
    if listing is None:
        return None, ["launchctl_unreadable:cannot enumerate loaded fleet lanes"]

    agents = agents_dir or Path.home() / "Library/LaunchAgents"
    lanes: list[Lane] = []
    for label in fleet_labels(listing, prefix):
        plist_path = agents / f"{label}.plist"
        try:
            plist = plistlib.loads(plist_path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            problems.append(f"lane_plist_unreadable:{label}:{exc}")
            continue
        lane = lane_from_plist(label, plist, prefix)
        if lane.state_dir is None:
            problems.append(f"lane_state_dir_missing:{label}")
        lanes.append(lane)
    return lanes, problems


def lane_state_dirs(lanes: list[Lane] | None) -> list[Path]:
    """The state directories to read, in stable order. Empty when unknown."""
    if not lanes:
        return []
    seen: set[Path] = set()
    dirs: list[Path] = []
    for lane in lanes:
        if lane.state_dir is None or lane.state_dir in seen:
            continue
        seen.add(lane.state_dir)
        dirs.append(lane.state_dir)
    return dirs


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--agents-dir", default=os.environ.get("TARTCI_AGENTS_DIR"))
    args = parser.parse_args(argv)

    lanes, problems = discover_lanes(Path(args.agents_dir) if args.agents_dir else None)
    payload = {
        "lanes": None if lanes is None else [
            {
                "label": lane.label,
                "identity": lane.identity,
                "state_dir": str(lane.state_dir) if lane.state_dir else None,
                "runner_name": lane.runner_name,
            }
            for lane in lanes
        ],
        "problems": problems,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif lanes is None:
        print("fleet lanes: unknown (launchctl unreadable)")
    else:
        print(f"fleet lanes loaded: {len(lanes)}")
        for lane in lanes:
            print(f"  {lane.identity:24} {lane.state_dir or '(no TARTCI_STATE_DIR)'}")
    for problem in problems:
        print(f"  problem: {problem}")
    return 0 if lanes is not None and not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
