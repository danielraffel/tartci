#!/usr/bin/env python3
"""Write this host's runner attestation: the artifact that replaces a
delegation checkmark with evidence.

Until now the two halves of the fleet each passed their own check by pointing
at the other. This watchdog printed

    ✓ actions.runner.<...>.pulp-preamble-m5: declared runner executable exists;
      runtime health is owned by Shipyard

over a service in a ``spawn scheduled`` crash loop with 3,684 launches and no
``.runner`` registration file, while Shipyard knew about one configured runner
id and nothing about this host. The checkmark was true as written. The lane was
dead, and every pull request in the repository sat unmergeable for six hours.

The rule this implements: **no side may pass a check by delegation unless it
names the artifact carrying the other side's verdict, and absence of that
artifact is a fault, not a pass.** This script is that artifact's writer.
Shipyard's landability preflight is its reader; with no fresh file it reports
``Unknown`` and says so, rather than either refusing every ship or — far worse
— quietly passing.

What it answers, which GitHub cannot
------------------------------------
One question: *does this machine declare and supervise a given runner label
set?* An empty GitHub runner census means "nothing is registered right now",
which for a just-in-time pool between jobs is normal and for a persistent
runner that crash-looped away is fatal. The census cannot tell those apart.
This host can.

Five facts, not one
-------------------
"Is the runner up" is really five separate facts, and the 2026-09-13 incident
passed the first four:

  declared (profile) -> installed (plist + executable) -> loaded (launchctl)
  -> alive (state, runs) -> **registered** (``.runner`` exists, parses, and its
  ``gitHubUrl`` names the repository the lane actually serves)

``.runner`` is parsed as ``utf-8-sig``: GitHub writes that file with a UTF-8
BOM, and a plain ``utf-8`` read raises, which a naive reader turns into
"unregistered" on a perfectly healthy runner.

Why ``launchctl print`` and never ``launchctl list``
----------------------------------------------------
``launchctl list`` renders a ``KeepAlive`` job in a crash loop as ``- 0`` —
byte-identical to a healthy loaded-but-idle service. The M5 preamble runner had
respawned 3,684 times and read as healthy in every ``list``-based check. Only
``launchctl print`` exposes ``state = spawn scheduled`` and the ``runs``
counter that makes a crash loop visible.

Self-reporting
--------------
The script records its own run: ``--self-check`` asserts that a launchd label
known to exist reads as present *and* a label known not to exist reads as
absent, on the same instrument and the same domain. If both come back the same
way the launchd domain is unreadable (over SSH the GUI domain can be invisible,
and an empty answer is then a scope error, not a dead host), and the file
records ``launchd_readable: false`` instead of a census of zero. A sensor that
cannot report its own failure is the failure mode this whole exercise exists to
fix.

Run: ``python3 scripts/tartci_host_attestation.py --write``
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

SCHEMA = 1
WRITER = "tartci_host_attestation.py@1"

DEFAULT_INTERVAL_SECS = 300
#: A supervisor heartbeat older than this is not evidence of supervision.
DEFAULT_HEARTBEAT_STALE_SECS = 300
#: Launch rate above which a KeepAlive service is called a crash loop. A
#: healthy persistent runner launches once and stays up for days, so any
#: sustained rate above this is respawning, not running.
CRASH_LOOP_RUNS_PER_HOUR = 12
#: Absolute launch count treated as pathological when no prior sample exists to
#: compute a rate from. Deliberately generous: a runner legitimately restarted
#: once a day for three months is under it, and the M5 preamble service was at
#: 3,684 and climbing.
CRASH_LOOP_RUNS_ABSOLUTE = 500

ACTIONS_RUNNER_PREFIX = "actions.runner."
SENSOR_PREFIXES = (
    "com.danielraffel.tartci.",
    "com.danielraffel.pulp.",
    "com.danielraffel.shipyard.",
    "com.danielraffel.network.",
    ACTIONS_RUNNER_PREFIX,
)

#: ``actions.runner.<owner>-<repo>.<name>`` — the slug half is what went stale
#: across the 2026-07-19 organisation move and nothing noticed.
RUNNER_LABEL_RE = re.compile(r"^actions\.runner\.(?P<slug>[A-Za-z0-9_.-]+)\.(?P<name>.+)$")


def utcnow() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(cmd: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host dependent
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _domain() -> str:
    return f"gui/{os.getuid()}"


def parse_launchctl_print(out: str) -> dict:
    """Pull ``state``, ``runs``, ``last exit code`` and ``pid`` out of
    ``launchctl print``.

    Returns an empty dict when the text carries none of them, which the caller
    must treat as "did not measure" rather than "measured zero".
    """
    found: dict = {}
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("state = "):
            found["state"] = stripped[len("state = "):].strip()
        elif stripped.startswith("runs = "):
            try:
                found["runs"] = int(stripped[len("runs = "):].strip())
            except ValueError:
                pass
        elif stripped.startswith("pid = "):
            try:
                found["pid"] = int(stripped[len("pid = "):].strip())
            except ValueError:
                pass
        elif stripped.startswith("last exit code = "):
            value = stripped[len("last exit code = "):].strip()
            found["last_exit_code"] = None if value in {"-", "(never exited)"} else value
    return found


def read_runner_registration(runner_dir: str) -> tuple[bool, str | None, list[str], str]:
    """Read ``<runner_dir>/.runner``.

    GitHub writes this file with a UTF-8 BOM. Reading it as plain ``utf-8``
    raises, and a reader that catches that exception broadly reports a healthy
    runner as unregistered — so the encoding is ``utf-8-sig`` deliberately, and
    that is the single most load-bearing character in this function.
    """
    path = os.path.join(runner_dir, ".runner")
    if not os.path.isfile(path):
        return False, None, [], f".runner absent at {path}"
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return False, None, [], f".runner unreadable: {exc}"
    url = data.get("gitHubUrl") or data.get("serverUrl") or ""
    slug = None
    if url:
        slug = "/".join(url.rstrip("/").split("/")[-2:]) or None
    labels: list[str] = []
    return True, slug, labels, f"registered against {slug or url or 'unknown'}"


def label_slug(label: str) -> str | None:
    match = RUNNER_LABEL_RE.match(label)
    return match.group("slug") if match else None


def slug_matches_repo(slug: str | None, repo: str | None) -> bool:
    """Whether ``slug`` names ``repo``, in either of the two spellings.

    Two different forms reach this function and they are easy to confuse: the
    launchd label carries the flattened ``Generous-Corp-pulp`` while ``.runner``
    carries ``Generous-Corp/pulp``. Normalising only one side made every
    healthy runner read as ``stale_repo`` — caught by the control test, which
    is the whole reason that test exists. So both sides are flattened, and the
    owner's own ``-`` is harmless because the comparison is on the whole
    string rather than on a split.
    """
    if not slug or not repo:
        return False
    return slug.replace("/", "-").lower() == repo.replace("/", "-").lower()


def discover_launch_agents(agents_dir: str) -> list[tuple[str, str]]:
    """Every relevant LaunchAgent on disk, as ``(label, plist path)``."""
    found: list[tuple[str, str]] = []
    if not os.path.isdir(agents_dir):
        return found
    for name in sorted(os.listdir(agents_dir)):
        if not name.endswith(".plist"):
            continue
        path = os.path.join(agents_dir, name)
        try:
            with open(path, "rb") as handle:
                data = plistlib.load(handle)
        except Exception:  # noqa: BLE001 - a malformed plist is not a crash
            continue
        label = data.get("Label")
        if isinstance(label, str) and label.startswith(SENSOR_PREFIXES):
            found.append((label, path))
    return found


def launchd_self_check(known_present: str | None) -> tuple[bool, str]:
    """Prove the launchd domain is readable before believing any absence.

    Two probes on the same instrument: a label expected to exist, and one that
    cannot. If the absent label reads present, or the present label reads
    absent, the domain answer is not trustworthy and every "not loaded" finding
    below would be a scope error dressed as a fault.
    """
    absent_rc, _, _ = _run(
        ["launchctl", "print", f"{_domain()}/com.danielraffel.tartci.control-never-installed"]
    )
    if absent_rc == 0:
        return False, "control label that cannot exist reported present; launchd answer is not trustworthy"
    if not known_present:
        return False, (
            "no known-loaded label to probe with, so an empty census cannot be told apart "
            "from an unreadable domain"
        )
    present_rc, out, _ = _run(["launchctl", "print", f"{_domain()}/{known_present}"])
    if present_rc != 0:
        return False, (
            f"known-loaded label {known_present} read as absent (rc={present_rc}); over SSH the "
            "GUI domain can be invisible and an empty answer is then a scope error"
        )
    if not parse_launchctl_print(out):
        return False, f"launchctl print {known_present} returned no parseable state"
    return True, f"domain readable: {known_present} present, control label absent"


def launch_rate_per_hour(label: str, runs: int, prior: dict, now: float) -> float | None:
    """Launches per hour since the previous attestation, if there was one.

    A rate is the honest measure of a crash loop; an absolute count is not,
    because a runner restarted deliberately once a week for a year is not
    sick. The previous attestation is the sample this compares against, which
    is also why this writer reads its own last output before overwriting it.
    """
    previous = prior.get(label)
    if not previous:
        return None
    prior_runs, prior_when = previous
    elapsed = now - prior_when
    if elapsed <= 0 or runs < prior_runs:
        return None
    return (runs - prior_runs) * 3600.0 / elapsed


def assess_persistent_runner(label: str, plist_path: str | None, lane_repo: str | None,
                             advertises: list[str], interval_secs: int,
                             prior: dict | None = None, now: float | None = None) -> dict:
    """Classify one persistent Actions runner across all five facts."""
    prior = prior or {}
    now = now if now is not None else utcnow()
    record = {
        "label": label,
        "declared": True,
        "installed": bool(plist_path and os.path.isfile(plist_path)),
        "loaded": False,
        "state": "",
        "runs": 0,
        "crash_loop": False,
        "registered": False,
        "registration_repo": None,
        "advertises": advertises,
        # Never let an empty label list silently match a lane: a reader that
        # treated "no labels known" as "advertises everything" would vouch for
        # a runner it knows nothing about.
        "advertises_unknown": not advertises,
        "launch_rate_per_hour": None,
        "verdict": "broken",
        "reason": "",
    }

    rc, out, _ = _run(["launchctl", "print", f"{_domain()}/{label}"])
    parsed = parse_launchctl_print(out) if rc == 0 else {}
    record["loaded"] = rc == 0
    record["state"] = parsed.get("state", "")
    record["runs"] = int(parsed.get("runs", 0) or 0)

    runner_dir = None
    if plist_path and os.path.isfile(plist_path):
        try:
            with open(plist_path, "rb") as handle:
                data = plistlib.load(handle)
            runner_dir = data.get("WorkingDirectory")
        except Exception:  # noqa: BLE001
            runner_dir = None

    if runner_dir:
        registered, slug, _, detail = read_runner_registration(runner_dir)
        record["registered"] = registered
        record["registration_repo"] = slug
    else:
        detail = "no WorkingDirectory in plist; cannot locate .runner"

    slug_from_label = label_slug(label)

    if not record["installed"]:
        record["verdict"] = "broken"
        record["reason"] = "declared but no plist on disk"
        return record
    if not record["loaded"]:
        record["verdict"] = "broken"
        record["reason"] = "plist present but launchd does not have it loaded"
        return record

    # A KeepAlive job that has launched many times has not been "up"; it has
    # been dying. `launchctl list` renders this identically to health.
    rate = launch_rate_per_hour(label, record["runs"], prior, now)
    record["launch_rate_per_hour"] = None if rate is None else round(rate, 1)
    looping = (
        record["state"] == "spawn scheduled"
        or (rate is not None and rate > CRASH_LOOP_RUNS_PER_HOUR)
        or (rate is None and record["runs"] > CRASH_LOOP_RUNS_ABSOLUTE)
    )
    if looping:
        record["crash_loop"] = True
        record["verdict"] = "broken"
        rate_text = "rate unmeasured" if rate is None else f"{rate:.1f} launches/hour"
        record["reason"] = (
            f"crash loop: state={record['state'] or 'unknown'} after {record['runs']} spawns "
            f"({rate_text})"
        )
        if not record["registered"]:
            record["reason"] += f"; {detail}"
        return record

    if not record["registered"]:
        record["verdict"] = "unregistered"
        record["reason"] = detail
        return record

    if lane_repo and not slug_matches_repo(record["registration_repo"], lane_repo):
        record["verdict"] = "stale_repo"
        record["reason"] = (
            f"registered against {record['registration_repo']} but the lane serves {lane_repo}"
        )
        return record

    if slug_from_label and lane_repo and not slug_matches_repo(slug_from_label, lane_repo):
        record["verdict"] = "stale_repo"
        record["reason"] = (
            f"launchd label names {slug_from_label} but the lane serves {lane_repo}"
        )
        return record

    record["verdict"] = "healthy"
    record["reason"] = f"loaded, {detail}"
    return record


def heartbeat_ages(state_dir: str, now: float) -> list[float]:
    """Ages in seconds of every supervisor heartbeat in ``state_dir``."""
    ages: list[float] = []
    if not os.path.isdir(state_dir):
        return ages
    for name in os.listdir(state_dir):
        if not name.endswith(".state.json"):
            continue
        path = os.path.join(state_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            stamp = data.get("ts")
            when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except Exception:  # noqa: BLE001 - fall back to mtime
            try:
                when = os.path.getmtime(path)
            except OSError:
                continue
        ages.append(max(0.0, now - when))
    return ages


def assess_jit_lane(lane: dict, home: str, now: float, stale_secs: int) -> dict:
    """Classify one just-in-time lane from its supervisors' heartbeats."""
    lane_id = lane.get("id", "")
    identity = lane.get("_identity") or lane_id
    state_dir = os.path.join(home, ".tartci", "state", "macos-fleet", identity)
    ages = heartbeat_ages(state_dir, now)
    fresh = [age for age in ages if age <= stale_secs]
    declared = int(lane.get("supervisors", 0) or 0)
    record = {
        "id": lane_id,
        "repo": lane.get("repo", ""),
        "labels": list(lane.get("labels", []) or []),
        "supervisors": declared,
        "fresh": len(fresh),
        "heartbeat_age_secs": int(min(ages)) if ages else None,
        "verdict": "attested" if fresh else "unattested",
        "reason": "",
    }
    if fresh:
        record["reason"] = (
            f"{len(fresh)}/{declared} supervisor heartbeat(s) within {stale_secs}s in {state_dir}"
        )
    elif ages:
        record["reason"] = (
            f"{len(ages)} heartbeat(s) present but the freshest is {int(min(ages))}s old "
            f"(threshold {stale_secs}s)"
        )
    else:
        record["reason"] = f"no supervisor heartbeat file in {state_dir}"
    return record


