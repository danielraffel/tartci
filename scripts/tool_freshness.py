#!/usr/bin/env python3
"""Installed-vs-released freshness for the host tools a fleet host runs.

tartci measures its own skew against main (fleet_self_update.py). The other
tools every gate and agent session on the host depends on, Shipyard and the
pulp CLI, had no equivalent, and an installed pulp CLI sat ~2,800 hours behind
its releases before anyone looked. This module gives each of them one line in
tartci pool status, a finding in tartci doctor fleet, and a WARN in the
launchd watchdog, and optionally applies the update itself:

  refresh   (the watchdog, at most every 30 minutes) reads the installed
            version by running the tool, and the published releases from
            the repository's public Atom feed (no REST quota). A tool whose
            installed version is older than the newest release is `behind`
            since that newer release was published; it is STALE once that is
            more than `stale_hours` ago. When a tool has an `apply_command`
            and `auto_apply`, a behind tool is updated once its newest
            release is `apply_after_minutes` old, at most once per target per
            `apply_retry_hours`, and the result is re-read and verified.
            A tool with `release_assets` (Shipyard or pulp) is first checked for readiness:
            every asset listed on the GitHub release and HEAD 200. A release
            that is not ready, or an apply that exits NOT_READY_EXIT (75: a
            download failed, nothing installed), is "not ready yet": re-checked
            on the next refresh, never recorded as an attempt, and reported as
            a problem only after `not_ready_alert_hours` (6). The per-target
            guard covers only failures after the downloads succeeded.
  events    every change of an installed version, whoever made it, appends
            one `tool_deployed` event (tool, from, to, verify) to
            events.jsonl; an automatic apply that did not land appends
            `tool_apply_failed`; a release still not ready after the alert
            window appends one `tool_release_incomplete`.
  summary   reads only the cached state; it never runs a tool or fetches.

A tool with a `generation_link` (Shipyard's ghapp auth generation) is also
measured through that generation's own `shipyard --version`; the older of
the two decides whether the tool is behind, so a generation left on an old
release reads STALE even when the CLI is current.

Settings (optional) live in ~/.config/tartci/tool-freshness.toml:

    stale_hours = 12
    host_class = "studio"         # default: the installed fleet profile's host.id
    [tools.shipyard]
    auto_apply = false            # stop automatic Shipyard updates here
    [tools.pulp]
    auto_apply = false            # measure the pulp CLI but never install it here
    local_archive_dir = "{home}/pulp-archives"   # optional <tag>/pulp-<platform>.tar.gz,
                                  # used only when its sha256 matches the release

`{home}`, `{tag}` and `{host_class}` are substituted in commands. A tool whose binary is
absent is `not_installed`, which is reported but is never a problem.

A tool with a `pin_file` is measured against the version pinned in this
TartCI generation, not against the project's releases: the pin is what the
fleet is meant to run, and moving it is a reviewed commit. Tart is pinned in
fleet/tart.toml (scripts/tart_pin.py). A pin has no publish time; a host
behind it is timed from when it first saw it.

Tart is measured only. Replacing it while a VM runs is unsafe, so
tool-freshness never applies it; `fleet_self_update` moves it to the pin in
its drained window when self-update.toml opts in (`[tart] upgrade = true`,
off by default; the canary first). Tart behind its pin is information only,
never a problem, unless self-update's tart-gate.json says this host is due to
run that pin (opted in and past the canary gate).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the launchd python is 3.9
    tomllib = None  # type: ignore[assignment]

STALE_HOURS = 12.0
APPLY_AFTER_MINUTES = 30.0
APPLY_RETRY_HOURS = 6.0
REFRESH_INTERVAL_S = 1800
COMMAND_TIMEOUT_S = 30
APPLY_TIMEOUT_S = 300
FEED_TIMEOUT_S = 15

DEFAULT_TOOLS: dict[str, dict[str, Any]] = {
    "shipyard": {
        "repo": "danielraffel/Shipyard",
        "version_command": ["{home}/.local/bin/shipyard", "--version"],
        # ghapp runs from a separate content-addressed auth generation that
        # `shipyard update` never installs, so it can lag the CLI silently.
        "generation_link": "{home}/.local/bin/ghapp.shipyard-generation",
        # The governed rollout for this host's class: binds the release, stages
        # CLI, daemon, ghapp, token helper and close guard as one generation,
        # probes it, swaps atomically and rolls back on failure.
        "apply_command": ["{home}/.local/bin/shipyard", "runner", "fleet-update", "--to", "{tag}",
                          "--host-class", "{host_class}", "--apply", "--json"],
        "verdict_json": True,
        "auto_apply": True,
        # A tag exists while its release is still a DRAFT: the Release job
        # queues behind the single Shipyard macOS runner, and fleet-update
        # --to that tag 404s. Without a readiness check that 404 was a spent
        # attempt, so every intermediate tag was skipped for apply_retry_hours.
        # A draft's assets answer 404 to an anonymous HEAD, so it reads "not
        # ready yet" until it is published. The fleet is macOS arm64.
        "release_assets": ["shipyard-macos-arm64.dmg", "checksums.sha256"],
    },
    "pulp": {
        "repo": "Generous-Corp/pulp",
        "version_command": ["{home}/.pulp/bin/pulp", "version"],
        # The CLI's own session-start update runs only from a checkout that
        # carries the hook, and a host whose agents open stale checkouts never
        # gets it. So the watchdog installs the release with that release's
        # own install.sh, pinned to the tag, the same way the hook does.
        "apply_command": ["/bin/bash", "-c", "{pulp_install}", "pulp-install", "{tag}", "{home}",
                          "{platform}", "{local_archive}"],
        "auto_apply": True,
        # Every release asset the install needs; checked (listed on the release
        # and HEAD 200) before any host attempts it. SHA256SUMS is what the
        # install verifies the archive against.
        "release_assets": ["pulp-{platform}.tar.gz", "SHA256SUMS"],
        # Optional backup: a directory holding <tag>/pulp-<platform>.tar.gz, used
        # only when the release archive is not downloadable and only when its
        # sha256 matches the release's own SHA256SUMS.
        "local_archive_dir": None,
    },
    "tart": {
        "repo": "openai/tart",
        "version_command": ["/opt/homebrew/bin/tart", "--version"],
        # The fleet's pin, not the newest release or what the tap offers.
        "pin_file": "fleet/tart.toml",
        # Behind the pin is a problem only where self-update says this host
        # is due to run it ([tart] upgrade = true and past the canary gate);
        # everywhere else it is information.
        "gate_file": "self-update/tart-gate.json",
        # Never applied here: a VM may be running. fleet_self_update moves it
        # to the pin after `pool off`, when no VM can be.
        "auto_apply": False,
    },
}

# A pulp apply that exits NOT_READY_EXIT changed nothing because something it
# needed could not be downloaded (a 404 or a network error). That is "not
# ready yet", never a failed attempt: the per-target guard is kept for
# failures after every download succeeded (checksum, install, verify).
NOT_READY_EXIT = 75
# How long a behind release may stay incomplete (assets missing or not
# downloadable) before that is reported as a problem. Until then the host
# waits quietly and re-checks on every refresh.
NOT_READY_ALERT_HOURS = 6.0

# Installs one pulp release into ~/.pulp/bin, pinned to the tag:
#   $1 tag  $2 home  $3 platform (darwin-arm64)  $4 optional local archive
# It downloads the tag's install.sh and the release's SHA256SUMS, takes the
# archive from the release (or, when given, a local copy), and installs only
# an archive whose sha256 matches SHA256SUMS. Any download failure exits
# NOT_READY_EXIT before anything is installed. A checksum mismatch after a
# successful download exits 4. An installer that still excludes the WebGPU
# runtime (releases before the fix) would strand pulp-cpp, so it is refused.
PULP_INSTALL_SCRIPT = r"""set -euo pipefail
tag="$1"; home="$2"; platform="$3"; local_archive="${4:-}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
base="https://github.com/Generous-Corp/pulp/releases/download/$tag"
fetch(){
  if ! curl -fsSL --max-time "$3" "$1" -o "$2"; then
    echo "not ready: $1 could not be downloaded" >&2
    exit 75
  fi
}
fetch "https://raw.githubusercontent.com/Generous-Corp/pulp/$tag/tools/install/install.sh" \
  "$work/install.sh" 60
