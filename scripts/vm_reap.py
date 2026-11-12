#!/usr/bin/env python3
"""Safe local janitor for tartci VM runners.

The janitor is intentionally conservative:
  * VM deletion requires a positive state-file ownership marker.
  * The VM or runner name must match an allowed CI prefix.
  * Protected names and golden/tagged images are never deleted.
  * GitHub runner deletion only touches stale offline registrations whose
    names match an allowed CI prefix and are not backed by a fresh live
    supervisor heartbeat.
  * A registration this host could not have minted is never deleted: a
    declared persistent runner, a name without the per-boot shape, or a
    runner bound by label to another host (see `foreign_runner_reason`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import plistlib
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from typing import Any

import debug_hold  # sibling module; owns the held- prefix
import runner_census
import tart_home as tart_home_resolver
from bounded_subprocess import ObservationError, require_success, run_bounded


class ObservationBudget:
    """One elapsed-command budget shared by read-only external probes."""

    def __init__(self, total_seconds: float) -> None:
        if total_seconds <= 0:
            raise ValueError("observation timeout must be positive")
        self.remaining_seconds = total_seconds

    def run_json(self, argv: list[str], limit: float, operation: str) -> Any:
        if self.remaining_seconds <= 0:
            raise ObservationError(operation, "timeout", "total observation deadline exhausted")
        started = time.monotonic()
        try:
            return observe_json(
                argv,
                timeout=min(limit, self.remaining_seconds),
                operation=operation,
            )
        finally:
            self.remaining_seconds = max(
                0.0,
                self.remaining_seconds - (time.monotonic() - started),
            )


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S %z"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            pass
    return None


def run(
    argv: list[str],
    *,
    check: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=check,
        capture_output=True,
        text=text,
    )


def run_json(argv: list[str]) -> Any:
    proc = run(argv)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv)} failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{' '.join(argv)} returned invalid JSON: {exc}") from exc


def observe_json(argv: list[str], *, timeout: float, operation: str) -> Any:
    proc = require_success(
        run_bounded(argv, timeout=timeout, operation=operation),
        operation=operation,
    )
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise ObservationError(operation, "invalid_json", str(exc)) from exc


def github_cli() -> str:
    """Return the configured GitHub CLI for every janitor API operation."""
    return os.environ.get("TARTCI_GH_CLI") or "gh"


def starts_with_any(name: str, prefixes: list[str]) -> bool:
    return any(name.startswith(prefix) for prefix in prefixes if prefix)


# A runner's state file lives only on the host that minted it, so "no local
# state" proves ownerlessness only for a registration THIS host mints. Every
# host's janitor sees every registration in the repository, and GitHub reports
# a healthy, connected runner as offline for the seconds its broker long-poll
# stalls. On 2026-09-29 m3's janitor read a bare-metal runner on another host
# as offline once in 1,618 passes, found no state for it, and deleted the
# registration; the runner then wiped its own configuration and the lane it
# served sat dead for sixteen days with nothing alerting.
PER_BOOT_RUNNER_RE = re.compile(r"^.+-[0-9]+-[0-9]+$")


def runner_label_names(runner: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for label in runner.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name:
            names.append(name)
    return names


def foreign_runner_reason(
    runner: dict[str, Any],
    persistent: list[str],
    host_label_prefixes: list[str],
    own_host_labels: list[str],
    own_name_prefixes: list[str],
) -> str:
    """Why this host must not delete `runner`, or "" when it may.

    Every doubt keeps the registration: a ghost costs one stale row, a wrong
    delete takes a live lane offline until someone re-registers it by hand.
    Ownership is checked on two axes, the name this host's lanes mint and the
    host-bound label they advertise. They are independent only for macOS and
    Windows runners: Linux per-boot names carry no host, so for Linux runners
    the label is the only axis, which is why resolve_host_labels refuses an
    ambiguous label set outright.
    """
    name = str(runner.get("name") or "")
    if name in persistent:
        return "persistent_runner"
    if not PER_BOOT_RUNNER_RE.match(name):
        return "not_a_per_boot_name"
    if not starts_with_any(name, own_name_prefixes):
        return "not_this_hosts_prefix"
    for label in runner_label_names(runner):
        if starts_with_any(label, host_label_prefixes) and label not in own_host_labels:
            return f"bound_to_other_host:{label}"
    return ""


# The plists the Linux and Windows lanes are installed from. They are rendered
# by hand with this host's `pulp-host-<host>` label baked into `--labels`, so
# the installed files are the ground truth for which host label and name
# prefix this host's own registrations carry.
LOCAL_LANE_PLISTS = {
    "com.danielraffel.pulp.tart-runner-linux.plist": "linux",
    "com.danielraffel.pulp.qemu-runner-windows.plist": "windows",
}


def resolve_host_labels(
    collected: list[str], host_label_prefixes: list[str]
) -> tuple[list[str], str]:
    """This host's one host-bound label, or none plus a problem when it is ambiguous.

    A host owns exactly one host-bound label. Linux per-boot names carry no
    host (`linux-ephr-<pid>-<n>` on every host), so for Linux runners the label
    is the only ownership axis; a lane plist that also advertises another
    host's label would let this janitor delete that host's runners. More than
    one distinct host-bound label across the plists and TARTCI_HOST_LABEL
    (which `collected` includes, so a declared label that disagrees with a
    plist is the same case) therefore owns none, and the conflict is reported.
    """
    bound = sorted({label for label in collected if starts_with_any(label, host_label_prefixes)})
    if len(bound) > 1:
        return ([label for label in collected if not starts_with_any(label, host_label_prefixes)],
                f"host_label_conflict:{','.join(bound)}")
    return collected, ""


def host_slug() -> str:
    raw = (socket.gethostname().split(".")[0] or "host").lower()
    return re.sub(r"[^a-z0-9]+", "-", raw).strip("-") or "host"


def local_lane_identity(agents_dir: pathlib.Path | None) -> tuple[list[str], list[str]]:
    """(host-bound labels, per-boot name prefixes) of this host's Linux/Windows lanes."""
    labels: list[str] = []
    prefixes: list[str] = []
    if agents_dir is None:
        return labels, prefixes
    for filename, kind in LOCAL_LANE_PLISTS.items():
        path = agents_dir / filename
        try:
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
        args = [str(a) for a in plist.get("ProgramArguments") or []]
        env = plist.get("EnvironmentVariables") or {}
        raw = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--labels"]
        raw.append(str(env.get("TARTCI_RUNNER_LABELS") or ""))
        for chunk in raw:
            for label in chunk.split(","):
                label = label.strip()
                if label and label not in labels:
                    labels.append(label)
        if kind == "linux":
            prefix = "linux-ephr-"
        else:
            prefix = str(env.get("TARTCI_RUNNER_NAME_PREFIX") or env.get("PULP_RUNNER_NAME_PREFIX")
                         or f"win-ephr-{host_slug()}") + "-"
        if prefix not in prefixes:
            prefixes.append(prefix)
    return labels, prefixes