def sensor_census(agents: list[tuple[str, str]], now: float,
                  expected_labels: set[str] | None = None) -> list[dict]:
    """Every sensor on this host with its launchd state and log age.

    This is the meta-detector whose absence let five sensors die unnoticed —
    one crash-looping every tick for roughly three months, another silent for
    seven weeks — while every surface that looked at them reported health.
    """
    census: list[dict] = []
    for label, plist_path in agents:
        rc, out, _ = _run(["launchctl", "print", f"{_domain()}/{label}"])
        parsed = parse_launchctl_print(out) if rc == 0 else {}
        log_age = None
        interval = None
        try:
            with open(plist_path, "rb") as handle:
                data = plistlib.load(handle)
            interval = data.get("StartInterval")
            log_path = data.get("StandardOutPath") or data.get("StandardErrorPath")
            if log_path and os.path.exists(log_path):
                log_age = int(max(0.0, now - os.path.getmtime(log_path)))
        except Exception:  # noqa: BLE001
            pass
        exit_code = parsed.get("last_exit_code")
        finding = None
        # An Actions-runner plist that the host profile does not declare and
        # launchd does not have loaded is a RETIRED LEFTOVER, not a fault. M3
        # carries eight of them from before the 2026-07-19 organisation move.
        # Reporting each as a finding is exactly how an alarm channel becomes
        # noise, which is worse than silence because it consumes the channel.
        retired_leftover = (
            label.startswith(ACTIONS_RUNNER_PREFIX)
            and expected_labels is not None
            and label not in expected_labels
        )
        if rc != 0:
            finding = None if retired_leftover else "not loaded"
        elif parsed.get("state") == "spawn scheduled" and int(parsed.get("runs", 0) or 0) > CRASH_LOOP_RUNS_PER_HOUR:
            finding = f"crash loop after {parsed.get('runs')} spawns"
        elif exit_code not in (None, "0", 0):
            finding = f"last exit {exit_code}"
        elif interval and log_age is not None and log_age > 3 * int(interval):
            finding = f"log {log_age}s old against a {interval}s interval"
        census.append(
            {
                "label": label,
                "retired_leftover": retired_leftover,
                "loaded": rc == 0,
                "state": parsed.get("state", ""),
                "runs": int(parsed.get("runs", 0) or 0),
                "last_exit_code": exit_code,
                "log_age_secs": log_age,
                "interval_secs": interval,
                "finding": finding,
            }
        )
    return census


