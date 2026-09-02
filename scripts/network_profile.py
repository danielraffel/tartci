#!/usr/bin/env python3
"""Reconcile an opt-in per-host network profile into tartci LaunchAgents.

The profile is deliberately host-local.  An absent or disabled profile is a
no-op: hosts that have not measured a need for the relay keep their direct
network path.  An enabled profile owns the restricted HTTP CONNECT relay and
the host/guest proxy environment of installed macOS VM controllers.
"""

from __future__ import annotations

import argparse
import ast
import base64
import contextlib
import fcntl
import glob
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # stock macOS Python is 3.9
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
RELAY_LABEL = "com.danielraffel.network.http-connect-ssh-relay"
RELAY_TEMPLATE = ROOT / "launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template"
HOST_PROXY = "http://127.0.0.1:49125"
GUEST_PROXY = "http://192.168.64.1:49125"
GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROXY_ENV = {
    "HTTP_PROXY": HOST_PROXY,
    "HTTPS_PROXY": HOST_PROXY,
    "http_proxy": HOST_PROXY,
    "https_proxy": HOST_PROXY,
    "NO_PROXY": "127.0.0.1,localhost,::1",
    "no_proxy": "127.0.0.1,localhost,::1",
    "TARTCI_GUEST_HTTP_PROXY": GUEST_PROXY,
}
_LAST_TART_VM_PROBE_REASON = "Tart VM inventory unavailable"


@dataclass(frozen=True)
class RelayProfile:
    enabled: bool
    relay_hosts: tuple[str, str] = ("", "")
    github_cli: str = "ghapp"
    github_probe_repo: str = ""
    probe_timeout_seconds: int = 15


def default_profile_path() -> Path:
    return Path(os.environ.get("TARTCI_NETWORK_PROFILE", "~/.config/tartci/network-profile.toml")).expanduser()


def default_agents_dir() -> Path:
    return Path(os.environ.get("TARTCI_LAUNCH_AGENTS_DIR", "~/Library/LaunchAgents")).expanduser()


def applied_receipt_path(profile_path: Path) -> Path:
    return profile_path.with_name(f"{profile_path.stem}.applied.json")


def default_participation_path() -> Path:
    return Path(
        os.environ.get(
            "TARTCI_POOL_PARTICIPATION_FILE",
            "~/.config/tartci/native-build-participation",
        )
    ).expanduser()


def default_pool_lock_path() -> Path:
    return Path(
        os.environ.get(
            "TARTCI_POOL_TRANSITION_LOCK",
            "~/.config/tartci/pool-transition.lock",
        )
    ).expanduser()


def pool_participating(path: Path) -> bool:
    try:
        return path.read_text(encoding="utf-8").strip().lower() not in {
            "0", "false", "off", "draining"
        }
    except OSError:
        return True


