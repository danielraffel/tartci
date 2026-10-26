#!/usr/bin/env python3
"""Discover the upstream Tart release and materialize a reviewed fleet pin.

The fleet deliberately installs Tart through the signed ``openai/tools``
Homebrew tap: Tart and Softnet are a tested pair, and the self-update engine
keeps the previous kegs for rollback.  This helper only *discovers* upstream
bytes and updates ``fleet/tart.toml`` in a checkout.  It never changes a host.

The scheduled GitHub workflow calls ``--write`` and opens a normal PR.  A
human review/merge is the release gate; the installed hosts then use the
existing drained, one-at-a-time Tart canary rollout.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable

import tart_pin

TART_REPO = "openai/tart"
TART_RELEASES_ATOM = f"https://github.com/{TART_REPO}/releases.atom"
TART_FORMULA = "https://raw.githubusercontent.com/openai/homebrew-tools/main/Formula/tart.rb"
SOFTNET_FORMULA = "https://raw.githubusercontent.com/openai/homebrew-tools/main/Formula/softnet.rb"
USER_AGENT = "tartci-tart-pin-update"
TIMEOUT_SECONDS = 30
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ATOM = "{http://www.w3.org/2005/Atom}"


class DiscoveryError(RuntimeError):
    """The release or tap metadata was unavailable or inconsistent."""


@dataclasses.dataclass(frozen=True)
class Candidate:
    version: str
    tart_sha256: str
    softnet_version: str
    softnet_sha256: str
    release_url: str
    release_published_at: str
    checksums_url: str
    tart_formula_url: str = TART_FORMULA
    softnet_formula_url: str = SOFTNET_FORMULA

    def as_json(self) -> dict[str, str]:
        return {
            "version": self.version,
            "tart_sha256": self.tart_sha256,
            "softnet_version": self.softnet_version,
            "softnet_sha256": self.softnet_sha256,
            "release_url": self.release_url,
            "release_published_at": self.release_published_at,
            "checksums_url": self.checksums_url,
            "tart_formula_url": self.tart_formula_url,
            "softnet_formula_url": self.softnet_formula_url,
        }


Fetch = Callable[[str], str]


def fetch_url(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            return response.read().decode("utf-8", "replace")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise DiscoveryError(f"fetch failed for {url}: {exc}") from exc


def _version(value: object, *, field: str) -> str:
    text = str(value or "")
    if not VERSION.fullmatch(text):
        raise DiscoveryError(f"{field} is not a stable X.Y.Z version: {text!r}")
    return text


def _version_key(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _release_metadata(text: str) -> tuple[str, str, str, str]:
    """Read the newest stable numeric release from GitHub's quota-free Atom feed."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise DiscoveryError(f"GitHub releases feed was not Atom XML: {exc}") from exc
    releases: list[tuple[tuple[int, int, int], str, str, str]] = []
    for entry in root.iter(f"{ATOM}entry"):
        link = entry.find(f"{ATOM}link")
        href = link.get("href", "") if link is not None else ""
        match = re.search(r"/releases/tag/(v?\d+\.\d+\.\d+)$", href)
        if not match:
            continue
        version = _version(match.group(1).removeprefix("v"), field="Tart release tag")
        updated = (entry.findtext(f"{ATOM}updated") or "").strip()
        releases.append((_version_key(version), version, href, updated))
    if not releases:
        raise DiscoveryError("GitHub releases feed has no stable numeric Tart release")
    _, version, release_url, published = max(releases)
    checksums_url = (f"https://github.com/{TART_REPO}/releases/download/{version}/"
                     f"tart_{version}_checksums.txt")
    return version, release_url, published, checksums_url


def _checksum(text: str, filename: str) -> str:
    values = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*./") == filename:
            values.append(parts[0].lower())
    if len(values) != 1 or not SHA256.fullmatch(values[0]):
        raise DiscoveryError(f"checksums file does not contain one valid {filename} digest")
    return values[0]


def _formula(text: str, name: str) -> tuple[str, str, str]:
    version = re.search(r'^\s*version\s+"([^"]+)"', text, re.MULTILINE)
    url = re.search(r'^\s*url\s+"([^"]+)"', text, re.MULTILINE)
    sha = re.search(r'^\s*sha256\s+"([^"]+)"', text, re.MULTILINE)
    if not version or not url or not sha:
        raise DiscoveryError(f"{name} formula is missing version, url or sha256")
    return (_version(version.group(1), field=f"{name} formula"), url.group(1), sha.group(1).lower())