def load_profile(path: str) -> tuple[dict, bool, str]:
    """Read the installed host profile snapshot.

    Returns ``(profile, readable, detail)``. The ``readable`` flag is not
    decoration: the first deployment of this script ran under launchd's
    ``/usr/bin/python3``, which on macOS is 3.9 and has no ``tomllib``. The
    profile silently parsed as ``{}``, so the record it wrote declared **zero**
    lanes and zero runners — a sensor that ran, wrote a file, and measured
    nothing, which is byte-for-byte the failure mode this whole artifact
    exists to end. A reader cannot tell that apart from a host that genuinely
    has no lanes unless the file says so, so now it says so, and
    ``--self-check`` exits non-zero on it.
    """
    if not os.path.exists(path):
        return {}, True, f"no profile at {path}; this host declares no fleet lanes"
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - python < 3.11
        return {}, False, (
            f"python {sys.version.split()[0]} has no tomllib, so {path} was NOT read; "
            "every lane and persistent runner below is missing, not absent"
        )
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle), True, f"parsed {path}"
    except (OSError, ValueError) as exc:
        return {}, False, f"{path} unreadable: {exc}"


def git_sha(root: str) -> str | None:
    rc, out, _ = _run(["git", "-C", root, "rev-parse", "HEAD"])
    return out.strip() if rc == 0 and out.strip() else None


