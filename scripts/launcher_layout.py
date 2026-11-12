#!/usr/bin/env python3
"""Which launcher layout a sealed-launcher host installs: v1 or v2.

v1 seals the support tree and lanes into TartCILauncher.app; v2 is the stable
app that runs a signed launch set (launch_set.py). A host moves to v2 only by
opting in, one host at a time, with ~/.config/tartci/launcher-v2.enabled.

- The opt-in file must be a regular file owned by this user and not group- or
  world-writable; anything else refuses loudly rather than being read as
  either answer.
- The move from v1 to v2 is a launcher change like any other: the opt-in never
  bypasses self-update's acknowledgement or the Removable Volumes grant.
- A v2 host whose opt-in file disappears does not fall back to v1. Layouts
  never flip silently: going back is an explicit install with
  `--launcher-layout v1`, itself a launcher change.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional

OPT_IN = Path(".config/tartci/launcher-v2.enabled")
LAYOUTS = ("v1", "v2")


class LayoutError(ValueError):
    pass


def opt_in_path(home: Path) -> Path:
    return Path(home) / OPT_IN


def opted_in(home: Path) -> bool:
    """Whether the opt-in file is present and trusted; refuses an untrusted one."""
    path = opt_in_path(home)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LayoutError(f"cannot read {path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LayoutError(f"{path} must be a regular file, not a link or directory; "
                          f"remove it and create it again with `touch {path}`")
    if info.st_uid != os.getuid():
        raise LayoutError(f"{path} is not owned by this user; remove it and create it again")
    if info.st_mode & 0o022:
        raise LayoutError(f"{path} is group- or world-writable; run `chmod 600 {path}`")
    return True


POINTER = Path(".config/tartci/launcher-generation.json")
PROFILE = Path(".config/tartci/macos-fleet-profile.toml")


def _profile_helper_path(text: str) -> Optional[str]:
    """`path` in the profile's [launch_helper] table, without tomllib.

    Status surfaces may run under the system Python 3.9, which has no tomllib.
    Only the one shape the installer writes is read: a `[launch_helper]` table
    header and a basic or literal string `path = "..."` inside it.
    """
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line.strip("[] \t")
            continue
        if section != "launch_helper":
            continue
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "path":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            inner = value[1:-1]
            if value[0] == '"':
                inner = inner.replace('\\"', '"').replace("\\\\", "\\")
            return inner
        return None
    return None


def profile_app(profile: Optional[Path]) -> Optional[Path]:
    """The installed profile's launch_helper.path: where this host's launch
    helper app lives whether or not an install receipt exists. None when the
    profile is absent, unreadable or names no helper."""
    if profile is None:
        return None
    try:
        text = Path(profile).read_text()
    except OSError:
        return None
    try:
        import tomllib  # noqa: PLC0415 - Python 3.11+
    except ImportError:
        tomllib = None
    if tomllib is not None:
        try:
            helper = tomllib.loads(text).get("launch_helper")
        except ValueError:
            return None
        path = helper.get("path") if isinstance(helper, dict) else None
    else:
        path = _profile_helper_path(text)
    return Path(path) if isinstance(path, str) and path else None


def receipt_app(receipt: Optional[dict]) -> Optional[Path]:
    """The launch helper path an install receipt recorded, if any."""
    helper = (receipt or {}).get("launch_helper")
    path = helper.get("path") if isinstance(helper, dict) else None
    return Path(path) if isinstance(path, str) and path else None


def helper_app(home: Path, receipt: Optional[dict]) -> Optional[Path]:
    """The launch helper app status surfaces read for v2 evidence: the
    installed profile's, so a host whose receipt is gone still finds its v2
    app, else the receipt's."""
    return profile_app(Path(home) / PROFILE) or receipt_app(receipt)


def v2_evidence(home: Path, app: Optional[Path] = None) -> Optional[str]:
    """What on disk says this host runs launcher v2, whatever its receipt says:
    the generation pointer, or a v2 app (policy.json schema 2) at `app`."""
    pointer = Path(home) / POINTER
    if pointer.exists() or pointer.is_symlink():
        return f"the launcher v2 pointer {pointer}"
    if app is not None:
        try:
            policy = json.loads((Path(app) / "Contents/Resources/policy.json").read_text())
        except (OSError, ValueError):
            policy = None
        if isinstance(policy, dict) and policy.get("schema") == 2:
            return f"a launcher v2 app at {app}"
    return None


