#!/usr/bin/env python3
"""Refuse a LaunchAgent whose program is not a stable macOS privacy identity.

macOS keys a privacy (TCC) decision such as Removable Volumes to a launchd
job's RESPONSIBLE process, which is the job's own program, not the binary
that later calls open(). An unsigned or ad-hoc binary gets an identity
derived from its code hash, so every rebuild or upgrade is a new identity and
a new consent alert; a program at a per-generation path is a new identity at
every install. While such an alert waits unanswered on an unattended host,
every launchd open of an external volume without a stored grant blocks
(tartci#840: m3 lost its Tart store for hours at a time, 2026-11-07 to 11-13).

TCC records an app bundle by its bundle identifier, but a bare executable by
its PATH (with its code requirement), so a bare binary at a new path is a new
identity even when it is Developer-ID signed: every Shipyard install runs from
a new auth-generations/<sha>/shipyard path and raised a new alert.

A stable identity is one of:

  * ``launcher``: the Developer-ID-signed TartCILauncher.app at its stable
    host-local path (``~/.local/libexec/TartCILauncher.app``). Its grant is
    keyed to bundle ID + team and survives updates; its children inherit it.
  * ``platform``: an Apple binary on the sealed system volume (/bin, /usr/bin,
    /usr/sbin, /usr/libexec, /sbin, /System). Stable, but it holds no grant of
    its own until someone answers for it.
  * ``developer_id`` (installed scan only): any other binary signed with a
    Team ID, at a path (symlinks resolved) with no per-generation component.

Anything else is refused:

  * ``per_generation``: a path component that changes per install or upgrade
    (a Homebrew Cellar version, a 40-64 hex digest, auth-generations,
    generations, a venv, a ``current`` symlink). The installed scan resolves
    symlinks first, so /opt/homebrew/bin/python3 reads as its Cellar target.
  * ``user_binary`` (templates): a program under $HOME or another writable
    tree. tartci ships no signed binary there except the launcher.
  * ``adhoc`` / ``unsigned`` (installed scan): no Team ID.
  * ``relative`` / ``missing``: no absolute program.

``--templates`` checks every tartci LaunchAgent template and exits 1 on any
refusal (lint.sh runs it). ``--installed DIR`` reports every plist in a
LaunchAgents directory with its identity class, so the agents still to move
behind a stable launcher are listed; ``--strict`` makes refusals exit 1.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_SUFFIX = "/.local/libexec/TartCILauncher.app/Contents/MacOS/tartci-launcher"
LAUNCHER_RELATIVE = tuple(LAUNCHER_SUFFIX.lstrip("/").split("/"))
PLATFORM_ROOTS = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/libexec/", "/System/")
OK_KINDS = frozenset({"launcher", "platform", "developer_id"})
HEX_SEGMENT = re.compile(r"^[0-9a-f]{40,64}$")
# Path components that change with an install, an upgrade or a rebuild.
GENERATION_SEGMENTS = frozenset({
    "Cellar", "generations", "auth-generations", "manual-generations",
    "current", "venv", ".venv", "versions", "Versions",
})
GENERATION_PREFIXES = ("externals.",)

Codesign = Callable[[str], Dict[str, Any]]


def per_generation_reason(path: str) -> Optional[str]:
    """Why `path` names a per-install location, or None."""
    for part in path.split("/"):
        if part in GENERATION_SEGMENTS:
            return f"path component {part!r} changes with each install or upgrade"
        if HEX_SEGMENT.match(part):
            return f"path component {part[:12]}... is a per-generation digest"
        if any(part.startswith(prefix) for prefix in GENERATION_PREFIXES):
            return f"path component {part!r} is versioned"
    return None


def program_of(value: Dict[str, Any]) -> Optional[str]:
    """The program launchd runs: `Program`, else ProgramArguments[0]."""
    program = value.get("Program")
    if isinstance(program, str) and program:
        return program
    arguments = value.get("ProgramArguments")
    if isinstance(arguments, list) and arguments and isinstance(arguments[0], str):
        return arguments[0] or None
    return None


def is_canonical_launcher(path: str) -> bool:
    """Whether `path` is the launcher in a user's canonical home directory."""
    parts = Path(os.path.normpath(path)).parts
    if tuple(parts[-len(LAUNCHER_RELATIVE):]) != LAUNCHER_RELATIVE:
        return False
    home = parts[:-len(LAUNCHER_RELATIVE)]
    return ((len(home) == 3 and home[:2] == (os.sep, "Users") and bool(home[2]))
            or home == (os.sep, "var", "root"))


def classify_static(program: Optional[str]) -> Dict[str, str]:
    """The identity class of a program path, from the path alone."""
    if not program:
        return {"kind": "missing", "reason": "no Program or ProgramArguments[0]"}
    # Collapse traversal before checking trusted prefixes.
    normalized = os.path.normpath(program.replace("$HOME", "/Users/example"))
    if not normalized.startswith("/"):
        return {"kind": "relative", "reason": f"{program} is not an absolute path"}
    if normalized.startswith(PLATFORM_ROOTS):
        # The sealed system volume: Apple's own paths, stable across updates.
        return {"kind": "platform", "reason": "an Apple binary on the system volume"}
    why = per_generation_reason(normalized)
    if why:
        return {"kind": "per_generation", "reason": why}
    if is_canonical_launcher(normalized):
        return {"kind": "launcher", "reason": "the stable Developer-ID TartCI launcher"}
    return {"kind": "user_binary",
            "reason": f"{program} is outside the system volume and is not the TartCI launcher"}