def load_profile(path: Path) -> RelayProfile:
    if not path.exists():
        return RelayProfile(enabled=False)
    if tomllib is not None:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    else:
        data = _load_compatible_toml(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("network profile requires schema_version = 1")
    relay = data.get("http_connect_relay")
    if not isinstance(relay, dict):
        raise ValueError("network profile requires [http_connect_relay]")
    enabled = relay.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("http_connect_relay.enabled must be boolean")
    if not enabled:
        return RelayProfile(enabled=False)
    hosts = relay.get("relay_hosts")
    if (
        not isinstance(hosts, list)
        or len(hosts) != 2
        or not all(isinstance(host, str) and host and not host.startswith("-") for host in hosts)
    ):
        raise ValueError("enabled relay requires exactly two non-empty relay_hosts")
    github_cli = relay.get("github_cli", "ghapp")
    probe_repo = relay.get("github_probe_repo")
    timeout = relay.get("probe_timeout_seconds", 15)
    if not isinstance(github_cli, str) or not github_cli:
        raise ValueError("github_cli must be a command name or absolute path")
    if not isinstance(probe_repo, str) or not GITHUB_REPO.fullmatch(probe_repo):
        raise ValueError("enabled relay requires github_probe_repo = OWNER/REPO")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 60:
        raise ValueError("probe_timeout_seconds must be an integer from 1 through 60")
    return RelayProfile(True, (hosts[0], hosts[1]), github_cli, probe_repo, timeout)


def _load_compatible_toml(source: str) -> dict[str, Any]:
    """Parse the deliberately small profile schema on stock macOS Python 3.9."""
    root: dict[str, Any] = {}
    current = root
    for number, raw in enumerate(source.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        section = re.fullmatch(r"\[([A-Za-z0-9_-]+)\]", line)
        if section:
            name = section.group(1)
            value = root.setdefault(name, {})
            if not isinstance(value, dict):
                raise ValueError(f"invalid TOML section on line {number}")
            current = value
            continue
        assignment = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", line)
        if not assignment:
            raise ValueError(f"unsupported TOML syntax on line {number}")
        key, encoded = assignment.groups()
        try:
            if encoded == "true":
                value = True
            elif encoded == "false":
                value = False
            elif re.fullmatch(r"[+-]?[0-9]+", encoded):
                value = int(encoded)
            elif encoded.startswith(('"', "'", "[")):
                value = ast.literal_eval(encoded)
            else:
                raise ValueError
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"unsupported TOML value on line {number}") from exc
        if key in current:
            raise ValueError(f"duplicate TOML key on line {number}: {key}")
        current[key] = value
    return root


def _read_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        value = plistlib.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"plist is not a dictionary: {path}")
    return value