def own_lane_name_prefixes(lanes: list[Any]) -> list[str]:
    """Per-boot name prefixes of this host's own macOS fleet lanes.

    Only the host-specific runner name (`studio-pulp-gate-01`), never the
    shared lane identity (`pulp-gate-`): another host's lane of the same
    identity mints names this host must not touch.
    """
    prefixes: list[str] = []
    for lane in lanes:
        name = getattr(lane, "runner_name", "") or ""
        if name:
            # A per-boot name is `<runner_name>-<pid>-<n>`.
            stem = name.rstrip("-") + "-"
            if stem not in prefixes:
                prefixes.append(stem)
    return prefixes


def offline_tracker_path(state_root: str) -> pathlib.Path:
    return pathlib.Path(state_root).expanduser() / "reap" / "runner-offline.json"


def load_offline_tracker(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def offline_key(runner: dict[str, Any]) -> str:
    return f"{runner.get('_endpoint') or ''}#{runner.get('id')}#{runner.get('name')}"


def is_protected_name(name: str, protected: list[str]) -> bool:
    if not name:
        return True
    if ":" in name:
        return True
    if name in protected:
        return True
    if name.startswith("bench-") or name.endswith("-bench") or "-bench-" in name:
        return True
    if name.endswith(":latest"):
        return True
    if debug_hold.is_held(name):
        # A failed VM kept for debugging (scripts/debug_hold.py): it has no
        # state file and no runner by design, and is deleted only by its TTL,
        # its PR closing, or an operator.
        return True
    return False


def pid_start(pid: int) -> str:
    proc = run(["ps", "-p", str(pid), "-o", "lstart="])
    if proc.returncode != 0:
        return ""
    return " ".join(proc.stdout.strip().split())


def pid_alive_same(pid_value: Any, expected_start: Any) -> bool:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    current = pid_start(pid)
    if not current:
        return False
    if isinstance(expected_start, str) and expected_start.strip():
        return current == " ".join(expected_start.strip().split())
    return True


def state_files(state_dirs: list[pathlib.Path]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    seen: set[pathlib.Path] = set()
    for state_dir in state_dirs:
        if not state_dir.exists():
            continue
        for path in sorted(state_dir.glob("*.state.json")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(path)
    return files


def load_states(state_dirs: list[pathlib.Path], now: dt.datetime) -> tuple[list[dict[str, Any]], list[str]]:
    states: list[dict[str, Any]] = []
    problems: list[str] = []
    for path in state_files(state_dirs):
        try:
            with path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"state_unreadable:{path}:{exc}")
            continue
        state["_path"] = str(path)
        parsed_ts = parse_ts(state.get("ts"))
        if parsed_ts is None:
            try:
                parsed_ts = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
            except OSError:
                parsed_ts = now
        state["_age_secs"] = max(0, int((now - parsed_ts).total_seconds()))
        state["_owner_pid_alive"] = pid_alive_same(
            state.get("supervisor_pid"),
            state.get("supervisor_pid_started_at"),
        )
        states.append(state)
    return states, problems


def tart_vms(
    timeout: float = 15.0,
    budget: ObservationBudget | None = None,
) -> list[dict[str, Any]]:
    argv = ["tart", "list", "--format", "json", "--source", "local"]
    data = (
        budget.run_json(argv, timeout, "tart_list")
        if budget
        else observe_json(argv, timeout=timeout, operation="tart_list")
    )
    if not isinstance(data, list):
        return []
    return data


def vm_name(vm: dict[str, Any]) -> str:
    return str(vm.get("Name") or vm.get("name") or "")


def vm_state(vm: dict[str, Any]) -> str:
    return str(vm.get("State") or vm.get("state") or "")


def is_running_state(state: str) -> bool:
    return state.lower().startswith("run")


def macos_running_count(
    vms: list[dict[str, Any]],
    timeout: float = 15.0,
    budget: ObservationBudget | None = None,
) -> int:
    count = 0
    for vm in vms:
        state = vm_state(vm)
        if not is_running_state(state):
            continue
        name = vm_name(vm)
        os_name = ""
        if name:
            try:
                argv = ["tart", "get", name, "--format", "json"]
                detail = (
                    budget.run_json(argv, timeout, "tart_get")
                    if budget
                    else observe_json(argv, timeout=timeout, operation="tart_get")
                )
                os_name = str(detail.get("OS", "")).lower()
            except ObservationError:
                raise
            except Exception:
                os_name = ""
        if os_name in ("", "darwin", "macos"):
            count += 1
    return count


def github_runner_census(
    repo: str,
    timeout: float = 15.0,
    budget: ObservationBudget | None = None,
) -> runner_census.RunnerCensus:
    """Read every registration scope that can serve `repo`.

    A repository-only listing omits organization-registered runners, so the
    janitor would classify a registration it cannot see as absent. Both scopes
    are read and a scope that fails is recorded as unread, never as empty.
    """

    def run_json(argv: list[str]) -> Any:
        return (
            budget.run_json(argv, timeout, "github_runners")
            if budget
            else observe_json(argv, timeout=timeout, operation="github_runners")
        )

    fetch = runner_census.cli_fetcher(github_cli(), run_json=run_json)
    return runner_census.collect(repo, fetch)


def github_runners(
    repo: str,
    timeout: float = 15.0,
    budget: ObservationBudget | None = None,
    problems: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Runner rows from every scope, each carrying the endpoint that owns it.

    Deletion and inspection must address a registration through its own scope,
    so every row keeps `_endpoint`. A scope that could not be read is reported
    through `problems` so a short list reads as partial rather than complete; a
    census where no scope could be read is an observation failure, not an empty
    fleet.
    """
    census = github_runner_census(repo, timeout, budget)
    if census.unreachable and len(census.unreachable) == len(census.scopes):
        raise ObservationError("github_runners", "unreadable", census.unreachable_detail())
    if problems is not None:
        for scope in census.unreachable:
            problems.append(f"github_runners_scope_unreadable:{scope.scope}")
    rows: list[dict[str, Any]] = []
    for record in census.runners:
        row = dict(record.raw)
        row["_scope"] = record.scope
        row["_endpoint"] = record.endpoint
        rows.append(row)
    return rows


def delete_vm(name: str, running: bool) -> list[str]:
    fixed: list[str] = []
    if running:
        run(["tart", "stop", name])
        fixed.append(f"vm_stopped:{name}")
    run(["tart", "delete", name], check=True)
    fixed.append(f"vm_deleted:{name}")
    return fixed


def delete_runner(
    repo: str, runner_id: Any, runner_name: str, endpoint: str = ""
) -> str:
    """Delete one registration through the scope that owns it.

    A repository URL cannot address an organization registration, so the
    endpoint recorded on the runner row is authoritative and the repository
    endpoint is only the fallback for a caller that has none.
    """
    target = endpoint or f"repos/{repo}/actions/runners"
    # Same identity binding as the census read that found this registration:
    # an App wrapper run from $HOME would otherwise mint for no repository (or
    # the wrong one) and the delete of an org-scope registration would 403.
    saved = runner_census.bind_identity(repo)
    try:
        run(
            [
                github_cli(),
                "api",
                "-X",
                "DELETE",
                f"{target}/{runner_id}",
            ],
            check=True,
        )
    finally:
        runner_census.restore_identity(saved)
    return f"github_runner_deleted:{runner_name}:{runner_id}"


def unlink_state(path_text: str) -> str:
    pathlib.Path(path_text).unlink(missing_ok=True)
    return f"state_deleted:{path_text}"


import fleet_lane_discovery
import re


# A lane names each VM it boots `<runner>-<supervisor pid>-<n>`. A VM whose
# supervisor died (a signal, a restart, a crash between boot and teardown) is
# left stopped with no state file pointing at it, because the lane's state
# file only ever names its CURRENT VM. The marker rule above therefore never
# owns it, and on m3 four such VMs held ~250 GB on 2026-09-30.
ORPHAN_NAME_RE = re.compile(r"^(?P<runner>.+)-(?P<pid>[0-9]+)-(?P<n>[0-9]+)$")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lease_references(store_dir: pathlib.Path | None = None) -> set[str] | None:
    """Every VM name any lease record names, or None when the store is unreadable."""
    store = store_dir or pathlib.Path(
        os.environ.get("TARTCI_LEASE_DIR", str(pathlib.Path.home() / ".tartci/state/leases")))
    path = store / "leases.json"
    if not path.exists():
        return set()
    try:
        records = json.loads(path.read_text() or "[]")
    except (OSError, ValueError):
        return None
    if not isinstance(records, list):
        return None
    names: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in ("vm_name", "id", "label"):
            value = record.get(key)
            if isinstance(value, str) and value:
                names.add(value)
    return names


# A clone left by a lane this host no longer runs (a retired lane, or one
# renamed) has no loaded runner to match, so the rule above never owned it:
# m1 kept pulp-preamble-m1-target-06-99292-1 for two months. Such a runner
# name is still provably this host's when it carries the host id as one of its
# dash-separated words and starts with an allowed CI prefix or the host id; it
# must also sit untouched for RETIRED_LANE_MIN_AGE_SECS.
RETIRED_LANE_MIN_AGE_SECS = 24 * 3600


class TartciNames:
    """Whether the fleet profile, a tartci config file or a LaunchAgent names a
    VM (a golden, a rollback image): tart_image_prune's ownership rule, read
    once and only when a retired-lane candidate needs it. Unreadable reads as
    named."""

    def __init__(self) -> None:
        self._text: str | None = None
        self._values: set[str] | None = None

    def __call__(self, name: str) -> bool:
        import tart_image_prune as prune  # noqa: PLC0415 - one ownership rule
        if self._text is None:
            home = pathlib.Path.home()
            try:
                self._values, _ = prune.profile_strings(pathlib.Path(os.environ.get(
                    "TARTCI_FLEET_PROFILE", str(home / ".config/tartci/macos-fleet-profile.toml"))))
            except (OSError, ValueError, RuntimeError):
                self._values = None
            # The state dir is left out: the retired lane's own state record
            # names its clone, and that record is the proof required below.
            self._text = prune.reference_text([home / ".config/tartci",
                                               home / "Library/LaunchAgents"])
        if self._values is None:
            return True
        return name in self._values or prune.named_in(name, self._text)


def retired_lane_record(state_root: pathlib.Path, name: str, runner: str, pid: str) -> bool:
    """Whether a lane's own state file (`<state_root>/<lane>/*.state.json`)
    records exactly this VM, from a Tart provider, for this runner and
    supervisor pid. A clone's name alone is no proof: an agent can name a
    VM `pulp-m1-repro-20261016-1`."""
    try:
        paths = sorted(state_root.glob("*/*.state.json"))
    except OSError:
        return False
    for path in paths:
        if path.is_symlink():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (isinstance(data, dict) and data.get("vm") == name
                and str(data.get("provider") or "").startswith("tart-")
                and data.get("runner") == runner and str(data.get("supervisor_pid")) == pid):
            return True
    return False


def retired_clones_enabled() -> bool:
    """Per-host opt-in for deleting retired lanes' clones; off reports only."""
    flag = os.environ.get("TARTCI_REAP_RETIRED_ENABLE_FILE") or str(
        pathlib.Path.home() / ".config" / "tartci" / "reap-retired-clones.enabled")
    return pathlib.Path(flag).is_file()


def retired_lane_runner(runner: str, host_id: str | None, prefixes: list[str]) -> bool:
    if not host_id or host_id not in runner.split("-"):
        return False
    return runner.startswith(f"{host_id}-") or starts_with_any(runner, prefixes)


def orphan_verdict(
    name: str,
    running: bool,
    lane_runners: set[str],
    states_by_vm: dict[str, dict[str, Any]],
    leases: set[str] | None,
    vm_dir: pathlib.Path,
    now: float,
    min_age_secs: int,
    alive: Any = pid_alive,
    host_id: str | None = None,
    prefixes: list[str] | None = None,
    named: Any = None,
    record: Any = None,
    retired_enabled: bool = False,
) -> tuple[bool, str]:
    """(delete?, why) for a VM no state file owns. Every doubt keeps it.

    Deleted only when ALL hold: the name is `<runner>-<pid>-<n>` for a lane
    loaded on this host right now, or for a retired lane of this host (see
    `retired_lane_runner`, which also needs a day untouched); that supervisor
    pid is not alive; no state file names the VM; no lease record (live or
    stale) names it; it is stopped; and its bundle has not changed for
    `min_age_secs`.
    """
    match = ORPHAN_NAME_RE.match(name)
    retired = False
    if not match:
        return False, "not_a_lane_vm"
    if match.group("runner") not in lane_runners:
        if ":" in name or not retired_lane_runner(match.group("runner"), host_id,
                                                   prefixes or []):
            return False, "not_a_lane_vm"
        # The image pruner's ownership rule: a VM any tartci file, the fleet
        # profile or a LaunchAgent names (a golden, a rollback image) is kept.
        if named is None or named(name):
            return False, "named_by_tartci"
        if record is None or not record(name, match.group("runner"), match.group("pid")):
            return False, "no_retired_lane_record"
        retired = True
        min_age_secs = max(min_age_secs, RETIRED_LANE_MIN_AGE_SECS)
    if running:
        return False, "running"
    if name in states_by_vm:
        return False, "state_file_names_it"
    if leases is None:
        return False, "lease_store_unreadable"
    if name in leases or any(ref.endswith(f"-{name}") for ref in leases):
        return False, "lease_names_it"
    if alive(int(match.group("pid"))):
        return False, "supervisor_pid_alive"
    try:
        age = now - vm_dir.stat().st_mtime
    except OSError:
        return False, "vm_bundle_unreadable"
    if age < min_age_secs:
        return False, "recent"
    if retired and not retired_enabled:
        return False, (f"retired_clone_reap_off: would delete (retired lane, supervisor "
                       f"{match.group('pid')} gone, stopped {int(age)}s)")
    lane = "retired lane " if retired else ""
    return True, f"{lane}supervisor {match.group('pid')} gone, stopped {int(age)}s"


def split_paths(value: str | None) -> list[str]:
    if not value:
        return []
    paths: list[str] = []
    for chunk in value.replace(",", os.pathsep).split(os.pathsep):
        chunk = chunk.strip()
        if chunk:
            paths.append(chunk)
    return paths


def scoped_view(args: argparse.Namespace) -> bool:
    """True when the caller narrowed the view away from the whole-host default."""
    if split_paths(args.state_dir):
        return True
    default_root = os.environ.get(
        "TARTCI_STATE_ROOT", str(pathlib.Path.home() / ".tartci/state")
    )
    return pathlib.Path(args.state_root).expanduser() != pathlib.Path(default_root).expanduser()


def state_dirs_from_args(
    args: argparse.Namespace,
    lanes: list[fleet_lane_discovery.Lane] | None = None,
) -> list[pathlib.Path]:
    """State directories to read.

    The legacy roots below are the single-lane layout. Fleet lanes each write
    to their OWN directory named by their installed plist, so a host running
    fleet lanes had every supervisor outside this list and matched zero. The
    lane directories come from launchd (see fleet_lane_discovery), never from
    a second hard-coded path list -- one source of truth is the whole point.
    """
    explicit = split_paths(args.state_dir)
    if explicit:
        return [pathlib.Path(path).expanduser() for path in explicit]
    root = pathlib.Path(args.state_root).expanduser()
    dirs = [root, root / "macos", root / "linux", root / "windows"]
    for lane_dir in fleet_lane_discovery.lane_state_dirs(lanes):
        if lane_dir not in dirs:
            dirs.append(lane_dir)
    return dirs


def safe_owned_path(path_text: Any, runner_name: str) -> pathlib.Path | None:
    if not isinstance(path_text, str) or not path_text.strip():
        return None
    path = pathlib.Path(path_text).expanduser()
    text = str(path)
    if text in ("", "/", str(pathlib.Path.home())):
        return None
    parts = set(path.parts)
    name = path.name
    if runner_name and (runner_name in name or runner_name in text):
        return path
    if "tartci-win" in parts or "tartci" in name:
        return path
    return None


def remove_owned_path(path_text: Any, runner_name: str) -> str | None:
    path = safe_owned_path(path_text, runner_name)
    if path is None or not path.exists():
        return None
    if path.is_dir():
        shutil.rmtree(path)
        return f"path_deleted:{path}"
    path.unlink()
    return f"path_deleted:{path}"


def reject_symlink_components(path: pathlib.Path, description: str) -> None:
    """Refuse a path whose existing components include a symlink.

    Comparing the state-file strings alone is insufficient: a parent symlink
    can make ``work_dir/golden.qcow2`` resolve outside the owned job directory
    while preserving the same lexical spelling.  ``lstat`` keeps this check
    independent of the target's file type and also covers a symlinked sidecar.
    Missing final files are allowed because recovery may be cleaning up a
    partially-created job; any existing parent component must still be real.
    """
    if not path.is_absolute():
        raise ValueError(f"relative {description} recovery path")
    # macOS exposes the temporary directory through these system aliases. They
    # are resolved before checking job-owned components; a symlink introduced
    # below either alias remains rejected.
    system_aliases = {
        pathlib.Path("/var"): pathlib.Path("/private/var"),
        pathlib.Path("/tmp"): pathlib.Path("/private/tmp"),
    }
    current = pathlib.Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"cannot inspect {description} recovery path") from exc
        if stat.S_ISLNK(mode):
            if current in system_aliases and pathlib.Path(os.path.realpath(current)) == system_aliases[current]:
                continue
            raise ValueError(f"linked Windows {description} recovery path")


def unseal_windows_snapshot(state: dict[str, Any]) -> None:
    """Clear the immutable flag only on this job's recorded private backing file."""
    work = safe_owned_path(state.get("work_dir"), str(state.get("runner") or ""))
    snapshot_text = state.get("golden_snapshot")
    if not work or not isinstance(snapshot_text, str) or not snapshot_text:
        return
    reject_symlink_components(work, "job directory")
    snapshot = pathlib.Path(snapshot_text)
    if snapshot != work / "golden.qcow2":
        raise ValueError("unsafe Windows golden snapshot recovery path")
    reject_symlink_components(snapshot, "golden snapshot")
    for path in (snapshot, pathlib.Path(f"{snapshot}.sha256")):
        reject_symlink_components(path, "golden snapshot")
        if path.exists():
            if not path.is_file():
                raise ValueError("non-file Windows golden snapshot recovery path")
            if hasattr(os, "chflags"):
                os.chflags(path, 0)


def stop_pid(pid_value: Any, expected_start: Any) -> bool:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not pid_alive_same(pid, expected_start):
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not pid_alive_same(pid, expected_start):
                    return True
                time.sleep(0.2)
    return not pid_alive_same(pid, expected_start)


def build_digest(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    now = utcnow()
    observation_budget = ObservationBudget(args.observation_timeout_secs)
    problems: list[str] = []
    fixed: list[str] = []
    unreadable: list[str] = []

    # Lane discovery applies to the DEFAULT, whole-host view only. A caller
    # that passed --state-dir or pointed --state-root elsewhere has
    # deliberately scoped the view, and silently widening it back to every
    # loaded lane would both surprise them and make this digest depend on live
    # host state. In that case the expected count is unknown rather than zero:
    # a narrowed view has no business asserting what the host should have.
    if scoped_view(args):
        lanes, lane_problems = None, []
    else:
        lanes, lane_problems = fleet_lane_discovery.discover_lanes()
    problems.extend(lane_problems)
    state_dirs = state_dirs_from_args(args, lanes)
    prefixes = [p for p in args.prefixes.split(",") if p]
    # Fleet VMs are named `<host_id>-<identity>-...`, which the legacy literal
    # prefix list does not cover. Derive them from the discovered lanes.
    for derived in fleet_lane_discovery.runner_name_prefixes(lanes or []):
        if derived not in prefixes:
            prefixes.append(derived)
    lane_runners = {lane.runner_name for lane in lanes or [] if lane.runner_name}
    # The host id is the first word every loaded lane's runner shares.
    host_words = {runner.split("-", 1)[0] for runner in lane_runners}
    host_id = host_words.pop() if len(host_words) == 1 else None
    leases_seen = lease_references() if lane_runners else set()
    tartci_names = TartciNames()
    store = ensure_tart_home()
    tart_home = pathlib.Path(store["path"])
    protected = [p for p in args.protected_names.split(",") if p]
    persistent_runners = [p for p in args.persistent_runners.split(",") if p]
    host_label_prefixes = [p for p in args.host_label_prefixes.split(",") if p]
    agents_dir = (pathlib.Path(args.launch_agents_dir).expanduser() if args.launch_agents_dir
                  else None if scoped_view(args)
                  else pathlib.Path.home() / "Library/LaunchAgents")
    lane_labels, lane_name_prefixes = local_lane_identity(agents_dir)
    own_host_labels = [p for p in args.host_labels.split(",") if p]
    own_host_labels += [label for label in lane_labels
                        if starts_with_any(label, host_label_prefixes) and label not in own_host_labels]
    own_host_labels, label_conflict = resolve_host_labels(own_host_labels, host_label_prefixes)
    if label_conflict:
        problems.append(label_conflict)
    own_name_prefixes = [p for p in args.own_name_prefixes.split(",") if p]
    for prefix in own_lane_name_prefixes(lanes or []) + lane_name_prefixes:
        if prefix not in own_name_prefixes:
            own_name_prefixes.append(prefix)
    # Names this host mints are by definition its residue to inspect.
    for prefix in own_name_prefixes:
        if prefix not in prefixes:
            prefixes.append(prefix)
    tracker_path = offline_tracker_path(args.state_root)
    offline_tracker = load_offline_tracker(tracker_path)
    offline_seen: dict[str, dict[str, Any]] = {}
    tart_providers = {"", "tart-macos", "tart-linux"}

    for tool in ("tart", github_cli()):
        if shutil.which(tool) is None:
            unreadable.append(f"missing_tool:{tool}")

    states, state_problems = load_states(state_dirs, now)
    problems.extend(state_problems)
    states_by_vm = {
        str(state.get("vm") or ""): state
        for state in states
        if str(state.get("vm") or "")
    }
    states_by_runner = {
        str(state.get("runner") or ""): state
        for state in states
        if str(state.get("runner") or "")
    }
    vms: list[dict[str, Any]] = []
    runners: list[dict[str, Any]] = []
    capacity = {"macos_cap": args.macos_cap, "running_macos_vms": None, "free": None}

    if not unreadable:
        try:
            raw_vms = tart_vms(args.tart_timeout_secs, observation_budget)
            running_macos = macos_running_count(
                raw_vms,
                args.tart_timeout_secs,
                observation_budget,
            )
        except ObservationError as exc:
            raw_vms = []
            unreadable.append(exc.problem_code)
        except Exception as exc:  # noqa: BLE001
            raw_vms = []
            unreadable.append(f"tart_unreadable:{exc}")
        else:
            capacity = {
                "macos_cap": args.macos_cap,
                "running_macos_vms": running_macos,
                "free": max(0, args.macos_cap - running_macos),
            }
            seen_vms: set[str] = set()
            for vm in raw_vms:
                name = vm_name(vm)
                if not name:
                    continue
                seen_vms.add(name)
                state_text = vm_state(vm)
                state = states_by_vm.get(name)
                protected_name = is_protected_name(name, protected)
                prefix_ok = starts_with_any(name, prefixes)
                state_runner = str((state or {}).get("runner") or "")
                state_vm = str((state or {}).get("vm") or "")
                state_provider = str((state or {}).get("provider") or "")
                marker_ok = bool(state) and state_vm == name
                owned = (
                    marker_ok
                    and prefix_ok
                    and not protected_name
                    and (state_provider in tart_providers)
                    and (not state_runner or starts_with_any(state_runner, prefixes))
                )
                age = int((state or {}).get("_age_secs") or 0)
                owner_pid_alive = bool((state or {}).get("_owner_pid_alive")) if state else None
                action = ""
                stale = False
                running = is_running_state(state_text)
                if owned and not running and age >= args.stopped_age_secs:
                    stale = True
                    action = "delete_stopped_vm"
                elif owned and running and not owner_pid_alive and age >= args.running_age_secs:
                    stale = True
                    action = "stop_delete_ownerless_running_vm"
                elif owned and running and owner_pid_alive and age >= args.heartbeat_stale_secs:
                    stale = True
                    action = "suspect_live_owner_stale_heartbeat"
                    problems.append(f"suspect_live_owner_stale_heartbeat:{name}")
                elif marker_ok and not owned and prefix_ok and protected_name:
                    problems.append(f"owned_marker_protected_name:{name}")
                orphan_reason = ""
                if not owned and not protected_name:
                    orphaned, orphan_reason = orphan_verdict(
                        name, running, lane_runners, states_by_vm, leases_seen,
                        tart_home / "vms" / name, now.timestamp(), args.orphan_age_secs,
                        host_id=host_id, prefixes=prefixes, named=tartci_names,
                        record=lambda vm, runner, pid: retired_lane_record(
                            pathlib.Path(args.state_root).expanduser(), vm, runner, pid),
                        retired_enabled=retired_clones_enabled())
                    if orphaned:
                        stale = True
                        action = "delete_orphaned_stopped_vm"
                row = {
                    "name": name,
                    "state": state_text,
                    "owned": owned,
                    "protected": protected_name,
                    "owner_pid_alive": owner_pid_alive,
                    "age_secs": age,
                    "stale": stale,
                    "action": action,
                    "state_file": (state or {}).get("_path"),
                    "orphan": orphan_reason or None,
                    "size_gb": vm.get("Size") if isinstance(vm.get("Size"), (int, float)) else None,
                }
                vms.append(row)
                if args.fix and action == "delete_orphaned_stopped_vm":
                    try:
                        fixed.extend(delete_vm(name, False))
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"fix_failed:{action}:{name}:{exc}")
                if args.fix and action in ("delete_stopped_vm", "stop_delete_ownerless_running_vm"):
                    try:
                        fixed.extend(delete_vm(name, running))
                        if state and state.get("_path"):
                            fixed.append(unlink_state(str(state.get("_path"))))
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"fix_failed:{action}:{name}:{exc}")
            for state in states:
                if str(state.get("provider") or "") not in tart_providers:
                    continue
                state_vm = str(state.get("vm") or "")
                path_text = str(state.get("_path") or "")
                owner_pid_alive = bool(state.get("_owner_pid_alive"))
                age = int(state.get("_age_secs") or 0)
                if not state_vm:
                    state_runner = str(state.get("runner") or "")
                    prefix_ok = starts_with_any(state_runner, prefixes)
                    protected_name = is_protected_name(state_runner, protected)
                    owned = prefix_ok and not protected_name
                    action = ""
                    stale = False
                    if owned and path_text and not owner_pid_alive and age >= args.stopped_age_secs:
                        stale = True
                        action = "delete_stale_state"
                        if args.fix:
                            try:
                                fixed.append(unlink_state(path_text))
                            except Exception as exc:  # noqa: BLE001
                                problems.append(f"fix_failed:delete_stale_state:{path_text}:{exc}")
                    if stale:
                        vms.append(
                            {
                                "name": str(state.get("runner") or path_text),
                                "state": "no_vm_state",
                                "owned": owned,
                                "protected": protected_name,
                                "owner_pid_alive": owner_pid_alive,
                                "age_secs": age,
                                "stale": stale,
                                "action": action,
                                "state_file": path_text,
                            }
                        )
                    continue
                if state_vm in seen_vms:
                    continue
                prefix_ok = starts_with_any(state_vm, prefixes)
                protected_name = is_protected_name(state_vm, protected)
                marker_ok = prefix_ok and not protected_name
                action = ""
                stale = False
                if marker_ok and not owner_pid_alive:
                    stale = True
                    action = "delete_stale_state"
                    if args.fix:
                        try:
                            fixed.append(unlink_state(path_text))
                        except Exception as exc:  # noqa: BLE001
                            problems.append(f"fix_failed:delete_stale_state:{path_text}:{exc}")
                vms.append(
                    {
                        "name": state_vm,
                        "state": "missing",
                        "owned": marker_ok,
                        "protected": protected_name,
                        "owner_pid_alive": owner_pid_alive,
                        "age_secs": age,
                        "stale": stale,
                        "action": action,
                        "state_file": path_text,
                    }
                )
    for state in states:
        if str(state.get("provider") or "") != "qemu-windows":
            continue
        name = str(state.get("vm") or state.get("runner") or "")
        runner_name = str(state.get("runner") or name)
        prefix_ok = starts_with_any(name, prefixes) or starts_with_any(runner_name, prefixes)
        protected_name = is_protected_name(name, protected) or is_protected_name(runner_name, protected)
        owned = prefix_ok and not protected_name
        owner_pid_alive = bool(state.get("_owner_pid_alive"))
        qemu_alive = pid_alive_same(state.get("qemu_pid"), state.get("qemu_pid_started_at"))
        age = int(state.get("_age_secs") or 0)
        keep_failed = bool(state.get("keep_failed"))
        unknown_qemu_pid = ((not state.get("qemu_pid")
                             or not state.get("qemu_pid_started_at"))
                            and bool(state.get("golden_snapshot"))
                            and state.get("phase") in {"preparing", "booting"})
        action = ""
        stale = False

        if not owned:
            if prefix_ok and protected_name:
                problems.append(f"owned_marker_protected_name:{name}")
        elif unknown_qemu_pid:
            action = "qemu_pid_unknown_preserve"
            problems.append(f"qemu_pid_unknown_preserve:{name}")
        elif keep_failed and qemu_alive and age < args.keep_failed_age_secs:
            action = "kept_failed_wait_for_operator"
        elif qemu_alive and not owner_pid_alive and age >= args.running_age_secs:
            stale = True
            action = "stop_delete_qemu_vm"
        elif not qemu_alive and not owner_pid_alive and age >= args.stopped_age_secs:
            stale = True
            action = "delete_qemu_workdir"
        elif owner_pid_alive and age >= args.heartbeat_stale_secs:
            stale = True
            action = "suspect_live_owner_stale_qemu_heartbeat"
            problems.append(f"suspect_live_owner_stale_qemu_heartbeat:{name}")

        if args.fix and action in ("stop_delete_qemu_vm", "delete_qemu_workdir"):
            try:
                if qemu_alive:
                    if not stop_pid(state.get("qemu_pid"), state.get("qemu_pid_started_at")):
                        raise RuntimeError("QEMU still alive; preserving Windows workdir")
                    fixed.append(f"qemu_stopped:{name}:{state.get('qemu_pid')}")
                unseal_windows_snapshot(state)
                for key in ("work_dir", "port_lock"):
                    removed = remove_owned_path(state.get(key), runner_name)
                    if removed:
                        fixed.append(removed)
                if state.get("_path"):
                    fixed.append(unlink_state(str(state.get("_path"))))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"fix_failed:{action}:{name}:{exc}")

        vms.append(
            {
                "name": name,
                "state": "unknown" if unknown_qemu_pid else ("running" if qemu_alive else "stopped"),
                "provider": "qemu-windows",
                "owned": owned,
                "protected": protected_name,
                "owner_pid_alive": owner_pid_alive,
                "qemu_pid_alive": qemu_alive,
                "keep_failed": keep_failed,
                "age_secs": age,
                "stale": stale,
                "action": action,
                "state_file": state.get("_path"),
                "work_dir": state.get("work_dir"),
            }
        )
    if not unreadable:
        try:
            runners = github_runners(
                args.repo,
                args.github_timeout_secs,
                observation_budget,
                problems,
            )
        except ObservationError as exc:
            unreadable.append(exc.problem_code)
        except Exception as exc:  # noqa: BLE001
            unreadable.append(f"github_unreadable:{exc}")
        else:
            for runner in runners:
                name = str(runner.get("name") or "")
                if not starts_with_any(name, prefixes):
                    continue
                # Ephemeral JIT registrations use the per-boot VM name while
                # the state record's `runner` field remains the stable lane
                # identity (for example `pulp-studio-01`).  Match either exact
                # identity before classifying an offline row as ownerless;
                # otherwise the janitor can delete a freshly minted runner in
                # the short offline window before its guest connects.
                state = states_by_runner.get(name)
                if state is None:
                    state = states_by_vm.get(name)
                state_age = int((state or {}).get("_age_secs") or 0) if state else None
                state_provider = str((state or {}).get("provider") or "")
                owner_pid_alive = bool((state or {}).get("_owner_pid_alive")) if state else None
                state_file = (state or {}).get("_path")
                live_fresh_supervisor = (
                    bool(state)
                    and state_provider in tart_providers
                    and owner_pid_alive is True
                    and state_age is not None
                    and state_age < args.heartbeat_stale_secs
                )
                status = str(runner.get("status") or "")
                busy = bool(runner.get("busy"))
                action = ""
                stale = False
                if status == "offline" and not busy:
                    if live_fresh_supervisor:
                        action = "wait_for_live_supervisor"
                    elif state and owner_pid_alive is True:
                        stale = True
                        action = "suspect_live_owner_stale_offline_runner"
                        problems.append(f"suspect_live_owner_stale_offline_runner:{name}")
                    elif refusal := foreign_runner_reason(
                        runner, persistent_runners, host_label_prefixes, own_host_labels,
                        own_name_prefixes,
                    ):
                        action = "keep_not_minted_here"
                        runner["refusal"] = refusal
                    else:
                        # A connected runner reads offline for the seconds its
                        # broker long-poll stalls, so one reading proves
                        # nothing. Delete only a registration seen offline on
                        # several consecutive passes spanning a minimum age.
                        key = offline_key(runner)
                        now_epoch = now.timestamp()
                        seen = dict(offline_tracker.get(key) or {"first_offline": now_epoch, "passes": 0})
                        seen["passes"] = int(seen.get("passes") or 0) + 1
                        offline_seen[key] = seen
                        offline_for = now_epoch - float(seen.get("first_offline") or now_epoch)
                        runner["offline_passes"] = seen["passes"]
                        runner["offline_secs"] = int(offline_for)
                        if (seen["passes"] < args.min_offline_passes
                                or offline_for < args.min_offline_secs):
                            action = "wait_offline_age"
                            runner["refusal"] = "offline_too_recent"
                        else:
                            stale = True
                            action = "delete_offline_runner"
                    if args.fix:
                        if action == "delete_offline_runner":
                            try:
                                fixed.append(
                                    delete_runner(
                                        args.repo,
                                        runner.get("id"),
                                        name,
                                        str(runner.get("_endpoint") or ""),
                                    )
                                )
                            except Exception as exc:  # noqa: BLE001
                                problems.append(f"fix_failed:delete_offline_runner:{name}:{exc}")
                elif status == "offline" and busy:
                    stale = True
                    # `offline + busy` is not enough to delete a runner: the
                    # GitHub row may outlive a guest that is still finishing a
                    # job.  Distinguish the local evidence so an operator or
                    # Shipyard can make a bounded, exact recovery decision.
                    if live_fresh_supervisor:
                        action = "offline_busy_live_local_owner"
                        problems.append(f"offline_busy_live_local_owner:{name}")
                    elif state:
                        action = "offline_busy_unconfirmed_local_state"
                        problems.append(f"offline_busy_unconfirmed_local_state:{name}")
                    else:
                        action = "offline_busy_orphaned_no_local_owner"
                        problems.append(f"offline_busy_orphaned_no_local_owner:{name}")
                runner["owned"] = True
                runner["stale"] = stale
                runner["action"] = action
                runner["heartbeat_age_secs"] = state_age
                runner["owner_pid_alive"] = owner_pid_alive
                runner["state_file"] = state_file

    # Only a complete census may rewrite the offline clocks: a row missing from
    # a failed read is not a runner seen online. A dry run writes nothing.
    if not unreadable and not getattr(args, "dry_run", False):
        try:
            tracker_path.parent.mkdir(parents=True, exist_ok=True)
            tracker_path.write_text(json.dumps(offline_seen, sort_keys=True))
        except OSError as exc:
            problems.append(f"offline_tracker_unwritable:{exc}")

    # The poka-yoke. `matched` counts supervisor heartbeats found in the state
    # files; `expected` counts fleet lanes launchd reports loaded. They come
    # from DIFFERENT sources on purpose, so a tool that goes blind against the
    # state files cannot also silence its own denominator -- which is exactly
    # how "no matching macOS supervisors" came to be printed beside
    # "problems=0" on a host running five lanes.
    supervisor_coverage = fleet_lane_discovery.coverage(
        len(states), lanes, "launchctl"
    )
    coverage_problem = fleet_lane_discovery.coverage_problem(supervisor_coverage)
    if coverage_problem:
        problems.append(coverage_problem)

    digest = {
        "ts": iso(now),
        "host": socket.gethostname(),
        "config": {
            "repo": args.repo,
            "state_dirs": [str(path) for path in state_dirs],
            "prefixes": prefixes,
            "own_name_prefixes": own_name_prefixes,
            "host_labels": own_host_labels,
            "min_offline_secs": args.min_offline_secs,
            "min_offline_passes": args.min_offline_passes,
            "protected_names": protected,
            "fix": args.fix,
            "stopped_age_secs": args.stopped_age_secs,
            "orphan_age_secs": args.orphan_age_secs,
            "running_age_secs": args.running_age_secs,
            "heartbeat_stale_secs": args.heartbeat_stale_secs,
            "keep_failed_age_secs": args.keep_failed_age_secs,
            "tart_timeout_secs": args.tart_timeout_secs,
            "github_timeout_secs": args.github_timeout_secs,
            "observation_timeout_secs": args.observation_timeout_secs,
            # Which Tart store this pass read. A pass over ssh without the
            # lanes' TART_HOME read Tart's default store and saw no gate VM.
            "tart_home": {"path": store["path"], "source": store["source"],
                          "warning": store.get("warning")},
        },
        "capacity": capacity,
        # Lane VMs whose supervisor is gone: what this pass deletes (or, in a
        # dry run, would), and the lane VMs it keeps and why.
        "orphans": {
            "delete": sorted(v["name"] for v in vms if v.get("action") == "delete_orphaned_stopped_vm"),
            "delete_gb": sum(v.get("size_gb") or 0 for v in vms
                             if v.get("action") == "delete_orphaned_stopped_vm"),
            "kept": {v["name"]: v["orphan"] for v in vms
                     if v.get("orphan") and v.get("orphan") != "not_a_lane_vm"
                     and v.get("action") != "delete_orphaned_stopped_vm"},
            "retired_would_delete": sorted(
                v["name"] for v in vms
                if str(v.get("orphan") or "").startswith("retired_clone_reap_off")),
        },
        "supervisor_coverage": supervisor_coverage.as_dict(),
        "supervisors": [
            {
                "runner": state.get("runner"),
                "vm": state.get("vm"),
                "vm_ip": state.get("vm_ip"),
                "phase": state.get("phase"),
                "repo": state.get("repo"),
                "labels": state.get("labels"),
                "run_id": state.get("run_id"),
                "job_id": state.get("job_id"),
                "ts": state.get("ts"),
                "heartbeat_age_secs": state.get("_age_secs"),
                # A fresh heartbeat beside a growing serve-less streak is the
                # signature of a lane that is alive and serving nothing, so the
                # two have to be readable in the same glance.
                "serving_blocked_since": state.get("serving_blocked_since"),
                "serving_blocked_streak": state.get("serving_blocked_streak"),
                "serving_blocked_last_phase": state.get("serving_blocked_last_phase"),
                "supervisor_pid": state.get("supervisor_pid"),
                "supervisor_pid_started_at": state.get("supervisor_pid_started_at"),
                "owner_pid_alive": state.get("_owner_pid_alive"),
                "state_file": state.get("_path"),
            }
            for state in states
        ],
        "vms": vms,
        "github_runners": [
            {
                "id": runner.get("id"),
                "name": runner.get("name"),
                "status": runner.get("status"),
                "busy": runner.get("busy"),
                "labels": [label.get("name") for label in runner.get("labels", []) if isinstance(label, dict)],
                "owned": runner.get("owned", False),
                "stale": runner.get("stale", False),
                "action": runner.get("action", ""),
                "heartbeat_age_secs": runner.get("heartbeat_age_secs"),
                "owner_pid_alive": runner.get("owner_pid_alive"),
                "state_file": runner.get("state_file"),
                "local_ownership": runner.get("action", ""),
                "refusal": runner.get("refusal", ""),
                "offline_passes": runner.get("offline_passes"),
                "offline_secs": runner.get("offline_secs"),
            }
            for runner in runners
            if runner.get("owned")
        ],
        "problems": sorted(set(problems + unreadable)),
        "fixed": fixed,
    }
    if unreadable:
        return digest, 2
    if digest["problems"]:
        return digest, 1
    if any(row.get("stale") and row.get("action") for row in vms) and not args.fix:
        return digest, 1
    if any(row.get("stale") and row.get("action") for row in digest["github_runners"]) and not args.fix:
        return digest, 1
    return digest, 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report or safely reap tartci CI VM residue")
    parser.add_argument("--json", action="store_true", help="emit JSON digest")
    parser.add_argument("--fix", action="store_true", help="perform safe fixes")
    parser.add_argument("--dry-run", action="store_true",
                        help="change nothing (overrides --fix); print the VMs a fix would delete")
    parser.add_argument("--repo", default=os.environ.get("TARTCI_RUNNER_REPO", "Generous-Corp/pulp"))
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("TARTCI_STATE_DIR"),
        help="explicit state dir(s), comma or os.pathsep separated; overrides --state-root",
    )
    parser.add_argument(
        "--state-root",
        default=os.environ.get("TARTCI_STATE_ROOT", str(pathlib.Path.home() / ".tartci/state")),
        help="root containing macos/linux/windows state dirs",
    )
    parser.add_argument("--prefixes", default=os.environ.get("TARTCI_REAP_PREFIXES", "pulp-,linux-ephr-,win-ephr-,tartci-"))
    parser.add_argument("--protected-names", default=os.environ.get("TARTCI_REAP_PROTECTED_NAMES", "pulp-vm,rosetta-probe"))
    parser.add_argument(
        "--persistent-runners",
        default=os.environ.get("TARTCI_REAP_PERSISTENT_RUNNERS", ""),
        help="comma-separated runner names never deleted, whatever their state",
    )
    parser.add_argument(
        "--host-label-prefixes",
        default=os.environ.get("TARTCI_REAP_HOST_LABEL_PREFIXES", "pulp-host-"),
        help="label prefixes that bind a runner to one host; another host's are never deleted",
    )
    parser.add_argument(
        "--host-labels",
        default=os.environ.get("TARTCI_HOST_LABEL", ""),
        help="extra host-bound labels this host owns, beyond those read from its installed "
             "Linux/Windows lane plists (comma-separated)",
    )
    parser.add_argument(
        "--own-name-prefixes",
        default=os.environ.get("TARTCI_REAP_OWN_NAME_PREFIXES", ""),
        help="extra per-boot name prefixes this host mints, beyond its discovered lanes",
    )
    parser.add_argument(
        "--launch-agents-dir",
        default=os.environ.get("TARTCI_REAP_LAUNCH_AGENTS_DIR", ""),
        help="where this host's lane plists are installed (default ~/Library/LaunchAgents "
             "for the whole-host view)",
    )
    parser.add_argument("--min-offline-secs", type=int,
                        default=int(os.environ.get("TARTCI_REAP_MIN_OFFLINE_SECS", "900")))
    parser.add_argument("--min-offline-passes", type=int,
                        default=int(os.environ.get("TARTCI_REAP_MIN_OFFLINE_PASSES", "3")))
    parser.add_argument("--macos-cap", type=int, default=int(os.environ.get("TARTCI_MACOS_VM_CAP", "2")))
    parser.add_argument("--stopped-age-secs", type=int, default=int(os.environ.get("TARTCI_REAP_STOPPED_AGE_SECS", "900")))
    parser.add_argument("--orphan-age-secs", type=int,
                        default=int(os.environ.get("TARTCI_REAP_ORPHAN_AGE_SECS", "3600")),
                        help="minimum untouched age of a lane VM whose supervisor is gone")
    parser.add_argument("--running-age-secs", type=int, default=int(os.environ.get("TARTCI_REAP_RUNNING_AGE_SECS", "10800")))
    parser.add_argument("--heartbeat-stale-secs", type=int, default=int(os.environ.get("TARTCI_REAP_HEARTBEAT_STALE_SECS", "900")))
    parser.add_argument("--keep-failed-age-secs", type=int, default=int(os.environ.get("TARTCI_REAP_KEEP_FAILED_AGE_SECS", "86400")))
    parser.add_argument("--tart-timeout-secs", type=float, default=float(os.environ.get("TARTCI_TART_TIMEOUT_SECS", "15")))
    parser.add_argument("--github-timeout-secs", type=float, default=float(os.environ.get("TARTCI_GH_TIMEOUT_SECS", "15")))
    parser.add_argument("--observation-timeout-secs", type=float, default=float(os.environ.get("TARTCI_OBSERVATION_TIMEOUT_SECS", "60")))
    return parser.parse_args(argv)


