#!/usr/bin/env python3
"""Keep a fleet host on the tartci code that main has, verified.

`tartci fleet-macos self-update [--target REF] [--plan] [--apply]` codifies the
manual procedure that brought m1, m5 and m3 up to date on 2026-09-23. It adds
no new mechanics; every step is a command that already exists:

  1. Measure skew. The installed commit is the EXECUTED cohort: the sealed
     launcher bundle's source_commit on a host whose profile has
     [launch_helper], otherwise the installed generation's manifest commit.
     Main is read from a tartci-owned checkout (never a shared, possibly
     dirty working copy). The target is the newest FIRST-PARENT main commit
     older than the soak; a PR-internal commit is never a target.
  2. Prepare from that checkout, read-only for the host: support-manifest
     write, fleet-macos validate, install dry-run, and on a sealed host the
     launcher build (signing identity EXTRACTED from the live bundle's leaf
     certificate) plus its verification. Nothing on the host changes yet.
  3. One host at a time: announce, then read every other published host's
     pool state and self-update marker over SSH; any peer not `on`, or
     updating, refuses. Two hosts that announce together both see each other
     and the lower host id proceeds.
  4. Capacity floor: drain passes --allow-last-serving-host ONLY when every
     last-serving label is on the explicit idle-by-design list, and the rule
     is written to the receipt. Capacity unknown refuses.
  5. drain, wait (bounded) for `pool off --plan` to show no mid-job lane,
     pool off, pin the new launcher approval (backed up), install --apply
     (retried while agents unload), relay reconcile, `pool on` through the
     INSTALLED shim, then verify.
  6. Any failure after the drain restores the approval pin and runs `pool
     on` through the installed shim, so the host is left serving on the
     previous generation, and records the failure loudly.

Every external effect goes through `System`, so the whole procedure runs
against fakes in the tests. The plan mode performs steps 1-4 read-only.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - the launchd python is 3.9
    tomllib = None  # type: ignore[assignment]

SCHEMA = "tartci.self-update/v1"
REPO_URL = "https://github.com/danielraffel/tartci.git"
SHA = re.compile(r"^[0-9a-f]{40}$")
DEFAULT_SOAK_SECONDS = 1800
DEFAULT_RATE_HOURS = 6
DEFAULT_STALE_HOURS = 24
DEFAULT_WAIT_SECONDS = 90 * 60
DEFAULT_POLL_SECONDS = 45
INSTALL_ATTEMPTS = 4
INSTALL_RETRY_SECONDS = 30
ACTIVE_MARKER_TTL = 3 * 3600
# Lanes whose only work is occasional by design: a host that is their last
# server may still be taken down for an update, because an idle release lane
# queues nothing while it is gone. Anything else that is last-serving refuses.
DEFAULT_IDLE_BY_DESIGN = ("pulp-release-tagged", "pulp-release-pr-gate")

# Exit codes.
EXIT_OK = 0            # updated, already current, or plan says it would proceed
EXIT_NOTHING = 0
EXIT_REFUSED = 3       # a precondition refused; the host was not touched
EXIT_FAILED = 4        # a mutation ran and failed; host restored to `on`
EXIT_UNKNOWN = 5       # skew or installed state could not be determined


class Refused(Exception):
    """A precondition failed before anything on the host changed."""


class Failed(Exception):
    """A step failed after the host was taken out of service."""


@dataclasses.dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def text(self) -> str:
        return (self.err or self.out).strip()


class System:
    """Every side effect. Tests replace it."""

    def run(self, argv: list[str], *, cwd: str | None = None,
            env: dict[str, str] | None = None, timeout: float = 900) -> Result:
        merged = {**os.environ, **(env or {})}
        try:
            proc = subprocess.run(argv, cwd=cwd, env=merged, capture_output=True,
                                  text=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return Result(127, "", f"{type(exc).__name__}: {exc}")
        return Result(proc.returncode, proc.stdout, proc.stderr)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.time()


@dataclasses.dataclass
class Config:
    home: Path
    soak_seconds: int = DEFAULT_SOAK_SECONDS
    rate_hours: float = DEFAULT_RATE_HOURS
    stale_hours: float = DEFAULT_STALE_HOURS
    wait_seconds: int = DEFAULT_WAIT_SECONDS
    poll_seconds: int = DEFAULT_POLL_SECONDS
    idle_by_design: tuple[str, ...] = DEFAULT_IDLE_BY_DESIGN
    peers: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def checkout(self) -> Path:
        return self.home / ".local" / "share" / "tartci" / "update-checkout"

    @property
    def state_dir(self) -> Path:
        return state_dir_for(self.home)

    @property
    def shim(self) -> Path:
        return self.home / ".local" / "bin" / "tartci"

    @property
    def installed_profile(self) -> Path:
        return self.home / ".config" / "tartci" / "macos-fleet-profile.toml"

    @property
    def install_receipt(self) -> Path:
        return self.home / ".config" / "tartci" / "macos-fleet-install.json"

    @property
    def network_profile(self) -> Path:
        return self.home / ".config" / "tartci" / "network-profile.toml"

    @property
    def settings(self) -> Path:
        return self.home / ".config" / "tartci" / "self-update.toml"


def state_dir_for(home: Path) -> Path:
    root = os.environ.get("TARTCI_HOME") or str(home / ".tartci")
    return Path(root) / "state" / "self-update"


def summary(home: Path | None = None) -> dict:
    """Cached skew + last attempt for status surfaces. Never fetches or raises."""
    state = state_dir_for(home or Path.home())
    skew = _read_json(state / "skew.json")
    last = _read_json(state / "last.json")
    problem = None
    if skew is None:
        problem = None  # never measured: reported, but not a warning by itself
    elif skew.get("state") in ("unknown", "diverged"):
        problem = f"skew {skew.get('state')}: {skew.get('reason')}"
    elif skew.get("stale"):
        problem = f"{skew.get('behind')} commits behind main since {skew.get('oldest_undeployed')}"
    if last and last.get("status") == "failed":
        failed = f"last self-update FAILED for {str(last.get('target'))[:12]}: {last.get('error')}"
        problem = f"{problem}; {failed}" if problem else failed
    return {"skew": skew, "last": last, "lines": status_lines(state), "problem": problem}


def load_config(home: Path, settings: Path | None = None) -> Config:
    """Defaults, overridden by ~/.config/tartci/self-update.toml when present.

    [peers] maps each other published host_id to an SSH target; one-at-a-time
    needs to read every peer, so an unmapped peer refuses rather than guessing.
    """
    cfg = Config(home=home)
    path = settings or cfg.settings
    if path.is_file() and tomllib is not None:
        data = tomllib.loads(path.read_text())
        cfg.peers = {str(k): str(v) for k, v in (data.get("peers") or {}).items()}
        for key in ("soak_seconds", "wait_seconds", "poll_seconds"):
            if isinstance(data.get(key), int):
                setattr(cfg, key, data[key])
        for key in ("rate_hours", "stale_hours"):
            if isinstance(data.get(key), (int, float)):
                setattr(cfg, key, float(data[key]))
        if isinstance(data.get("idle_by_design_labels"), list):
            cfg.idle_by_design = tuple(str(v) for v in data["idle_by_design_labels"])
    return cfg


def _toml(path: Path) -> dict:
    if tomllib is None:
        raise Refused("Python 3.11+ with tomllib is required")
    return tomllib.loads(path.read_text())


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


# ── installed state and skew ───────────────────────────────────────────────

def launch_helper(cfg: Config) -> dict | None:
    try:
        helper = _toml(cfg.installed_profile).get("launch_helper")
    except (OSError, ValueError) as exc:
        raise Refused(f"installed profile unreadable: {exc}") from exc
    return helper if isinstance(helper, dict) else None


def installed_commit(cfg: Config) -> tuple[str, str]:
    """(commit, source) of the cohort this host EXECUTES. Raises Refused."""
    helper = launch_helper(cfg)
    if helper is not None:
        path = Path(helper["path"]) / "Contents" / "Resources" / "bundle.json"
        value = _read_json(path)
        commit = (value or {}).get("source_commit")
        source = f"sealed launcher {path}"
    else:
        value = _read_json(cfg.install_receipt)
        support = (value or {}).get("support")
        commit = support.get("source_commit") if isinstance(support, dict) else None
        source = f"installed generation {cfg.install_receipt}"
    if not isinstance(commit, str) or not SHA.fullmatch(commit):
        raise Refused(f"installed commit unknown (no source_commit in {source})")
    return commit, source


def refresh_checkout(cfg: Config, sys_: System) -> None:
    """Fetch main into the tartci-owned checkout, creating it if needed."""
    path = cfg.checkout
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        result = sys_.run(["git", "clone", "--quiet", "--no-checkout", REPO_URL, str(path)])
        if result.rc != 0:
            raise Refused(f"cannot create the update checkout: {result.text}")
    origin = sys_.run(["git", "-C", str(path), "remote", "get-url", "origin"])
    if origin.rc != 0 or origin.out.strip().rstrip("/").removesuffix(".git").lower() \
            != REPO_URL.removesuffix(".git").lower():
        raise Refused(f"update checkout {path} is not a danielraffel/tartci clone")
    fetched = sys_.run(["git", "-C", str(path), "fetch", "--quiet", "--prune", "origin", "main"])
    if fetched.rc != 0:
        raise Refused(f"git fetch failed: {fetched.text}")


def measure_skew(cfg: Config, sys_: System, installed: str, now: float,
                 target_ref: str = "origin/main") -> dict:
    """First-parent commits on main the host does not run, and the soak target."""
    git = ["git", "-C", str(cfg.checkout)]
    skew: dict[str, Any] = {"installed": installed, "measured_at": _iso(now),
                            "state": "unknown", "behind": None, "oldest_undeployed": None,
                            "target": None, "soak_seconds": cfg.soak_seconds}
    head = sys_.run([*git, "rev-parse", "--verify", f"{target_ref}^{{commit}}"])
    if head.rc != 0 or not SHA.fullmatch(head.out.strip()):
        skew["reason"] = f"cannot resolve {target_ref}: {head.text}"
        return skew
    skew["main"] = head.out.strip()
    if sys_.run([*git, "cat-file", "-e", f"{installed}^{{commit}}"]).rc != 0:
        skew["reason"] = f"installed commit {installed[:12]} is not in the tartci history"
        return skew
    if sys_.run([*git, "merge-base", "--is-ancestor", installed, skew["main"]]).rc != 0:
        skew.update(state="diverged",
                    reason=f"installed {installed[:12]} is not an ancestor of {target_ref}")
        return skew
    log = sys_.run([*git, "log", "--first-parent", "--format=%H %ct",
                    f"{installed}..{skew['main']}"])
    if log.rc != 0:
        skew["reason"] = f"git log failed: {log.text}"
        return skew
    commits = []
    for line in log.out.splitlines():
        parts = line.split()
        if len(parts) == 2 and SHA.fullmatch(parts[0]) and parts[1].isdigit():
            commits.append((parts[0], int(parts[1])))
    skew["behind"] = len(commits)
    if not commits:
        skew["state"] = "current"
        return skew
    skew["oldest_undeployed"] = _iso(min(ts for _, ts in commits))
    soaked = [(sha, ts) for sha, ts in commits if ts <= now - cfg.soak_seconds]
    skew["target"] = soaked[0][0] if soaked else None
    skew["state"] = "behind" if soaked else "soaking"
    skew["stale"] = min(ts for _, ts in commits) <= now - cfg.stale_hours * 3600
    return skew


def render_skew(skew: dict | None) -> str:
    if not skew:
        return "tartci: skew UNKNOWN (never measured; run tartci fleet-macos self-update --plan)"
    state = skew.get("state")
    if state == "current":
        return f"tartci: current with main (measured {skew.get('measured_at')})"
    if state in ("behind", "soaking"):
        flag = " STALE" if skew.get("stale") else ""
        return (f"tartci: {skew['behind']} commits behind main (oldest undeployed: "
                f"{skew['oldest_undeployed']}){flag}"
                + ("" if state == "behind" else " [all still soaking]")
                + f" (measured {skew.get('measured_at')})")
    return f"tartci: skew {str(state).upper()} ({skew.get('reason') or 'no reason'})"


# ── one host at a time ─────────────────────────────────────────────────────

def published_hosts(cfg: Config, sys_: System) -> list[str]:
    """Hosts in the CURRENT published supply (main's), not the target's copy.

    The target can predate a host being added, and one-at-a-time must see
    every host that exists now.
    """
    shown = sys_.run(["git", "-C", str(cfg.checkout), "show",
                      "origin/main:fleet/advertised-labels.json"])
    try:
        value = json.loads(shown.out) if shown.rc == 0 else None
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, dict):
        raise Refused("published supply origin/main:fleet/advertised-labels.json is unreadable")
    return sorted({row["host_id"] for row in value.get("registrations", [])
                   if isinstance(row, dict) and isinstance(row.get("host_id"), str)})


def self_host_id(cfg: Config) -> str:
    host = _toml(cfg.installed_profile).get("host") or {}
    if not isinstance(host.get("id"), str):
        raise Refused("installed profile has no host.id")
    return host["id"]


def peer_state(cfg: Config, sys_: System, host_id: str, now: float) -> tuple[bool, str]:
    """(busy, evidence). Unreachable or unmapped peers are busy: fail closed."""
    target = cfg.peers.get(host_id)
    if not target:
        return True, f"no SSH target for peer {host_id} in {cfg.settings} [peers]"
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target]
    status = sys_.run([*ssh, "cd ~ && ~/.local/bin/tartci pool status --json"], timeout=60)
    try:
        value = json.loads(status.out)
    except json.JSONDecodeError:
        return True, f"peer {host_id} pool status unreadable (exit {status.rc}): {status.text[:160]}"
    if value.get("state") != "on" or value.get("participating") is not True:
        return True, f"peer {host_id} is {value.get('state')} (participating={value.get('participating')})"
    marker = sys_.run([*ssh, "cat ~/.tartci/state/self-update/active.json 2>/dev/null || true"],
                      timeout=60)
    active = None
    try:
        active = json.loads(marker.out) if marker.out.strip() else None
    except json.JSONDecodeError:
        return True, f"peer {host_id} self-update marker unreadable"
    if isinstance(active, dict) and now - float(active.get("ts", 0)) < ACTIVE_MARKER_TTL:
        return True, f"peer {host_id} is self-updating to {str(active.get('target'))[:12]}"
    return False, f"peer {host_id} on, not updating"


def check_peers(cfg: Config, sys_: System, me: str, now: float) -> list[str]:
    busy = []
    for peer in published_hosts(cfg, sys_):
        if peer == me:
            continue
        is_busy, evidence = peer_state(cfg, sys_, peer, now)
        if is_busy:
            busy.append(evidence)
    return busy


def stagger_seconds(host_id: str, window: int = 600) -> int:
    """A stable per-host offset so scheduled runs on different hosts spread out."""
    return int(hashlib.sha256(host_id.encode()).hexdigest(), 16) % window


# ── capacity floor ─────────────────────────────────────────────────────────

def census_env() -> dict[str, str]:
    # The census binds its identity per call (#227); this only pins the CLI.
    return {"TARTCI_GH_CLI": os.environ.get("TARTCI_GH_CLI") or "ghapp"}


def floor_decision(cfg: Config, sys_: System) -> tuple[bool, str]:
    """(allow_last_serving_host, rule). Refuses unless the floor allows it."""
    result = sys_.run(["python3", "scripts/capacity_floor.py", "check", "--action", "drain",
                       "--json"], cwd=str(cfg.checkout), env=census_env(), timeout=180)
    try:
        decision = json.loads(result.out)
    except json.JSONDecodeError as exc:
        raise Refused(f"capacity floor gave no decision (exit {result.rc}): {result.text}") from exc
    if decision.get("allowed"):
        return False, "capacity floor: every required label is served elsewhere"
    if decision.get("reason") != "last_serving_host":
        raise Refused(f"capacity floor refused ({decision.get('reason')}): "
                      f"{decision.get('message')}")
    last = [f for f in decision.get("findings", []) if f.get("verdict") == "last_serving_host"]
    blocked = [f["label"] for f in last if f.get("label") not in cfg.idle_by_design]
    if blocked:
        raise Refused(f"this host is the last server of {', '.join(blocked)}, which is not "
                      "idle by design; refusing to take it down for an update")
    labels = ", ".join(sorted({f["label"] for f in last}))
    return True, (f"--allow-last-serving-host: last server only of idle-by-design "
                  f"label(s) {labels} (rule: idle_by_design_labels)")


# ── sealed launcher ────────────────────────────────────────────────────────

def extract_signing_identity(sys_: System, bundle: Path, workdir: Path) -> str:
    """SHA-1 of the leaf certificate that signs the LIVE bundle."""
    prefix = workdir / "livecert"
    result = sys_.run(["codesign", "-d", f"--extract-certificates={prefix}", str(bundle)])
    if result.rc != 0:
        raise Refused(f"cannot extract the live launcher certificate: {result.text}")
    fp = sys_.run(["openssl", "x509", "-inform", "DER", "-in", f"{prefix}0", "-noout",
                   "-fingerprint", "-sha1"])
    match = re.search(r"Fingerprint=([0-9A-Fa-f:]{59})", fp.out)
    if fp.rc != 0 or match is None:
        raise Refused(f"cannot fingerprint the live launcher certificate: {fp.text}")
    return match.group(1).replace(":", "").upper()


def set_immutable(root: Path, manifest: Path, sys_: System) -> None:
    """The build's `verify --immutable` preconditions (2026-09-21 reseal runbook)."""
    members = [m["path"] for m in json.loads(manifest.read_text())["members"]]
    directories = set()
    for member in members:
        path = root / member
        if path.exists():
            os.chmod(path, (path.stat().st_mode & 0o7777) & ~0o222)
        parent = path.parent
        while parent != root and root in parent.parents:
            directories.add(parent)
            parent = parent.parent
    os.chmod(manifest, 0o444)
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        os.chmod(directory, 0o555)
    os.chmod(root, 0o555)


def restore_writable(root: Path) -> None:
    os.chmod(root, 0o755)
    for current, dirs, files in os.walk(root):
        if ".git" in Path(current).parts:
            continue
        for name in dirs:
            path = Path(current) / name
            if not path.is_symlink():
                os.chmod(path, 0o755)
        for name in files:
            path = Path(current) / name
            if not path.is_symlink():
                os.chmod(path, (path.stat().st_mode & 0o7777) | 0o200)


def build_launcher(cfg: Config, sys_: System, helper: dict, profile: Path, target: str,
                   out_dir: Path) -> tuple[Path, Path]:
    live = Path(helper["path"])
    identity = extract_signing_identity(sys_, live, out_dir)
    identities = sys_.run(["security", "find-identity", "-v", "-p", "codesigning"])
    if identity not in identities.out.upper():
        raise Refused(f"signing identity {identity} (extracted from the live bundle) is not "
                      "usable in a keychain; run `pulp ship doctor` first, never answer a "
                      "keychain password prompt")
    bundle = out_dir / "TartCILauncher.app"
    approval = out_dir / "approved.sha256"
    manifest = cfg.checkout / ".tartci-support-manifest.json"
    set_immutable(cfg.checkout, manifest, sys_)
    try:
        result = sys_.run(["bash", "scripts/build_macos_launcher.sh", "--output", str(bundle),
                           "--approval-output", str(approval), "--identity", identity,
                           "--support-root", ".", "--profile", str(profile)],
                          cwd=str(cfg.checkout), timeout=1800)
    finally:
        restore_writable(cfg.checkout)
    # The builder's cleanup trap cannot remove its read-only staging copy and
    # prints `rm:` noise after a successful build; the bundle is the verdict.
    if not bundle.is_dir() or not approval.is_file():
        raise Refused(f"launcher build produced no bundle (exit {result.rc}): "
                      + "\n".join(l for l in result.text.splitlines() if not l.startswith("rm:")))
    verify_bundle(cfg, sys_, bundle, profile, target)
    return bundle, approval


def verify_bundle(cfg: Config, sys_: System, bundle: Path, profile: Path, target: str) -> None:
    resources = bundle / "Contents" / "Resources"
    meta = _read_json(resources / "bundle.json") or {}
    if meta.get("source_commit") != target:
        raise Refused(f"new launcher bundle source_commit {meta.get('source_commit')} != "
                      f"target {target}")
    lanes = (_read_json(resources / "lanes.json") or {}).get("lanes") or {}
    rendered = cfg.state_dir / "verify-render"
    shutil.rmtree(rendered, ignore_errors=True)
    result = sys_.run(["python3", "scripts/macos_fleet_lanes.py", "render", str(profile),
                       "--output", str(rendered)], cwd=str(cfg.checkout))
    if result.rc != 0:
        raise Refused(f"cannot render the profile to verify the bundle: {result.text}")
    import plistlib
    expected = {}
    for plist in sorted(rendered.glob("*.plist")):
        env = plistlib.loads(plist.read_bytes()).get("EnvironmentVariables") or {}
        expected[env.get("TARTCI_QUEUE_LANE_ID")] = dict(sorted(env.items()))
    if not expected or {k: (v or {}).get("environment") for k, v in lanes.items()} != expected:
        raise Refused("new launcher lanes.json does not carry the rendered lane environment")
    check = sys_.run(["codesign", "--verify", "--deep", "--strict", str(bundle)])
    if check.rc != 0:
        raise Refused(f"new launcher fails codesign --verify --deep --strict: {check.text}")


# ── receipts ───────────────────────────────────────────────────────────────

class Receipt:
    def __init__(self, cfg: Config, sys_: System, target: str | None, mode: str) -> None:
        self.cfg, self.sys = cfg, sys_
        now = sys_.now()
        self.value: dict[str, Any] = {"schema": SCHEMA, "mode": mode, "target": target,
                                      "started_at": _iso(now), "steps": [], "status": "running"}
        stamp = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = cfg.state_dir / "attempts" / f"{stamp}-{(target or 'none')[:12]}.json"

    def step(self, name: str, detail: str = "", ok: bool = True) -> None:
        self.value["steps"].append({"at": _iso(self.sys.now()), "step": name,
                                    "ok": ok, "detail": detail[:2000]})
        mark = "" if ok else ("REFUSED " if name == "refused" else "FAILED ")
        print(f"self-update: {mark}{name}{': ' + detail if detail else ''}", flush=True)

    def finish(self, status: str, error: str = "") -> None:
        self.value.update(status=status, error=error or None, finished_at=_iso(self.sys.now()))
        _write_json(self.path, self.value)
        if self.value["mode"] != "apply":
            return  # a plan never overwrites the record of the last real attempt
        _write_json(self.cfg.state_dir / "last.json", {
            "status": status, "target": self.value["target"], "error": error or None,
            "at": self.value["finished_at"], "receipt": str(self.path)})


def recent_attempt(cfg: Config, target: str, now: float) -> str | None:
    for path in sorted((cfg.state_dir / "attempts").glob(f"*-{target[:12]}.json")):
        value = _read_json(path) or {}
        # A refusal changed nothing, so it does not spend the attempt.
        if value.get("mode") != "apply" or value.get("status") == "refused":
            continue
        try:
            started = dt.datetime.strptime(value["started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=dt.timezone.utc).timestamp()
        except (KeyError, ValueError):
            continue
        if now - started < cfg.rate_hours * 3600:
            return f"{path.name} ({value.get('status')})"
    return None


# ── the procedure ──────────────────────────────────────────────────────────

def checked_in_profile(cfg: Config) -> Path:
    name = _toml(cfg.installed_profile).get("name")
    for path in sorted((cfg.checkout / "profiles").glob("*-macos-fleet.toml")):
        if _toml(path).get("name") == name:
            return path
    raise Refused(f"no checked-in profile named {name!r} at the target commit")


def relay_enabled(cfg: Config) -> bool:
    try:
        return bool((_toml(cfg.network_profile).get("http_connect_relay") or {}).get("enabled"))
    except (OSError, Refused):
        return False
    except ValueError:
        return True  # unreadable profile: run reconcile, which will say why


def tartci(cfg: Config, sys_: System, *args: str, timeout: float = 900) -> Result:
    """The TARGET commit's tartci, run from the managed checkout."""
    return sys_.run(["./tartci", *args], cwd=str(cfg.checkout), env=census_env(),
                    timeout=timeout)


def installed_tartci(cfg: Config, sys_: System, *args: str, timeout: float = 900) -> Result:
    """The INSTALLED shim, run from $HOME (never a checkout)."""
    return sys_.run([str(cfg.shim), *args], cwd=str(cfg.home), env=census_env(),
                    timeout=timeout)


def plan_or_apply(cfg: Config, sys_: System, *, apply: bool, target_ref: str,
                  scheduled: bool = False) -> int:
    now = sys_.now()
    try:
        installed, source = installed_commit(cfg)
        refresh_checkout(cfg, sys_)
    except Refused as exc:
        print(f"self-update: UNKNOWN: {exc}")
        _write_json(cfg.state_dir / "skew.json", {"state": "unknown", "reason": str(exc),
                                                  "measured_at": _iso(now)})
        return EXIT_UNKNOWN
    skew = measure_skew(cfg, sys_, installed, now, target_ref)
    _write_json(cfg.state_dir / "skew.json", skew)
    print(f"self-update: installed {installed[:12]} ({source})")
    print(f"self-update: {render_skew(skew)}")
    if skew["state"] in ("unknown", "diverged"):
        return EXIT_UNKNOWN
    target = skew.get("target")
    if not target:
        print("self-update: nothing to do" + (" (undeployed commits are still soaking)"
                                               if skew["state"] == "soaking" else ""))
        return EXIT_NOTHING
    receipt = Receipt(cfg, sys_, target, "apply" if apply else "plan")
    try:
        me = self_host_id(cfg)
        if apply:
            prior = recent_attempt(cfg, target, now)
            if prior:
                raise Refused(f"already attempted {target[:12]} within {cfg.rate_hours}h: {prior}")
        if apply and scheduled:
            sys_.sleep(stagger_seconds(me))
        checkout = sys_.run(["git", "-C", str(cfg.checkout), "checkout", "--quiet",
                             "--detach", "--force", target])
        if checkout.rc != 0:
            raise Refused(f"cannot check out {target[:12]}: {checkout.text}")
        receipt.step("checkout", f"{cfg.checkout} detached at {target}")
        profile = checked_in_profile(cfg)
        for name, args in (
                ("support-manifest", ["support-manifest", "write", "--root", ".",
                                      "--output", ".tartci-support-manifest.json"]),
                ("validate", ["fleet-macos", "validate", str(profile)])):
            result = tartci(cfg, sys_, *args)
            if result.rc != 0:
                raise Refused(f"{name} failed: {result.text}")
            receipt.step(name)
        helper = launch_helper(cfg)
        bundle = approval = None
        if helper is not None and apply:
            out_dir = cfg.state_dir / "builds" / target
            shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True)
            bundle, approval = build_launcher(cfg, sys_, helper, profile, target, out_dir)
            receipt.step("launcher-build", f"{bundle} verified for {target[:12]}")
        elif helper is not None:
            with tempfile.TemporaryDirectory() as scratch:
                identity = extract_signing_identity(sys_, Path(helper["path"]), Path(scratch))
            usable = identity in sys_.run(["security", "find-identity", "-v", "-p",
                                           "codesigning"]).out.upper()
            receipt.step("launcher-build", f"would build and verify a sealed launcher signed by "
                         f"{identity} (extracted from the live bundle's leaf certificate; "
                         f"{'usable in a keychain' if usable else 'NOT usable: run pulp ship doctor'})",
                         ok=usable)
            if not usable:
                raise Refused(f"signing identity {identity} is not usable in a keychain")
        install_args = ["fleet-macos", "install", str(profile), "--support-source", ".",
                        "--support-manifest", ".tartci-support-manifest.json"]
        if bundle is not None:
            install_args += ["--launch-helper-source", str(bundle)]
        if helper is None:
            dry = tartci(cfg, sys_, *install_args)
            if dry.rc != 0:
                raise Refused(f"install dry-run failed: {dry.text}")
            receipt.step("install-dry-run", "ok")
        else:
            # The installer verifies the new bundle against the approval pin,
            # and the pin may only move once the host is out of service, so a
            # sealed host's dry run happens right after the pin, before --apply.
            receipt.step("install-dry-run", "deferred until the approval pin moves (after pool off)")
        # A plan evaluates every gate and reports all refusals; an apply stops
        # at the first.
        refusals = []
        busy = check_peers(cfg, sys_, me, now)
        if busy:
            refusal = "another fleet host is not serving normally: " + "; ".join(busy)
            if apply:
                raise Refused(refusal)
            refusals.append(refusal)
            receipt.step("peers", refusal, ok=False)
        else:
            receipt.step("peers", "every other published host is on and not updating")
        allow = False
        try:
            allow, rule = floor_decision(cfg, sys_)
            receipt.step("capacity-floor", rule)
        except Refused as exc:
            if apply:
                raise
            refusals.append(str(exc))
            receipt.step("capacity-floor", str(exc), ok=False)
        if refusals:
            raise Refused(" | ".join(refusals))
        if not apply:
            receipt.step("plan", "would drain, wait for no mid-job lane, pool off, "
                         + ("pin approval, " if helper else "")
                         + "install --apply, " + ("relay reconcile, " if relay_enabled(cfg) else "")
                         + "pool on, verify")
            receipt.finish("planned")
            return EXIT_OK
        return _apply(cfg, sys_, receipt, me, target, profile, install_args, allow,
                      helper, approval)
    except Refused as exc:
        receipt.step("refused", str(exc), ok=False)
        receipt.finish("refused", str(exc))
        return EXIT_REFUSED


def _announce(cfg: Config, sys_: System, me: str, target: str) -> None:
    marker = cfg.state_dir / "active.json"
    _write_json(marker, {"host_id": me, "target": target, "ts": sys_.now()})
    # Re-read peers AFTER announcing: two hosts that announce together both
    # see each other here, and only the lower host id proceeds.
    now = sys_.now()
    for peer in published_hosts(cfg, sys_):
        if peer == me:
            continue
        busy, evidence = peer_state(cfg, sys_, peer, now)
        if busy and ("self-updating" not in evidence or peer < me):
            marker.unlink(missing_ok=True)
            raise Refused(f"peer changed after announcing: {evidence}")


def _apply(cfg: Config, sys_: System, receipt: Receipt, me: str, target: str,
           profile: Path, install_args: list[str], allow: bool, helper: dict | None,
           approval: Path | None) -> int:
    flag = ["--allow-last-serving-host"] if allow else []
    pin_backup: Path | None = None
    pin_path = Path(helper["approval_sha256_path"]) if helper else None
    _announce(cfg, sys_, me, target)
    receipt.step("announce", f"{cfg.state_dir / 'active.json'}")
    try:
        drain = tartci(cfg, sys_, "pool", "drain", *flag)
        if drain.rc not in (0, 3):  # 3 = drain pending on a persistent runner
            raise Refused(f"pool drain refused: {drain.text}")
        receipt.step("drain", drain.text[:300])
        try:
            deadline = sys_.now() + cfg.wait_seconds
            while True:
                plan = tartci(cfg, sys_, "pool", "off", "--plan", *flag)
                if plan.rc == 0:
                    break
                if plan.rc != 12:
                    raise Failed(f"pool off --plan refused (exit {plan.rc}): {plan.text[:300]}")
                if sys_.now() >= deadline:
                    raise Failed(f"a lane was still mid-job after {cfg.wait_seconds}s")
                sys_.sleep(cfg.poll_seconds)
            receipt.step("wait-idle", "no owned lane mid-job")
            off = tartci(cfg, sys_, "pool", "off", *flag)
            if off.rc != 0:
                raise Failed(f"pool off failed: {off.text}")
            receipt.step("off")
            if pin_path is not None and approval is not None:
                stamp = dt.datetime.fromtimestamp(sys_.now(), dt.timezone.utc).strftime(
                    "%Y%m%dT%H%M%SZ")
                pin_backup = pin_path.with_name(f"{pin_path.name}.bak-{stamp}")
                shutil.copy2(pin_path, pin_backup)
                tmp = pin_path.with_name(f".{pin_path.name}.new")
                tmp.write_text(approval.read_text())
                os.chmod(tmp, 0o600)
                os.replace(tmp, pin_path)
                receipt.step("pin", f"approval pinned; previous saved as {pin_backup}")
                dry = tartci(cfg, sys_, *install_args)
                if dry.rc != 0:
                    raise Failed(f"install dry-run failed against the new pin: {dry.text}")
                receipt.step("install-dry-run", "ok against the new pin")
            for attempt in range(1, INSTALL_ATTEMPTS + 1):
                result = tartci(cfg, sys_, *install_args, "--apply", timeout=1800)
                if result.rc == 0:
                    break
                receipt.step("install", f"attempt {attempt} failed: {result.text[:300]}", ok=False)
                if attempt == INSTALL_ATTEMPTS:
                    raise Failed(f"install --apply failed {INSTALL_ATTEMPTS} times: {result.text}")
                sys_.sleep(INSTALL_RETRY_SECONDS)
            receipt.step("install", f"applied {target[:12]}")
            if relay_enabled(cfg):
                relay = sys_.run(["python3", "scripts/network_profile.py", "reconcile",
                                  "--json"], cwd=str(cfg.checkout))
                value = {}
                try:
                    value = json.loads(relay.out)
                except json.JSONDecodeError:
                    pass
                if relay.rc != 0 or value.get("ok") is not True or not value.get("probe"):
                    raise Failed(f"relay reconcile/probe failed: {relay.text[:300]}")
                receipt.step("relay", f"reconciled; probe: {value.get('probe')}")
        except Failed:
            if pin_backup is not None and pin_path is not None:
                shutil.copy2(pin_backup, pin_path)
                receipt.step("pin-restore", f"restored {pin_path} from {pin_backup}")
            raise
        finally:
            on = installed_tartci(cfg, sys_, "pool", "on")
            receipt.step("pool-on", "installed shim" if on.rc == 0 else on.text[:300],
                         ok=on.rc == 0)
        verify(cfg, sys_, target, helper, receipt)
    except Failed as exc:
        receipt.step("failed", str(exc), ok=False)
        receipt.finish("failed", str(exc))
        (cfg.state_dir / "active.json").unlink(missing_ok=True)
        print(f"self-update: FAILED: {exc} (host left on the previous generation)",
              file=sys.stderr)
        return EXIT_FAILED
    except Refused as exc:
        receipt.step("refused", str(exc), ok=False)
        receipt.finish("refused", str(exc))
        (cfg.state_dir / "active.json").unlink(missing_ok=True)
        installed_tartci(cfg, sys_, "pool", "on")
        return EXIT_REFUSED
    (cfg.state_dir / "active.json").unlink(missing_ok=True)
    receipt.finish("succeeded")
    return EXIT_OK


def verify(cfg: Config, sys_: System, target: str, helper: dict | None,
           receipt: Receipt) -> None:
    problems = []
    status = installed_tartci(cfg, sys_, "pool", "status", "--json")
    try:
        value = json.loads(status.out)
    except json.JSONDecodeError:
        value = {}
        problems.append(f"pool status unreadable: {status.text[:200]}")
    if value:
        fleet = value.get("fleet") or {}
        if value.get("state") != "on" or value.get("participating") is not True:
            problems.append(f"pool is {value.get('state')} after pool on")
        if fleet.get("managed") and fleet.get("fleet_ready") is not True:
            problems.append(f"fleet not ready: {fleet.get('problems')}")
        if (fleet.get("serving") or {}).get("blocked") is True:
            problems.append("serving BLOCKED")
    try:
        running, source = installed_commit(cfg)
        if running != target:
            problems.append(f"{source} runs {running[:12]}, not {target[:12]}")
    except Refused as exc:
        problems.append(str(exc))
    guard = installed_tartci(cfg, sys_, "launchd", "guard", "--command",
                             "launchctl kickstart -k gui/1/com.danielraffel.tartci."
                             "tart-runner-macos-fleet.verify.lane")
    allow = installed_tartci(cfg, sys_, "launchd", "guard", "--command", "launchctl list")
    if guard.rc != 2 or allow.rc != 0:
        problems.append(f"installed `tartci launchd guard` missing or wrong "
                        f"(block={guard.rc}, allow={allow.rc})")
    if problems:
        raise Failed("verification: " + "; ".join(problems))
    receipt.step("verify", f"pool on and ready, executing {target[:12]}, guard present")


def status_lines(state_dir: Path) -> list[str]:
    """For pool status / doctor / watchdog: skew and the last attempt."""
    lines = [render_skew(_read_json(state_dir / "skew.json"))]
    last = _read_json(state_dir / "last.json")
    if last and last.get("status") == "failed":
        lines.append(f"self-update: LAST ATTEMPT FAILED at {last.get('at')} for "
                     f"{str(last.get('target'))[:12]}: {last.get('error')}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tartci fleet-macos self-update")
    parser.add_argument("--target", default="origin/main")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="read-only (default)")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--status", action="store_true", help="print cached skew and last attempt")
    mode.add_argument("--refresh-skew", action="store_true",
                      help="fetch main and record skew only (no other checks)")
    parser.add_argument("--scheduled", action="store_true",
                        help="apply after this host's stagger offset (the periodic agent)")
    parser.add_argument("--if-older", type=int, default=0,
                        help="with --refresh-skew: skip unless the cache is older (seconds)")
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--settings", type=Path, default=None,
                        help="settings TOML (default ~/.config/tartci/self-update.toml)")
    args = parser.parse_args(argv)
    cfg = load_config(Path(args.home), args.settings)
    sys_ = System()
    if args.status:
        print("\n".join(status_lines(cfg.state_dir)))
        return 0
    if args.refresh_skew:
        cached = _read_json(cfg.state_dir / "skew.json")
        if cached and args.if_older:
            try:
                age = sys_.now() - dt.datetime.strptime(
                    cached["measured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=dt.timezone.utc).timestamp()
                if age < args.if_older:
                    print(render_skew(cached))
                    return 0
            except (KeyError, ValueError):
                pass
        try:
            installed, _ = installed_commit(cfg)
            refresh_checkout(cfg, sys_)
            skew = measure_skew(cfg, sys_, installed, sys_.now(), args.target)
        except Refused as exc:
            skew = {"state": "unknown", "reason": str(exc), "measured_at": _iso(sys_.now())}
        _write_json(cfg.state_dir / "skew.json", skew)
        print(render_skew(skew))
        return 0
    return plan_or_apply(cfg, sys_, apply=args.apply, target_ref=args.target,
                         scheduled=args.scheduled)


if __name__ == "__main__":
    sys.exit(main())
