#!/usr/bin/env python3
"""Plan, then remove, Tart images and VM husks nothing references.

Why this exists: Tart golden rollouts leave tagged local images behind
(`pulp-build-runner:m1-rollback-20260826-pre-m3`, `pulp-linux-build:gh-...`),
and a lane slot that dies after `tart clone` but before its VM is written
leaves an empty `$TART_HOME/vms/<slot>` directory holding only a socket. On
2026-10-05 m1 held about 380 GiB of such images from July and August, m5
about 290 GiB and 303 empty slot directories, m3 hundreds more. No tartci
tool removed either: vm_reap.py deletes only VMs its state files own, and
`goldens.sh --prune` covers Windows qcow2 goldens.

    tart_image_prune.py            # plan (the default): print verdicts, change nothing
    tart_image_prune.py --apply    # delete what the plan says it would

An IMAGE is a local Tart VM whose name carries a tag (`base:tag`) or starts
with the base of a golden the host references. It is deleted (`tart delete`)
only when every gate holds:

  * no installed fleet profile value names it, and its base is not named by a
    Pulp vm-image manifest (`.shipyard/vm-image*.toml` in the profile's
    [reclaim] repo), which keeps every tag of a manifest image;
  * no file under ~/.config/tartci, ~/.tartci/state or a LaunchAgent plist
    names it (self-update receipts, golden-sync state, lane plists);
  * it is not running and nothing holds a file open inside its directory;
  * it was not accessed for --min-idle-days (default 14);
  * it is not a `:latest` tag (a pipeline's live image: Pulp's bake tiers
    macos-build-base and macos-apple-xcode, another repo's image), so only
    superseded variants (dated rollbacks, quarantines, `incoming`) qualify;
  * it is not the newest `rollback` image of its base, which is always kept.

Other local VMs (lane slots such as m1-pulp-gate-01-*) are vm_reap.py's and
are only listed. An EMPTY VM DIRECTORY is one under $TART_HOME/vms that
`tart list` does not know, holds no config.json or disk image, and is older
than an hour; it is removed. Tart's own `cache` and `tmp` are never touched.
Anything that cannot be read keeps the entry.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Iterable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - launchd hosts run 3.11+
    tomllib = None  # type: ignore[assignment]

# scripts/debug_hold.py HELD_PREFIX; this module stays import-light.
HELD_PREFIX = "held-"
GIB = 1024 ** 3
DAY = 86400
DEFAULT_MIN_IDLE_DAYS = 14
EMPTY_DIR_MIN_AGE_S = 3600
SCAN_FILE_MAX_BYTES = 4 * 1024 * 1024
TART_INTERNAL = frozenset({"cache", "tmp", "OCIs"})
DISK_FILES = ("config.json", "disk.img", "nvram.bin")

Runner = Callable[..., subprocess.CompletedProcess]


def profile_strings(profile: pathlib.Path) -> tuple[set[str], dict[str, Any]]:
    """Every string value in the installed profile, and the parsed table."""
    if tomllib is None:
        raise RuntimeError("no tomllib (needs Python 3.11+)")
    with profile.open("rb") as handle:
        data = tomllib.load(handle)
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return found, data


def manifest_bases(repo: pathlib.Path | None) -> set[str]:
    """`name` of every Pulp vm-image manifest: the images a bake produces."""
    bases: set[str] = set()
    if repo is None or tomllib is None:
        return bases
    for manifest in sorted((repo / ".shipyard").glob("vm-image*.toml")):
        try:
            with manifest.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, ValueError):
            continue
        for table in (data, *[v for v in data.values() if isinstance(v, dict)]):
            name = table.get("name") if isinstance(table, dict) else None
            if isinstance(name, str) and "/" not in name and name not in {"ccache", "fetchcontent"}:
                bases.add(name)
    return bases


def reference_text(paths: Iterable[pathlib.Path]) -> str:
    """The text of every small file under `paths` (receipts, plists, state)."""
    chunks = []
    for root in paths:
        if root.is_file():
            files: Iterable[pathlib.Path] = [root]
        elif root.is_dir():
            files = (p for p in root.rglob("*") if p.is_file())
        else:
            continue
        for path in files:
            try:
                if path.stat().st_size <= SCAN_FILE_MAX_BYTES:
                    chunks.append(path.read_text(errors="replace"))
            except OSError:
                continue
    return "\n".join(chunks)


def named_in(name: str, text: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_.:-])" + re.escape(name) + r"(?![A-Za-z0-9_.:-])",
                     text) is not None


def base_of(name: str) -> str:
    return name.split(":", 1)[0]


def open_paths(runner: Runner) -> list[str] | None:
    try:
        proc = runner(["lsof", "-w", "-n", "-P", "-Fn"], capture_output=True, text=True,
                      timeout=180, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    paths = [line[1:] for line in proc.stdout.splitlines() if line.startswith("n/")]
    return paths or None


def accessed_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def plan(vms: list[dict[str, Any]], *, vms_dir: pathlib.Path, profile_values: set[str],
         manifest: set[str], text: str, opened: list[str] | None, now: float,
         min_idle_days: float) -> dict[str, Any]:
    local = [vm for vm in vms if vm.get("Source") == "local" and isinstance(vm.get("Name"), str)]
    names = {vm["Name"] for vm in local}
    goldens = {base_of(v) for v in profile_values if v in names} | manifest

    def image_like(name: str) -> bool:
        base = base_of(name)
        return ":" in name or any(base == g or base.startswith(g + "-") for g in goldens)

    newest_rollback: dict[str, tuple[float, str]] = {}
    for vm in local:
        if "rollback" in vm["Name"] and image_like(vm["Name"]):
            ts = accessed_ts(vm.get("Accessed")) or 0
            base = base_of(vm["Name"])
            if ts >= newest_rollback.get(base, (-1, ""))[0]:
                newest_rollback[base] = (ts, vm["Name"])

    images, slots = [], []
    for vm in sorted(local, key=lambda v: v["Name"]):
        name = vm["Name"]
        size = int(vm.get("Size") or 0) * GIB
        if name.startswith(HELD_PREFIX):
            # A failed gate VM kept for debugging (scripts/debug_hold.py owns
            # its expiry); never an image this prunes.
            images.append({"name": name, "verdict": "keep", "reason": "debug_hold",
                           "accessed": vm.get("Accessed"), "size_bytes": size})
            continue
        if not image_like(name):
            slots.append({"name": name, "state": vm.get("State"), "size_bytes": size})
            continue
        directory = vms_dir / name
        reason = None
        if name in profile_values:
            reason = "named_by_profile"
        elif base_of(name) in manifest:
            reason = "vm_image_manifest"
        elif named_in(name, text):
            reason = "named_by_state"
        elif name.endswith(":latest") or ":" not in name and name in goldens:
            # A pipeline's live tag (bake tiers macos-build-base:latest,
            # another repo's vellum-authority-linux:latest): never superseded.
            reason = "latest_tag"
        elif re.search(r"quarantine|proof|evidence|forensic", name):
            # Kept for an investigation; its owner removes it.
            reason = "evidence_name"
        elif vm.get("Running") or vm.get("State") == "running":
            reason = "running"
        elif opened is None:
            reason = "open_files_unknown"
        elif any(p == str(directory) or p.startswith(str(directory) + os.sep) for p in opened):
            reason = "open_files"
        elif newest_rollback.get(base_of(name), (0, None))[1] == name:
            reason = "newest_rollback"
        else:
            ts = accessed_ts(vm.get("Accessed"))
            if ts is None:
                reason = "accessed_unknown"
            elif now - ts < min_idle_days * DAY:
                reason = "recent"
        images.append({"name": name, "verdict": "keep" if reason else "delete",
                       "reason": reason or f"unreferenced, idle >= {min_idle_days:g} days",
                       "accessed": vm.get("Accessed"), "size_bytes": size})

    empty = []
    try:
        entries = sorted(os.scandir(vms_dir), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        if entry.name in TART_INTERNAL or entry.name in names or entry.is_symlink() \
                or not entry.is_dir(follow_symlinks=False):
            continue
        path = pathlib.Path(entry.path)
        try:
            children = list(path.iterdir())
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if any(child.name in DISK_FILES for child in children) or age < EMPTY_DIR_MIN_AGE_S:
            continue
        if opened is None or any(p == str(path) or p.startswith(str(path) + os.sep)
                                 for p in opened):
            continue
        if any(not child.is_socket() and child.stat().st_size > 0 for child in children):
            continue
        empty.append(str(path))
    return {"images": images, "slot_vms": slots, "empty_dirs": empty,
            "delete_bytes": sum(i["size_bytes"] for i in images if i["verdict"] == "delete")}


def apply(report: dict[str, Any], runner: Runner) -> list[str]:
    errors = []
    for image in report["images"]:
        if image["verdict"] != "delete":
            continue
        proc = runner(["tart", "delete", image["name"]], capture_output=True, text=True,
                      timeout=600, check=False)
        image["deleted"] = proc.returncode == 0
        if proc.returncode != 0:
            errors.append(f"tart delete {image['name']}: {proc.stderr.strip()}")
    for path in report["empty_dirs"]:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return errors


def main(argv: list[str] | None = None, runner: Runner = subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete what the plan names")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--min-idle-days", type=float, default=DEFAULT_MIN_IDLE_DAYS)
    parser.add_argument("--profile", type=pathlib.Path, default=pathlib.Path(os.environ.get(
        "TARTCI_FLEET_PROFILE",
        str(pathlib.Path.home() / ".config/tartci/macos-fleet-profile.toml"))))
    args = parser.parse_args(argv)
    if args.min_idle_days < 7:
        parser.error("--min-idle-days must be at least 7")
    try:
        values, data = profile_strings(args.profile)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"tart-image-prune: cannot read the fleet profile ({exc}); nothing planned",
              file=sys.stderr)
        return 2
    host = data.get("host") or {}
    tart_home = pathlib.Path(os.environ.get("TART_HOME") or host.get("tart_home")
                             or pathlib.Path.home() / ".tart")
    repo = (data.get("reclaim") or {}).get("repo")
    listing = runner(["tart", "list", "--format", "json"], capture_output=True, text=True,
                     timeout=120, check=False, env={**os.environ, "TART_HOME": str(tart_home)})
    if listing.returncode != 0:
        print(f"tart-image-prune: tart list failed: {listing.stderr.strip()}", file=sys.stderr)
        return 2
    home = pathlib.Path.home()
    report = plan(json.loads(listing.stdout), vms_dir=tart_home / "vms",
                  profile_values=values,
                  manifest=manifest_bases(pathlib.Path(repo) if isinstance(repo, str) else None),
                  text=reference_text([home / ".config/tartci", home / ".tartci/state",
                                       home / "Library/LaunchAgents"]),
                  opened=open_paths(runner), now=time.time(),
                  min_idle_days=args.min_idle_days)
    report["mode"] = "apply" if args.apply else "plan"
    report["tart_home"] = str(tart_home)
    if args.apply:
        os.environ["TART_HOME"] = str(tart_home)
        report["errors"] = apply(report, runner)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        verb = "deleted" if args.apply else "would delete"
        for image in report["images"]:
            print(f"{image['verdict']:6} {image['size_bytes'] / GIB:6.0f} GiB  {image['name']}"
                  f"  ({image['reason']})")
        for slot in report["slot_vms"]:
            print(f"slot   {slot['size_bytes'] / GIB:6.0f} GiB  {slot['name']}  "
                  f"({slot['state']}; vm_reap.py owns lane VMs)")
        print(f"empty VM dirs: {len(report['empty_dirs'])} {verb}")
        print(f"images: {verb} {report['delete_bytes'] / GIB:.0f} GiB")
        for error in report.get("errors", []):
            print(f"error: {error}")
    return 1 if report.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
