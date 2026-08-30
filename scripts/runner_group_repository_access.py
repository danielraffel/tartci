#!/usr/bin/env python3
"""Fail-closed repository-access admission for GitHub JIT runner groups."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any


REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PER_PAGE = 100
MAX_PAGES = 20


class AccessError(RuntimeError):
    pass


class RepositoryInaccessible(AccessError):
    pass


def api(gh_cli: str, path: str, repo: str, timeout: int) -> dict[str, Any]:
    env = os.environ.copy()
    env["SHIPYARD_GH_APP_REPO"] = repo
    env["GH_REPO"] = repo
    result = subprocess.run(
        [gh_cli, "api", path],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )
    if result.returncode:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise AccessError(f"GitHub API failed for {path}: {detail}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AccessError(f"GitHub API returned malformed JSON for {path}") from exc
    if not isinstance(payload, dict):
        raise AccessError(f"GitHub API returned a non-object for {path}")
    return payload


def verify(repo: str, runner_group_id: int, gh_cli: str, timeout: int) -> dict[str, Any]:
    if runner_group_id == 1:
        return {
            "schema": 1,
            "verdict": "admit",
            "repo": repo,
            "runner_group_id": runner_group_id,
            "registration_scope": "repository",
            "reason": "repository-scoped JIT endpoint binds visibility to the repository",
        }

    owner = repo.split("/", 1)[0]
    group_path = f"orgs/{owner}/actions/runner-groups/{runner_group_id}"
    group = api(gh_cli, group_path, repo, timeout)
    visibility = group.get("visibility")
    if visibility == "all":
        raise RepositoryInaccessible(
            f"runner group {runner_group_id} is visible to multiple repositories; "
            "tartci assignment observation is repository-scoped"
        )
    if visibility != "selected":
        raise AccessError(
            f"runner group {runner_group_id} has unsupported visibility {visibility!r}"
        )

    seen = 0
    selected: list[str] = []
    for page in range(1, MAX_PAGES + 1):
        path = (
            f"{group_path}/repositories?per_page={PER_PAGE}&page={page}"
        )
        payload = api(gh_cli, path, repo, timeout)
        total = payload.get("total_count")
        repositories = payload.get("repositories")
        if type(total) is not int or total < 0 or not isinstance(repositories, list):
            raise AccessError("runner-group repository response has invalid pagination schema")
        for item in repositories:
            if not isinstance(item, dict) or not isinstance(item.get("full_name"), str):
                raise AccessError("runner-group repository response has malformed entries")
            seen += 1
            selected.append(item["full_name"])
        if seen >= total:
            break
        if not repositories:
            raise AccessError("runner-group repository pagination ended before total_count")
    else:
        raise AccessError("runner-group repository pagination exceeded the safety bound")

    if seen != total:
        raise AccessError(
            f"runner-group repository pagination count mismatch ({seen} != {total})"
        )
    if len(selected) != 1 or selected[0].lower() != repo.lower():
        raise RepositoryInaccessible(
            f"runner group {runner_group_id} must select only {repo}; "
            f"observed {len(selected)} selected repositories"
        )
    return {
        "schema": 1,
        "verdict": "admit",
        "repo": repo,
        "runner_group_id": runner_group_id,
        "registration_scope": "organization-single-repository",
        "visibility": visibility,
        "reason": "runner group is exclusively visible to this repository",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--runner-group-id", required=True, type=int)
    parser.add_argument("--gh-cli", default=os.environ.get("TARTCI_JIT_GH_CLI") or "gh")
    parser.add_argument("--timeout-seconds", type=int, default=20)
    args = parser.parse_args(argv)
    if not REPO.fullmatch(args.repo):
        parser.error("--repo must be OWNER/REPO")
    if args.runner_group_id < 1:
        parser.error("--runner-group-id must be positive")
    if not args.gh_cli or any(char.isspace() for char in args.gh_cli):
        parser.error("--gh-cli must be one executable path or name")
    if not 1 <= args.timeout_seconds <= 120:
        parser.error("--timeout-seconds must be 1..120")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        receipt = verify(
            args.repo, args.runner_group_id, args.gh_cli, args.timeout_seconds
        )
    except RepositoryInaccessible as exc:
        print(
            json.dumps(
                {
                    "schema": 1,
                    "verdict": "deny",
                    "repo": args.repo,
                    "runner_group_id": args.runner_group_id,
                    "reason": str(exc),
                },
                sort_keys=True,
            )
        )
        print(f"runner repository access denied: {exc}", file=sys.stderr)
        return 3
    except (AccessError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"runner repository access error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
