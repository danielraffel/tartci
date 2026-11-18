#!/usr/bin/env python3
"""Is the GitHub Actions runner each golden's VMs run still accepted by GitHub?

GitHub retires old runner versions. A retired agent registers, prints "Runner
version vX is deprecated and cannot receive messages" and exits, so every clone
of a golden that carries it fails the same way. A runner pool stopped for 13
days like that with nothing alerting.

Per golden this reports one of:

  fail     a recent runner log shows GitHub refusing this version
  warn     the version trails the latest release by
           `runner_version.WARN_MINOR_LAG` minor versions or more
  ok       the version is known and within that lag
  unknown  the version or the latest release could not be read

The verdict comes from `runner_version.evaluate`, the rule the macOS pin check
already uses, so the two can never disagree about the same version.

`warn` and `fail` are problems for the watchdog. `unknown` is shown by doctor
and is neither ok nor an alert: a host that has never recorded its golden, or
cannot reach GitHub, must not page anyone.

Where the version comes from. Nothing here boots a VM.
  Linux golden   a record written by `record` (provision.sh writes one when the
                 golden carries a runner; an operator writes one after updating
                 the agent inside the golden).
  macOS golden   the supervisor installs its pinned runner into every VM, so
                 the version is the pin
                 (`providers/tart-macos/runner.sh --print-runner-version`).

CLI:
  runner_age.py check [--json] [--golden NAME ...]
  runner_age.py record --golden NAME --version V [--source TEXT]
Exit 1 when any golden fails, otherwise 0.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

import runner_version as rv

DEFAULT_GOLDENS = ("pulp-build-runner", "pulp-linux-build")
# Goldens whose VMs run the supervisor's pinned runner, not the baked one.
PINNED_GOLDENS = ("pulp-build-runner",)
MACOS_RUNNER = Path(__file__).resolve().parent.parent / "providers" / "tart-macos" / "runner.sh"
# A refusal older than this no longer counts: the lane has not run since, or
# the golden was fixed without a record.
REFUSAL_WINDOW_SECS = 86400.0
# The refusal is printed when the listener starts, so the head of a log is
# enough and a long build log is never read whole.
LOG_HEAD_BYTES = 262144


def clean(version: Optional[str]) -> Optional[str]:
    parsed = rv.parse_version(version)
    return ".".join(str(part) for part in parsed) if parsed else None


_VERDICTS = {"problem": "fail", "warn": "warn", "ok": "ok", "unknown": "unknown"}


def classify(installed: Optional[str], latest: Optional[str],
             refused: Optional[str] = None) -> dict:
    """Pure policy. `refused` is a version a recent runner log says GitHub retired.

    A refusal counts when it names the installed version, or when the installed
    version is unknown: a lane that is being refused is broken either way.
    """
    have, newest = clean(installed), clean(latest)
    row = {"installed": have, "latest": newest}
    if refused and (have is None or clean(refused) == have):
        return dict(row, verdict="fail",
                    reason=f"GitHub refuses runner v{clean(refused)} as deprecated")
    if have is None:
        return dict(row, verdict="unknown", reason="runner version is not recorded")
    status, _ = rv.evaluate(have, newest, [])
    if status == "unknown":
        reason = "latest runner release could not be read"
    elif status == "warn":
        reason = (f"runner v{have} is {rv.minor_lag(have, newest)} minor versions "
                  f"behind v{newest}")
    else:
        reason = f"runner v{have} (latest v{newest})"
    return dict(row, verdict=_VERDICTS[status], reason=reason)


def base_name(golden: str) -> str:
    return golden.split(":", 1)[0]


def tartci_home(home: Optional[Path] = None) -> Path:
    return Path(os.environ.get("TARTCI_HOME") or str((home or Path.home()) / ".tartci"))


def state_dir(home: Optional[Path] = None) -> Path:
    return tartci_home(home) / "state" / "golden-runner"


def record(golden: str, version: str, *, home: Optional[Path] = None,
           now: Optional[float] = None, source: str = "provision") -> Path:
    """Write the runner version a golden carries. Refuses an unparseable version."""
    parsed = clean(version.strip().lstrip("v")) if isinstance(version, str) else None
    if parsed is None:
        raise ValueError(f"not a runner version: {version!r}")
    directory = state_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{base_name(golden)}.json"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    body = {"golden": base_name(golden), "version": parsed, "source": source,
            "recorded_at": stamp}
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


def log_globs(golden: str, home: Optional[Path] = None) -> list:
    """Where the lane that clones `golden` keeps each runner's output."""
    if base_name(golden) in PINNED_GOLDENS:
        root = os.environ.get("TARTCI_STATE_DIR") or str(tartci_home(home) / "state" / "macos")
        return [os.path.join(root, "*.actions-runner.log")]
    root = (os.environ.get("TARTCI_LINUX_LOGS") or os.environ.get("PULP_LINUX_LOGS")
            or str((home or Path.home()) / "VMs" / "logs" / "tartci-linux"))
    return [os.path.join(root, "*", "runner-output.log")]


def recent_refusal(patterns: list, now: float,
                   window: float = REFUSAL_WINDOW_SECS) -> Optional[str]:
    """The version a runner log written in the last `window` seconds says is retired."""
    for pattern in patterns:
        for path in glob.glob(pattern):
            try:
                if now - os.path.getmtime(path) > window:
                    continue
                with open(path, errors="replace") as handle:
                    version = rv.find_deprecated(handle.read(LOG_HEAD_BYTES))
            except OSError:
                continue
            if version:
                return version
    return None


def latest_release(home: Optional[Path] = None) -> Optional[str]:
    """Latest actions/runner version through the cached lookup the pin check uses."""
    cache = (home or Path.home()) / ".cache" / "tartci" / "runner-latest.json"
    try:
        return rv.latest_release(str(cache))
    except Exception:  # noqa: BLE001 - an unreadable release is "unknown"
        return None


def collect(goldens, now: float, *, home: Optional[Path] = None,
            latest: Optional[str] = None,
            present: Optional[Callable[[], Optional[set]]] = None,
            read: Optional[Callable[[str], Optional[str]]] = None,
            pin: Optional[Callable[[], Optional[str]]] = None,
            refusal: Optional[Callable[[str], Optional[str]]] = None) -> list:
    """One row per golden that exists on this host."""
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
        refused = (refusal or (lambda g: recent_refusal(log_globs(g, home), now)))(golden)
        row = classify(installed, latest, refused)
        row["golden"] = base_name(golden)
        row["source"] = "pin" if pinned else "record"
        rows.append(row)
    return rows


def render(row: dict) -> str:
    return f"{row['golden']}: {row['verdict'].upper()}: {row['reason']}"


def problems(rows: list) -> list:
    """Only a version known to be too old, or refused by GitHub, is a problem."""
    return [render(row) for row in rows if row.get("verdict") in ("warn", "fail")]


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="runner_age.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check")
    check.add_argument("--json", action="store_true")
    check.add_argument("--golden", action="append", default=[])
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
    rows = collect(args.golden or list(DEFAULT_GOLDENS), time.time(), latest=latest_release())
    if args.json:
        print(json.dumps({"goldens": rows}, indent=2, sort_keys=True))
    elif rows:
        print("\n".join(render(row) for row in rows))
    else:
        print("no golden images found on this host")
    return 1 if any(row["verdict"] == "fail" for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
