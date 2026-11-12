#!/usr/bin/env python3
"""Record who changed the pool or a lane: one JSON line per change.

At 05:19:03Z on 2026-10-16 something on m5, over ssh, disabled and booted out
every lane and re-enabled them 31 s later, killing two required jobs. Every
agent denied it and nothing recorded who it was. Every `tartci pool
on|off|drain|undrain` and every `tartci launchd reload` now appends a line to
~/Library/Logs/tartci/manual-changes.log (TARTCI_MANUAL_CHANGE_LOG overrides)
with what was asked, whether it was refused or forced, how the caller got
there (launchd, ssh with its client address, or a terminal) and the command
line of each parent process, so the next one names itself. Credentials in
those command lines are redacted, and the log is owner-only (0600).

    manual_change_log.py ACTION OUTCOME [ARG...]

Standard library only; never fails the change it records.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

MAX_PARENTS = 8
# Parent command lines can carry credentials: a `GH_TOKEN=... gh api` prefix,
# a curl `-H "Authorization: token ..."` or `-u user:pass`, a URL with
# `user:pass@`, the fleet's `security unlock-keychain -p "$PW"` once the shell
# has expanded it. They are redacted before the write.
REDACTIONS = (
    # NAME=value where the name ENDS in a secret word (GH_TOKEN=, API_KEY=,
    # AWS_SECRET_ACCESS_KEY=); StrictHostKeyChecking=accept-new is context.
    (re.compile(r"(?i)\b([A-Z0-9_-]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIALS?))"
                r"=(\"[^\"]*\"|'[^']*'|\S+)"), r"\1=[redacted]"),
    # --token VALUE / --password VALUE: a flag that ends in the secret word
    # (--keychain PATH is not one).
    (re.compile(r"(?i)((?:^|(?<=\s))--?[a-z0-9-]*(?:token|key|secret|password|passwd)\s+)"
                r"(?!-)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1[redacted]"),
    # Credentials in a URL: https://user:pass@host, https://oauth2:TOKEN@host.
    (re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1[redacted]@"),
    # security unlock-keychain -p PASSWORD (the fleet's signing recipe, with
    # the password already expanded by the shell).
    (re.compile(r"(?i)(\bsecurity\b[^|;&\n]*?\s-p\s+)(\"[^\"]*\"|'[^']*'|\S+)"),
     r"\1[redacted]"),
    # curl -u / --user USER:PASS.
    (re.compile(r"(?i)((?:^|(?<=\s))(?:-u|--user)(?:\s+|=))(\"[^\"]*:[^\"]*\"|'[^']*:[^']*'|"
                r"[^\s:]*:\S*)"), r"\1[redacted]"),
    # Authorization headers, any scheme, and bare Bearer values.
    (re.compile(r"(?i)(authorization\s*:\s*)(?:\S+\s+)?[^\s\"']+"), r"\1[redacted]"),
    (re.compile(r"(?i)\b(bearer\s+)[^\s\"']+"), r"\1[redacted]"),
    # GitHub token shapes wherever they appear.
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,})"), "[redacted]"),
)


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def log_path() -> Path:
    return Path(os.environ.get("TARTCI_MANUAL_CHANGE_LOG") or os.path.expanduser(
        "~/Library/Logs/tartci/manual-changes.log"))


def _ps(pid: int) -> Optional[tuple[int, str]]:
    try:
        out = subprocess.run(["/bin/ps", "-o", "ppid=,command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.strip().splitlines()[0] if out.strip() else ""
    head, _, command = line.strip().partition(" ")
    if not head.isdigit():
        return None
    return int(head), command.strip()


def parents(start: int, ps: Callable[[int], Optional[tuple[int, str]]] = _ps) -> list[dict]:
    """[{pid, command}] from `start` up toward launchd, at most MAX_PARENTS."""
    chain = []
    pid = start
    while pid > 1 and len(chain) < MAX_PARENTS:
        found = ps(pid)
        if found is None:
            break
        ppid, command = found
        chain.append({"pid": pid, "command": redact(command)[:400]})
        pid = ppid
    return chain


def caller(env: dict) -> dict:
    launchd = env.get("TARTCI_LAUNCHD") == "1" or \
        str(env.get("XPC_SERVICE_NAME") or "").startswith("com.danielraffel.")
    ssh = env.get("SSH_CONNECTION") or env.get("SSH_CLIENT") or ""
    via = "launchd" if launchd else "ssh" if ssh else "terminal"
    try:
        tty = os.ttyname(0)
    except OSError:
        tty = ""
    return {"via": via, "ssh_client": ssh.split(" ")[0] if ssh else "",
            "ssh_tty": env.get("SSH_TTY", ""), "tty": tty,
            "launchd_job": env.get("XPC_SERVICE_NAME", ""), "user": env.get("USER", "")}


def record(action: str, outcome: str, args: list[str], *, env: Optional[dict] = None,
           start: Optional[int] = None, ps: Callable[[int], Optional[tuple[int, str]]] = _ps,
           now: Optional[float] = None) -> dict:
    env = dict(os.environ) if env is None else env
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now or time.time())),
           "action": action, "outcome": outcome, "args": [redact(a) for a in args],
           **caller(env), "parents": parents(os.getppid() if start is None else start, ps)}
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Owner-only: command lines name hosts, paths and callers.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass
    return row


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.splitlines()[-3].strip(), file=sys.stderr)
        return 2
    record(argv[0], argv[1], argv[2:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
