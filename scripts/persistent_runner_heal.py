#!/usr/bin/env python3
"""Keep declared persistent (non-JIT) GitHub Actions runners registered and listening.

A persistent runner fails silently in two ways. If its listener exits, the
launchd job that svc.sh writes has no KeepAlive, so it stays down. If its
registration is deleted on GitHub, the runner deletes its own `.runner` and
`.credentials` and exits 0. In both cases launchd reports the job as loaded and
not running, the lane's jobs wait until they are cancelled, and nothing on the
host looks broken.

This runs on a fleet host that holds a GitHub CLI with an App identity. It
must not run on the runner's own host, which keeps no GitHub credential. For
each runner declared in the config, it compares GitHub's view with the host's
view (over ssh by the host's fleet name) and repairs the gap:

  registered, online                         -> healthy
  registered, offline, listener not running  -> restart its launchd job
  registered, offline, listener running      -> wait; restart after the stuck grace
  not registered (or host lost .runner)      -> re-register with a fresh,
                                                one-hour registration token
                                                passed over ssh stdin, then start

The registration token is never written to disk and never appears on the ssh
command line; it travels over ssh stdin, and on the runner's host it is an
argument to config.sh only for the seconds configuration takes. The tool never deletes a registration on GitHub, and it
re-registers a given runner at most once per `--reregister-cooldown-secs`.

Config (JSON, host-local, never committed; it names hosts and paths):

    {"runners": [{
        "name": "my-runner",            # registration name
        "repo": "OWNER/REPO",           # repository scope it registers to
        "host": "ssh-host-alias",       # fleet ssh name, never an address
        "dir": "~/actions-runner",      # runner directory on that host
        "service": "actions.runner.OWNER-REPO.my-runner",
        "labels": "self-hosted,macOS,X64,custom-label"
    }]}

Exit status: 0 every runner healthy or repaired this pass, 1 a runner needs
attention, 2 the config or GitHub could not be read.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
from typing import Any, Callable

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
REQUIRED_KEYS = ("name", "repo", "host", "dir", "service", "labels")


def github_cli() -> str:
    return os.environ.get("TARTCI_GH_CLI") or "gh"


def remote_path(path: str) -> str:
    """Quote a runner path for a remote shell, keeping a leading ~/ live."""
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def run(argv: list[str], stdin: str | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)


class Fleet:
    """Every side effect, injectable for tests."""

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = run) -> None:
        self.run = runner

    def census(self, repo: str) -> list[dict[str, Any]]:
        proc = self.run([github_cli(), "api", "--paginate",
                         f"repos/{repo}/actions/runners?per_page=100", "--jq", ".runners[]"])
        if proc.returncode != 0:
            raise RuntimeError(f"runner census for {repo} failed: {proc.stderr.strip()[:300]}")
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def probe(self, spec: dict[str, Any]) -> dict[str, str] | None:
        d = remote_path(spec["dir"])
        script = (
            f"if [ -f {d}/.runner ]; then echo configured=1; else echo configured=0; fi; "
            # bin is a symlink to bin.<version>; match either spelling.
            f"if pgrep -f {d}/'bin[^/ ]*/Runner.Listener' >/dev/null 2>&1; "
            "then echo listening=1; else echo listening=0; fi"
        )
        proc = self.run(["ssh", *SSH_OPTS, spec["host"], script], timeout=60)
        if proc.returncode != 0:
            return None
        values = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        if {"configured", "listening"} - values.keys():
            return None
        return values

    def restart(self, spec: dict[str, Any]) -> bool:
        service = shlex.quote(spec["service"])
        script = f'launchctl kickstart -k "gui/$(id -u)/"{service}'
        return self.run(["ssh", *SSH_OPTS, spec["host"], script], timeout=60).returncode == 0

    def mint_token(self, repo: str) -> str:
        proc = self.run([github_cli(), "api", "-X", "POST",
                         f"repos/{repo}/actions/runners/registration-token", "--jq", ".token"])
        token = proc.stdout.strip()
        if proc.returncode != 0 or not token:
            raise RuntimeError(f"registration token for {repo} failed: {proc.stderr.strip()[:300]}")
        return token

    def reregister(self, spec: dict[str, Any], token: str) -> tuple[bool, str]:
        proc = self.run(["ssh", *SSH_OPTS, spec["host"], reregister_script(spec)],
                        stdin=token + "\n", timeout=300)
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[-600:]


def reregister_script(spec: dict[str, Any]) -> str:
    """Remote shell that reads the token from stdin and re-registers in place.

    The previous local identity is moved aside, never deleted, so a wrong
    decision is recoverable by hand. The token arrives on stdin, so the ssh
    command line that carries this script never contains it.
    """
    d = remote_path(spec["dir"])
    service = shlex.quote(spec["service"])
    url = shlex.quote(f"https://github.com/{spec['repo']}")
    name = shlex.quote(spec["name"])
    labels = shlex.quote(spec["labels"])
    return (
        "set -eu; IFS= read -r RUNNER_TOKEN; "
        f"cd {d}; "
        f'launchctl kill TERM "gui/$(id -u)/"{service} >/dev/null 2>&1 || true; '
        "sleep 3; "
        'aside=".identity-replaced-$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$aside"; '
        'for f in .runner .credentials .credentials_rsaparams; do '
        '[ ! -e "$f" ] || mv "$f" "$aside/"; done; '
        f"./config.sh --unattended --replace --url {url} --name {name} "
        f'--labels {labels} --work _work --token "$RUNNER_TOKEN"; '
        "unset RUNNER_TOKEN; "
        f'launchctl kickstart -k "gui/$(id -u)/"{service}'
    )


def load_state(path: pathlib.Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def decide(
    registration: dict[str, Any] | None,
    probe: dict[str, str] | None,
    state: dict[str, Any],
    now: float,
    stuck_grace_secs: int,
) -> str:
    if registration is not None and registration.get("status") == "online":
        return "healthy"
    if probe is None:
        return "host_unreachable"
    configured = probe.get("configured") == "1"
    listening = probe.get("listening") == "1"
    if registration is None or not configured:
        return "reregister"
    if not listening:
        return "restart"
    offline_since = float(state.get("offline_since") or now)
    if now - offline_since >= stuck_grace_secs:
        return "restart"
    return "wait"


def heal(
    specs: list[dict[str, Any]],
    fleet: Fleet,
    state_dir: pathlib.Path,
    fix: bool,
    now: float,
    stuck_grace_secs: int,
    cooldown_secs: int,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    rc = 0
    censuses: dict[str, list[dict[str, Any]]] = {}
    for spec in specs:
        row: dict[str, Any] = {"name": spec["name"], "repo": spec["repo"], "host": spec["host"]}
        rows.append(row)
        try:
            if spec["repo"] not in censuses:
                censuses[spec["repo"]] = fleet.census(spec["repo"])
        except RuntimeError as exc:
            row.update(action="census_unreadable", detail=str(exc))
            rc = 2
            continue
        registration = next((r for r in censuses[spec["repo"]] if r.get("name") == spec["name"]), None)
        row["github"] = "missing" if registration is None else str(registration.get("status"))
        state_path = state_dir / f"{spec['name']}.json"
        state = load_state(state_path)
        probe = None if row["github"] == "online" else fleet.probe(spec)
        row["host_view"] = probe
        action = decide(registration, probe, state, now, stuck_grace_secs)
        if action == "healthy":
            state.pop("offline_since", None)
        else:
            state.setdefault("offline_since", now)
        if action == "reregister" and now - float(state.get("last_reregister") or 0) < cooldown_secs:
            action = "reregister_cooldown"
        row["action"] = action
        if fix and action == "restart":
            row["result"] = "restarted" if fleet.restart(spec) else "restart_failed"
        elif fix and action == "reregister":
            state["last_reregister"] = now
            try:
                ok, detail = fleet.reregister(spec, fleet.mint_token(spec["repo"]))
            except RuntimeError as exc:
                ok, detail = False, str(exc)
            row["result"] = "reregistered" if ok else "reregister_failed"
            row["detail"] = detail
        if action not in ("healthy", "wait") and row.get("result") not in ("restarted", "reregistered"):
            rc = max(rc, 1)
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))
    return rows, rc


def load_specs(path: pathlib.Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    specs = data.get("runners") if isinstance(data, dict) else None
    if not isinstance(specs, list) or not specs:
        raise ValueError("config must hold a non-empty `runners` list")
    for spec in specs:
        missing = [k for k in REQUIRED_KEYS if not isinstance(spec.get(k), str) or not spec[k]]
        if missing:
            raise ValueError(f"runner {spec.get('name')!r} is missing {', '.join(missing)}")
    return specs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--config", default=os.environ.get(
        "TARTCI_PERSISTENT_RUNNERS_CONFIG",
        str(pathlib.Path.home() / ".config/tartci/persistent-runners.json")))
    ap.add_argument("--state-dir", default=str(
        pathlib.Path.home() / ".tartci/state/persistent-runner-heal"))
    ap.add_argument("--fix", action="store_true", help="act; without it, only report")
    ap.add_argument("--stuck-grace-secs", type=int, default=1800,
                    help="offline while its listener runs, before a restart")
    ap.add_argument("--reregister-cooldown-secs", type=int, default=3600)
    args = ap.parse_args(argv)
    try:
        specs = load_specs(pathlib.Path(args.config))
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": f"config unreadable: {exc}"}))
        return 2
    rows, rc = heal(specs, Fleet(), pathlib.Path(args.state_dir), args.fix, time.time(),
                    args.stuck_grace_secs, args.reregister_cooldown_secs)
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                      "fix": args.fix, "runners": rows}, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