if grep -q -- "--exclude='libwgpu_native.dylib'" "$work/install.sh"; then
  echo "refused: the $tag installer strands pulp-cpp without its runtime" >&2
  exit 3
fi
fetch "$base/SHA256SUMS" "$work/SHA256SUMS" 60
want="$(awk -v f="pulp-$platform.tar.gz" '$2 == f || $2 == "*" f { print $1; exit }' "$work/SHA256SUMS")"
if [ -z "$want" ]; then
  echo "not ready: SHA256SUMS for $tag lists no pulp-$platform.tar.gz" >&2
  exit 75
fi
if [ -n "$local_archive" ]; then
  cp "$local_archive" "$work/pulp.tar.gz"
  source="local archive $local_archive"
else
  fetch "$base/pulp-$platform.tar.gz" "$work/pulp.tar.gz" 300
  source="$base/pulp-$platform.tar.gz"
fi
got="$(shasum -a 256 "$work/pulp.tar.gz" | awk '{ print $1 }')"
if [ "$got" != "$want" ]; then
  echo "refused: $source has sha256 $got, SHA256SUMS says $want" >&2
  exit 4
fi
PULP_INSTALL_ARCHIVE="$work/pulp.tar.gz" PULP_VERSION="${tag#v}" \
  PULP_INSTALL_DIR="$home/.pulp/bin" PULP_NO_MODIFY_PATH=1 PULP_SKIP_SDK_INSTALL=1 \
  bash "$work/install.sh"