def discover_plists(agents_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    for raw in sorted(glob.glob(str(agents_dir / "*.plist"))):
        path = Path(raw)
        try:
            found.append((path, _read_plist(path)))
        except (OSError, plistlib.InvalidFileException, ValueError):
            continue
    return found


def is_macos_controller(value: dict[str, Any]) -> bool:
    label = value.get("Label", "")
    if not isinstance(label, str) or not (
        label.startswith("com.danielraffel.pulp.tart-runner")
        or label.startswith("com.danielraffel.tartci.tart-runner-")
    ):
        return False
    environment = value.get("EnvironmentVariables", {})
    arguments = value.get("ProgramArguments", [])
    return (
        isinstance(environment, dict) and "TARTCI_MACOS_GOLDEN" in environment
    ) or (
        isinstance(arguments, list)
        and any(isinstance(arg, str) and arg == "macos" for arg in arguments)
    )


def desired_relay_plist(home: Path, profile: RelayProfile) -> dict[str, Any]:
    source = re.sub(rb"<!--.*?-->", b"", RELAY_TEMPLATE.read_bytes(), flags=re.DOTALL)
    value = plistlib.loads(source)

    def replace(item: Any) -> Any:
        if isinstance(item, str):
            return (
                item.replace("$HOME", str(home))
                .replace("$TARTCI_HTTP_RELAY_PRIMARY", profile.relay_hosts[0])
                .replace("$TARTCI_HTTP_RELAY_SECONDARY", profile.relay_hosts[1])
            )
        if isinstance(item, list):
            return [replace(child) for child in item]
        if isinstance(item, dict):
            return {key: replace(child) for key, child in item.items()}
        return item

    rendered = replace(value)
    if rendered.get("Label") != RELAY_LABEL:
        raise ValueError("relay template label invariant changed")
    return rendered


def desired_controller(value: dict[str, Any]) -> dict[str, Any]:
    desired = dict(value)
    environment = dict(desired.get("EnvironmentVariables") or {})
    environment.update(PROXY_ENV)
    desired["EnvironmentVariables"] = environment
    return desired


def _loaded_path(label: str) -> Path | None:
    proc = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        if proc.returncode == 113 and "Could not find service" in proc.stderr:
            return None
        raise OSError(f"launchctl could not determine state for {label}: {proc.stderr.strip() or proc.returncode}")
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if line.startswith("path = "):
            return Path(line.removeprefix("path = "))
    return None


def _plist_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(plistlib.dumps(value, sort_keys=True)).hexdigest()


def _load_receipt(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "agents": {}, "ownership": {"controllers": {}}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid network profile receipt {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(value.get("agents"), dict):
        raise ValueError(f"invalid network profile receipt structure: {path}")
    value.setdefault("ownership", {"controllers": {}})
    if not isinstance(value["ownership"], dict):
        raise ValueError(f"invalid network profile receipt ownership: {path}")
    value["ownership"].setdefault("controllers", {})
    if not isinstance(value["ownership"]["controllers"], dict):
        raise ValueError(f"invalid network profile receipt controllers: {path}")
    return value


def _write_receipt(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(raw, path)
    except BaseException:
        try:
            os.unlink(raw)
        except FileNotFoundError:
            pass
        raise


@contextlib.contextmanager
def mutation_lock(profile_path: Path, timeout_seconds: float = 60.0):
    lock_path = profile_path.with_name("network-profile.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out acquiring {lock_path}")
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def admission_lock(lock_path: Path, already_held: bool = False, timeout_seconds: float = 10.0):
    if already_held:
        yield
        return
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            lock_path.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"pool transition busy: {lock_path}")
            time.sleep(0.1)
    pid_path = lock_path / "pid"
    try:
        pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        yield
    finally:
        try:
            if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_path.unlink()
                lock_path.rmdir()
        except OSError:
            pass


def _atomic_write_plist(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            plistlib.dump(value, fh, sort_keys=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(raw, path)
    except BaseException:
        try:
            os.unlink(raw)
        except FileNotFoundError:
            pass
        raise


def _reload(label: str, path: Path, dry_run: bool) -> bool:
    sys.path.insert(0, str(ROOT / "scripts"))
    import tartci_launchd_watchdog as watchdog  # pylint: disable=import-outside-toplevel

    return watchdog.reload_agent(label, str(path), dry_run=dry_run)


def _unload(label: str, dry_run: bool = False) -> bool:
    if dry_run:
        return True
    sys.path.insert(0, str(ROOT / "scripts"))
    import tartci_launchd_watchdog as watchdog  # pylint: disable=import-outside-toplevel

    loaded_path = _loaded_path(label)
    if loaded_path is None:
        return True
    rc, out, _ = watchdog._run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    if rc != 0:
        return False
    timeout = watchdog.parse_launchctl_exit_timeout(out)
    if timeout is None or timeout == 0:
        return False
    watchdog._run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"])
    return watchdog.wait_until_unloaded(label, timeout_s=timeout + 5.0)


def _any_tart_vm_running() -> bool | None:
    """Compatibility seam retained for existing callers and focused tests."""
    global _LAST_TART_VM_PROBE_REASON
    sys.path.insert(0, str(ROOT / "scripts"))
    import tartci_launchd_watchdog as watchdog  # pylint: disable=import-outside-toplevel

    probe = watchdog.probe_tart_vm_running()
    _LAST_TART_VM_PROBE_REASON = probe.reason
    return probe.running


def _tart_vm_probe_reason() -> str:
    """Return the typed inventory cause when the compatibility seam is unavailable."""
    return _LAST_TART_VM_PROBE_REASON


def authenticated_probe(profile: RelayProfile) -> tuple[bool, str]:
    command = profile.github_cli
    resolved = command if os.path.isabs(command) else shutil.which(command)
    if not resolved:
        return False, f"authenticated probe command not found: {command}"
    env = os.environ.copy()
    env.update({
        "HTTP_PROXY": HOST_PROXY,
        "HTTPS_PROXY": HOST_PROXY,
        "http_proxy": HOST_PROXY,
        "https_proxy": HOST_PROXY,
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
        # ghapp requires explicit repository authority even for installation-
        # scoped endpoints such as rate_limit. Bind both probes to the same
        # profile-declared repository instead of ambient shell state.
        "SHIPYARD_GH_APP_REPO": profile.github_probe_repo,
        "GH_REPO": profile.github_probe_repo,
    })
    try:
        rate = subprocess.run(
            [resolved, "api", "rate_limit", "--jq", ".resources.core.limit"],
            env=env,
            text=True,
            capture_output=True,
            timeout=profile.probe_timeout_seconds,
            check=False,
        )
        if rate.returncode != 0:
            return False, f"authenticated rate-limit probe failed: {rate.stderr.strip() or rate.returncode}"
        try:
            limit = int(rate.stdout.strip())
        except ValueError:
            return False, "authenticated rate-limit probe returned a non-integer limit"
        if limit <= 60:
            return False, f"GitHub probe was anonymous or degraded (limit={limit})"
        repo = subprocess.run(
            [resolved, "api", f"repos/{profile.github_probe_repo}", "--jq", ".full_name"],
            env=env,
            text=True,
            capture_output=True,
            timeout=profile.probe_timeout_seconds,
            check=False,
        )
        if repo.returncode != 0 or repo.stdout.strip().lower() != profile.github_probe_repo.lower():
            return False, f"authenticated repository probe failed for {profile.github_probe_repo}"
    except subprocess.TimeoutExpired:
        return False, f"authenticated relay probe timed out after {profile.probe_timeout_seconds}s"
    return True, f"authenticated GitHub probe passed through {HOST_PROXY} (limit={limit})"


def _capture_relay_ownership(
    receipt: dict[str, Any], relay_path: Path, current: dict[str, Any] | None,
    desired: dict[str, Any], loaded: bool,
) -> None:
    if "relay" in receipt["ownership"]:
        return
    # An exact provisional install is adopted as profile-owned. A different
    # pre-existing relay is preserved byte-for-byte for explicit rollback.
    if current is None or current == desired:
        receipt["ownership"]["relay"] = {"existed": False, "path": str(relay_path)}
        return
    receipt["ownership"]["relay"] = {
        "existed": True,
        "path": str(relay_path),
        "loaded": loaded,
        "plist": base64.b64encode(plistlib.dumps(current, sort_keys=False)).decode("ascii"),
    }


def _capture_controller_ownership(
    receipt: dict[str, Any], label: str, path: Path, current: dict[str, Any]
) -> None:
    controllers = receipt["ownership"]["controllers"]
    if label in controllers:
        # Installers may move a plist while retaining its launchd label. Keep
        # the original environment snapshot but target rollback at the current
        # authoritative plist.
        controllers[label]["path"] = str(path)
        return
    environment = current.get("EnvironmentVariables") or {}
    original: dict[str, Any] = {}
    for key, desired_value in PROXY_ENV.items():
        # Exact provisional values are adopted as profile-owned and therefore
        # removed on rollback. Non-profile values are preserved.
        if environment.get(key) == desired_value:
            original[key] = {"present": False}
        elif key in environment:
            original[key] = {"present": True, "value": environment[key]}
        else:
            original[key] = {"present": False}
    controllers[label] = {"path": str(path), "environment": original}


def _rollback_unlocked(profile_path: Path, agents_dir: Path, participation_path: Path) -> dict[str, Any]:
    receipt_path = applied_receipt_path(profile_path)
    result: dict[str, Any] = {
        "profile": str(profile_path), "enabled": False, "changes": [], "ok": True
    }
    if not receipt_path.exists():
        result["reason"] = "no applied network profile receipt; nothing to roll back"
        return result
    if load_profile(profile_path).enabled:
        result.update(ok=False, reason="disable or remove the network profile before rollback")
        return result
    if pool_participating(participation_path):
        result.update(ok=False, reason="rollback requires tartci pool off")
        return result
    vm_running = _any_tart_vm_running()
    if vm_running is None:
        result.update(ok=False, reason=f"rollback Tart VM probe unavailable: {_tart_vm_probe_reason()}")
        return result
    if vm_running:
        result.update(ok=False, reason="rollback refused while a Tart VM is running")
        return result
    receipt = _load_receipt(receipt_path)
    ownership = receipt.get("ownership", {})
    controllers = ownership.get("controllers", {})
    for label in controllers:
        if _loaded_path(label) is not None:
            result.update(ok=False, reason=f"rollback requires unloaded controller: {label}")
            return result

    for label, state in controllers.items():
        path = Path(state["path"])
        if not path.exists():
            result["changes"].append({"label": label, "action": "skip-removed-controller"})
            continue
        current = _read_plist(path)
        if current.get("Label") != label:
            result["changes"].append({"label": label, "action": "skip-replaced-controller"})
            continue
        environment = dict(current.get("EnvironmentVariables") or {})
        for key, original in state["environment"].items():
            if original.get("present"):
                environment[key] = original["value"]
            else:
                environment.pop(key, None)
        current["EnvironmentVariables"] = environment
        _atomic_write_plist(path, current)
        result["changes"].append({"label": label, "action": "restore-controller-plist"})

    relay = ownership.get("relay")
    if relay:
        relay_path = Path(relay["path"])
        if not _unload(RELAY_LABEL):
            result.update(ok=False, reason="could not unload relay during rollback")
            return result
        if relay.get("existed"):
            original = plistlib.loads(base64.b64decode(relay["plist"]))
            _atomic_write_plist(relay_path, original)
            if relay.get("loaded") and not _reload(RELAY_LABEL, relay_path, False):
                result.update(ok=False, reason="could not restore pre-profile relay")
                return result
        else:
            try:
                relay_path.unlink()
            except FileNotFoundError:
                pass
        result["changes"].append({"label": RELAY_LABEL, "action": "restore-relay"})

    receipt_path.unlink()
    result["reason"] = "network profile ownership rolled back"
    return result


def rollback(profile_path: Path, agents_dir: Path, participation_path: Path) -> dict[str, Any]:
    with admission_lock(default_pool_lock_path()):
        with mutation_lock(profile_path):
            return _rollback_unlocked(profile_path, agents_dir, participation_path)


def _reconcile_unlocked(profile_path: Path, agents_dir: Path, *, dry_run: bool = False,
                        do_probe: bool = True,
                        participation_path: Path | None = None) -> dict[str, Any]:
    profile = load_profile(profile_path)
    receipt_path = applied_receipt_path(profile_path)
    receipt = _load_receipt(receipt_path)
    participating = pool_participating(participation_path or default_participation_path())
    result: dict[str, Any] = {
        "profile": str(profile_path),
        "enabled": profile.enabled,
        "host_proxy": HOST_PROXY if profile.enabled else None,
        "guest_proxy": GUEST_PROXY if profile.enabled else None,
        "changes": [],
        "ok": True,
    }
    if not profile.enabled:
        if receipt_path.exists():
            result.update(
                ok=False,
                reason=(
                    "an applied relay profile cannot be removed or disabled silently; "
                    f"restore {profile_path} before pool admission, then perform an explicit idle rollback"
                ),
            )
            return result
        result["reason"] = "profile absent or relay disabled; direct networking unchanged"
        return result

    plists = discover_plists(agents_dir)
    relays = [(path, value) for path, value in plists if value.get("Label") == RELAY_LABEL]
    if len(relays) > 1:
        raise ValueError(f"multiple relay plists declare {RELAY_LABEL}")
    relay_path = relays[0][0] if relays else agents_dir / f"{RELAY_LABEL}.plist"
    wanted_relay = desired_relay_plist(Path.home(), profile)
    wanted: list[tuple[str, Path, dict[str, Any], bool]] = []
    wanted.append((RELAY_LABEL, relay_path, wanted_relay, not relays or relays[0][1] != wanted_relay))
    for path, value in discover_plists(agents_dir):
        if is_macos_controller(value):
            desired = desired_controller(value)
            wanted.append((str(value["Label"]), path, desired, value != desired))

    plans: list[tuple[str, Path, dict[str, Any], bool, str, bool, Path | None]] = []
    promotions: list[tuple[str, Path, str]] = []
    desired_labels: set[str] = set()
    for label, path, desired, disk_changed in wanted:
        desired_labels.add(label)
        digest = _plist_digest(desired)
        prior = receipt["agents"].get(label, {})
        loaded_path = _loaded_path(label)
        exact_receipt = (
            prior.get("digest") == digest
            and prior.get("path") == str(path)
        )
        applied = exact_receipt and prior.get("state") == "loaded" and loaded_path == path
        if (
            label != RELAY_LABEL
            and participating
            and exact_receipt
            and prior.get("state") == "staged"
            and loaded_path == path
        ):
            # The controller was staged while pool-off, then launchd loaded the
            # exact file during pool-on. Promote without a redundant restart.
            promotions.append((label, path, digest))
            applied = True
        if disk_changed or not applied:
            stage_only = label != RELAY_LABEL and not participating
            plans.append((label, path, desired, disk_changed, digest, stage_only, loaded_path))
            result["changes"].append({
                "label": label,
                "action": "write-and-stage" if stage_only else "write-and-full-reload",
            })

    if plans and not dry_run:
        vm_running = _any_tart_vm_running()
        if vm_running is None:
            result.update(
                ok=False,
                reason=f"Tart VM probe unavailable before network-profile reload: {_tart_vm_probe_reason()}",
            )
            return result
        if vm_running:
            result.update(ok=False, reason="network-profile reload deferred while a Tart VM is running")
            return result

    unexpectedly_loaded = [
        label for label, _, _, _, _, stage_only, loaded_path in plans
        if stage_only and loaded_path is not None
    ]
    if unexpectedly_loaded and not dry_run:
        result.update(
            ok=False,
            reason=(
                "pool-off controller is still loaded; run tartci pool off before staging: "
                + ", ".join(unexpectedly_loaded)
            ),
        )
        return result

    relay_plans = [plan for plan in plans if plan[0] == RELAY_LABEL]
    controller_plans = [plan for plan in plans if plan[0] != RELAY_LABEL]

    for label, path, desired, disk_changed, digest, _, loaded_path in relay_plans:
        if dry_run:
            continue
        _capture_relay_ownership(
            receipt, path, relays[0][1] if relays else None, desired, loaded_path is not None
        )
        receipt["agents"][label] = {"digest": digest, "path": str(path), "state": "pending"}
        _write_receipt(receipt_path, receipt)
        if disk_changed:
            _atomic_write_plist(path, desired)
        if not _reload(label, path, False):
            result.update(ok=False, reason=f"full bootout/bootstrap failed: {label}")
            return result
        receipt["agents"][label] = {"digest": digest, "path": str(path)}
        receipt["agents"][label]["state"] = "loaded"
        _write_receipt(receipt_path, receipt)

    # Prove the relay before any controller is changed to depend on it.
    if not dry_run and do_probe:
        ok, reason = authenticated_probe(profile)
        result["probe"] = reason
        if not ok:
            result.update(ok=False, reason=reason)
            return result
    elif do_probe:
        result["probe"] = "skipped during dry-run"

    current_by_label = {
        str(value.get("Label")): value for _, value in discover_plists(agents_dir)
    }
    if participating and not pool_participating(participation_path or default_participation_path()):
        # Emergency pool-off bypasses the cooperative admission lock. Never
        # resurrect a controller from the stale participating snapshot.
        still_loaded = [label for label, *_ in controller_plans if _loaded_path(label) is not None]
        if still_loaded:
            result.update(
                ok=False,
                reason="pool transitioned off during reconciliation; retry after bootout completes",
            )
            return result
        controller_plans = [
            (label, path, desired, disk_changed, digest, True, loaded_path)
            for label, path, desired, disk_changed, digest, _, loaded_path in controller_plans
        ]
    for label, path, desired, disk_changed, digest, stage_only, _ in controller_plans:
        if dry_run:
            continue
        if not stage_only and not pool_participating(participation_path or default_participation_path()):
            result.update(
                ok=False,
                reason="pool transitioned off before controller reload; retry after bootout completes",
            )
            return result
        _capture_controller_ownership(receipt, label, path, current_by_label[label])
        receipt["agents"][label] = {"digest": digest, "path": str(path), "state": "pending"}
        _write_receipt(receipt_path, receipt)
        if disk_changed:
            _atomic_write_plist(path, desired)
        if stage_only:
            receipt["agents"][label] = {
                "digest": digest,
                "path": str(path),
                "state": "staged",
            }
            _write_receipt(receipt_path, receipt)
            continue
        if not _reload(label, path, False):
            result.update(ok=False, reason=f"full bootout/bootstrap failed: {label}")
            return result
        if not pool_participating(participation_path or default_participation_path()):
            # Emergency off deliberately bypasses the cooperative lock. If it
            # crossed this reload, undo our bootstrap so off remains terminal.
            if not _unload(label):
                result.update(ok=False, reason=f"pool went off and controller could not be unloaded: {label}")
                return result
            receipt["agents"][label] = {
                "digest": digest,
                "path": str(path),
                "state": "staged",
            }
            _write_receipt(receipt_path, receipt)
            result.update(ok=False, reason="pool transitioned off during controller reload; controller unloaded")
            return result
        receipt["agents"][label] = {
            "digest": digest,
            "path": str(path),
            "state": "loaded",
        }
        _write_receipt(receipt_path, receipt)

    for label, path, digest in promotions:
        receipt["agents"][label] = {
            "digest": digest,
            "path": str(path),
            "state": "loaded",
        }

    if not dry_run:
        receipt["agents"] = {
            label: state for label, state in receipt["agents"].items()
            if label in desired_labels
        }
        receipt["ownership"]["controllers"] = {
            label: state
            for label, state in receipt["ownership"]["controllers"].items()
            if label in desired_labels
        }
        _write_receipt(receipt_path, receipt)
    result["controllers"] = [
        str(value["Label"])
        for _, value in discover_plists(agents_dir)
        if is_macos_controller(value)
    ]
    result["reason"] = "opt-in relay profile converged"
    return result


def reconcile(profile_path: Path, agents_dir: Path, *, dry_run: bool = False,
              do_probe: bool = True, participation_path: Path | None = None,
              admission_lock_held: bool = False) -> dict[str, Any]:
    # Preserve the unconfigured-host contract: pool admission and watchdog heal
    # must not create ~/.config/tartci merely to discover there is no profile.
    if not profile_path.exists() and not applied_receipt_path(profile_path).exists():
        return _reconcile_unlocked(
            profile_path, agents_dir, dry_run=True, do_probe=False,
            participation_path=participation_path,
        )
    if dry_run:
        return _reconcile_unlocked(
            profile_path, agents_dir, dry_run=True, do_probe=do_probe,
            participation_path=participation_path,
        )
    with admission_lock(default_pool_lock_path(), admission_lock_held):
        with mutation_lock(profile_path):
            return _reconcile_unlocked(
                profile_path, agents_dir, dry_run=False, do_probe=do_probe,
                participation_path=participation_path,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci network-profile")
    parser.add_argument("command", nargs="?", choices=("status", "reconcile", "rollback"), default="status")
    parser.add_argument("--profile", type=Path, default=default_profile_path())
    parser.add_argument("--launch-agents-dir", type=Path, default=default_agents_dir())
    parser.add_argument("--participation-file", type=Path, default=default_participation_path())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--pool-lock-held", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "rollback":
            if args.dry_run:
                raise ValueError("rollback does not support --dry-run; use status first")
            result = rollback(
                args.profile.expanduser(),
                args.launch_agents_dir.expanduser(),
                args.participation_file.expanduser(),
            )
        else:
            result = reconcile(
                args.profile.expanduser(),
                args.launch_agents_dir.expanduser(),
                dry_run=args.dry_run or args.command == "status",
                do_probe=not args.no_probe and args.command == "reconcile",
                participation_path=args.participation_file.expanduser(),
                admission_lock_held=args.pool_lock_held,
            )
    except (OSError, TimeoutError, ValueError, plistlib.InvalidFileException) as exc:
        result = {"profile": str(args.profile), "enabled": None, "ok": False, "reason": str(exc)}
    if not args.quiet:
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            state = "ok" if result.get("ok") else "FAIL"
            print(f"network-profile: {state}: {result.get('reason', '')}")
            for change in result.get("changes", []):
                print(f"  {change['action']}: {change['label']}")
            if result.get("probe"):
                print(f"  probe: {result['probe']}")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
