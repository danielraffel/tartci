#!/usr/bin/env python3
"""Read-only tartci CI routing profile helpers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - macOS hosts should use 3.11+
    print("profile.py requires Python 3.11+ tomllib", file=sys.stderr)
    sys.exit(2)


ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIR = ROOT / "profiles"

PULP_DEFAULT_VARS = {
    ("pr", "macos"): "PULP_LOCAL_MACOS_RUNS_ON_JSON",
    ("pr", "linux"): "PULP_LOCAL_LINUX_RUNS_ON_JSON",
    ("pr", "windows"): "PULP_LOCAL_WINDOWS_RUNS_ON_JSON",
    ("release", "macos"): "PULP_RELEASE_MACOS_RUNS_ON_JSON",
    ("coverage", "macos"): "PULP_COVERAGE_MACOS_RUNS_ON_JSON",
    ("coverage", "windows"): "PULP_COVERAGE_WINDOWS_RUNS_ON_JSON",
}

# Target providers tartci understands. `github` = GitHub-hosted; `tartci` = a
# self-scheduled local Tart/QEMU VM (the runner LaunchAgent path).
VALID_TARGET_PROVIDERS = frozenset({"github", "tartci"})
LANE_SELECTABLE_PROVIDERS = VALID_TARGET_PROVIDERS


def load_profile(name: str) -> tuple[Path, dict[str, Any]]:
    path = PROFILE_DIR / f"{name}.toml"
    if not path.exists():
        raise SystemExit(f"profile not found: {name} ({path})")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    if data.get("name") != name:
        raise SystemExit(f"profile {path} has name={data.get('name')!r}, expected {name!r}")
    return path, data


def profile_files() -> list[Path]:
    return sorted(PROFILE_DIR.glob("*.toml"))


def repo_table(profile: dict[str, Any], repo: str) -> dict[str, Any]:
    repos = profile.get("repo") or {}
    table = repos.get(repo)
    if not isinstance(table, dict):
        raise SystemExit(f"profile {profile.get('name')} has no repo entry for {repo}")
    return table


def target_catalog(profile: dict[str, Any]) -> dict[str, Any]:
    targets = profile.get("targets") or {}
    if not isinstance(targets, dict):
        raise SystemExit("profile targets table must be a table")
    return targets


def selector_json(selector: Any) -> str:
    return json.dumps(selector, separators=(",", ":"))


def resolve_lanes(profile: dict[str, Any], repo: str) -> list[dict[str, Any]]:
    repo_cfg = repo_table(profile, repo)
    targets = target_catalog(profile)
    lanes: list[dict[str, Any]] = []
    for context in sorted(repo_cfg):
        context_cfg = repo_cfg[context]
        if not isinstance(context_cfg, dict):
            continue
        for lane_name in sorted(context_cfg):
            lane_cfg = context_cfg[lane_name]
            if not isinstance(lane_cfg, dict):
                continue
            target_ids = list(lane_cfg.get("targets") or [])
            target_rows = []
            warnings = []
            for target_id in target_ids:
                target = targets.get(target_id)
                if not isinstance(target, dict):
                    warnings.append(f"target {target_id!r} is not defined")
                    target_rows.append({"id": target_id, "missing": True})
                    continue
                row = {"id": target_id, **target}
                target_rows.append(row)
                provider = target.get("provider")
                if provider not in VALID_TARGET_PROVIDERS:
                    warnings.append(
                        f"{target_id} has unknown provider {provider!r}; "
                        f"expected one of {sorted(VALID_TARGET_PROVIDERS)}"
                    )
                if target.get("arch") == "x64" and provider != "github" and not target.get("proven"):
                    warnings.append(f"{target_id} is local x64; treat as smoke until proven")
            if lane_cfg.get("ephemeral_required"):
                for row in target_rows:
                    if row.get("provider") != "github" and not row.get("ephemeral"):
                        warnings.append(f"{row['id']} is not marked ephemeral for coverage")
            github_variable = lane_cfg.get("github_variable") or PULP_DEFAULT_VARS.get((context, lane_name))
            concrete = next((row for row in target_rows if not row.get("missing")), None)
            lanes.append(
                {
                    "context": context,
                    "lane": lane_name,
                    "strategy": lane_cfg.get("strategy", "ordered-fallback"),
                    "enabled": lane_cfg.get("enabled", True),
                    "branch": lane_cfg.get("branch"),
                    "issue_on_failure": lane_cfg.get("issue_on_failure", False),
                    "ephemeral_required": lane_cfg.get("ephemeral_required", False),
                    "github_variable": github_variable,
                    "targets": target_rows,
                    "selected_now": concrete,
                    "selected_runs_on_json": selector_json(concrete.get("runs_on_json")) if concrete else None,
                    "warnings": warnings,
                }
            )
    return lanes


def cmd_list(args: argparse.Namespace) -> int:
    rows = []
    for path in profile_files():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        rows.append({"name": data.get("name", path.stem), "description": data.get("description", ""), "path": str(path)})
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        for row in rows:
            print(f"{row['name']}\t{row['description']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path, _ = load_profile(args.name)
    print(path.read_text())
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    _, profile = load_profile(args.name)
    lanes = resolve_lanes(profile, args.repo)
    data = {"profile": profile["name"], "description": profile.get("description", ""), "repo": args.repo, "lanes": lanes}
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print(f"{profile['name']}: {profile.get('description', '')}")
    print(f"repo: {args.repo}")
    for lane in lanes:
        chain = " -> ".join(row["id"] for row in lane["targets"])
        print(f"{lane['context']}.{lane['lane']}: {chain}")
        if lane.get("github_variable"):
            print(f"  variable: {lane['github_variable']} = {lane.get('selected_runs_on_json')}")
        for warning in lane["warnings"]:
            print(f"  warning: {warning}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    _, profile = load_profile(args.name)
    lanes = resolve_lanes(profile, args.repo)
    changes = []
    warnings = []
    for lane in lanes:
        warnings.extend(f"{lane['context']}.{lane['lane']}: {w}" for w in lane["warnings"])
        if lane.get("github_variable") and lane.get("selected_runs_on_json"):
            changes.append(
                {
                    "variable": lane["github_variable"],
                    "value": lane["selected_runs_on_json"],
                    "context": lane["context"],
                    "lane": lane["lane"],
                    "target": lane["selected_now"]["id"],
                }
            )
    data = {
        "profile": profile["name"],
        "repo": args.repo,
        "read_only": True,
        "changes": changes,
        "warnings": warnings,
        "note": "plan chooses the first configured target only; fleet-aware Shipyard should re-resolve with live host status before applying",
    }
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    print(f"Read-only plan for {args.repo} using profile {profile['name']}")
    for change in changes:
        print(f"set {change['variable']}={change['value']}  # {change['context']}.{change['lane']} via {change['target']}")
    if warnings:
        print("warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    print(data["note"])
    return 0


def _iter_lane_target_refs(node: Any) -> Any:
    """Yield every target id referenced by a lane's `targets = [...]` list,
    anywhere in the profile tree (the top-level `[targets]` catalog is a table,
    not a list, so it is walked into rather than yielded)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "targets" and isinstance(value, list):
                for target_id in value:
                    yield target_id
            elif isinstance(value, dict):
                yield from _iter_lane_target_refs(value)


