#!/usr/bin/env python3
"""Bounded retention for per-boot lane debris and old support generations.

Two things grow without bound on every fleet host:

* Per-boot lane files under ~/.tartci/state: one actions-runner log, one
  admission-clean receipt and one repository-access receipt per VM boot. On
  2026-10-09 m1 and m5 each held about 5,900 such files older than 30 days.
* Support generations under ~/.local/share/tartci-generations: every
  self-update stages one and nothing removes it (39 to 73 per host).

Both are small on disk (under 200 MB a host); the cost is that every scan of
those trees grows forever. This plans, and with --apply deletes, only:

* per-boot files, named `<runner>-<pid>-<seq>.<kind>`, older than
  --max-age-days AND outside the newest --keep-per-dir of their directory.
  Per-lane files without a boot suffix (state.json, events.jsonl, locks) are
  never touched;
* generation directories outside the newest --keep-generations and older
  than --generation-grace-days, that nothing names: not the wrapper at
  ~/.local/bin/tartci, not any LaunchAgent plist, not an install receipt,
  not a rollback snapshot's commit, not the generation running this script.

Idle-safe: a file or generation that any running process names in its
command line, or holds as its working directory, is kept. If the process
table cannot be read, nothing is deleted. Generations are skipped entirely
while a self-update marker exists, since an install may be staging one.
Plan mode (the default) writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_KEEP_PER_DIR = 50
# Rollback needs the previous generation; KEEP_SNAPSHOTS in
# fleet_self_update.py keeps five snapshots, so ten generations always cover
# every snapshot's commit even before the snapshot references are counted.
DEFAULT_KEEP_GENERATIONS = 10
# Generations are kept by count, not by age: a host deploys several times a
# day, so nearly every generation is younger than the state-file bound. This
# grace only spares the one just replaced while its last processes exit.
DEFAULT_GENERATION_GRACE_DAYS = 2

PER_BOOT = re.compile(
    r"-\d+-\d+\.(actions-runner\.log|admission-clean\.json|repository-access\.json"
    r"|repository-access-error|jit-error)$")
GENERATION_REF = re.compile(r"tartci-generations/([0-9a-f]{7,40}(?:-[0-9a-f]+)?)")


@dataclass
class Plan:
    files: list[Path] = field(default_factory=list)
    files_bytes: int = 0
    files_kept_recent: int = 0
    files_kept_busy: int = 0
    generations: list[Path] = field(default_factory=list)
    generations_bytes: int = 0
    generations_kept: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def process_table() -> tuple[list[str], list[str]] | None:
    """(command lines, working directories) of every process, or None."""
    try:
        ps = subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True,
                            text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if ps.returncode != 0:
        return None
    cwds: list[str] = []
    try:
        lsof = subprocess.run(["lsof", "-a", "-d", "cwd", "-F", "n", "-w"], capture_output=True,
                              text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof exits 1 when some process could not be inspected; the rows it did
    # read are still real. No rows at all means it read nothing.
    cwds = [line[1:] for line in lsof.stdout.splitlines() if line.startswith("n")]
    if not cwds:
        return None
    return ps.stdout.splitlines(), cwds


def _mtime(path: Path) -> float:
    return path.lstat().st_mtime


def _size(path: Path) -> int:
    if path.is_symlink() or not path.is_dir():
        return path.lstat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def plan_state_files(state_root: Path, now: float, max_age: float, keep_per_dir: int,
                     commands: list[str], plan: Plan) -> None:
    if not state_root.is_dir():
        plan.notes.append(f"no state directory at {state_root}")
        return
    joined = "\n".join(commands)
    for root, _dirs, names in os.walk(state_root):
        boots = [Path(root) / n for n in names if PER_BOOT.search(n)]
        boots = [p for p in boots if p.is_file() and not p.is_symlink()]
        boots.sort(key=_mtime, reverse=True)
        plan.files_kept_recent += min(len(boots), keep_per_dir)
        for path in boots[keep_per_dir:]:
            if now - _mtime(path) < max_age:
                plan.files_kept_recent += 1
                continue
            # The boot's own name (runner-pid-seq) on a live command line means
            # its VM or supervisor is still around, however old the file.
            match = PER_BOOT.search(path.name)
            stem = path.name[:match.start(1) - 1] if match else ""
            if stem and stem in joined:
                plan.files_kept_busy += 1
                continue
            plan.files.append(path)
            plan.files_bytes += path.lstat().st_size


def protected_generations(home: Path, running_from: Path | None) -> dict[str, str]:
    """Generation dir name -> why it must be kept."""
    keep: dict[str, str] = {}

    def scan(path: Path, why: str) -> None:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return
        for name in GENERATION_REF.findall(text):
            keep.setdefault(name, why)

    scan(home / ".local/bin/tartci", "named by the tartci wrapper")
    agents = home / "Library/LaunchAgents"
    if agents.is_dir():
        for plist in sorted(agents.glob("*.plist")):
            scan(plist, f"named by {plist.name}")
    config = home / ".config/tartci"
    if config.is_dir():
        for receipt in sorted(config.glob("*.json")):
            scan(receipt, f"named by {receipt.name}")
    if running_from is not None:
        match = GENERATION_REF.search(str(running_from))
        if match:
            keep.setdefault(match.group(1), "running this retention pass")
    return keep


def rollback_commits(home: Path) -> set[str]:
    commits: set[str] = set()
    root = home / ".tartci/state/self-update/rollback"
    if not root.is_dir():
        return commits
    for snapshot in root.glob("*/snapshot.json"):
        try:
            value = json.loads(snapshot.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        previous = value.get("previous") if isinstance(value, dict) else None
        if isinstance(previous, str) and re.fullmatch(r"[0-9a-f]{7,40}", previous):
            commits.add(previous)
    return commits


def plan_generations(home: Path, now: float, grace: float, keep_newest: int,
                     commands: list[str], cwds: list[str], plan: Plan,
                     running_from: Path | None) -> None:
    root = home / ".local/share/tartci-generations"
    if not root.is_dir():
        plan.notes.append(f"no generations directory at {root}")
        return
    marker = home / ".tartci/state/self-update/active.json"
    if marker.exists():
        plan.notes.append("a self-update marker exists; generations are left alone this pass")
        return
    # Staging directories start with a dot and belong to a running install.
    entries = [p for p in root.iterdir()
               if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")]
    entries.sort(key=_mtime, reverse=True)
    protected = protected_generations(home, running_from)
    commits = rollback_commits(home)
    joined = "\n".join(commands)
    for index, path in enumerate(entries):
        name = path.name
        commit = name.split("-", 1)[0]
        why = protected.get(name)
        if why is None and commit in commits:
            why = "a rollback snapshot's previous commit"
        if why is None and index < keep_newest:
            why = f"among the newest {keep_newest}"
        if why is None and now - _mtime(path) < grace:
            why = "younger than the grace period"
        if why is None and (str(path) in joined
                            or any(c == str(path) or c.startswith(str(path) + "/")
                                   for c in cwds)):
            why = "in use by a running process"
        if why is not None:
            plan.generations_kept[name] = why
            continue
        plan.generations.append(path)
        plan.generations_bytes += _size(path)


def _make_writable(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            target = os.path.join(root, name)
            if not os.path.islink(target):
                mode = os.lstat(target).st_mode
                os.chmod(target, stat.S_IMODE(mode) | stat.S_IWUSR)
    mode = os.lstat(path).st_mode
    os.chmod(path, stat.S_IMODE(mode) | stat.S_IWUSR)


def apply(plan: Plan) -> list[str]:
    failures = []
    for path in plan.files:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    for path in plan.generations:
        try:
            # Generations are installed read-only, so rmtree alone fails.
            _make_writable(path)
            shutil.rmtree(path)
        except OSError as exc:
            failures.append(f"{path}: {exc}")
    return failures


def build_plan(home: Path, *, now: float, max_age_days: float, keep_per_dir: int,
               keep_generations: int, table: tuple[list[str], list[str]] | None,
               running_from: Path | None,
               generation_grace_days: float = DEFAULT_GENERATION_GRACE_DAYS) -> Plan:
    plan = Plan()
    if table is None:
        plan.notes.append("the process table could not be read; nothing will be deleted")
        return plan
    commands, cwds = table
    max_age = max_age_days * 86400
    state_root = Path(os.environ.get("TARTCI_HOME", str(home / ".tartci"))) / "state"
    plan_state_files(state_root, now, max_age, keep_per_dir, commands, plan)
    plan_generations(home, now, generation_grace_days * 86400, keep_generations, commands, cwds,
                     plan, running_from)
    return plan


def report(plan: Plan, *, applied: bool, failures: list[str]) -> dict:
    return {
        "mode": "apply" if applied else "plan",
        "state_files": {"delete": len(plan.files), "bytes": plan.files_bytes,
                        "kept_recent": plan.files_kept_recent,
                        "kept_busy": plan.files_kept_busy},
        "generations": {"delete": [p.name for p in plan.generations],
                        "bytes": plan.generations_bytes,
                        "kept": plan.generations_kept},
        "notes": plan.notes,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci retention", description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="delete; default is a plan")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--max-age-days", type=float, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument("--keep-per-dir", type=int, default=DEFAULT_KEEP_PER_DIR)
    parser.add_argument("--keep-generations", type=int, default=DEFAULT_KEEP_GENERATIONS)
    parser.add_argument("--generation-grace-days", type=float,
                        default=DEFAULT_GENERATION_GRACE_DAYS)
    args = parser.parse_args(argv)
    if (args.max_age_days < 7 or args.keep_per_dir < 0 or args.keep_generations < 3
            or args.generation_grace_days < 1):
        parser.error("bounds too tight: --max-age-days >= 7, --keep-generations >= 3, "
                     "--generation-grace-days >= 1")
    plan = build_plan(args.home, now=time.time(), max_age_days=args.max_age_days,
                      keep_per_dir=args.keep_per_dir, keep_generations=args.keep_generations,
                      table=process_table(), running_from=Path(__file__).resolve(),
                      generation_grace_days=args.generation_grace_days)
    failures = apply(plan) if args.apply else []
    value = report(plan, applied=args.apply, failures=failures)
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        verb = "deleted" if args.apply else "would delete"
        print(f"retention ({value['mode']}): {verb} {len(plan.files)} per-boot state files "
              f"({plan.files_bytes // 1024} KiB), kept {plan.files_kept_recent} recent and "
              f"{plan.files_kept_busy} in use")
        print(f"retention ({value['mode']}): {verb} {len(plan.generations)} generations "
              f"({plan.generations_bytes // (1024 * 1024)} MiB), kept "
              f"{len(plan.generations_kept)}")
        for name, why in sorted(plan.generations_kept.items()):
            print(f"  keep {name[:12]}: {why}")
        for note in plan.notes:
            print(f"  note: {note}")
        for failure in failures:
            print(f"  FAILED: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
