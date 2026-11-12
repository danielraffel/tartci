#!/usr/bin/env python3
"""Keep an installed Shipyard queue tick on the code of the running tartci.

The tick used to be COPIED out of the generation, so a tartci self-update
never reached it: after the merge path was removed, m3 kept running a
2026-08-15 copy in `mode=live` until someone re-ran the installer by hand.
The agent now runs ~/.local/bin/tartci queue-tick, which follows the
installed generation, so the only drift left is an agent still installed the
old way.

drift() reads the installed agent's ProgramArguments. refresh() re-runs the running tartci's installer with the settings the host
already has: the GitHub App wrapper from the canonical config, the repo root
when one is set, and the mode from the LaunchAgent (SHIPYARD_TICK_APPLY=1 is
reap, 0 is dry-run). It acts only on a LOADED tick. An installed but unloaded
tick was switched off by someone, and reinstalling would switch it back on, so
that case is only reported. Settings it cannot read are refused, never guessed.
The launchd watchdog runs refresh() on its heal pass, and `pool status` prints
status_line().
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any, Callable

LABEL = "com.danielraffel.shipyard.queue-tick"
SUBCOMMAND = "queue-tick"
INSTALLER = "install_shipyard_queue_tick.sh"
# The installer waits for its first tick to finish (up to TICK_MAX_S while
# the tick runs, HEALTH_WAIT_S while it does not) and rolls back on its own.
# The timeout here must outlast both, or a kill would skip that rollback.
HEALTH_WAIT_S = "180"
TICK_MAX_S = "900"
INSTALL_TIMEOUT_S = 1500

Runner = Callable[..., subprocess.CompletedProcess]


def default_root() -> Path:
    """The tartci generation this module runs from."""
    return Path(__file__).resolve().parents[1]


def desired_program(plist: Path) -> list[str]:
    """The ProgramArguments of a tick that follows the installed generation."""
    home = plist.parent.parent.parent
    return ["/bin/bash", str(home / ".local" / "bin" / "tartci"), SUBCOMMAND]


def _program(plist: Path) -> list[str] | None:
    try:
        value = plistlib.loads(plist.read_bytes()).get("ProgramArguments")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return [str(arg) for arg in value] if isinstance(value, list) else None


def default_plist(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def default_config(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config" / "shipyard" / "queue-tick.env"


def _loaded(runner: Runner) -> bool | None:
    try:
        proc = runner(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                      capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 113 or "Could not find service" in (proc.stderr or proc.stdout or ""):
        return False
    return None


def drift(plist: Path | None = None, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Installed tick vs one that follows the installed generation. Read-only.

    state: not_installed | current | drift | drift_unloaded | unknown
    """
    plist = plist or default_plist()
    if not plist.exists():
        return {"state": "not_installed"}
    program = _program(plist)
    if program == desired_program(plist):
        return {"state": "current"}
    differing = ["ProgramArguments"]
    detail = (f"{plist.name} runs {' '.join(program or ['<unreadable>'])}, a copied tick "
              f"no self-update reaches, not tartci {SUBCOMMAND}")
    loaded = _loaded(runner)
    if loaded is None:
        return {"state": "unknown", "files": differing,
                "detail": f"{detail}; launchd state of {LABEL} unreadable"}
    if not loaded:
        return {"state": "drift_unloaded", "files": differing,
                "detail": f"{detail}; the tick is not loaded, so it is left off"}
    return {"state": "drift", "files": differing, "detail": detail}


def _env_file(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip()
    return values


def install_args(plist: Path, config: Path) -> tuple[list[str] | None, str]:
    """(installer arguments that keep this host's settings, why not)."""
    try:
        env = plistlib.loads(plist.read_bytes()).get("EnvironmentVariables") or {}
        settings = _env_file(config)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        return None, f"cannot read the installed settings: {exc}"
    gh_cli = settings.get("SHIPYARD_QUEUE_GH_CLI", "")
    if not gh_cli:
        return None, f"{config} names no SHIPYARD_QUEUE_GH_CLI"
    apply = str(env.get("SHIPYARD_TICK_APPLY", ""))
    if apply not in ("0", "1"):
        return None, f"{plist} has no SHIPYARD_TICK_APPLY, so the mode is unknown"
    args = ["--gh-cli", gh_cli, "--mode", "reap" if apply == "1" else "dry-run"]
    repo_root = settings.get("SHIPYARD_QUEUE_REPO_ROOT", "")
    if repo_root and Path(repo_root).is_dir():
        args += ["--repo-root", repo_root]
    return args, ""


def refresh(fix: bool, root: Path | None = None, plist: Path | None = None,
            config: Path | None = None, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Reinstall a loaded, drifted tick from the running tartci. Never raises."""
    root = root or default_root()
    plist = plist or default_plist()
    config = config or default_config()
    before = drift(plist, runner)
    if before["state"] != "drift":
        return before
    args, why = install_args(plist, config)
    if args is None:
        return {"state": "refresh_refused", "files": before["files"],
                "detail": f"{before['detail']}; {why}"}
    if not fix:
        return {"state": "would_refresh", "files": before["files"], "detail": before["detail"],
                "args": args}
    env = dict(os.environ, SHIPYARD_QUEUE_INSTALL_HEALTH_WAIT_SECS=HEALTH_WAIT_S,
               SHIPYARD_QUEUE_INSTALL_TICK_MAX_SECS=TICK_MAX_S)
    try:
        proc = runner(["/bin/bash", str(root / "scripts" / INSTALLER), *args, "--install"],
                      capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S, check=False,
                      cwd=str(root), env=env)
        rc, text = proc.returncode, (proc.stderr or proc.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        rc, text = 1, str(exc)
    after = drift(plist, runner)
    if rc == 0 and after["state"] == "current":
        return {"state": "refreshed", "files": before["files"], "args": args}
    return {"state": "refresh_failed", "files": before["files"],
            "detail": f"installer exit {rc}: {text[-300:]}; now {after['state']}"}


def status_line(value: dict[str, Any] | None = None) -> str | None:
    """One pool status line when the installed tick is not the running tartci's."""
    value = value if value is not None else drift()
    state = value.get("state")
    if state == "drift":
        return f"queue tick: DRIFT ({value['detail']}; the watchdog reinstalls it)"
    if state == "drift_unloaded":
        return f"queue tick: STALE COPY, NOT LOADED ({value['detail']})"
    if state in ("unknown", "refresh_refused", "refresh_failed"):
        return f"queue tick: {state.upper().replace('_', ' ')} ({value.get('detail')})"
    return None
