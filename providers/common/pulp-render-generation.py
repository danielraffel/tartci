#!/usr/bin/env python3
"""Verify and receipt one exact Pulp render-toolchain golden generation.

The verifier deliberately imports the fetch validators from the exact Pulp
checkout being baked.  This keeps TartCI from inventing a second interpretation
of Pulp's provider receipts and means a future stronger receipt schema is
inherited automatically by the golden refresh path.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
PLATFORM_KEYS = {
    "darwin-arm64": "mac-arm64",
    "darwin-x64": "mac-x86_64",
    "linux-arm64": "linux-arm64",
    "linux-x64": "linux-x64",
}
EXPECTED_PROBES = {
    "darwin-arm64": [("arm64", "native")],
    "darwin-x64": [("x86_64", "native")],
    "darwin-universal": [("arm64", "native"), ("x86_64", "rosetta-x86_64")],
    "linux-arm64": [("aarch64", "native")],
    "linux-x64": [("x86_64", "native")],
}


def fail(message: str) -> None:
    raise ValueError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        fail(f"cannot load verifier module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True
    )
    if result.returncode != 0:
        fail(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def dependency(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    for entry in manifest.get("dependencies", []):
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    fail(f"exact Pulp manifest has no {name} dependency")


def require_hex(value: object, length: int, label: str) -> str:
    pattern = HEX40 if length == 40 else HEX64
    text = str(value or "")
    if pattern.fullmatch(text) is None:
        fail(f"{label} is not an exact {length}-hex identity")
    return text


def verify_capability_result(
    path: Path,
    platform: str,
    asset_sha: str,
    receipt_sha: str,
    probe_source_sha: str,
) -> dict[str, Any]:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot read capability result {path}: {error}")
    expected = {
        "schema_version": 1,
        "status": "pass",
        "platform": platform,
        "asset_sha256": asset_sha,
        "generation_receipt_sha256": receipt_sha,
        "probe_source_sha256": probe_source_sha,
    }
    for field, value in expected.items():
        if result.get(field) != value:
            fail(
                f"capability result {field} mismatch: "
                f"actual={result.get(field)!r}, expected={value!r}"
            )
    probes = result.get("probes")
    expected_probes = EXPECTED_PROBES[platform]
    if not isinstance(probes, list) or len(probes) != len(expected_probes):
        fail(
            "capability result probe count mismatch: "
            f"actual={len(probes) if isinstance(probes, list) else 'invalid'}, "
            f"expected={len(expected_probes)}"
        )
    for probe, (architecture, run_mode) in zip(probes, expected_probes):
        if not isinstance(probe, dict) or any(
            probe.get(field) != "pass" for field in ("compile", "link", "run")
        ):
            fail("capability result contains a non-passing probe")
        if probe.get("architecture") != architecture:
            fail("capability result probe architecture mismatch")
        if probe.get("run_mode") != run_mode:
            fail("capability result probe run mode mismatch")
    return result


def verify(args: argparse.Namespace) -> dict[str, Any]:
    repo = args.repo.resolve()
    expected_pulp_sha = require_hex(args.pulp_sha, 40, "Pulp SHA")
    if git(repo, "rev-parse", "HEAD") != expected_pulp_sha:
        fail("Pulp checkout HEAD does not equal the immutable requested SHA")
    if git(repo, "status", "--porcelain=v1", "--untracked-files=no"):
        fail("Pulp checkout has tracked modifications")

    manifest_path = repo / "tools/deps/manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot read exact Pulp dependency manifest: {error}")
    platform_key = PLATFORM_KEYS[args.platform]

    skia = dependency(manifest, "Skia")
    if skia.get("version") != "chrome/m153":
        fail(f"golden refresh requires exact chrome/m153, found {skia.get('version')!r}")
    skia_det = skia.get("determinism")
    if not isinstance(skia_det, dict):
        fail("Skia determinism block is missing")
    skia_commit = require_hex(skia_det.get("skia_commit"), 40, "Skia commit")
    built_dawn = require_hex(skia_det.get("built_dawn"), 40, "built Dawn commit")
    try:
        skia_asset_sha = require_hex(
            skia_det["release_assets"][platform_key]["sha256"],
            64,
            "Skia asset SHA-256",
        )
    except (KeyError, TypeError) as error:
        fail(f"Skia manifest has no {platform_key} asset: {error}")

    skia_root = repo / "external/skia-build"
    skia_fetch = load_module(
        repo / "tools/scripts/fetch_skia_for_release.py", "pulp_fetch_skia"
    )
    if not skia_fetch.cache_generation_valid(
        skia_root, args.platform, skia_asset_sha
    ):
        fail("Skia/Dawn extracted generation failed Pulp's deep receipt validator")
    skia_receipt = skia_root / skia_fetch.GENERATION_RECEIPT
    skia_receipt_sha = sha256_file(skia_receipt)
    capability_verifier = load_module(
        repo / "tools/scripts/verify_skia_m153_capabilities.py",
        "pulp_verify_skia_m153_capabilities",
    )
    probe_source = getattr(capability_verifier, "PROBE_SOURCE", None)
    if not isinstance(probe_source, str) or not probe_source:
        fail("exact Pulp capability verifier has no PROBE_SOURCE")
    probe_source_sha = hashlib.sha256(probe_source.encode()).hexdigest()
    capability = verify_capability_result(
        args.capability_result,
        args.platform,
        skia_asset_sha,
        skia_receipt_sha,
        probe_source_sha,
    )

    v8 = dependency(manifest, "V8")
    v8_det = v8.get("determinism")
    if not isinstance(v8_det, dict):
        fail("V8 determinism block is missing")
    if v8_det.get("milestone") != 153 or v8_det.get("pair_kind") != "chromium-milestone":
        fail("V8 is not the matched Chromium m153 provider")
    if v8_det.get("paired_skia") != skia_commit:
        fail("V8 paired Skia does not equal the active Skia generation")
    if v8_det.get("paired_dawn") != built_dawn:
        fail("V8 paired Dawn does not equal the active bundled Dawn generation")
    try:
        v8_asset_sha = require_hex(
            v8_det["release_assets"][platform_key]["sha256"],
            64,
            "V8 asset SHA-256",
        )
    except (KeyError, TypeError) as error:
        fail(f"V8 manifest has no {platform_key} asset: {error}")

    v8_receipt_sha: str | None = None
    if args.v8_disposition == "baked-provider-only":
        v8_fetch = load_module(
            repo / "tools/scripts/fetch_v8_for_release.py", "pulp_fetch_v8"
        )
        v8_root = repo / "external/v8-build" / platform_key
        if not v8_fetch.generation_valid(
            v8_root, manifest, v8, platform_key, v8_asset_sha
        ):
            fail("V8 extracted generation failed Pulp's deep receipt validator")
        v8_receipt_sha = sha256_file(v8_root / v8_fetch.GENERATION_RECEIPT)

    parent_digest = require_hex(args.parent_digest, 64, "parent digest")
    receipt = {
        "schema": 1,
        "status": "pass",
        "pulp": {
            "repository": args.pulp_repository,
            "commit": expected_pulp_sha,
            "manifest_sha256": sha256_file(manifest_path),
        },
        "parent": {
            "kind": args.parent_kind,
            "identity": args.parent_identity,
            "digest_sha256": parent_digest,
        },
        "skia_dawn": {
            "release": skia["version"],
            "skia_commit": skia_commit,
            "built_dawn": built_dawn,
            "platform": args.platform,
            "asset_sha256": skia_asset_sha,
            "generation_receipt_sha256": skia_receipt_sha,
            "capability_result_sha256": sha256_file(args.capability_result),
            "capabilities": [
                "SkLogHandler.GetInstance.SetInstance.compile-link-run",
                "Graphite.ContextOptions.fExecutor.compile-link-run",
            ],
            "probe_count": len(capability["probes"]),
        },
        "v8": {
            "disposition": args.v8_disposition,
            "version": v8.get("version"),
            "platform": args.platform,
            "asset_sha256": v8_asset_sha,
            "generation_receipt_sha256": v8_receipt_sha,
            "runtime_policy": "provider-cached; Pulp defaults to QuickJS",
        },
    }
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--pulp-sha", required=True)
    parser.add_argument(
        "--pulp-repository", default="https://github.com/Generous-Corp/pulp"
    )
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORM_KEYS))
    parser.add_argument("--capability-result", required=True, type=Path)
    parser.add_argument(
        "--v8-disposition",
        choices=("baked-provider-only", "manifest-only-default-quickjs"),
        required=True,
    )
    parser.add_argument("--parent-kind", required=True)
    parser.add_argument("--parent-identity", required=True)
    parser.add_argument("--parent-digest", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = verify(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, args.output)
        print(f"pulp-render-generation: verified {args.output}")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"pulp-render-generation: ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
