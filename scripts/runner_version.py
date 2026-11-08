#!/usr/bin/env python3
"""Detect a stale pinned GitHub Actions Runner before GitHub retires it.

GitHub retires old actions/runner releases on a schedule. A retired listener
exits at start with "Runner version vX is deprecated and cannot receive
messages", so every minted JIT VM dies at once and the lane keeps booting
fresh ones. The macOS lane already installs its pinned runner version into each
VM, so the failure is a stale PIN, which nothing compared with upstream.

Two read-only questions, both fail-open (an unreadable answer never turns into
a problem):

  * pin lag: how many minor versions is the pin behind the latest release?
    The latest release is cached; at most one API call per cache TTL (a day).
  * retirement evidence: does any lane runner log say the pinned runner is
    deprecated? That is a PROBLEM regardless of lag.

CLI: runner_version.py check --pinned V --state-dir DIR [--cache FILE] [--json]
Exit 0 for ok/warn/unknown, 1 for a PROBLEM.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

WARN_MINOR_LAG = 2
CACHE_TTL_SECS = 86400
DEPRECATED_RE = re.compile(
    r"Runner version v?(\d+\.\d+\.\d+) is deprecated and cannot receive messages")
_VER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(text):
    m = _VER_RE.match((text or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


def minor_lag(pinned, latest):
    """Minor versions the pin trails the latest release; None if unparseable."""
    p, l = parse_version(pinned), parse_version(latest)
    if p is None or l is None:
        return None
    if l[0] != p[0]:
        return 99 if l > p else 0
    return max(0, l[1] - p[1])


def find_deprecated(text):
    """The retired version named by a runner log, or None."""
    m = DEPRECATED_RE.search(text or "")
    return m.group(1) if m else None


def _gh_latest(gh_cli):
    out = subprocess.run(
        [gh_cli, "api", "repos/actions/runner/releases/latest", "--jq", ".tag_name"],
        capture_output=True, text=True, timeout=20, check=True).stdout.strip()
    return out.lstrip("v") if parse_version(out) else None


def latest_release(cache_path, now=None, ttl=CACHE_TTL_SECS, fetch=None):
    """Latest actions/runner version, cached; None when unknown (fail open)."""
    now = time.time() if now is None else now
    cached = None
    try:
        with open(cache_path) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        pass
    if cached and now - float(cached.get("fetched_at", 0)) < ttl and cached.get("latest"):
        return cached["latest"]
    try:
        latest = (fetch or (lambda: _gh_latest(os.environ.get("TARTCI_GH_CLI", "gh"))))()
    except Exception:
        latest = None
    if latest:
        try:
            os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
            with open(cache_path, "w") as f:
                json.dump({"latest": latest, "fetched_at": now}, f)
        except OSError:
            pass
        return latest
    return cached.get("latest") if cached else None


def scan_logs(state_dir, pinned):
    """Lane runner logs whose listener reported the pinned version retired."""
    hits = []
    for path in glob.glob(os.path.join(state_dir, "*.actions-runner.log")):
        try:
            with open(path, errors="replace") as f:
                version = find_deprecated(f.read())
        except OSError:
            continue
        if version and version == pinned.lstrip("v"):
            hits.append({"log": path, "version": version})
    return hits


def evaluate(pinned, latest, deprecated_hits):
    if deprecated_hits:
        return "problem", ("runner v%s is deprecated and cannot receive messages "
                           "(seen in %s); bump the pin in providers/tart-macos/runner.sh"
                           % (deprecated_hits[0]["version"], deprecated_hits[0]["log"]))
    lag = minor_lag(pinned, latest)
    if lag is None:
        return "unknown", "latest actions/runner release unavailable; pin v%s not compared" % pinned
    if lag >= WARN_MINOR_LAG:
        return "warn", ("pinned runner v%s is %d minor versions behind v%s; "
                        "GitHub retires old runners, bump the pin" % (pinned, lag, latest))
    return "ok", "pinned runner v%s (latest v%s)" % (pinned, latest)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["check"])
    ap.add_argument("--pinned", required=True)
    ap.add_argument("--state-dir", default="")
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/tartci/runner-latest.json"))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    hits = scan_logs(a.state_dir, a.pinned) if a.state_dir else []
    status, msg = evaluate(a.pinned, latest_release(a.cache), hits)
    if a.json:
        print(json.dumps({"status": status, "message": msg, "pinned": a.pinned}))
    else:
        print("%s: %s" % (status.upper(), msg))
    return 1 if status == "problem" else 0


if __name__ == "__main__":
    sys.exit(main())
