#!/usr/bin/env python3
"""Is the GitHub Actions runner each golden's VMs run still accepted by GitHub?

GitHub retires old runner versions. A retired agent registers, prints "Runner
version vX is deprecated and cannot receive messages" and exits, so every clone
of a golden that carries it fails the same way. A runner pool stopped for 13
days like that with nothing alerting.

Per golden this reports one of:

  fail     a runner log shows GitHub refusing this version. The refusal is
           remembered and clears only when the version changes, so the alert
           outlives the log and a lane that has stopped trying
  warn     the version trails the latest release by
           `runner_version.WARN_MINOR_LAG` minor versions or more (doctor only)
  ok       the version is known and within that lag
  unknown  the version or the latest release could not be read

The lag verdict comes from `runner_version.evaluate`, the rule the macOS pin
check already uses, so the two cannot disagree about how far behind is too
far. Refusals are read from every lane's runner logs, including the fleet
lanes' own state directories.

Only `fail` is a problem for the watchdog: GitHub is refusing the runner now.
`warn` and `unknown` are doctor output. A lag warning would fire on every host
each time upstream ships two releases, and a host that has never recorded its
golden, or cannot reach GitHub, must not page anyone.

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
# Only logs written this recently are opened. A refusal found in one is
# remembered in the state directory, so the verdict does not depend on the log
# still being recent.
LOG_SCAN_WINDOW_SECS = 86400.0
# The refusal is printed when the listener starts, so the head of a log is
# enough and a long build log is never read whole.
LOG_HEAD_BYTES = 262144


def clean(version: Optional[str]) -> Optional[str]:
    parsed = rv.parse_version(version)
    return ".".join(str(part) for part in parsed) if parsed else None


def _newest(versions) -> Optional[str]:
    parsed = sorted(v for v in (rv.parse_version(x) for x in versions or ()) if v)
    return ".".join(str(part) for part in parsed[-1]) if parsed else None


def classify(installed: Optional[str], latest: Optional[str], refused=()) -> dict:
    """Pure policy. `refused` holds the versions GitHub is refusing for this
    golden's lane right now (see `refusals`, which drops the ones that no
    longer apply). Any of them is a fail.
    """
    have, newest = clean(installed), clean(latest)
    row = {"installed": have, "latest": newest}
    named = {clean(v) for v in refused or ()} - {None}
    if named:
        hit = have if have in named else _newest(named)
        return dict(row, verdict="fail", reason=f"GitHub refuses runner v{hit} as deprecated")
    if have is None:
        return dict(row, verdict="unknown", reason="runner version is not recorded")
    status, _ = rv.evaluate(have, newest, [])
    if status == "unknown":
        return dict(row, verdict="unknown", reason="latest runner release could not be read")
    if status == "warn":
        return dict(row, verdict="warn", reason=(
            f"runner v{have} is {rv.minor_lag(have, newest)} minor versions behind v{newest}"))
    return dict(row, verdict="ok", reason=f"runner v{have} (latest v{newest})")


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
    version = value.get("version") if isinstance(value, dict) else None
    return version if isinstance(version, str) else None


def recorded_at(golden: str, home: Optional[Path] = None) -> Optional[float]:
    """When the record was written (file time), or None without a record."""
    try:
        return os.path.getmtime(state_dir(home) / f"{base_name(golden)}.json")
    except OSError:
        return None


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
    env = dict(os.environ)
    try:
        import tart_home
        env["TART_HOME"] = tart_home.resolve()["path"]
    except Exception:  # noqa: BLE001 - fall back to the caller's environment
        pass
    try:
        proc = subprocess.run(["tart", "list", "--quiet"], capture_output=True, text=True,
                              timeout=30, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {base_name(line.strip()) for line in proc.stdout.splitlines() if line.strip()}


def log_globs(golden: str, home: Optional[Path] = None) -> list:
    """Where the lanes that clone `golden` keep each runner's output."""
    if base_name(golden) in PINNED_GOLDENS:
        patterns = rv.lane_log_globs(str(tartci_home(home) / "state" / "macos"))
        if os.environ.get("TARTCI_STATE_DIR"):
            patterns += rv.lane_log_globs(os.environ["TARTCI_STATE_DIR"])
        return sorted(set(patterns))
    root = (os.environ.get("TARTCI_LINUX_LOGS") or os.environ.get("PULP_LINUX_LOGS")
            or str((home or Path.home()) / "VMs" / "logs" / "tartci-linux"))
    return [os.path.join(root, "*", "runner-output.log")]