def installed(receipt: Optional[dict], home: Optional[Path] = None,
              app: Optional[Path] = None) -> Optional[str]:
    """The layout the host runs: from its install receipt, or, when the receipt
    has no launch helper record (deleted, or never written), from v2 evidence
    on disk, so a lost receipt never reads as "nothing installed"."""
    helper = (receipt or {}).get("launch_helper")
    if isinstance(helper, dict):
        return "v2" if helper.get("layout") == "v2" else "v1"
    if home is not None and v2_evidence(home, app):
        return "v2"
    return None


def decide(home: Path, receipt: Optional[dict], explicit: Optional[str] = None,
           app: Optional[Path] = None) -> str:
    """The layout an install may write, or LayoutError saying why not."""
    if explicit is not None and explicit not in LAYOUTS:
        raise LayoutError(f"--launcher-layout must be v1 or v2, not {explicit!r}")
    requested = "v2" if opted_in(home) else "v1"
    current = installed(receipt, home, app)
    if explicit == "v2" and requested != "v2":
        raise LayoutError(f"--launcher-layout v2 needs the per-host opt-in {opt_in_path(home)}")
    if explicit == "v1":
        return "v1"
    if current == "v2" and requested == "v1":
        recorded = isinstance((receipt or {}).get("launch_helper"), dict)
        evidence = ("its install receipt says so" if recorded else
                    f"its install receipt is missing but {v2_evidence(home, app)} is on disk")
        raise LayoutError(
            f"this host runs launcher v2 ({evidence}) but {opt_in_path(home)} is gone; a layout "
            "never flips back silently. To keep v2, restore the file (`touch` it, mode 600). To "
            "return to v1 deliberately, install with `--launcher-layout v1`: that replaces the "
            "launcher, so it needs the launcher-change acknowledgement and the Removable Volumes "
            "grant")
    return requested


def status(home: Path, receipt: Optional[dict], app: Optional[Path] = None) -> dict:
    """What doctor and pool status report: the layout and the opt-in file."""
    try:
        opt_in = "present" if opted_in(home) else "absent"
        problem = None
    except LayoutError as exc:
        opt_in, problem = "refused", str(exc)
    current = installed(receipt, home, app)
    if problem is None and current == "v2" and opt_in == "absent":
        try:
            decide(home, receipt, app=app)
        except LayoutError as exc:
            problem = str(exc)
    return {"layout": current, "opt_in": opt_in, "opt_in_path": str(opt_in_path(home)),
            "problem": problem}


def status_text(value: dict) -> str:
    layout = value.get("layout") or "none (no launch helper installed)"
    line = f"launcher: layout {layout}, v2 opt-in {value['opt_in']} ({value['opt_in_path']})"
    if value.get("problem"):
        line += f"\n  launcher problem: {value['problem']}"
    return line


def read_receipt(path: Optional[Path]) -> Optional[dict]:
    if path is None or not path.exists():
        return None
    # An install receipt that exists but cannot be read is not "no layout
    # installed": that would let a v2 host flip back silently.
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise LayoutError(f"install receipt {path} is unreadable: {exc}") from exc


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--receipt", type=Path, default=None)
    parser.add_argument("--explicit", default=None)
    parser.add_argument("--app", type=Path, default=None,
                        help="the launch helper app, read for v2 evidence; defaults to "
                             "the installed profile's launch_helper.path, else the receipt's")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--installed", action="store_true",
                      help="print the installed layout (empty when none)")
    mode.add_argument("--status", action="store_true", help="print the layout report as JSON")
    mode.add_argument("--status-text", action="store_true", help="print the layout report as text")
    args = parser.parse_args(argv)
    try:
        receipt = read_receipt(args.receipt)
        if args.app is None:
            args.app = helper_app(args.home, receipt)
        if args.installed:
            print(installed(receipt, args.home, args.app) or "")
        elif args.status:
            print(json.dumps(status(args.home, receipt, args.app), sort_keys=True))
        elif args.status_text:
            print(status_text(status(args.home, receipt, args.app)))
        else:
            print(decide(args.home, receipt, args.explicit or None, args.app))
    except LayoutError as exc:
        print(f"launcher layout: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