"""

ATOM = "{http://www.w3.org/2005/Atom}"
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
TAG_RE = re.compile(r"/releases/tag/(v?\d+\.\d+\.\d+)$")


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return dt.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        return None


def _version(text: str | None) -> tuple[int, int, int] | None:
    match = VERSION_RE.search(text or "")
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def _vstr(version: tuple[int, int, int] | None) -> str | None:
    return ".".join(str(part) for part in version) if version else None


def state_dir_for(home: Path) -> Path:
    root = os.environ.get("TARTCI_HOME") or str(home / ".tartci")
    return Path(root) / "state" / "tool-freshness"


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _append_event(state: Path, event: dict) -> None:
    state.mkdir(parents=True, exist_ok=True)
    with (state / "events.jsonl").open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def load_settings(home: Path, path: Path | None = None) -> dict[str, Any]:
    """Defaults merged with ~/.config/tartci/tool-freshness.toml when present."""
    settings: dict[str, Any] = {
        "stale_hours": STALE_HOURS, "apply_after_minutes": APPLY_AFTER_MINUTES,
        "apply_retry_hours": APPLY_RETRY_HOURS,
        "tools": {name: dict(tool, enabled=True) for name, tool in DEFAULT_TOOLS.items()},
    }
    path = path or home / ".config" / "tartci" / "tool-freshness.toml"
    if path.is_file() and tomllib is not None:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        for key in ("stale_hours", "apply_after_minutes", "apply_retry_hours",
                    "not_ready_alert_hours"):
            if key in data:
                settings[key] = float(data[key])
        if isinstance(data.get("host_class"), str):
            settings["host_class"] = data["host_class"]
        for name, override in (data.get("tools") or {}).items():
            settings["tools"].setdefault(name, {"enabled": True, "apply_command": None,
                                                "auto_apply": False})
            settings["tools"][name].update(override)
    return settings


def _expand(argv: list[str] | None, home: Path, tag: str = "",
            host_class: str = "", platform: str = "", local_archive: str = "") -> list[str] | None:
    if not argv:
        return None
    return [PULP_INSTALL_SCRIPT if part == "{pulp_install}" else
            part.replace("{home}", str(home)).replace("{tag}", tag)
            .replace("{platform}", platform).replace("{local_archive}", local_archive)
            .replace("{host_class}", host_class) for part in argv]


def host_class(home: Path, settings: dict) -> str | None:
    """The fleet-update host class: settings, else the fleet profile's host.id."""
    if settings.get("host_class"):
        return str(settings["host_class"])
    profile = home / ".config" / "tartci" / "macos-fleet-profile.toml"
    if tomllib is None or not profile.is_file():
        return None
    try:
        with profile.open("rb") as handle:
            host = tomllib.load(handle).get("host") or {}
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return host.get("id") if isinstance(host.get("id"), str) else None


def json_documents(text: str) -> list[dict]:
    """Every JSON object in a stream of (possibly pretty-printed) documents."""
    decoder, found, index = json.JSONDecoder(), [], 0
    while True:
        index = text.find("{", index)
        if index < 0:
            return found
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(value, dict):
            found.append(value)
        index = end


def read_versions(tool: dict, home: Path,
                  run: Callable[[list[str], float], tuple[int, str]]) -> dict:
    """Installed CLI version and, when the tool has one, its generation's version."""
    argv = _expand(tool.get("version_command"), home)
    rc, out = run(argv, COMMAND_TIMEOUT_S) if argv else (127, "no version_command")
    value: dict[str, Any] = {"rc": rc, "out": out,
                             "installed": _version(out) if rc == 0 else None,
                             "generation": None, "generation_error": None}
    link = _expand([tool["generation_link"]], home)[0] if tool.get("generation_link") else None
    if link:
        try:
            target = Path(os.readlink(link))
        except FileNotFoundError:
            return value  # this host has no auth generation: nothing to lag
        except OSError as exc:
            value["generation_error"] = f"{link} unreadable: {exc}"
            return value
        if not target.is_absolute():
            target = Path(link).parent / target
        grc, gout = run([str(target.parent / "shipyard"), "--version"], COMMAND_TIMEOUT_S)
        value["generation"] = _version(gout) if grc == 0 else None
        value["generation_dir"] = str(target.parent)
        if value["generation"] is None:
            value["generation_error"] = (f"generation {target.parent.name[:12]} version "
                                         f"unreadable (exit {grc}): {gout[:120]}")
    return value