def discover(fetch: Fetch = fetch_url) -> Candidate:
    """Return a candidate only when GitHub release and tap bytes agree."""
    version, release_url, published, checksums_url = _release_metadata(fetch(TART_RELEASES_ATOM))
    checksums = _checksum(fetch(checksums_url), "tart.tar.gz")
    tart_version, tart_url, tart_sha = _formula(fetch(TART_FORMULA), "Tart")
    softnet_version, softnet_url, softnet_sha = _formula(fetch(SOFTNET_FORMULA), "Softnet")
    expected_asset_url = f"https://github.com/{TART_REPO}/releases/download/{version}/tart.tar.gz"
    if tart_version != version:
        raise DiscoveryError(f"Tart formula {tart_version} does not match release {version}")
    if tart_url != expected_asset_url:
        raise DiscoveryError(f"Tart formula URL does not point at release {version}: {tart_url}")
    if tart_sha != checksums:
        raise DiscoveryError("Tart formula sha256 does not match the release checksums asset")
    if not SHA256.fullmatch(softnet_sha):
        raise DiscoveryError("Softnet formula sha256 is not a lowercase SHA-256 digest")
    if not softnet_url.startswith("https://github.com/openai/softnet/releases/download/"):
        raise DiscoveryError(f"Softnet formula URL is not an OpenAI release: {softnet_url}")
    return Candidate(version, tart_sha, softnet_version, softnet_sha,
                     release_url, published, checksums_url)


def _replace_field(block: str, field: str, value: str, table: str) -> str:
    pattern = re.compile(rf"^(\s*{re.escape(field)}\s*=\s*)\"[^\"]*\"\s*$", re.MULTILINE)
    result, count = pattern.subn(rf'\1"{value}"', block)
    if count != 1:
        raise DiscoveryError(f"fleet/tart.toml [{table}] must contain exactly one {field}")
    return result


def render_pin(text: str, candidate: Candidate) -> str:
    """Update only version/checksum fields and retain the pin's comments/order."""
    tables = list(re.finditer(r"^\[(tart|softnet)\]\s*$", text, re.MULTILINE))
    if [match.group(1) for match in tables] != ["tart", "softnet"]:
        raise DiscoveryError("fleet/tart.toml must contain [tart] followed by [softnet]")
    replacements: list[tuple[int, int, str]] = []
    values = {
        "tart": (candidate.version, candidate.tart_sha256),
        "softnet": (candidate.softnet_version, candidate.softnet_sha256),
    }
    for index, match in enumerate(tables):
        end = tables[index + 1].start() if index + 1 < len(tables) else len(text)
        block = text[match.start():end]
        version, sha = values[match.group(1)]
        block = _replace_field(block, "version", version, match.group(1))
        block = _replace_field(block, "sha256", sha, match.group(1))
        replacements.append((match.start(), end, block))
    for start, end, block in reversed(replacements):
        text = text[:start] + block + text[end:]
    return text


def report(root: Path, candidate: Candidate) -> dict:
    path = root / tart_pin.PIN_FILE
    try:
        current = tart_pin.load_pin(root)
    except tart_pin.PinError as exc:
        raise DiscoveryError(str(exc)) from exc
    current_value = {
        "version": current.version,
        "tart_sha256": current.sha256,
        "softnet_version": current.softnet_version,
        "softnet_sha256": current.softnet_sha256,
    }
    candidate_value = {
        "version": candidate.version,
        "tart_sha256": candidate.tart_sha256,
        "softnet_version": candidate.softnet_version,
        "softnet_sha256": candidate.softnet_sha256,
    }
    return {
        "current": current_value,
        "candidate": candidate.as_json(),
        "changed": current_value != candidate_value,
        "pin_file": str(path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tart_pin_update")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="discover and report; default")
    mode.add_argument("--write", action="store_true", help="write fleet/tart.toml in --root")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--json", action="store_true", help="emit one machine-readable report")
    parser.add_argument("--fail-on-update", action="store_true", help="check mode exits 10 when a pin is newer")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        candidate = discover()
        value = report(root, candidate)
        if args.write and value["changed"]:
            path = root / tart_pin.PIN_FILE
            path.write_text(render_pin(path.read_text(), candidate))
            value["written"] = True
        else:
            value["written"] = False
    except (DiscoveryError, OSError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, sort_keys=True))
        else:
            print(f"tart pin update: ERROR: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        action = "updated" if value["written"] else ("update available" if value["changed"] else "current")
        print(f"tart pin: {action} ({value['current']['version']} -> {value['candidate']['version']}; "
              f"Softnet {value['candidate']['softnet_version']})")
        print(f"release: {value['candidate']['release_url']}")
    return 10 if args.fail_on_update and value["changed"] and not args.write else 0


if __name__ == "__main__":
    raise SystemExit(main())
