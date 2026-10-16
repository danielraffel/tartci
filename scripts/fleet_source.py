#!/usr/bin/env python3
"""Resolve the fleet source used by TartCI's offline tooling.

The public checkout remains the default. Operators may point a process at an
installed private source with ``TARTCI_FLEET_SOURCE_ROOT``. An explicit source
is validated and fails closed; it never silently falls back to the checkout.
"""

from __future__ import annotations

import os
import re
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


PROFILE_GLOB = "*-macos-fleet.toml"
PUBLISHED_SUPPLY = "fleet/advertised-labels.json"
COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class FleetSource:
    root: Path
    profiles_dir: Path
    published_supply: Path
    configured: bool

    @property
    def source_commit(self) -> str | None:
        if not self.configured:
            return None
        value = os.environ.get("TARTCI_FLEET_SOURCE_COMMIT", "").strip()
        return value or None

    @property
    def repository(self) -> str | None:
        if not self.configured:
            return None
        value = os.environ.get("TARTCI_FLEET_SOURCE_REPOSITORY", "").strip()
        return value or None

    @property
    def published_supply_sha256(self) -> str:
        return hashlib.sha256(self.published_supply.read_bytes()).hexdigest()

    @property
    def tree_sha256(self) -> str:
        digest = hashlib.sha256()
        paths = [*sorted(self.profiles_dir.glob(PROFILE_GLOB)), self.published_supply]
        for path in paths:
            _update_digest(digest, path, self.root)
        return digest.hexdigest()

    @property
    def profiles_sha256(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.profiles_dir.glob(PROFILE_GLOB)):
            _update_digest(digest, path, self.root)
        return digest.hexdigest()


def _update_digest(digest: hashlib._Hash, path: Path, root: Path) -> None:
    """Hash path names and bytes in a stable, length-delimited form."""
    relative = path.relative_to(root).as_posix().encode()
    digest.update(len(relative).to_bytes(8, "big"))
    digest.update(relative)
    content = path.read_bytes()
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def resolve(default_root: Path, *, allow_environment: bool = True) -> FleetSource:
    """Return the active source, rejecting an invalid explicit source."""
    configured_root = (
        os.environ.get("TARTCI_FLEET_SOURCE_ROOT", "").strip()
        if allow_environment else ""
    )
    root = Path(configured_root).expanduser() if configured_root else default_root
    root = root.resolve()
    profiles_dir = root / "profiles"
    published_supply = root / PUBLISHED_SUPPLY
    if configured_root:
        if not profiles_dir.is_dir():
            raise ValueError(f"configured fleet source has no profiles directory: {profiles_dir}")
        if not published_supply.is_file():
            raise ValueError(
                "configured fleet source has no published supply: "
                f"{published_supply}"
            )
        if not list(profiles_dir.glob(PROFILE_GLOB)):
            raise ValueError(f"configured fleet source has no macOS fleet profiles: {profiles_dir}")
        try:
            published = json.loads(published_supply.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"configured fleet source has invalid published supply: {published_supply}"
            ) from exc
        if (not isinstance(published, dict)
                or published.get("schema") != "tartci.advertised-labels/v1"
                or not isinstance(published.get("registrations"), list)
                or not isinstance(published.get("generated_from"), dict)):
            raise ValueError(
                "configured fleet source published supply must be a "
                "tartci.advertised-labels/v1 document"
            )
        generated = published["generated_from"]
        if (not isinstance(generated.get("repo"), str) or not generated["repo"].strip()
                or not isinstance(generated.get("profiles"), list)
                or not generated["profiles"]
                or not all(isinstance(item, str) and item for item in generated["profiles"])
                or not isinstance(generated.get("profiles_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", generated["profiles_sha256"])
                or generated["profiles_sha256"] != FleetSource(
                    root, profiles_dir, published_supply, True).profiles_sha256):
            raise ValueError(
                "configured fleet source published supply has stale or incomplete profile metadata"
            )
        configured_commit = os.environ.get("TARTCI_FLEET_SOURCE_COMMIT", "").strip()
        if not COMMIT.fullmatch(configured_commit):
            raise ValueError(
                "configured fleet source requires TARTCI_FLEET_SOURCE_COMMIT "
                "to be a 40-character commit"
            )
        if not os.environ.get("TARTCI_FLEET_SOURCE_REPOSITORY", "").strip():
            raise ValueError(
                "configured fleet source requires TARTCI_FLEET_SOURCE_REPOSITORY"
            )
        configured_repo = os.environ["TARTCI_FLEET_SOURCE_REPOSITORY"].strip()
        if generated.get("repo") != configured_repo or generated.get("commit") != configured_commit:
            raise ValueError(
                "configured fleet source published supply provenance does not match environment"
            )
    return FleetSource(root, profiles_dir, published_supply, bool(configured_root))


def selected_published_supply() -> Path | None:
    """The published supply this process must read, or None for the caller's default.

    A configured source is validated first and wins; TARTCI_ADVERTISED_LABELS,
    when also set, must name that same file. Without a configured source the
    file TARTCI_ADVERTISED_LABELS names is returned as is. Raises ValueError
    when a configured source is invalid or disagrees with
    TARTCI_ADVERTISED_LABELS; a caller must then not read any other supply.
    """
    named = os.environ.get("TARTCI_ADVERTISED_LABELS", "").strip()
    if not os.environ.get("TARTCI_FLEET_SOURCE_ROOT", "").strip():
        return Path(named).expanduser() if named else None
    supply = resolve(Path("/")).published_supply
    if named and Path(named).expanduser().resolve() != supply.resolve():
        raise ValueError(
            f"TARTCI_ADVERTISED_LABELS names {named}, but the configured fleet source "
            f"publishes {supply}")
    return supply