# The generation this code runs from; its fleet/ holds the pins.
SUPPORT_ROOT = Path(__file__).resolve().parent.parent


def pin_releases(tool: dict, now: float, previous: dict | None
                 ) -> list[tuple[tuple[int, int, int], str, float]]:
    """The pinned version as the one release, timed from when this host first
    saw it (a pin has no publish time). Raises ValueError."""
    import tart_pin  # noqa: PLC0415 - sibling module; parses pin files without tomllib
    path = SUPPORT_ROOT / str(tool["pin_file"])
    data = tart_pin.parse_pin(path.read_text())
    name = str(tool.get("pin_table") or Path(str(tool["version_command"][0])).name)
    version = _version(str((data.get(name) or {}).get("version", "")))
    if version is None:
        raise ValueError(f"{path}: no [{name}] version")
    first_seen = now
    if previous and previous.get("latest") == _vstr(version):
        first_seen = _epoch(previous.get("latest_published_at")) or now
    return [(version, _vstr(version) or "", first_seen)]


def run_command(argv: list[str], timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "not installed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"{type(exc).__name__}: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def fetch_feed(repo: str) -> str:
    request = urllib.request.Request(f"https://github.com/{repo}/releases.atom",
                                     headers={"User-Agent": "tartci-tool-freshness"})
    with urllib.request.urlopen(request, timeout=FEED_TIMEOUT_S) as response:  # noqa: S310
        return response.read().decode("utf-8", "replace")


def parse_releases(feed: str) -> list[tuple[tuple[int, int, int], str, float]]:
    """(version, tag, published epoch) for every plain vX.Y.Z release in a feed.

    A repository that publishes more than one release family (the pulp repo
    also tags plugin-vX.Y.Z) keeps only the plain one.
    """
    releases = []
    for entry in ET.fromstring(feed).iter(f"{ATOM}entry"):
        link = entry.find(f"{ATOM}link")
        match = TAG_RE.search(link.get("href", "") if link is not None else "")
        updated = _epoch((entry.findtext(f"{ATOM}updated") or "").strip())
        version = _version(match.group(1)) if match else None
        if version and updated is not None:
            releases.append((version, match.group(1), updated))  # type: ignore[union-attr]
    return sorted(releases)


def published_releases(tool: dict, releases: list[tuple[tuple[int, int, int], str, float]],
                       probe: Callable[[str, str, list[str]], dict] | None = None
                       ) -> list[tuple[tuple[int, int, int], str, float]]:
    """Keep only releases whose required assets are publicly downloadable.

    GitHub's public Atom feed includes tags while their Release objects are
    still drafts (and can include prereleases).  A draft has no downloadable
    release assets, so treating the feed's newest entry as published makes a
    host report false skew against a release it cannot install.  Asset HEADs
    are the same quota-free readiness check used before applying Shipyard
    updates and are authoritative for the fleet's release channel.

    Tools without a release-asset contract keep the Atom-feed behavior; their
    release feeds do not provide an equivalent portable publication signal.
    """
    assets = tool.get("release_assets")
    if not assets:
        return releases
    if probe is None:
        probe = probe_release
    expanded = [str(asset).replace("{platform}", host_platform()) for asset in assets]
    published = []
    for version, tag, published_at in releases:
        try:
            ready = probe(str(tool["repo"]), tag, expanded)
        except Exception:  # noqa: BLE001 - an unreadable candidate is not published
            continue
        if ready.get("ready") is True:
            published.append((version, tag, published_at))
    return published


def measure(name: str, tool: dict, home: Path, now: float, previous: dict | None,
            run: Callable[[list[str], float], tuple[int, str]],
            feed: Callable[[str], str], stale_hours: float,
            release_probe: Callable[[dict, list[tuple[tuple[int, int, int], str, float]]],
                                    list[tuple[tuple[int, int, int], str, float]]] | None = None
            ) -> dict:
    row: dict[str, Any] = {"tool": name, "repo": tool.get("repo"), "measured_at": _iso(now)}
    versions = read_versions(tool, home, run)
    rc, out, installed = versions["rc"], versions["out"], versions["installed"]
    row["installed"] = _vstr(installed)
    if versions.get("generation_dir"):
        row["generation"] = _vstr(versions["generation"])
        row["generation_dir"] = versions["generation_dir"]
    if rc == 127:
        row.update(state="not_installed", reason=out)
        return row
    if installed is None:
        row.update(state="unknown", reason=f"version unreadable (exit {rc}): {out[:200]}")
        return row
    if versions["generation_error"]:
        row.update(state="unknown", reason=versions["generation_error"])
        return row
    # The older of the CLI and its auth generation is what the host really runs.
    installed = min(installed, versions["generation"] or installed)
    row["effective"] = _vstr(installed)
    try:
        releases = (pin_releases(tool, now, previous) if tool.get("pin_file")
                    else parse_releases(feed(str(tool["repo"]))))
        if release_probe is not None and not tool.get("pin_file"):
            releases = release_probe(tool, releases)
    except Exception as exc:  # noqa: BLE001 - an unreadable feed is unknown, never current
        row.update(state="unknown", reason=f"releases unreadable: {type(exc).__name__}: {exc}")
        return row
    if not releases:
        row.update(state="unknown", reason="no vX.Y.Z release in the feed")
        return row
    latest, latest_tag, latest_at = releases[-1]
    row.update(latest=_vstr(latest), latest_tag=latest_tag, latest_published_at=_iso(latest_at))
    newer = [release for release in releases if release[0] > installed]
    if not newer:
        row["state"] = "current"
        return row
    behind_since = newer[0][2]
    # The feed holds only the newest releases, so an install older than all of
    # them is behind since at least the oldest one listed. Keep the earliest
    # time recorded for this installed version, so the bound never moves later.
    lower_bound = len(newer) == len(releases)
    if previous and (previous.get("effective") or previous.get("installed")) == row["effective"]:
        earlier = _epoch(previous.get("behind_since"))
        if earlier is not None and earlier < behind_since:
            behind_since = earlier
            lower_bound = bool(previous.get("behind_since_lower_bound"))
    hours = max(0.0, (now - behind_since) / 3600)
    row.update(state="behind", releases_behind=len(newer), behind_since=_iso(behind_since),
               behind_since_lower_bound=lower_bound, behind_hours=round(hours, 1),
               stale=hours > stale_hours,
               # Every release this host could move to, oldest first: the apply
               # takes the newest one that has finished its soak.
               candidates=[{"version": _vstr(version), "tag": tag, "published_at": _iso(at)}
                           for version, tag, at in newer])
    if tool.get("gate_file"):
        gate = _read_json(state_dir_for(home).parent / str(tool["gate_file"]))
        if not (isinstance(gate, dict) and gate.get("upgrade") is True and gate.get("due") is True
                and gate.get("pin") == _vstr(latest)):
            row["stale"] = False
            row["info_only"] = "not due on this host (opt-in off, or waiting for the canary)"
    return row


def soaked_candidate(row: dict, now: float, soak_minutes: float) -> tuple[dict | None, str]:
    """(newest release that has soaked, why none has).

    Keying the soak on the NEWEST release starved a host whenever releases came
    faster than the soak: each one restarted the wait before the previous one
    could apply (Shipyard v0.232.0 was held back by v0.233.0 being under 30
    minutes old). Applying the newest SOAKED release moves the host forward,
    and the next pass takes the newer one once it has soaked too.
    """
    candidates = row.get("candidates") or [{"version": row.get("latest"),
                                            "tag": row.get("latest_tag"),
                                            "published_at": row.get("latest_published_at")}]
    soaked = [c for c in candidates
              if now - (_epoch(c.get("published_at")) or now) >= soak_minutes * 60]
    if soaked:
        return soaked[-1], ""
    return None, f"waiting: {candidates[0]['tag']} is younger than {soak_minutes:g} min"


def host_platform() -> str:
    """The release platform suffix this host installs (pulp-<platform>.tar.gz)."""
    arch = os.uname().machine
    os_name = "darwin" if sys.platform == "darwin" else "linux"
    return f"{os_name}-{'arm64' if arch in ('arm64', 'aarch64') else 'x64'}"


def _http(url: str, method: str = "GET", timeout: float = FEED_TIMEOUT_S) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": "tartci-tool-freshness"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read() if method == "GET" else b""
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (OSError, ValueError) as exc:
        return 0, str(exc).encode()


def probe_release(repo: str, tag: str, assets: list[str],
                  http: Callable[..., tuple[int, bytes]] = _http) -> dict:
    """Whether every asset the install needs is downloadable from the release.

    Each asset's release download URL must answer HEAD 200: that URL exists
    only for an asset uploaded to a published release, so it answers both
    "listed" and "downloadable" without the REST API (whose unauthenticated
    quota a fleet behind one address shares). SHA256SUMS is fetched as well,
    for the checksum. {"ready", "missing", "sums", "detail"}; every failure is
    "not ready", never an error.
    """
    base = f"https://github.com/{repo}/releases/download/{tag}"
    missing, codes = [], {}
    for name in assets:
        code, _ = http(f"{base}/{name}", "HEAD")
        codes[name] = code
        if code != 200:
            missing.append(name)
    sums = None
    if "SHA256SUMS" in assets and "SHA256SUMS" not in missing:
        code, text = http(f"{base}/SHA256SUMS")
        sums = text.decode("utf-8", "replace") if code == 200 else None
    def why(name: str) -> str:
        code = codes[name]
        return "missing" if code == 404 else f"unreachable (HTTP {code or 'none'})"

    detail = "ready" if not missing else "; ".join(
        f"asset {name} {why(name)}" for name in missing)
    return {"ready": not missing, "missing": missing, "sums": sums, "detail": detail}


def published_sha256(sums: str | None, name: str) -> str | None:
    for line in (sums or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    return None


def verified_local_archive(tool: dict, tag: str, platform: str, sums: str | None,
                           home: Path) -> tuple[str | None, str | None]:
    """(path, None) for a local archive whose sha256 matches the release, else (None, why)."""
    directory = tool.get("local_archive_dir")
    if not directory:
        return None, None
    name = f"pulp-{platform}.tar.gz"
    path = Path(str(directory).replace("{home}", str(home))).expanduser() / tag / name
    if not path.is_file():
        return None, None
    want = published_sha256(sums, name)
    if want is None:
        return None, f"local {path} not used: the release publishes no checksum for {name} yet"
    got = hashlib.sha256(path.read_bytes()).hexdigest()
    if got != want:
        return None, f"local {path} refused: sha256 {got[:12]} != release {want[:12]}"
    return str(path), None


def _not_ready_before_install(record: dict) -> bool:
    """A recorded attempt that never got past its downloads.

    Records written before not-ready handling existed carry the download
    failure only in their text (v0.884.0 on m1, m3 and m5: "could not download
    ... returned error: 404"); they must not keep holding the host back.
    """
    if record.get("not_ready"):
        return True
    text = str(record.get("result") or "")
    # Exit 75 is NOT_READY_EXIT for every tool: for pulp a download that failed,
    # for Shipyard's fleet-update another rollout holding the controller lock
    # (m3's v0.231.0 record). Neither installed anything.
    return text.startswith("FAILED") and any(marker in text for marker in (
        "could not download", "returned error: 404", "could not be downloaded",
        f"exit {NOT_READY_EXIT},", f"exit {NOT_READY_EXIT})"))


def _wait_not_ready(name: str, tag: str, detail: str, row: dict, now: float, state: Path,
                    settings: dict, pending: dict) -> None:
    """Record a release that is not ready yet; loud only past the alert window."""
    entry = pending.get(name) if isinstance(pending.get(name), dict) else {}
    if entry.get("target") != tag:
        entry = {"target": tag, "since": _iso(now)}
    entry.update(checked_at=_iso(now), detail=detail)
    hours = (now - (_epoch(entry["since"]) or now)) / 3600
    window = float(settings.get("not_ready_alert_hours", NOT_READY_ALERT_HOURS))
    row["apply"] = f"{name} {tag} not ready yet ({detail}), waiting"
    row["not_ready"] = {"target": tag, "since": entry["since"], "hours": round(hours, 1)}
    if hours >= window:
        row["release_incomplete"] = (f"{name} {tag} still not ready after {hours:.1f} h "
                                     f"(> {window:g} h): {detail}")
        if not entry.get("alerted"):
            entry["alerted"] = _iso(now)
            _append_event(state, {"event": "tool_release_incomplete", "tool": name,
                                  "at": _iso(now), "target": tag, "since": entry["since"],
                                  "hours": round(hours, 1), "detail": detail})
    pending[name] = entry
    return None


def maybe_apply(name: str, tool: dict, row: dict, home: Path, now: float, state: Path,
                settings: dict, attempts: dict,
                run: Callable[[list[str], float], tuple[int, str]],
                pending: dict | None = None, probe: dict | None = None) -> dict | None:
    """Apply a behind tool's update when allowed; return the re-measured version.

    `pending` holds releases that are not ready yet (never attempts); `probe`
    replaces the release check in tests ({"platform", "result"}).
    """
    pending = {} if pending is None else pending
    if row.get("state") != "behind" or not tool.get("auto_apply") or not tool.get("apply_command"):
        return None
    candidate, waiting = soaked_candidate(row, now, float(settings["apply_after_minutes"]))
    if candidate is None:
        row["apply"] = waiting
        return None
    tag, target_version = str(candidate["tag"]), candidate["version"]
    last = attempts.get(name) or {}
    if last.get("target") == tag and not _not_ready_before_install(last) and \
            now - (_epoch(last.get("at")) or 0) < float(settings["apply_retry_hours"]) * 3600:
        row["apply"] = f"already attempted {tag} at {last.get('at')}: {last.get('result')}"
        return None
    platform = host_platform() if probe is None else probe.get("platform", host_platform())
    local = ""
    if tool.get("release_assets"):
        assets = [a.replace("{platform}", platform) for a in tool["release_assets"]]
        ready = (probe or {}).get("result") or probe_release(str(tool["repo"]), tag, assets)
        if not ready["ready"]:
            local, refusal = verified_local_archive(tool, tag, platform, ready.get("sums"), home)
            if refusal:
                row["local_archive"] = refusal
            if not local:
                return _wait_not_ready(name, tag, ready["detail"], row, now, state, settings, pending)
    pending.pop(name, None)
    klass = host_class(home, settings) or ""
    if "{host_class}" in " ".join(tool["apply_command"]) and not klass:
        row["apply"] = ("refused: no host class (set host_class in tool-freshness.toml "
                        "or install a fleet profile)")
        return None
    argv = _expand(tool["apply_command"], home, tag, klass, platform, local or "")
    rc, out = run(argv, APPLY_TIMEOUT_S)  # type: ignore[arg-type]
    verdict = None
    if tool.get("verdict_json"):
        summaries = [doc for doc in json_documents(out) if doc.get("event") == "fleet_summary"]
        verdict = summaries[-1].get("verdict") if summaries else "no fleet_summary"
    if rc == NOT_READY_EXIT:
        # A download failed before anything was installed (a 404 or a network
        # error): not ready yet, never a spent attempt.
        return _wait_not_ready(name, tag, out.strip()[-200:] or "download failed", row, now,
                               state, settings, pending)
    after = read_versions(tool, home, run)
    effective = min(v for v in (after["installed"], after["generation"] or after["installed"]) if v) \
        if after["installed"] else None
    landed = _vstr(effective) == target_version and verdict in (None, "verified")
    detail = f"verdict {verdict}, " if verdict is not None else ""
    result = "ok" if landed else (f"FAILED ({detail}exit {rc}, installed {_vstr(after['installed'])}"
                                  f", generation {_vstr(after['generation'])}): {out[-300:]}")
    attempts[name] = {"target": tag, "at": _iso(now), "result": result}
    row["apply"] = f"applied {tag}: {result}" if landed else f"applied {tag}: {result[:200]}"
    if not landed:
        _append_event(state, {"event": "tool_apply_failed", "tool": name, "at": _iso(now),
                              "from": row["effective"], "target": target_version,
                              "installed_after": _vstr(after["installed"]),
                              "generation_after": _vstr(after["generation"]),
                              "verdict": verdict, "exit": rc, "detail": out[-300:]})
    return {"installed": _vstr(after["installed"])}


def refresh(home: Path, now: float | None = None, *, if_older: int = 0,
            settings: dict | None = None,
            run: Callable[[list[str], float], tuple[int, str]] = run_command,
            feed: Callable[[str], str] = fetch_feed,
            probe: Callable[[str, str], dict] | None = None) -> dict:
    """Measure every enabled tool, apply where allowed, record deployments."""
    now = time.time() if now is None else now
    settings = settings or load_settings(home)
    state = state_dir_for(home)
    cached = _read_json(state / "state.json") or {}
    if if_older and cached.get("measured_at"):
        age = now - (_epoch(cached["measured_at"]) or 0)
        if age < if_older:
            return cached
    previous = cached.get("tools") or {}
    attempts = _read_json(state / "attempts.json") or {}
    pending = _read_json(state / "pending.json") or {}
    tools: dict[str, dict] = {}
    for name, tool in settings["tools"].items():
        if not tool.get("enabled", True):
            continue
        prior = previous.get(name) if isinstance(previous.get(name), dict) else None
        # An injected probe supplies hermetic release-readiness results for
        # tests; production refreshes use the quota-free asset publication
        # filter above before selecting the newest release.
        publication_filter = None if probe is not None else published_releases
        row = measure(name, tool, home, now, prior, run, feed, float(settings["stale_hours"]),
                      publication_filter)
        before = {key: prior.get(key) for key in ("installed", "generation")} if prior else {}
        applied = maybe_apply(name, tool, row, home, now, state, settings, attempts, run,
                              pending, probe(name, str(row.get("latest_tag"))) if probe else None)
        if applied and applied.get("installed"):
            before = {key: row.get(key) for key in ("installed", "generation")}
            note = row.get("apply")
            row = measure(name, tool, home, now, None, run, feed, float(settings["stale_hours"]),
                          publication_filter)
            row["apply"] = note
        for key, component in (("installed", "cli"), ("generation", "auth_generation")):
            old, new = before.get(key), row.get(key)
            if old and new and old != new:
                _append_event(state, {
                    "event": "tool_deployed", "tool": name, "component": component,
                    "at": _iso(now), "from": old, "to": new, "latest": row.get("latest"),
                    "by": "auto_apply" if applied else "observed",
                    "downgrade": _version(new) < _version(old),  # type: ignore[operator]
                    "verify": "current" if row.get("state") == "current"
                    else str(row.get("state"))})
        tools[name] = row
    value = {"measured_at": _iso(now), "stale_hours": settings["stale_hours"], "tools": tools}
    _write_json(state / "state.json", value)
    _write_json(state / "attempts.json", attempts)
    _write_json(state / "pending.json", pending)
    return value


def render_row(row: dict) -> str:
    name, state = row.get("tool"), row.get("state")
    installed = row.get("installed")
    if row.get("generation_dir"):
        installed = f"{installed} (ghapp generation {row.get('generation') or 'unreadable'})"
    if state == "current":
        text = f"{name}: {installed} current with latest {row['latest_tag']}"
    elif state == "behind":
        bound = ">=" if row.get("behind_since_lower_bound") else ""
        text = (f"{name}: {installed} behind latest {row['latest_tag']} by "
                f"{row['releases_behind']} release(s) for {bound}{row['behind_hours']:g} h "
                f"(since {row['behind_since']})" + (" STALE" if row.get("stale") else "")
                + (f"; information only: {row['info_only']}" if row.get("info_only") else ""))
    elif state == "not_installed":
        text = f"{name}: not installed"
    else:
        text = f"{name}: freshness UNKNOWN ({row.get('reason')})"
    if row.get("apply"):
        text += f" [{row['apply']}]"
    return text + f" (measured {row.get('measured_at')})"


def summary(home: Path | None = None) -> dict:
    """Cached freshness for status surfaces. Never runs a tool or fetches."""
    value = _read_json(state_dir_for(home or Path.home()) / "state.json")
    if value is None:
        return {"state": None, "lines": ["tools: freshness UNKNOWN (never measured; run "
                                         "tartci fleet-macos tool-freshness --refresh)"],
                "problem": None}
    rows = [row for row in (value.get("tools") or {}).values() if isinstance(row, dict)]
    import state_age
    aged = state_age.stale_note(value.get("measured_at"), REFRESH_INTERVAL_S,
                                "launchd watchdog (tartci launchd heal)")
    problems = [f"tool freshness {aged}"] if aged else []
    problems += [f"{row['tool']} {row['behind_hours']:g} h behind {row['latest_tag']}"
                 for row in rows if row.get("stale")]
    problems += [f"{row['tool']} freshness unknown" for row in rows
                 if row.get("state") == "unknown"]
    problems += [row["release_incomplete"] for row in rows if row.get("release_incomplete")]
    lines = [render_row(row) for row in rows]
    if aged:
        lines.append(f"tools: freshness {aged}")
    return {"state": value, "lines": lines, "problem": "; ".join(problems) or None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci fleet-macos tool-freshness")
    parser.add_argument("--refresh", action="store_true",
                        help="measure now (and apply where auto_apply allows)")
    parser.add_argument("--if-older", type=int, default=0,
                        help="with --refresh: skip unless the cache is older (seconds)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--home", default=str(Path.home()))
    args = parser.parse_args(argv)
    home = Path(args.home)
    if args.refresh:
        refresh(home, if_older=args.if_older)
    value = summary(home)
    if args.json:
        print(json.dumps(value, indent=2, sort_keys=True))
    else:
        print("\n".join(value["lines"]))
    return 1 if value["problem"] else 0


if __name__ == "__main__":
    sys.exit(main())
