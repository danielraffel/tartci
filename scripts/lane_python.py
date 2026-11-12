#!/usr/bin/env python3
"""Does `python3` on each lane's PATH import tomllib?

Lanes call several TOML-reading helpers with a bare `python3`:
`gate_supply.py decide` (gate placement on every assignment pass,
providers/tart-macos/assignment-v2.lib.sh), `macos_fleet_lanes.py render`
(scripts/build_macos_launcher.sh) and `host_profile.py`
(providers/common/onboard.lib.sh). That is correct only while the lane PATH
puts a 3.11+ python3 (Homebrew's) ahead of /usr/bin: macOS's own
/usr/bin/python3 is 3.9 and has no tomllib. A host that loses its Homebrew
python falls through to it, and those helpers die on `import tomllib` inside a
lane, which for `gate_supply decide` means gates placed without its answer.

The installed lane plists are the authority: the PATH there is what launchd
gives the supervisor. For each distinct PATH this resolves `python3` the way
a shell would and runs it once, reporting its path, version and whether it
imports tomllib.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PROBE = ("import sys, importlib.util; "
         "print(sys.version.split()[0]); "
         "print(importlib.util.find_spec('tomllib') is not None)")
Runner = Callable[[List[str], Dict[str, str]], "subprocess.CompletedProcess[str]"]


def _run(argv: List[str], env: Dict[str, str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20,
                          stdin=subprocess.DEVNULL, check=False)


def lane_paths(agents_dir: Path, prefix: str) -> Dict[str, List[str]]:
    """Distinct lane PATH values -> the lane labels that use them."""
    out: Dict[str, List[str]] = {}
    for plist in sorted(agents_dir.glob(f"{prefix}*.plist")):
        if not plist.is_file() or plist.is_symlink():
            continue
        try:
            data = plistlib.loads(plist.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        env = data.get("EnvironmentVariables") if isinstance(data, dict) else None
        path = env.get("PATH") if isinstance(env, dict) else None
        if isinstance(path, str) and path:
            out.setdefault(path, []).append(plist.name[:-len(".plist")])
    return out


def probe(path: str, run: Runner = _run) -> Dict[str, Any]:
    """`python3` as a shell with this PATH would find it, and what it is."""
    python = shutil.which("python3", path=path)
    if python is None:
        return {"python": None, "version": None, "tomllib": False,
                "error": "no python3 on this PATH"}
    try:
        proc = run([python, "-c", PROBE], {"PATH": path})
    except (OSError, subprocess.SubprocessError) as exc:
        return {"python": python, "version": None, "tomllib": None,
                "error": f"{type(exc).__name__}: {exc}"}
    lines = proc.stdout.split()
    if proc.returncode != 0 or len(lines) != 2 or lines[1] not in ("True", "False"):
        return {"python": python, "version": None, "tomllib": None,
                "error": (proc.stderr.strip() or f"exit {proc.returncode}")[:200]}
    return {"python": python, "version": lines[0], "tomllib": lines[1] == "True",
            "error": None}


def status(agents_dir: Path, prefix: str, run: Runner = _run) -> Dict[str, Any]:
    """One row per distinct lane PATH: its labels and its python3."""
    paths = lane_paths(agents_dir, prefix)
    return {"rows": [{"path": path, "labels": labels, **probe(path, run)}
                     for path, labels in sorted(paths.items())]}


def problem_rows(value: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in (value or {}).get("rows", []) if row.get("tomllib") is False]