def scan_refusals(golden: str, now: float, home: Optional[Path] = None) -> dict:
    """{version: newest log time} for refusals in this golden's recent runner logs.

    Only logs written in the scan window are opened, and only their head.
    """
    found: dict = {}
    for pattern in log_globs(golden, home):
        for path in glob.glob(pattern):
            try:
                mtime = os.path.getmtime(path)
                if now - mtime > LOG_SCAN_WINDOW_SECS:
                    continue
                with open(path, errors="replace") as handle:
                    version = rv.find_deprecated(handle.read(LOG_HEAD_BYTES))
            except OSError:
                continue
            if version:
                found[version] = max(mtime, found.get(version, 0.0))
    return found


def _refused_path(golden: str, home: Optional[Path] = None) -> Path:
    return state_dir(home) / f"{base_name(golden)}.refused.json"


def read_refused(golden: str, home: Optional[Path] = None) -> dict:
    """Remembered refusals as {version: time last seen}."""
    try:
        value = json.loads(_refused_path(golden, home).read_text())
    except (OSError, ValueError):
        return {}
    seen = value.get("seen") if isinstance(value, dict) else None
    out = {}
    for version, when in (seen.items() if isinstance(seen, dict) else ()):
        if clean(version) and isinstance(when, (int, float)):
            out[clean(version)] = float(when)
    return out


def remember_refused(golden: str, seen: dict, home: Optional[Path] = None) -> None:
    """Keep refusals across passes. An empty mapping forgets them."""
    path = _refused_path(golden, home)
    try:
        if not seen:
            if path.exists():
                path.unlink()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".refused.")
        with os.fdopen(fd, "w") as handle:
            json.dump({"golden": base_name(golden), "seen": seen}, handle, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def refusals(golden: str, installed: Optional[str], now: float, *,
             home: Optional[Path] = None, persist: bool = False,
             since: Optional[float] = None) -> set:
    """Versions GitHub is refusing for this golden's lane, remembered or fresh.

    A refusal stops applying when it names a version other than the installed
    one AND was last seen before `since` (when the Linux record was written).
    That is how recording a new version clears it, while a refusal logged after
    the record still fails: the record is then wrong about what the lane runs.
    The macOS pin has no such time, so there only the pinned version applies.
    With no installed version every refusal applies.
    """
    have = clean(installed)
    seen = read_refused(golden, home)
    for version, when in scan_refusals(golden, now, home).items():
        seen[version] = max(when, seen.get(version, 0.0))
    live = {v: t for v, t in seen.items()
            if have is None or v == have or (since is not None and t > since)}
    if persist and live != read_refused(golden, home):
        remember_refused(golden, live, home)
    return set(live)


def latest_release(home: Optional[Path] = None) -> Optional[str]:
    """Latest actions/runner version through the cached lookup the pin check uses."""
    cache = (home or Path.home()) / ".cache" / "tartci" / "runner-latest.json"
    try:
        return rv.latest_release(str(cache))
    except Exception:  # noqa: BLE001 - an unreadable release is "unknown"
        return None


def collect(goldens, now: float, *, home: Optional[Path] = None,
            latest: Optional[str] = None, persist: bool = False,
            present: Optional[Callable[[], Optional[set]]] = None,
            read: Optional[Callable[[str], Optional[str]]] = None,
            pin: Optional[Callable[[], Optional[str]]] = None,
            refusal: Optional[Callable[[str, Optional[str]], set]] = None) -> list:
    """One row per golden this host holds.

    When the local images cannot be listed nothing is judged: every golden is
    `unknown`, so a host that may not hold a golden never alerts for it.
    `persist` lets the watchdog pass remember refusals; doctor only reads.
    """
    here = (present or present_goldens)()
    rows = []
    for golden in goldens:
        name = base_name(golden)
        pinned = name in PINNED_GOLDENS
        source = "pin" if pinned else "record"
        if here is None:
            # Still report a refusal already on file; judge nothing else.
            row = classify(None, latest, set(read_refused(golden, home)))
            if row["verdict"] != "fail":
                row["reason"] = "local Tart images could not be listed"
            row.update(golden=name, source=source)
            rows.append(row)
            continue
        if name not in here:
            continue
        if pinned:
            installed, since = (pin or macos_pin)(), None
        else:
            installed = (read or (lambda g: read_record(g, home)))(golden)
            since = recorded_at(golden, home)
        refused = (refusal or (lambda g, v: refusals(g, v, now, home=home, persist=persist,
                                                     since=since)))(golden, installed)
        row = classify(installed, latest, refused)
        row.update(golden=name, source=source)
        rows.append(row)
    return rows


def render(row: dict) -> str:
    return (f"{row.get('golden', '?')}: {str(row.get('verdict', 'unknown')).upper()}: "
            f"{row.get('reason', '')}")


def problems(rows: list) -> list:
    """Only a live refusal is a problem. Its text names the refused version and
    nothing that moves with the clock or with upstream releases."""
    return [render(row) for row in rows if row.get("verdict") == "fail"]


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
