#!/usr/bin/env python3
"""Fail-closed preparation report for the not-yet-receipted Pulp macOS golden."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tomllib


def load_source_resolver():
    path = Path(__file__).with_name("pulp-source-pin.py")
    spec = importlib.util.spec_from_file_location("pulp_source_pin", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load source resolver {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report(manifest_path: Path) -> dict[str, object]:
    with manifest_path.open("rb") as handle:
        manifest = tomllib.load(handle)
    source_pin = load_source_resolver()
    repository, commit = source_pin.resolve(manifest_path)
    readiness = manifest.get("golden_readiness")
    if not isinstance(readiness, dict) or readiness.get("status") != "unready":
        raise ValueError(
            "macOS golden cannot be marked ready until TartCI implements "
            "receipt-backed exact-golden verification"
        )
    reason = readiness.get("reason")
    if not isinstance(reason, str) or not reason:
        raise ValueError("unready macOS golden must record a non-empty reason")
    if manifest.get("os") != "macos" or manifest.get("arch") != "arm64":
        raise ValueError("readiness report requires the macOS arm64 Pulp manifest")
    skia = manifest.get("skia", {})
    v8 = manifest.get("v8", {})
    return {
        "schema": 1,
        "status": "unready",
        "reason": reason,
        "required": {
            "pulp_repository": repository,
            "pulp_commit": commit,
            "skia_release": skia.get("release"),
            "skia_variant": skia.get("variant"),
            "v8_disposition": v8.get("disposition"),
            "v8_variant": v8.get("variant"),
        },
        "next_action": (
            "bake and export an exact-golden render receipt, then implement "
            "receipt-to-golden identity verification before promotion"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = report(args.manifest)
    except (OSError, RuntimeError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"pulp-macos-readiness: ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1  # An unready preparation report is intentionally never green.


if __name__ == "__main__":
    raise SystemExit(main())
