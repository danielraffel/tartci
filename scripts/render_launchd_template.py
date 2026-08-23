#!/usr/bin/env python3
"""Render plist string placeholders without shell or XML escaping hazards."""

from __future__ import annotations

import argparse
import plistlib
import re
import sys
from pathlib import Path
from typing import Any


def replace_strings(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for name, replacement in replacements.items():
            value = value.replace(f"${name}", replacement)
        return value
    if isinstance(value, list):
        return [replace_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: replace_strings(item, replacements)
            for key, item in value.items()
        }
    return value


def parse_setting(raw: str) -> tuple[str, str]:
    name, separator, value = raw.partition("=")
    if not separator or not name or not name.replace("_", "").isalnum():
        raise argparse.ArgumentTypeError("settings must use NAME=value")
    return name, value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("template", type=Path)
    parser.add_argument("--set", action="append", default=[], type=parse_setting)
    parser.add_argument(
        "--environment",
        action="append",
        default=[],
        type=parse_setting,
        help="inject an EnvironmentVariables NAME=value entry",
    )
    args = parser.parse_args()
    replacements = dict(args.set)

    # Some historical template comments contain shell snippets with raw `&`.
    # They are accepted by plutil but not by Python's strict XML parser. They
    # are installation guidance rather than plist data, so omit them from the
    # rendered agent before parsing and serializing the actual payload.
    source = args.template.read_bytes()
    source = re.sub(rb"<!--.*?-->", b"", source, flags=re.DOTALL)
    value = plistlib.loads(source)
    rendered = replace_strings(value, replacements)
    environment = rendered.setdefault("EnvironmentVariables", {})
    for name, setting in args.environment:
        environment[name] = setting
    plistlib.dump(rendered, sys.stdout.buffer, sort_keys=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
