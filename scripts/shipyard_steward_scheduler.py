#!/usr/bin/env python3
"""Bounded, single-controller Shipyard carrier scheduler.

The scheduler owns cadence, process isolation, the write-ahead intent, and the
plan ledger. Shipyard's `runner carrier` owns observation and the plan, which
it derives from GitHub facts alone, and performs only the mutations the intent
names and a fresh plan still proposes.

Modes:
  disabled  invoke nothing.
  plan      read GitHub through `runner carrier` and record every plan; zero
            mutations by construction (no `--apply` is ever passed).
  live      as plan, then write the intent for the enabled classes and run one
            `runner carrier --apply` per repository that has an action.
Exactly one host in the fleet may run live; every other install is plan or
disabled.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


SCHEMA_VERSION = 2
INTENT_SCHEMA_VERSION = 1
MODES = ("disabled", "plan", "live")
# update_branch stays planned only until the own-lines invariant exists.
LIVE_CLASSES = ("redispatch", "rearm")
AMBIENT_TOKENS = ("GH_TOKEN", "GITHUB_TOKEN")
MAX_CONFIG_BYTES = 1024 * 1024
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
REPO_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+"
)
GITHUB_REMOTES = (
    re.compile(r"https://github\.com/([^/]+)/([^/]+)"),
    re.compile(r"git@github\.com:([^/]+)/([^/]+)"),
    re.compile(r"ssh://git@github\.com/([^/]+)/([^/]+)"),
)
ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
ACTIVE_MUTATES = False
QUARANTINE_PATH: Path | None = None


class ConfigurationError(ValueError):
    """The trusted scheduler configuration is unsafe or malformed."""


class SchedulerLock:
    """Non-blocking process-wide scheduler exclusion."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.descriptor: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        validate_protected_path(self.path.parent.resolve(), "scheduler lock directory")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            os.close(descriptor)
            raise ConfigurationError("scheduler lock is not a user-owned regular file")
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        os.ftruncate(descriptor, 0)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        self.descriptor = descriptor
        return True

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None


class SchedulerLog:
    """Small append-only operational log with bounded local generations."""

    def __init__(self, path: Path, max_bytes: int, generations: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.generations = generations
        self._prepare()

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() or self.path.is_symlink():
            metadata = self.path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise ConfigurationError("scheduler log is not a user-owned regular file")
            if metadata.st_size >= self.max_bytes:
                oldest = Path(f"{self.path}.{self.generations}")
                oldest.unlink(missing_ok=True)
                for index in range(self.generations - 1, 0, -1):
                    source = Path(f"{self.path}.{index}")
                    if source.exists():
                        os.replace(source, Path(f"{self.path}.{index + 1}"))
                os.replace(self.path, Path(f"{self.path}.1"))
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)

    def write(self, message: str) -> None:
        observed = now()
        with self.path.open("a", encoding="utf-8") as destination:
            destination.write(f"{observed} [steward-scheduler] {message}\n")

    def write_json(self, value: object) -> None:
        """Append one JSON line (the plan ledger's record format)."""
        with self.path.open("a", encoding="utf-8") as destination:
            destination.write(json.dumps(value, sort_keys=True) + "\n")


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temp_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(value, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def publish_quarantine(reason: str) -> None:
    """Durably fence later ticks before attempting mutation-process cleanup."""
    if QUARANTINE_PATH is not None:
        atomic_json(
            QUARANTINE_PATH,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "quarantined",
                "reason": reason,
                "observed_at": now(),
            },
        )