def cmd_validate(args: argparse.Namespace) -> int:
    names = [args.name] if getattr(args, "name", None) else [p.stem for p in profile_files()]
    errors: list[str] = []
    for name in names:
        _, profile = load_profile(name)
        targets = profile.get("targets") or {}
        if not isinstance(targets, dict):
            errors.append(f"{name}: [targets] must be a table")
            continue
        for target_id, target in targets.items():
            if not isinstance(target, dict):
                continue
            provider = target.get("provider")
            if provider not in VALID_TARGET_PROVIDERS:
                errors.append(
                    f"{name}: target {target_id!r} has unknown provider {provider!r} "
                    f"(expected one of {sorted(VALID_TARGET_PROVIDERS)})"
                )
    if errors:
        for err in sorted(set(errors)):
            print(f"profile-validate: {err}", file=sys.stderr)
        return 1
    print(f"profile-validate: OK ({len(names)} profile(s))")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tartci profile")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)
    p_show = sub.add_parser("show")
    p_show.add_argument("name")
    p_show.set_defaults(func=cmd_show)
    p_validate = sub.add_parser("validate", help="check target providers + lane selectability")
    p_validate.add_argument("name", nargs="?", help="profile name (default: all)")
    p_validate.set_defaults(func=cmd_validate)
    for name, func in (("explain", cmd_explain), ("plan", cmd_plan)):
        p = sub.add_parser(name)
        p.add_argument("name")
        p.add_argument("--repo", required=True)
        p.add_argument("--json", action="store_true")
        p.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