def file_sha256(path: str) -> str | None:
    import hashlib

    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def load_prior_samples(path: str) -> dict:
    """``label -> (runs, written_at epoch)`` from the previous attestation."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    try:
        when = datetime.strptime(data.get("written_at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).timestamp()
    except ValueError:
        return {}
    samples = {}
    for record in data.get("persistent_runners", []):
        label = record.get("label")
        if label:
            samples[label] = (int(record.get("runs", 0) or 0), when)
    return samples


def self_sha256() -> str | None:
    """SHA-256 of this script.

    Stamped into every record so a host running an older writer is visible as
    **skewed** rather than as stale or dead. A prior incident had a stale
    checker report every lane on a healthy host as `heartbeat_missing`; the
    reader was behind, not the fleet, and nothing in the output said so.
    """
    return file_sha256(os.path.abspath(__file__))


def build_attestation(agents_dir: str, profile_path: str, home: str,
                      interval_secs: int, stale_secs: int, root: str,
                      advertise_map: dict | None = None,
                      prior_path: str | None = None) -> dict:
    now = utcnow()
    advertise_map = advertise_map or {}
    prior = load_prior_samples(prior_path) if prior_path else {}
    profile, profile_readable, profile_detail = load_profile(profile_path)
    host_block = profile.get("host", {}) if isinstance(profile, dict) else {}
    host_id = host_block.get("id") or socket.gethostname().split(".")[0]
    lanes = profile.get("lane", []) if isinstance(profile, dict) else []

    agents = discover_launch_agents(agents_dir)
    known_present = None
    for label, _ in agents:
        if label.startswith("com.danielraffel.tartci."):
            rc, _, _ = _run(["launchctl", "print", f"{_domain()}/{label}"])
            if rc == 0:
                known_present = label
                break
    launchd_readable, launchd_detail = launchd_self_check(known_present)

    plists = {label: path for label, path in agents}

    persistent: list[dict] = []
    declared_labels = list(host_block.get("persistent_runner_labels", []) or [])
    # A lane's repo is what a persistent runner's registration must match. When
    # several lanes exist, the first one is the host's primary repository.
    lane_repo = lanes[0].get("repo") if lanes else None
    profile_advertises = host_block.get("persistent_runner_advertises", {}) or {}
    for label in declared_labels:
        advertises = list(advertise_map.get(label) or profile_advertises.get(label) or [])
        record = assess_persistent_runner(
            label, plists.get(label), lane_repo, advertises, interval_secs, prior, now
        )
        persistent.append(record)
    # A persistent runner installed but NOT declared is also a fact worth
    # attesting: it is exactly the shape of a pre-org-move leftover.
    for label, path in agents:
        if not label.startswith(ACTIONS_RUNNER_PREFIX) or label in declared_labels:
            continue
        advertises = list(advertise_map.get(label) or profile_advertises.get(label) or [])
        record = assess_persistent_runner(
            label, path, lane_repo, advertises, interval_secs, prior, now
        )
        record["declared"] = False
        if record["verdict"] == "healthy":
            record["verdict"] = "undeclared"
            record["reason"] = "installed and registered but not declared by the host profile"
        persistent.append(record)

    jit = [assess_jit_lane(lane, home, now, stale_secs) for lane in lanes]

    return {
        "schema": SCHEMA,
        "host": host_id,
        "written_at": iso_now(),
        "interval_secs": interval_secs,
        "generation": {
            "tartci_root_sha": git_sha(root),
            "profile_sha256": file_sha256(profile_path),
            "writer": WRITER,
            "writer_sha256": self_sha256(),
            "deployment": os.environ.get("PULP_ATTESTATION_GENERATION"),
        },
        "launchd_readable": launchd_readable,
        "launchd_detail": launchd_detail,
        "profile_readable": profile_readable,
        "profile_detail": profile_detail,
        "persistent_runners": persistent,
        "jit_lanes": jit,
        "sensors": sensor_census(agents, now, set(declared_labels)),
    }


def atomic_write_json(path: str, value: dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".host-attestation.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def default_output(home: str) -> str:
    root = os.environ.get("TARTCI_HOME") or os.path.join(home, ".tartci")
    return os.path.join(root, "state", "host-attestation.json")


def main(argv: list[str] | None = None) -> int:
    home = os.path.expanduser("~")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="write the attestation file")
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the writer version and its content hash, then exit",
    )
    parser.add_argument("--out", default=None, help="output path")
    parser.add_argument(
        "--launch-agents-dir", default=os.path.join(home, "Library", "LaunchAgents")
    )
    parser.add_argument(
        "--profile", default=os.path.join(home, ".config", "tartci", "macos-fleet-profile.toml")
    )
    parser.add_argument("--home", default=home)
    parser.add_argument("--interval-secs", type=int, default=DEFAULT_INTERVAL_SECS)
    parser.add_argument(
        "--heartbeat-stale-secs", type=int, default=DEFAULT_HEARTBEAT_STALE_SECS
    )
    parser.add_argument(
        "--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    parser.add_argument(
        "--advertise",
        action="append",
        default=[],
        metavar="LABEL=a,b,c",
        help=(
            "labels a persistent runner advertises; GitHub does not record them in .runner, "
            "so without this (or [host.persistent_runner_advertises] in the profile) the "
            "record carries advertises_unknown and cannot vouch for any lane"
        ),
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="exit non-zero unless the launchd domain proved readable",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(json.dumps({"writer": WRITER, "writer_sha256": self_sha256(),
                          "schema": SCHEMA,
                          "deployment": os.environ.get("PULP_ATTESTATION_GENERATION")}))
        return 0

    advertise_map: dict[str, list[str]] = {}
    for entry in args.advertise:
        if "=" not in entry:
            print(f"--advertise expects LABEL=a,b,c, got {entry!r}", file=sys.stderr)
            return 2
        label, _, labels = entry.partition("=")
        advertise_map[label.strip()] = [
            part.strip() for part in labels.split(",") if part.strip()
        ]

    out = args.out or default_output(args.home)
    attestation = build_attestation(
        args.launch_agents_dir,
        args.profile,
        args.home,
        args.interval_secs,
        args.heartbeat_stale_secs,
        args.root,
        advertise_map,
        out,
    )
    if args.write:
        atomic_write_json(out, attestation)
        print(f"wrote {out}")
    else:
        print(json.dumps(attestation, indent=2, sort_keys=True))

    if args.self_check:
        broken = []
        if not attestation["launchd_readable"]:
            broken.append(attestation["launchd_detail"])
        if not attestation["profile_readable"]:
            broken.append(attestation["profile_detail"])
        if broken:
            for reason in broken:
                print(f"self-check FAILED: {reason}", file=sys.stderr)
            return 1
    # A broken persistent runner or an unattested declared lane is a finding,
    # not a crash: the file is still written so the reader can see WHY.
    findings = [
        record for record in attestation["persistent_runners"]
        if record["verdict"] not in {"healthy", "undeclared"}
    ]
    findings += [
        sensor for sensor in attestation["sensors"] if sensor["finding"]
    ]
    if findings:
        for record in findings:
            name = record.get("label", "?")
            detail = record.get("reason") or record.get("finding")
            print(f"finding: {name}: {detail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
