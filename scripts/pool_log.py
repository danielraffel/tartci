#!/usr/bin/env python3
"""Keep a bounded log of this host's pool status, so host signals can be scored.

`tartci pool status` says what a host is doing now and keeps no history, so a
watcher that reads it can only be judged live. The pool-log support agent runs
`tartci pool log` every 300 s; each pass reads this generation's
`tartci pool status --json` (nothing else: no ssh, no GitHub) and appends one
JSONL record to `~/.tartci/state/pool-log/events.jsonl` when the condensed
state changed, plus one heartbeat record an hour, so a gap in the log reads as
the agent being down rather than as "no change". A record carries exactly what
a host reader classifies:

    {"at", "host", "state", "participating", "problems": {section: text}}

where `problems` maps each `fleet.config` section with a problem to its first
500 characters. A status that cannot be read writes `{"at", "error"}`, once
per distinct error. Free-standing numbers in problem and error text do not
count as a change (a stale tool's "24.8 h behind" counts up every pass), while
numbers inside a version, a sha or a name do; the record keeps the text as
read. The log rotates at 2 MiB to `events.jsonl.1`, so a host
keeps at most two files.

`tartci pool log --status [--json]` prints the log's size, its last record and
the age of the last write.

Python 3.9-safe: the launchd python on fleet hosts is /usr/bin/python3.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAX_BYTES = 2 * 1024 * 1024
HEARTBEAT_S = 3600
STATUS_TIMEOUT_S = 60.0
PROBLEM_CHARS = 500

# argv -> (exit code, stdout, stderr)
Runner = Callable[[Sequence[str], float], Tuple[int, str, str]]


def state_dir() -> pathlib.Path:
    root = os.environ.get("TARTCI_HOME") or str(pathlib.Path.home() / ".tartci")
    return pathlib.Path(root) / "state" / "pool-log"


def max_bytes() -> int:
    try:
        value = int(os.environ.get("TARTCI_POOL_LOG_MAX_BYTES", ""))
    except ValueError:
        return MAX_BYTES
    return value if value > 0 else MAX_BYTES


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_argv() -> List[str]:
    """This generation's `tartci pool status --json`.

    `TARTCI_POOL_LOG_STATUS_ARGV` (a JSON list) replaces it, for tests that
    drive the pass through the CLI.
    """
    override = os.environ.get("TARTCI_POOL_LOG_STATUS_ARGV")
    if override:
        value = json.loads(override)
        if isinstance(value, list) and value and all(isinstance(part, str) for part in value):
            return value
        raise ValueError("TARTCI_POOL_LOG_STATUS_ARGV must be a non-empty JSON list of strings")
    return ["/bin/bash", str(ROOT / "tartci"), "pool", "status", "--json"]


def run(argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return 124, "", f"pool status did not finish in {timeout:.0f} s"
    except OSError as error:
        return 127, "", str(error)
    return done.returncode, done.stdout, done.stderr


def condense(status: Dict[str, Any]) -> Dict[str, Any]:
    """What a host reader classifies, and nothing else."""
    problems: Dict[str, str] = {}
    fleet = status.get("fleet") if isinstance(status.get("fleet"), dict) else {}
    config = fleet.get("config") if isinstance(fleet.get("config"), dict) else {}
    for section, body in sorted(config.items()):
        problem = body.get("problem") if isinstance(body, dict) else None
        if problem:
            problems[str(section)] = str(problem)[:PROBLEM_CHARS]
    return {
        "host": status.get("host"),
        "state": status.get("state"),
        "participating": status.get("participating"),
        "problems": problems,
    }


def read_status(runner: Runner) -> Dict[str, Any]:
    """The condensed status, or `{"error": <first line>}`."""
    try:
        argv = status_argv()
    except ValueError as error:
        return {"error": str(error)}
    code, out, err = runner(argv, STATUS_TIMEOUT_S)
    if code != 0:
        first = (err.strip() or out.strip() or f"exit {code}").splitlines()[0]
        return {"error": f"pool status exited {code}: {first[:PROBLEM_CHARS]}"}
    try:
        value = json.loads(out)
    except ValueError as error:
        return {"error": f"pool status printed no JSON: {error}"}
    if not isinstance(value, dict):
        return {"error": "pool status printed JSON that is not an object"}
    return condense(value)


# A free-standing quantity only: not part of a version (v0.955.1), a commit
# sha, or a name (m1), whose change is a real change.
NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")


def _digest(record: Dict[str, Any]) -> str:
    """What makes a record a change. Free-standing numbers in problem and
    error text are left out: a stale tool's problem reads "pulp 24.8 h behind"
    and counts up every pass, and the same condition must not write a record
    each time. Numbers inside versions, shas and names still count."""
    def steady(value: Any) -> Any:
        if isinstance(value, str):
            return NUMBER.sub("#", value)
        if isinstance(value, dict):
            return {key: steady(item) for key, item in value.items()}
        return value
    stable = {key: steady(value) if key in ("problems", "error") else value
              for key, value in record.items()}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def _read_last(path: pathlib.Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_atomic(path: pathlib.Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def append(directory: pathlib.Path, line: str, limit: int) -> None:
    """Append one line, first rotating the log if it would pass `limit`."""
    log = directory / "events.jsonl"
    data = line.encode()
    try:
        size = log.stat().st_size
    except OSError:
        size = 0
    if size and size + len(data) > limit:
        os.replace(log, directory / "events.jsonl.1")
    with log.open("ab") as handle:
        handle.write(data)


def log_pass(now: float, runner: Runner = run,
             directory: Optional[pathlib.Path] = None) -> Optional[Dict[str, Any]]:
    """One pass: the record written, or None when nothing changed."""
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    condensed = read_status(runner)
    digest = _digest(condensed)
    last_path = directory / "last.json"
    last = _read_last(last_path)
    changed = last.get("digest") != digest
    due = now - float(last.get("written_at") or 0) >= HEARTBEAT_S
    if not changed and not due:
        return None
    record: Dict[str, Any] = {"at": _iso(now), **condensed}
    if not changed:
        record["heartbeat"] = True
    append(directory, json.dumps(record, sort_keys=True) + "\n", max_bytes())
    _write_atomic(last_path, json.dumps({"digest": digest, "written_at": now}))
    return record


def log_status(now: float, directory: Optional[pathlib.Path] = None) -> Dict[str, Any]:
    directory = directory or state_dir()
    log = directory / "events.jsonl"
    rotated = directory / "events.jsonl.1"
    sizes = {name: path.stat().st_size for name, path in
             (("events.jsonl", log), ("events.jsonl.1", rotated)) if path.exists()}
    last_record = None
    if log.exists():
        lines = [line for line in log.read_text().splitlines() if line.strip()]
        if lines:
            last_record = json.loads(lines[-1])
    written_at = _read_last(directory / "last.json").get("written_at")
    return {
        "log": str(log),
        "bytes": sizes,
        "last": last_record,
        "last_write_age_s": round(now - float(written_at)) if written_at else None,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci pool log")
    parser.add_argument("--status", action="store_true",
                        help="print the log's size, last record and last write age")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    now = time.time()
    if args.status:
        result = log_status(now)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            age = result["last_write_age_s"]
            print(f"pool-log: {result['log']} {result['bytes'] or 'empty'}; last write "
                  f"{'never' if age is None else f'{age} s ago'}")
            if result["last"] is not None:
                print(f"pool-log: last record {json.dumps(result['last'], sort_keys=True)}")
        return 0
    record = log_pass(now)
    if args.json:
        print(json.dumps({"written": record}, sort_keys=True))
    elif record is not None:
        kind = "heartbeat" if record.get("heartbeat") else "error" if "error" in record else "change"
        print(f"{record['at']} pool-log: wrote {kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
