#!/usr/bin/env python3
"""Bounded, single-controller Shipyard stewardship scheduler.

The scheduler owns cadence and process isolation only. Shipyard remains the
authority for exact-head observation, queue mutations, retry policy, and the
trusted recovery-worker contract.
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


SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 1024 * 1024
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
REPO_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]+"
)
VERSION_PATTERN = re.compile(r"shipyard (\d+)\.(\d+)\.(\d+)")
GITHUB_REMOTES = (
    re.compile(r"https://github\.com/([^/]+)/([^/]+)"),
    re.compile(r"git@github\.com:([^/]+)/([^/]+)"),
    re.compile(r"ssh://git@github\.com/([^/]+)/([^/]+)"),
)
ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
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
        "enabled",
        "authority",
        "shipyard",
        "repositories",
        "steward_timeout_seconds",
        "recovery_timeout_seconds",
        "max_log_bytes",
        "log_generations",
    }
    if set(value) != expected:
        raise ConfigurationError("scheduler config has missing or unknown fields")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ConfigurationError("unsupported scheduler config schema")
    if type(value.get("enabled")) is not bool or type(value.get("authority")) is not bool:
        raise ConfigurationError("enabled and authority must be exact booleans")
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
                    repo, row.get("checkout"), require_protected=value["enabled"]
                ),
            }
        )
    value["repositories"] = normalized
    for name, minimum, maximum in (
        ("steward_timeout_seconds", 1, 600),
        ("recovery_timeout_seconds", 1, 3600),
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
    global ACTIVE_PROCESS
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


def run_bounded(argv: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    result = bounded_capture(argv, cwd, timeout, quarantine_on_timeout=True)
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


def valid_steward_report(result: dict[str, Any], repo: str) -> bool:
    value = result.get("json")
    repos = value.get("repos") if isinstance(value, dict) else None
    return (
        result.get("exit_code") == 0
        and result.get("timed_out") is False
        and isinstance(value, dict)
        and value.get("schema_version") == 1
        and value.get("command") == "runner.steward"
        and value.get("apply") is True
        and isinstance(value.get("handoff_ledger"), str)
        and isinstance(repos, list)
        and len(repos) == 1
        and isinstance(repos[0], dict)
        and repos[0].get("repo") == repo
        and repos[0].get("errors") == []
    )


def valid_recovery_report(result: dict[str, Any]) -> bool:
    value = result.get("json")
    return (
        result.get("exit_code") == 0
        and result.get("timed_out") is False
        and isinstance(value, dict)
        and value.get("schema_version") == 1
        and value.get("command") == "runner:recovery-worker"
        and value.get("apply") is True
        and isinstance(value.get("requests"), list)
    )


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


def check_version(shipyard: str, cwd: Path) -> tuple[bool, str]:
    result = run_bounded_text([shipyard, "--version"], cwd, 15)
    if result.get("timed_out") or result.get("exit_code") != 0:
        return False, "Shipyard version check failed"
    match = VERSION_PATTERN.fullmatch(str(result.get("stdout", "")).strip())
    if not match or tuple(map(int, match.groups())) < (0, 113, 0):
        return False, "Shipyard 0.113.0 or newer is required"
    return True, str(result["stdout"]).strip()


def scheduler(config: dict[str, Any], logger: SchedulerLog) -> tuple[int, dict[str, Any]]:
    started = now()
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "started_at": started,
        "completed_at": None,
        "enabled": config["enabled"],
        "authority": config["authority"],
        "status": "disabled",
        "repositories": [],
        "recovery": {"attempted": False, "status": "not_run"},
    }
    if not config["enabled"]:
        logger.write("disabled by trusted config; no Shipyard or GitHub command invoked")
        report["completed_at"] = now()
        return 0, report
    if not config["authority"]:
        report["status"] = "unhealthy"
        report["error"] = "enabled scheduler requires explicit authority=true"
        report["completed_at"] = now()
        logger.write(report["error"])
        return 1, report

    shipyard = str(config["shipyard"])
    repositories = config["repositories"]
    version_ok, version = check_version(shipyard, repositories[0]["checkout"])
    report["shipyard_version"] = version
    if not version_ok:
        report["status"] = "unhealthy"
        report["error"] = version
        report["completed_at"] = now()
        logger.write(version)
        return 1, report

    healthy = True
    for row in repositories:
        repo = str(row["repo"])
        logger.write(f"starting deterministic stewardship for {repo}")
        result = run_bounded(
            [shipyard, "--json", "runner", "steward", "--repo", repo, "--apply"],
            row["checkout"],
            config["steward_timeout_seconds"],
        )
        valid = valid_steward_report(result, repo)
        healthy &= valid
        report["repositories"].append({"repo": repo, **public_result(result, valid)})
        logger.write(f"deterministic stewardship for {repo}: {'ok' if valid else 'error'}")
        if result.get("timed_out"):
            report["status"] = "quarantined"
            report["error"] = "mutation command timed out without complete descendant-termination proof"
            report["completed_at"] = now()
            logger.write("quarantining scheduler before peer or recovery mutation")
            return 1, report

    # Recovery is deliberately sequenced after every deterministic repository
    # pass and invoked once. Shipyard's trusted worker owns request selection,
    # exact-head revalidation, deduplication, and any model process.
    logger.write("starting at-most-one trusted recovery-worker pass")
    recovery = run_bounded(
        [shipyard, "--json", "runner", "recovery-worker", "--once", "--apply"],
        repositories[0]["checkout"],
        config["recovery_timeout_seconds"],
    )
    recovery_valid = valid_recovery_report(recovery)
    report["recovery"] = {"attempted": True, **public_result(recovery, recovery_valid)}
    logger.write(f"trusted recovery-worker pass: {'ok' if recovery_valid else 'error'}")
    if recovery.get("timed_out"):
        report["status"] = "quarantined"
        report["error"] = "recovery command timed out without complete descendant-termination proof"
        report["completed_at"] = now()
        logger.write("quarantining scheduler after escaped recovery descendant")
        return 1, report
    healthy &= recovery_valid
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
            return 0
        config = load_config(args.config)
        logger = SchedulerLog(args.log, config["max_log_bytes"], config["log_generations"])
        args.quarantine.parent.mkdir(parents=True, exist_ok=True)
        validate_protected_path(args.quarantine.parent.resolve(), "scheduler quarantine directory")
        if config["enabled"] and args.quarantine.exists():
            failure = {
                "schema_version": SCHEMA_VERSION,
                "status": "quarantined",
                "reason": "prior command termination requires explicit descendant-clearance proof",
                "observed_at": now(),
            }
            atomic_json(args.report, failure)
            atomic_json(args.health, failure)
            logger.write("quarantine present; refusing all stewardship and recovery mutations")
            return 2
        atomic_json(
            args.startup,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "started",
                "enabled": config["enabled"],
                "authority": config["authority"],
                "observed_at": now(),
            },
        )
        exit_code, report = scheduler(config, logger)
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