def ensure_tart_home() -> dict[str, Any]:
    """Resolve the Tart store and export it, so every `tart` child reads it.

    The dispatcher already exports the fleet profile's store; this covers a
    direct `python3 scripts/vm_reap.py` from a shell with no TART_HOME.
    """
    store = tart_home_resolver.resolve()
    if store["source"] == "profile":
        os.environ["TART_HOME"] = store["path"]
    return store


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.dry_run:
        args.fix = False
    digest, rc = build_digest(args)
    if args.dry_run and not args.json:
        orphans = digest.get("orphans") or {}
        for row in digest.get("vms") or []:
            if row.get("action") and row.get("stale"):
                print(f"would {row['action']}: {row['name']}"
                      + (f" ({row['orphan']})" if row.get("orphan") else ""))
        for name in orphans.get("retired_would_delete", []):
            print(f"would delete once ~/.config/tartci/reap-retired-clones.enabled exists: "
                  f"{name} ({orphans.get('kept', {}).get(name, '')})")
    if args.json:
        print(json.dumps(digest, indent=2, sort_keys=True))
    else:
        print(f"tartci reap — host={digest['host']} problems={len(digest['problems'])} fixed={len(digest['fixed'])}")
        store = (digest.get("config") or {}).get("tart_home") or {}
        if store:
            print(f"  tart store: {store.get('path')} (from {store.get('source')})"
                  + (f" WARNING: {store['warning']}" if store.get("warning") else ""))
        for problem in digest["problems"]:
            print(f"  problem: {problem}")
        for item in digest["fixed"]:
            print(f"  fixed: {item}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
