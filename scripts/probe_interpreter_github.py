#!/usr/bin/env python3
"""Probe GitHub reachability through the exact pinned interpreter."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence


PROBE_CODE = """
import urllib.error
import urllib.request
try:
    urllib.request.urlopen('https://api.github.com', timeout=2).read(1)
except urllib.error.HTTPError:
    # Any HTTP status, including 403/429 rate limiting, proves network reachability.
    pass
except urllib.error.URLError:
    # DNS, refused connections, and other transport failures are not reachable.
    raise
""".strip()


def probe(interpreter: str, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> str | None:
    """Return an actionable failure, or None when the endpoint responded."""
    try:
        result = run([interpreter, "-c", PROBE_CODE], text=True,
                     capture_output=True, timeout=3, check=False)
    except subprocess.TimeoutExpired:
        return f"interpreter GitHub probe timed out after 3s: {interpreter}"
    if result.returncode == 0:
        return None
    detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["unknown error"]
    return f"interpreter GitHub probe failed ({interpreter}): {detail[0]}"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 1:
        print(f"usage: {sys.argv[0]} INTERPRETER", file=sys.stderr)
        return 64
    error = probe(args[0])
    if error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
