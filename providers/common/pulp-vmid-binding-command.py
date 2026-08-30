#!/usr/bin/env python3
"""Render the root QGA payload that binds a nonce to Pulp's `ci` consumer."""

from __future__ import annotations

import argparse
import re
import shlex
import sys


HEX64 = re.compile(r"[0-9a-f]{64}")
BINDING_PATH = re.compile(r"/run/tartci-pulp-golden-[0-9]+[.]binding")


def render(user: str, path: str, nonce: str) -> str:
    if user != "ci":
        raise ValueError("binding consumer must be canonical protected user ci")
    if BINDING_PATH.fullmatch(path) is None:
        raise ValueError("binding path is not the canonical VMID-scoped /run path")
    if HEX64.fullmatch(nonce) is None:
        raise ValueError("binding nonce must be lowercase 64-hex")
    quoted_user = shlex.quote(user)
    quoted_path = shlex.quote(path)
    quoted_nonce = shlex.quote(nonce)
    return "\n".join(
        (
            "set -eu",
            f"uid=$(id -u -- {quoted_user})",
            f"gid=$(id -g -- {quoted_user})",
            f"target={quoted_path}",
            'tmp="${target}.tmp.$$"',
            "cleanup() { rm -f -- \"$tmp\"; }",
            "trap cleanup EXIT HUP INT TERM",
            "umask 077",
            f"printf '%s' {quoted_nonce} >\"$tmp\"",
            'chown "$uid:$gid" "$tmp"',
            'chmod 0400 "$tmp"',
            'mv -f -- "$tmp" "$target"',
            "trap - EXIT HUP INT TERM",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args(argv)
    try:
        print(render(args.user, args.path, args.nonce))
    except ValueError as error:
        print(f"pulp-vmid-binding-command: ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
