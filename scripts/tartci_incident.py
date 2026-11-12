#!/usr/bin/env python3
"""The tartci-incident marker every alert issue carries.

An alert issue's body ends with exactly one line:

    <!-- tartci-incident {"host":...,"class":...,"symptom_at":...,"detected_at":...,"fixed_at":null} -->

`symptom_at` is when the condition began, `detected_at` when the alert
fired, and `fixed_at` when the host recovered. Readers (Shipyard inspect's
attribution, time-to-detect and time-to-repair) parse that line. When the
condition clears, the body is PATCHed so the line carries `fixed_at`, then
the issue is closed.
"""
from __future__ import annotations

import json
import re
from typing import Optional

PREFIX = "<!-- tartci-incident "
SUFFIX = " -->"
_LINE = re.compile(r"^<!-- tartci-incident (\{.*\}) -->$")
FIELDS = ("host", "class", "symptom_at", "detected_at", "fixed_at")


def marker(host: str, cls: str, symptom_at: str, detected_at: str,
           fixed_at: Optional[str] = None) -> str:
    """The marker line for one incident."""
    payload = {"host": host, "class": cls, "symptom_at": symptom_at,
               "detected_at": detected_at, "fixed_at": fixed_at}
    return PREFIX + json.dumps(payload, separators=(",", ":")) + SUFFIX


def with_marker(body: str, line: str) -> str:
    """`body` ending with exactly the one marker line `line`."""
    kept = [row for row in body.rstrip("\n").split("\n") if not _LINE.match(row.strip())]
    return "\n".join(kept).rstrip("\n") + "\n\n" + line


def parse(body: str) -> Optional[dict]:
    """The marker on the body's last line, or None when it is not one."""
    lines = body.rstrip("\n").split("\n")
    found = _LINE.match(lines[-1].strip()) if lines else None
    if not found:
        return None
    try:
        value = json.loads(found.group(1))
    except ValueError:
        return None
    return value if isinstance(value, dict) and all(key in value for key in FIELDS) else None


def fixed(body: str, fixed_at: str) -> str:
    """The body with its one marker line carrying `fixed_at`.

    Raises ValueError when the body does not end with exactly one marker.
    """
    rows = [row for row in body.split("\n") if _LINE.match(row.strip())]
    value = parse(body)
    if len(rows) != 1 or value is None:
        raise ValueError(f"expected exactly one tartci-incident marker, found {len(rows)}")
    value["fixed_at"] = fixed_at
    return with_marker(body, marker(value["host"], value["class"], value["symptom_at"],
                                    value["detected_at"], fixed_at))