def codesign_info(path: str, run: Callable[..., Any] = subprocess.run) -> Dict[str, Any]:
    """`codesign -dv` for one binary: whether it is ad-hoc and its Team ID."""
    try:
        completed = run(["/usr/bin/codesign", "-dv", "--verbose=2", path],
                        capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"signed": False, "error": str(exc)[:200]}
    text = (completed.stderr or "") + (completed.stdout or "")
    if completed.returncode != 0:
        return {"signed": False, "error": text.strip()[:200]}
    team = re.search(r"^TeamIdentifier=(\S+)$", text, re.M)
    return {"signed": True, "adhoc": bool(re.search(r"^Signature=adhoc$", text, re.M)),
            "team": None if not team or team.group(1) == "not set" else team.group(1)}


def classify_installed(program: Optional[str], *, codesign: Optional[Codesign] = None,
                       realpath: Callable[[str], str] = os.path.realpath) -> Dict[str, str]:
    """The identity class of an installed program: symlinks resolved, and a
    binary outside the system volume read for its signature."""
    static = classify_static(program)
    if static["kind"] in ("missing", "relative", "per_generation"):
        return static
    resolved = os.path.normpath(realpath(str(program)))
    # Reclassify the resolved target too: a trusted-looking symlink can escape
    # its prefix (for example /usr/bin/tool -> /tmp/tool).
    target = classify_static(resolved)
    if target["kind"] == "per_generation":
        return {"kind": "per_generation",
                "reason": f"{program} resolves to {resolved}: {target['reason']}"}
    if target["kind"] in ("platform", "launcher"):
        return target
    static = target
    if static["kind"] != "user_binary":
        return static
    info = (codesign or codesign_info)(resolved)
    if not info.get("signed"):
        return {"kind": "unsigned", "reason": f"{resolved} is not signed ({info.get('error')})"}
    if info.get("adhoc") or not info.get("team"):
        return {"kind": "adhoc",
                "reason": f"{resolved} is ad-hoc signed: its identity is its code hash, "
                          "so every rebuild is a new privacy identity"}
    return {"kind": "developer_id", "reason": f"signed by team {info['team']}"}


def templates(root: Path = ROOT) -> List[Path]:
    return sorted((root / "launchd").glob("*.plist.template")) + sorted(
        (root / "launchd" / "system").glob("*.plist"))


def _load(path: Path) -> Dict[str, Any]:
    text = path.read_text()
    try:
        value = plistlib.loads(text.encode())
    except Exception:  # noqa: BLE001 - placeholders make some templates invalid XML values
        value = None
    if isinstance(value, dict):
        return value
    # A template's placeholders are inside <string> values; read the program
    # straight from the first array entry when the plist parser refuses it.
    match = re.search(r"<key>Program</key>\s*<string>([^<]*)</string>", text)
    if match:
        return {"Program": match.group(1)}
    match = re.search(r"<key>ProgramArguments</key>\s*<array>\s*<string>([^<]*)</string>", text)
    return {"ProgramArguments": [match.group(1)]} if match else {}


def check_templates(root: Path = ROOT) -> List[Dict[str, str]]:
    rows = []
    for path in templates(root):
        program = program_of(_load(path))
        verdict = classify_static(program)
        rows.append({"path": str(path.relative_to(root)), "program": program or "",
                     **verdict})
    return rows


def scan_installed(directory: Path, *, codesign: Optional[Codesign] = None,
                   realpath: Callable[[str], str] = os.path.realpath) -> List[Dict[str, str]]:
    rows = []
    for path in sorted(Path(directory).glob("*.plist")):
        try:
            value = plistlib.loads(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - one unreadable plist must not stop the scan
            rows.append({"path": str(path), "label": path.stem, "program": "",
                         "kind": "missing", "reason": f"unreadable plist: {exc}"[:200]})
            continue
        if not isinstance(value, dict):
            continue
        program = program_of(value)
        rows.append({"path": str(path), "label": str(value.get("Label") or path.stem),
                     "program": program or "",
                     **classify_installed(program, codesign=codesign, realpath=realpath)})
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--templates", action="store_true",
                      help="check every tartci LaunchAgent template (exit 1 on a refusal)")
    mode.add_argument("--installed", metavar="DIR",
                      help="report every installed plist in DIR with its identity class")
    parser.add_argument("--strict", action="store_true",
                        help="with --installed: exit 1 when any plist is refused")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = check_templates() if args.templates else scan_installed(Path(args.installed))
    bad = [row for row in rows if row["kind"] not in OK_KINDS]
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            mark = "ok " if row["kind"] in OK_KINDS else "BAD"
            print(f"{mark} {row['kind']:<15} {row.get('label') or row['path']}: "
                  f"{row['program']} ({row['reason']})")
    if args.templates and not rows:
        print("launchd identity: no templates found", file=sys.stderr)
        return 1
    return 1 if bad and (args.templates or args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