def clear_quarantine() -> None:
    """Remove a completed mutation's fence and durably record its absence."""
    if QUARANTINE_PATH is None:
        return
    QUARANTINE_PATH.unlink(missing_ok=True)
    directory = os.open(QUARANTINE_PATH.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def read_protected_json(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigurationError("scheduler config must be a regular file")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ConfigurationError("scheduler config must be owned by the current user and mode 600")
    if metadata.st_size > MAX_CONFIG_BYTES:
        raise ConfigurationError("scheduler config is too large")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, encoding="utf-8") as source:
            value = json.load(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"scheduler config is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ConfigurationError("scheduler config must contain a JSON object")
    return value


def exact_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be an integer in {minimum}..{maximum}")
    return value


def parse_remote(remote: str) -> str:
    for pattern in GITHUB_REMOTES:
        match = pattern.fullmatch(remote.strip())
        if match:
            repository = match.group(2).removesuffix(".git")
            return f"{match.group(1)}/{repository}"
    raise ConfigurationError("repository checkout has no supported GitHub origin")


def validate_protected_path(path: Path, kind: str, *, executable: bool = False) -> Path:
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ConfigurationError(f"{kind} path must be absolute and canonical")
    target = resolved.stat()
    if target.st_uid not in {0, os.geteuid()} or stat.S_IMODE(target.st_mode) & 0o022:
        raise ConfigurationError(f"{kind} must be protected from other local users")
    if executable and (not stat.S_ISREG(target.st_mode) or not os.access(resolved, os.X_OK)):
        raise ConfigurationError(f"{kind} must be an executable regular file")
    if not executable and not stat.S_ISDIR(target.st_mode):
        raise ConfigurationError(f"{kind} must be a directory")
    for parent in (resolved.parent, *resolved.parents):
        metadata = parent.stat()
        if metadata.st_uid not in {0, os.geteuid()} or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ConfigurationError(f"{kind} parent is writable by another local user: {parent}")
    return resolved


def validate_checkout(repo: str, raw_path: object, *, require_protected: bool) -> Path:
    if not isinstance(raw_path, str):
        raise ConfigurationError(f"checkout path for {repo} must be a string")
    path = Path(raw_path)
    if not path.is_absolute() or path.resolve() != path:
        raise ConfigurationError(f"checkout path for {repo} must be absolute and canonical")
    if require_protected:
        validate_protected_path(path, f"checkout for {repo}")
    completed = subprocess.run(
        ["git", "-C", str(path), "remote", "get-url", "origin"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if completed.returncode != 0 or parse_remote(completed.stdout).casefold() != repo.casefold():
        raise ConfigurationError(f"checkout origin does not match configured repository {repo}")
    return path


def load_config(path: Path) -> dict[str, Any]:
    value = read_protected_json(path)
    expected = {
        "schema_version",
        "mode",
        "authority",
        "classes",
        "shipyard",
        "repositories",
        "carrier_timeout_seconds",
        "max_log_bytes",
        "log_generations",
    }
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError(
            "unsupported scheduler config schema; rerun the installer to write schema 2"
        )
    if set(value) != expected:
        raise ConfigurationError("scheduler config has missing or unknown fields")
    mode = value.get("mode")
    if mode not in MODES:
        raise ConfigurationError("mode must be disabled, plan, or live")
    if type(value.get("authority")) is not bool:
        raise ConfigurationError("authority must be an exact boolean")
    if value["authority"] != (mode == "live"):
        raise ConfigurationError("authority=true is required for live mode and refused otherwise")
    classes = value.get("classes")
    if (
        not isinstance(classes, list)
        or any(entry not in LIVE_CLASSES for entry in classes)
        or len(set(classes)) != len(classes)
    ):
        raise ConfigurationError(f"classes must be unique entries from {', '.join(LIVE_CLASSES)}")
    if (mode == "live") != bool(classes):
        raise ConfigurationError("live mode needs at least one class; other modes take none")
    shipyard = value.get("shipyard")
    if not isinstance(shipyard, str) or not Path(shipyard).is_absolute():
        raise ConfigurationError("shipyard must be an absolute executable path")
    shipyard_path = validate_protected_path(Path(shipyard), "Shipyard executable", executable=True)
    value["shipyard"] = str(shipyard_path)
    repositories = value.get("repositories")
    if not isinstance(repositories, list) or not 1 <= len(repositories) <= 32:
        raise ConfigurationError("repositories must contain 1..32 entries")
    normalized: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in repositories:
        if not isinstance(row, dict) or set(row) != {"repo", "checkout"}:
            raise ConfigurationError("each repository requires exactly repo and checkout")
        repo = row.get("repo")
        folded_repo = repo.casefold() if isinstance(repo, str) else ""
        if not isinstance(repo, str) or not REPO_PATTERN.fullmatch(repo) or folded_repo in seen:
            raise ConfigurationError("repository identities must be canonical and unique")
        seen.add(folded_repo)
        normalized.append(
            {
                "repo": repo,
                "checkout": validate_checkout(
                    repo, row.get("checkout"), require_protected=mode != "disabled"
                ),
            }
        )
    value["repositories"] = normalized
    for name, minimum, maximum in (
        ("carrier_timeout_seconds", 1, 600),
        ("max_log_bytes", 1024, 100 * 1024 * 1024),
        ("log_generations", 1, 20),
    ):
        value[name] = exact_int(value.get(name), name, minimum, maximum)
    return value


def terminate_group(process: subprocess.Popen[bytes]) -> bool:
    """Boundedly terminate a process group and report whether its leader reaped."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # The leader can exit on SIGTERM while descendants survive in its process
    # group. Always sweep the group after the grace period before returning.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return False
    return True


def terminate_active_child(signum: int, _frame: object) -> None:
    """Bind the detached command group to the scheduler service lifecycle."""
    if ACTIVE_PROCESS is not None:
        try:
            if ACTIVE_MUTATES:
                publish_quarantine("scheduler terminated while a mutation command was active")
        finally:
            terminate_group(ACTIVE_PROCESS)
    raise SystemExit(128 + signum)


def drain_bounded_process(
    process: subprocess.Popen[bytes],
    timeout: int,
    *,
    quarantine_on_timeout: bool,
) -> dict[str, Any]:
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, ("stdout", MAX_STDOUT_BYTES))
    selector.register(process.stderr, selectors.EVENT_READ, ("stderr", MAX_STDERR_BYTES))
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    truncated = {"stdout": False, "stderr": False}
    deadline = time.monotonic() + timeout
    timed_out = False
    drain_deadline: float | None = None
    drain_incomplete = False
    termination_incomplete = False
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0 and not timed_out:
            timed_out = True
            try:
                if quarantine_on_timeout:
                    publish_quarantine(
                        "mutation command timed out before descendant termination was proven"
                    )
            finally:
                termination_incomplete |= not terminate_group(process)
            drain_deadline = time.monotonic() + 2
        if drain_deadline is not None and time.monotonic() >= drain_deadline:
            drain_incomplete = True
            for key in list(selector.get_map().values()):
                stream_name, _ = key.data
                truncated[stream_name] = True
                selector.unregister(key.fileobj)
                key.fileobj.close()
            break
        events = selector.select(timeout=0.1 if timed_out else min(0.1, max(remaining, 0)))
        for key, _ in events:
            stream_name, limit = key.data
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            room = limit - len(captured[stream_name])
            if room > 0:
                captured[stream_name].extend(chunk[:room])
            if len(chunk) > max(room, 0):
                truncated[stream_name] = True
    selector.close()
    if process.poll() is None:
        if time.monotonic() >= deadline:
            timed_out = True
            try:
                if quarantine_on_timeout:
                    publish_quarantine(
                        "mutation command timed out before descendant termination was proven"
                    )
            finally:
                termination_incomplete |= not terminate_group(process)
        else:
            try:
                process.wait(timeout=max(deadline - time.monotonic(), 0.001))
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    if quarantine_on_timeout:
                        publish_quarantine(
                            "mutation command timed out before descendant termination was proven"
                        )
                finally:
                    termination_incomplete |= not terminate_group(process)
    return {
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout_truncated": truncated["stdout"],
        "stderr_truncated": truncated["stderr"],
        "drain_incomplete": drain_incomplete,
        "termination_incomplete": termination_incomplete,
        "stdout_bytes": bytes(captured["stdout"]),
        "stderr_bytes": bytes(captured["stderr"]),
    }


def bounded_capture(
    argv: list[str],
    cwd: Path,
    timeout: int,
    *,
    quarantine_on_timeout: bool,
) -> dict[str, Any]:
    global ACTIVE_PROCESS, ACTIVE_MUTATES
    environment = os.environ.copy()
    environment.pop("GH_TOKEN", None)
    environment.pop("GITHUB_TOKEN", None)
    if quarantine_on_timeout:
        # Arm the durable fence before process creation. A crash, signal,
        # timeout, or later permission change therefore blocks future ticks.
        publish_quarantine("mutation command is active; completion is not yet proven")
    handled_signals = {signal.SIGTERM, signal.SIGINT}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)
    try:
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            if quarantine_on_timeout:
                clear_quarantine()
            return {"exit_code": None, "timed_out": False, "error": f"launch failed: {error}"}
        ACTIVE_PROCESS = process
        ACTIVE_MUTATES = quarantine_on_timeout
    finally:
        # A pending termination signal is delivered only after ACTIVE_PROCESS
        # identifies the newly detached process group, closing the spawn race.
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
    try:
        result = drain_bounded_process(
            process,
            timeout,
            quarantine_on_timeout=quarantine_on_timeout,
        )
        if quarantine_on_timeout and not result["timed_out"]:
            clear_quarantine()
        return result
    except BaseException:
        try:
            if quarantine_on_timeout:
                publish_quarantine(
                    "mutation command ended through an unexpected scheduler exception"
                )
        finally:
            terminate_group(process)
        raise
    finally:
        ACTIVE_PROCESS = None
        ACTIVE_MUTATES = False


def run_bounded(
    argv: list[str], cwd: Path, timeout: int, *, quarantine_on_timeout: bool
) -> dict[str, Any]:
    result = bounded_capture(argv, cwd, timeout, quarantine_on_timeout=quarantine_on_timeout)
    if "error" in result:
        return result
    stdout_bytes = result.pop("stdout_bytes")
    stderr_bytes = result.pop("stderr_bytes")
    result["stderr"] = stderr_bytes.decode("utf-8", errors="replace")
    if result.get("drain_incomplete"):
        result["error"] = "command descendants retained output after timeout"
        return result
    if not result["stdout_truncated"]:
        try:
            result["json"] = json.loads(stdout_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            result["error"] = "command did not emit valid bounded JSON"
    else:
        result["error"] = "command JSON exceeded the scheduler output bound"
    return result


def run_bounded_text(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    # Plain-text collection uses the same process-group and byte bounds as JSON
    # commands without invoking the command a second time.
    result = bounded_capture(argv, cwd, timeout, quarantine_on_timeout=False)
    if "error" in result:
        return result
    stdout_bytes = result.pop("stdout_bytes")
    stderr_bytes = result.pop("stderr_bytes")
    result["stdout"] = stdout_bytes.decode("utf-8", errors="replace")
    result["stderr"] = stderr_bytes.decode("utf-8", errors="replace")
    if result.get("drain_incomplete"):
        result["error"] = "command descendants retained output after timeout"
    return result


def carrier_report(result: dict[str, Any], repo: str, *, apply: bool) -> dict[str, Any] | None:
    """The single-repository carrier envelope, or `None` when it is not one."""
    value = result.get("json")
    repos = value.get("repos") if isinstance(value, dict) else None
    if not (
        result.get("timed_out") is False
        and result.get("exit_code") in (0, 1)
        and isinstance(value, dict)
        and value.get("schema_version") == 1
        and value.get("command") == "runner.carrier"
        and value.get("apply") is apply
        and isinstance(repos, list)
        and len(repos) == 1
        and isinstance(repos[0], dict)
        and isinstance(repos[0].get("repo"), str)
        and repos[0]["repo"].casefold() == repo.casefold()
        and isinstance(repos[0].get("prs"), list)
        and isinstance(repos[0].get("errors"), list)
    ):
        return None
    return repos[0]


def public_result(result: dict[str, Any], valid: bool) -> dict[str, Any]:
    return {
        "status": "ok" if valid else "error",
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out", False),
        "stdout_truncated": result.get("stdout_truncated", False),
        "stderr_truncated": result.get("stderr_truncated", False),
        "drain_incomplete": result.get("drain_incomplete", False),
        "termination_incomplete": result.get("termination_incomplete", False),
        "error": result.get("error"),
    }


def check_capability(shipyard: str, cwd: Path) -> tuple[bool, str]:
    """Prove this Shipyard has the carrier interface the scheduler drives.

    Replaying an empty fact file exercises the exact JSON envelope without any
    GitHub read, so it is a capability probe rather than a version guess.
    """
    result = run_bounded_text(
        [shipyard, "--json", "runner", "carrier", "--replay", "/dev/null"], cwd, 30
    )
    if result.get("timed_out") or result.get("exit_code") != 0:
        return False, "Shipyard lacks `runner carrier`; a newer Shipyard is required"
    try:
        value = json.loads(str(result.get("stdout", "")))
    except json.JSONDecodeError:
        return False, "Shipyard `runner carrier --replay` did not emit JSON"
    if value.get("command") != "runner.carrier" or value.get("plans") != []:
        return False, "Shipyard `runner carrier --replay` emitted an unexpected envelope"
    return True, "runner carrier available"


def probe_child_environment(cwd: Path) -> dict[str, bool]:
    """Which ambient GitHub tokens a child process launched by this tick sees.

    The probe runs through the same launch path as every Shipyard command, so a
    regression in token stripping shows here as `true`. Only names are read;
    values never enter the report.
    """
    result = run_bounded_text(["/usr/bin/env"], cwd, 10)
    names = {
        line.split("=", 1)[0]
        for line in str(result.get("stdout", "")).splitlines()
        if "=" in line
    }
    return {token: token in names for token in AMBIENT_TOKENS}


def summarize_plan(planned: dict[str, Any]) -> dict[str, Any]:
    """One plan for the ledger: every decision, and the facts behind actions."""
    row = {key: value for key, value in planned.items() if key not in {"facts"}}
    if planned.get("decision") == "propose":
        row["facts"] = planned.get("facts")
    return row


def intent_actions(repo: str, prs: list[Any], classes: list[str]) -> list[dict[str, Any]]:
    actions = []
    for planned in prs:
        if not isinstance(planned, dict) or planned.get("decision") != "propose":
            continue
        if planned.get("action") not in classes:
            continue
        action = {
            "repo": repo,
            "number": planned.get("number"),
            "head_sha": planned.get("head_sha"),
            "action": planned.get("action"),
        }
        for key in ("head", "run_ids"):
            if key in planned:
                action[key] = planned[key]
        actions.append(action)
    return actions


def scheduler(
    config: dict[str, Any],
    logger: SchedulerLog,
    plans: SchedulerLog,
    intent_path: Path,
) -> tuple[int, dict[str, Any]]:
    started = now()
    mode = config["mode"]
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "started_at": started,
        "completed_at": None,
        "mode": mode,
        "authority": config["authority"],
        "classes": config["classes"],
        "status": "disabled",
        "repositories": [],
        "proposals": 0,
        "mutations": 0,
    }
    if mode == "disabled":
        logger.write("disabled by trusted config; no Shipyard or GitHub command invoked")
        report["completed_at"] = now()
        return 0, report

    shipyard = str(config["shipyard"])
    repositories = config["repositories"]
    capable, detail = check_capability(shipyard, repositories[0]["checkout"])
    report["shipyard_capability"] = detail
    probe = probe_child_environment(repositories[0]["checkout"])
    report["child_environment_tokens"] = probe
    if not capable or any(probe.values()):
        report["status"] = "unhealthy"
        report["error"] = detail if not capable else "an ambient GitHub token reached a child process"
        report["completed_at"] = now()
        logger.write(report["error"])
        return 1, report

    healthy = True
    pending: list[tuple[dict[str, object], list[dict[str, Any]]]] = []
    for row in repositories:
        repo = str(row["repo"])
        result = run_bounded(
            [shipyard, "--json", "runner", "carrier", "--repo", repo],
            row["checkout"],
            config["carrier_timeout_seconds"],
            quarantine_on_timeout=False,
        )
        envelope = carrier_report(result, repo, apply=False)
        mutated = envelope is not None and any(
            isinstance(planned, dict) and "mutation" in planned for planned in envelope["prs"]
        )
        valid = envelope is not None and not mutated and result.get("exit_code") == 0
        entry = {"repo": repo, "pass": "plan", **public_result(result, valid)}
        if mutated:
            entry["error"] = "a plan pass reported a mutation"
        if envelope is not None:
            entry["errors"] = envelope["errors"][:20]
            proposals = [p for p in envelope["prs"] if isinstance(p, dict) and p.get("decision") == "propose"]
            entry["prs"] = len(envelope["prs"])
            entry["proposals"] = len(proposals)
            report["proposals"] += len(proposals)
            plans.write_json(
                {
                    "tick": started,
                    "mode": mode,
                    "repo": repo,
                    "plans": [summarize_plan(p) for p in envelope["prs"] if isinstance(p, dict)],
                    "errors": envelope["errors"][:20],
                }
            )
            if mode == "live" and not mutated:
                actions = intent_actions(repo, envelope["prs"], config["classes"])
                if actions:
                    pending.append((row, actions))
        healthy &= valid
        report["repositories"].append(entry)
        logger.write(f"carrier plan for {repo}: {'ok' if valid else 'error'}")

    if pending:
        intent = {
            "schema_version": INTENT_SCHEMA_VERSION,
            "tick": started,
            "classes": config["classes"],
            "actions": [action for _, actions in pending for action in actions],
        }
        # Write-ahead: the intent names every action before any is attempted,
        # so a timeout or crash leaves a record of what did not complete.
        atomic_json(intent_path, intent)
        plans.write_json({"tick": started, "mode": mode, "intent": intent})
        for row, actions in pending:
            repo = str(row["repo"])
            argv = [shipyard, "--json", "runner", "carrier", "--repo", repo, "--apply"]
            for entry_class in config["classes"]:
                argv.extend(["--class", entry_class])
            argv.extend(["--intent", str(intent_path)])
            logger.write(f"applying {len(actions)} intended action(s) for {repo}")
            result = run_bounded(
                argv, row["checkout"], config["carrier_timeout_seconds"], quarantine_on_timeout=True
            )
            envelope = carrier_report(result, repo, apply=True)
            valid = envelope is not None and result.get("exit_code") == 0
            entry = {"repo": repo, "pass": "apply", **public_result(result, valid)}
            if envelope is not None:
                outcomes = [
                    {
                        "number": p.get("number"),
                        "head_sha": p.get("head_sha"),
                        "mutation": p.get("mutation"),
                        "error": p.get("error"),
                    }
                    for p in envelope["prs"]
                    if isinstance(p, dict) and ("mutation" in p or "error" in p)
                ]
                entry["outcomes"] = outcomes
                report["mutations"] += sum(
                    1
                    for outcome in outcomes
                    if outcome["mutation"] and not str(outcome["mutation"]).startswith("not applied")
                )
                plans.write_json({"tick": started, "mode": mode, "repo": repo, "outcomes": outcomes})
            healthy &= valid
            report["repositories"].append(entry)
            if result.get("timed_out"):
                report["status"] = "quarantined"
                report["error"] = (
                    "mutation command timed out without complete descendant-termination proof"
                )
                report["completed_at"] = now()
                logger.write("quarantining scheduler after an apply timeout")
                return 1, report
        atomic_json(intent_path, {**intent, "completed_at": now()})

    report["status"] = "healthy" if healthy else "unhealthy"
    report["completed_at"] = now()
    return (0 if healthy else 1), report


def parse_args() -> argparse.Namespace:
    home = Path.home()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=home / ".config/shipyard/steward-scheduler.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=home / "Library/Logs/shipyard-steward-scheduler.report.json",
    )
    parser.add_argument(
        "--health",
        type=Path,
        default=home / "Library/Logs/shipyard-steward-scheduler.health.json",
    )
    parser.add_argument(
        "--startup",
        type=Path,
        default=home / "Library/Logs/shipyard-steward-scheduler.startup.json",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=home / "Library/Logs/shipyard-steward-scheduler.log",
    )
    parser.add_argument(
        "--lock",
        type=Path,
        default=home / ".local/state/tartci/shipyard-steward-scheduler.lock",
    )
    parser.add_argument(
        "--quarantine",
        type=Path,
        default=home / ".local/state/tartci/shipyard-steward-scheduler.quarantine.json",
    )
    parser.add_argument(
        "--intent",
        type=Path,
        default=home / ".local/state/tartci/shipyard-steward-scheduler.intent.json",
    )
    parser.add_argument(
        "--plans",
        type=Path,
        default=home / "Library/Logs/shipyard-steward-scheduler.plans.jsonl",
    )
    return parser.parse_args()


def main() -> int:
    global QUARANTINE_PATH
    args = parse_args()
    QUARANTINE_PATH = args.quarantine
    signal.signal(signal.SIGTERM, terminate_active_child)
    signal.signal(signal.SIGINT, terminate_active_child)
    lock = SchedulerLock(args.lock)
    try:
        if not lock.acquire():
            print("steward scheduler: another tick holds the scheduler lock", file=sys.stderr)
            return 0
        config = load_config(args.config)
        logger = SchedulerLog(args.log, config["max_log_bytes"], config["log_generations"])
        plans = SchedulerLog(args.plans, config["max_log_bytes"], config["log_generations"])
        args.quarantine.parent.mkdir(parents=True, exist_ok=True)
        validate_protected_path(args.quarantine.parent.resolve(), "scheduler quarantine directory")
        if config["mode"] != "disabled" and args.quarantine.exists():
            failure = {
                "schema_version": SCHEMA_VERSION,
                "status": "quarantined",
                "mode": config["mode"],
                "reason": "prior command termination requires explicit descendant-clearance proof",
                "observed_at": now(),
            }
            atomic_json(args.report, failure)
            atomic_json(args.health, failure)
            logger.write("quarantine present; refusing every carrier pass")
            return 2
        atomic_json(
            args.startup,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "started",
                "mode": config["mode"],
                "authority": config["authority"],
                "classes": config["classes"],
                "observed_at": now(),
            },
        )
        exit_code, report = scheduler(config, logger, plans, args.intent)
        if report["status"] == "quarantined":
            atomic_json(
                args.quarantine,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "quarantined",
                    "reason": report["error"],
                    "observed_at": report["completed_at"],
                },
            )
        atomic_json(args.report, report)
        atomic_json(
            args.health,
            {
                "schema_version": SCHEMA_VERSION,
                "status": report["status"],
                "mode": report["mode"],
                "classes": report["classes"],
                "proposals": report["proposals"],
                "mutations": report["mutations"],
                "reason": report.get("error", report["status"]),
                "observed_at": report["completed_at"],
            },
        )
        return exit_code
    except (ConfigurationError, FileNotFoundError, OSError, subprocess.SubprocessError) as error:
        failure = {
            "schema_version": SCHEMA_VERSION,
            "status": "unhealthy",
            "reason": str(error),
            "observed_at": now(),
        }
        for receipt in (args.report, args.health):
            try:
                atomic_json(receipt, failure)
            except OSError:
                pass
        print(f"steward scheduler: {error}", file=sys.stderr)
        return 2
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
