#!/usr/bin/env python3
"""GitHub Actions runner age for the runner agent baked into each golden image.

Every job VM is cloned from a golden that carries the runner agent at
~/actions-runner. GitHub retires a runner version 30 days after a newer one is
released: the old agent then registers, prints "Runner version vX is
deprecated and cannot receive messages" and exits. A golden that still carries
such an agent makes every clone fail the same way, which stopped a runner pool
for 13 days without any alert.

This module answers, per golden, three things: the installed runner version,
the latest released version with its publish date, and a verdict.

  ok       installed is current, or the first newer release is not yet near
           its deadline
  warn     the deadline (first newer release + 30 days) is at most `warn_days`
           away (default 10)
  fail     the deadline has passed, or the installed version is below the
           configured minimum
  unknown  the installed version or the release data could not be read. It is
           its own state and is never reported as ok.

The installed version is never read by booting a golden. It is recorded when
the golden is provisioned (`record`), and `check` reads that record. A golden
with no record is unknown.

The macOS lane is the exception: its supervisor installs a pinned runner into
every VM, so what runs there is the pin, not what the golden carries. For the
macOS golden the installed version is therefore the pin
(`providers/tart-macos/runner.sh --print-runner-version`).

CLI:
  runner_age.py check [--json] [--golden NAME ...] [--min V] [--warn-days N]
  runner_age.py record --golden NAME --version V [--source TEXT]
Exit 1 when any golden fails, otherwise 0.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

DEADLINE_DAYS = 30.0
WARN_DAYS = 10.0
RUNNER_REPO = "actions/runner"
DEFAULT_GOLDENS = ("pulp-build-runner", "pulp-linux-build")
# Goldens whose VMs run the supervisor's pinned runner, not the baked one.
PINNED_GOLDENS = ("pulp-build-runner",)
MACOS_RUNNER = Path(__file__).resolve().parent.parent / "providers" / "tart-macos" / "runner.sh"
DAY = 86400.0
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

Version = tuple
Release = tuple  # (version tuple, tag, published epoch)


def parse_version(text: Optional[str]) -> Optional[Version]:
    match = _VERSION_RE.search(text or "")
    return tuple(int(part) for part in match.groups()) if match else None


def _vstr(version: Optional[Version]) -> Optional[str]:
    return ".".join(str(part) for part in version) if version else None


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify(installed: Optional[str], releases: Optional[list], now: float, *,
             minimum: Optional[str] = None, warn_days: float = WARN_DAYS,
             deadline_days: float = DEADLINE_DAYS) -> dict:
    """Pure policy. `releases` is [(version tuple, tag, published epoch)].

    The deadline runs from the OLDEST release newer than the installed version,
    which is when GitHub's 30-day clock for the installed version started. With
    one new release that is the latest release. Fails closed: anything that
    cannot be read is `unknown`.
    """
    have = parse_version(installed)
    floor = parse_version(minimum) if minimum else None
    if minimum and floor is None:
        return _row("unknown", f"configured minimum {minimum!r} is not a version", installed, releases)
    if have is None:
        return _row("unknown", "installed runner version is not recorded", installed, releases)
    if floor is not None and have < floor:
        return _row("fail", f"installed v{_vstr(have)} is below the minimum v{_vstr(floor)}",
                    installed, releases)
    if not releases:
        return _row("unknown", "latest runner release could not be read", installed)
    latest = max(releases)
    if have >= latest[0]:
        return _row("ok", f"v{_vstr(have)} is the latest release", installed, releases)
    first_newer = min(r for r in releases if r[0] > have and r[2] is not None) \
        if any(r[0] > have and r[2] is not None for r in releases) else None
    if first_newer is None:
        return _row("unknown", "publish date of the newer release is unknown", installed, releases)
    deadline = first_newer[2] + deadline_days * DAY
    days_left = (deadline - now) / DAY
    if now > deadline:
        return _row("fail", f"v{_vstr(have)} is past GitHub's {deadline_days:g}-day window: "
                    f"v{_vstr(first_newer[0])} was released {_iso(first_newer[2])}, "
                    f"deadline {_iso(deadline)}", installed, releases, deadline, days_left)
    if days_left <= warn_days:
        return _row("warn", f"v{_vstr(have)} must update within {days_left:.1f} day(s): "
                    f"v{_vstr(first_newer[0])} was released {_iso(first_newer[2])}, "
                    f"deadline {_iso(deadline)}", installed, releases, deadline, days_left)
    return _row("ok", f"v{_vstr(have)} is behind v{_vstr(latest[0])} with {days_left:.1f} "
                f"day(s) before the deadline", installed, releases, deadline, days_left)


def _row(verdict: str, reason: str, installed: Optional[str], releases: Optional[list] = None,
         deadline: Optional[float] = None, days_left: Optional[float] = None) -> dict:
    latest = max(releases) if releases else None
    return {"verdict": verdict, "reason": reason,
            "installed": _vstr(parse_version(installed)),
            "latest": _vstr(latest[0]) if latest else None,
            "latest_published": _iso(latest[2]) if latest else None,
            "deadline": _iso(deadline),
            "days_left": round(days_left, 1) if days_left is not None else None}


def base_name(golden: str) -> str:
    return golden.split(":", 1)[0]


def state_dir(home: Optional[Path] = None) -> Path:
    root = os.environ.get("TARTCI_HOME") or str((home or Path.home()) / ".tartci")
    return Path(root) / "state" / "golden-runner"


def record(golden: str, version: str, *, home: Optional[Path] = None, now: Optional[float] = None,
           source: str = "provision") -> Path:
    """Write the runner version a golden carries. Refuses an unparseable version."""
    parsed = parse_version(version)
    if parsed is None:
        raise ValueError(f"not a runner version: {version!r}")
    directory = state_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{base_name(golden)}.json"
    body = {"golden": base_name(golden), "version": _vstr(parsed), "source": source,
            "recorded_at": _iso(time.time() if now is None else now)}
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".record.")
    with os.fdopen(fd, "w") as handle:
        json.dump(body, handle, sort_keys=True)
    os.replace(tmp, path)
    return path


def read_record(golden: str, home: Optional[Path] = None) -> Optional[str]:
    try:
        value = json.loads((state_dir(home) / f"{base_name(golden)}.json").read_text())
    except (OSError, ValueError):
        return None
    return value.get("version") if isinstance(value, dict) else None


def macos_pin(script: Optional[Path] = None) -> Optional[str]:
    """The runner version the macOS supervisor installs into each VM, or None."""
    try:
        proc = subprocess.run(["bash", str(script or MACOS_RUNNER), "--print-runner-version"],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def present_goldens() -> Optional[set]:
    """Base names of the local Tart VMs, or None when tart cannot be listed."""
    try:
        proc = subprocess.run(["tart", "list", "--quiet"], capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {base_name(line.strip()) for line in proc.stdout.splitlines() if line.strip()}


def fetch_releases(feed: Optional[Callable[[str], str]] = None) -> Optional[list]:
    """Plain vX.Y.Z releases of actions/runner from the public Atom feed, or None."""
    import tool_freshness as tf
    try:
        return tf.parse_releases((feed or tf.fetch_feed)(RUNNER_REPO)) or None
    except Exception:  # noqa: BLE001 - an unreadable feed is "unknown", never "ok"
        return None


def collect(goldens, now: float, *, home: Optional[Path] = None, minimum: Optional[str] = None,
            warn_days: float = WARN_DAYS, releases: Optional[list] = None,
            present: Optional[Callable[[], Optional[set]]] = None,
            read: Optional[Callable[[str], Optional[str]]] = None,
            pin: Optional[Callable[[], Optional[str]]] = None) -> list:
    """One row per golden that exists on this host. Reads records and the macOS
    pin only; boots nothing."""
    here = (present or present_goldens)()
    rows = []
    for golden in goldens:
        if here is not None and base_name(golden) not in here:
            continue
        pinned = base_name(golden) in PINNED_GOLDENS
        if pinned:
            installed = (pin or macos_pin)()
        else:
            installed = (read or (lambda g: read_record(g, home)))(golden)
        row = classify(installed, releases, now, minimum=minimum, warn_days=warn_days)
        if pinned and row["installed"] is None:
            row["reason"] = "pinned macOS runner version could not be read"
        row["golden"] = base_name(golden)
        row["source"] = "pin" if pinned else "record"
        rows.append(row)
    return rows


def render(row: dict) -> str:
    return (f"{row['golden']}: {row['verdict'].upper()}: installed "
            f"{row['installed'] or 'unknown'}, latest {row['latest'] or 'unknown'}"
            f"{' (' + row['latest_published'] + ')' if row['latest_published'] else ''}; "
            f"{row['reason']}")


def problems(rows: list) -> list:
    """Every non-ok row is worth a human's attention."""
    return [render(row) for row in rows if row["verdict"] != "ok"]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="runner_age.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check")
    check.add_argument("--json", action="store_true")
    check.add_argument("--golden", action="append", default=[])
    check.add_argument("--min", dest="minimum", default=None)
    check.add_argument("--warn-days", type=float, default=WARN_DAYS)
    rec = sub.add_parser("record")
    rec.add_argument("--golden", required=True)
    rec.add_argument("--version", required=True)
    rec.add_argument("--source", default="provision")
    args = parser.parse_args(argv)
    if args.cmd == "record":
        try:
            print(record(args.golden, args.version, source=args.source))
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        return 0
    rows = collect(args.golden or list(DEFAULT_GOLDENS), time.time(), minimum=args.minimum,
                   warn_days=args.warn_days, releases=fetch_releases())
    if args.json:
        print(json.dumps({"goldens": rows}, indent=2, sort_keys=True))
    elif rows:
        print("\n".join(render(row) for row in rows))
    else:
        print("no golden images found on this host")
    return 1 if any(row["verdict"] == "fail" for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
