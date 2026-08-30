#!/usr/bin/env python3
"""Resolve the one immutable Pulp source identity used by golden provisioners."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
import tomllib


HEX40 = re.compile(r"[0-9a-f]{40}")


def resolve(
    manifest_path: Path,
    required_skia_release: str | None = None,
    required_v8_disposition: str | None = None,
) -> tuple[str, str]:
    with manifest_path.open("rb") as handle:
        manifest = tomllib.load(handle)
    source = manifest.get("source")
    if not isinstance(source, dict):
        raise ValueError("manifest has no [source] table")
    repository = source.get("repository")
    commit = source.get("commit")
    dependency_manifest = source.get("manifest")
    if not isinstance(repository, str) or not repository or any(
        character in repository for character in "\r\n"
    ):
        raise ValueError("source.repository must be one non-empty line")
    if not isinstance(commit, str) or HEX40.fullmatch(commit) is None:
        raise ValueError("source.commit must be immutable lowercase 40-hex")
    if dependency_manifest != "tools/deps/manifest.json":
        raise ValueError(
            "source.manifest must be canonical tools/deps/manifest.json; "
            "alternate dependency-manifest paths are not implemented"
        )
    if required_skia_release is not None:
        if manifest.get("skia", {}).get("release") != required_skia_release:
            raise ValueError(
                f"manifest Skia release is not {required_skia_release}"
            )
    if required_v8_disposition is not None:
        if manifest.get("v8", {}).get("disposition") != required_v8_disposition:
            raise ValueError(
                f"manifest V8 disposition is not {required_v8_disposition}"
            )
    return repository, commit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--require-skia-release")
    parser.add_argument("--require-v8-disposition")
    args = parser.parse_args(argv)
    try:
        repository, commit = resolve(
            args.manifest,
            args.require_skia_release,
            args.require_v8_disposition,
        )
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"pulp-source-pin: ERROR: {error}", file=sys.stderr)
        return 1
    print(repository)
    print(commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
