#!/usr/bin/env python3
"""Is a store volume stalled? A bounded probe for doctor and pool status.

A host can keep its lanes "loaded" while every process that touches its VM
store or repository volume hangs: one host's reclaim agent sat 300 s in
`git rev-parse` on its repository volume and showed only as "reclaim: STALE"
seven hours later. Such a stall is usually system-wide (a wedged sandbox or
TCC daemon) and does not clear on its own; a reboot clears it.

This module probes each store the installed fleet profile names, with a hard
timeout, and reports the volume, how long the oldest stuck process on it has
been hanging, the likely cause and the operator step:

  - the Tart VM store (`host.tart_home`): a directory listing, and `tart list`
    when tart is installed;
  - the repository volumes of `[reclaim]` (`repo`, `worktrees_root`):
    `git rev-parse --git-dir`.

Who probes, and who only reads:

  - `tartci doctor fleet` and the launchd watchdog (at most every
    PROBE_INTERVAL_S) run the probe and write the result to a cache
    (`$TARTCI_HOME/state/store-stall/status.json`);
  - `tartci pool status`, which runs every few minutes from the pool-log
    agent, from every peer's self-update survey and from wait-idle loops,
    only reads that cache (`--cached`). It never spawns a probe, so a stalled
    host cannot collect stuck children from it.

A probe that does not finish is killed and never waited for again. A child
stuck in an uninterruptible wait may not die, so each volume has a marker
holding its probe PIDs (`<volume>.pids`), written as each child starts: while
one of them is still alive, the next probe of that volume spawns nothing and
reports the volume still stalled since the marker's time. Nothing on a lane's
path runs any of this.

States: ok, stalled, unprobed (no installed profile, no TOML parser, or no
cached result yet).
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional

try:
    import tomllib
except ModuleNotFoundError:  # the hosts' /usr/bin/python3 is 3.9
    tomllib = None  # type: ignore[assignment]

PROBE_TIMEOUT_S = 15
PROBE_INTERVAL_S = 15 * 60     # the watchdog's probe cadence
CAUSE = ("likely a system-wide sandbox or TCC stall (a wedged sandboxd or tccd): the volume "
         "itself answers nothing, and it does not clear on its own")
STEP = ("read the tcc_wedge finding: when the watchdog classified a wedged tccd it ran "
        "`sudo -n /usr/local/sbin/tartci-heal-tcc` (restarts tccd and sandboxd, once per 30 "
        "min) and recorded the outcome; a missing wrapper means install "
        "fleet/ops/tartci-heal-tcc. Otherwise drain this host (`tartci pool drain`), let "
        "running jobs finish, then reboot it; a reboot clears it. Verify: `tartci doctor "
        "fleet` reports store_ok")

# runner(argv, timeout, env, started) -> seconds, or None when it did not
# finish; it calls started(pid) as soon as the child exists.
Runner = Callable[[list, float, Optional[dict], Callable[[int], None]], Optional[float]]


def volume_of(path: str) -> str:
    """The mount a path lives on, as an operator would name it."""
    parts = pathlib.PurePosixPath(path).parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return f"/Volumes/{parts[2]}"
    return "/"


def run_bounded(argv: list, timeout: float, env: Optional[dict] = None,
                started: Optional[Callable[[int], None]] = None) -> Optional[float]:
    """Seconds the command took, or None when it did not finish in `timeout`.

    A command that does not finish is killed and abandoned: a process stuck
    in an uninterruptible wait may never exit, and waiting on it would hang
    the caller the same way.
    """
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True,
                                env={**os.environ, **(env or {})})
    except OSError:
        return 0.0      # not runnable: not a stall
    if started is not None:
        started(proc.pid)
    began = time.monotonic()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        return None
    return time.monotonic() - began


def targets(profile: dict) -> list[dict]:
    """The probes for one installed profile: {volume, path, what, argv, env}."""
    out = []
    host = profile.get("host") or {}
    store = host.get("tart_home")
    if isinstance(store, str) and store.startswith("/"):
        out.append({"volume": volume_of(store), "path": store, "what": "VM store listing",
                    "argv": ["/bin/ls", "-1", store], "env": None})
        tart = shutil.which("tart", path=os.pathsep.join(
            [str(pathlib.Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]))
        if tart:
            # Only on an existing store (tart creates a missing TART_HOME), and
            # tested in the child: an in-process stat would hang on a stall.
            out.append({"volume": volume_of(store), "path": store, "what": "tart list",
                        "argv": ["/bin/sh", "-c", 'test -d "$TART_HOME" || exit 0; '
                                 'exec "$0" list --quiet', tart],
                        "env": {"TART_HOME": store}})
    reclaim = profile.get("reclaim") or {}
    seen = set()
    for key in ("repo", "worktrees_root"):
        path = reclaim.get(key)
        if not isinstance(path, str) or not path.startswith("/") or path in seen:
            continue
        seen.add(path)
        argv = (["/usr/bin/git", "-C", path, "rev-parse", "--git-dir"] if key == "repo"
                else ["/bin/ls", "-1", path])
        out.append({"volume": volume_of(path), "path": path,
                    "what": "git rev-parse" if key == "repo" else "worktrees listing",
                    "argv": argv, "env": None})
    return out


_ETIME = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")


def etime_seconds(text: str) -> Optional[int]:
    match = _ETIME.match(text.strip())
    if not match:
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def stuck_processes(volumes: set, ps_output: Optional[str] = None,
                    min_age_s: float = PROBE_TIMEOUT_S) -> dict:
    """volume -> (oldest age in seconds, its command) for git or tart processes
    that name a path on that volume and have run longer than `min_age_s`."""
    if ps_output is None:
        try:
            ps_output = subprocess.run(["/bin/ps", "-Ao", "etime=,command="],
                                       capture_output=True, text=True, timeout=10,
                                       check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return {}
    oldest: dict = {}
    for line in ps_output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        age, command = etime_seconds(fields[0]), fields[1]
        name = os.path.basename(command.split()[0]) if command.split() else ""
        if age is None or age < min_age_s or name not in ("git", "tart"):
            continue
        for volume in volumes:
            if volume != "/" and volume + "/" in command:
                if volume not in oldest or age > oldest[volume][0]:
                    oldest[volume] = (age, command[:160])
    return oldest


def _age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def state_dir(home: Optional[pathlib.Path] = None) -> pathlib.Path:
    root = os.environ.get("TARTCI_HOME") or str((home or pathlib.Path.home()) / ".tartci")
    return pathlib.Path(root) / "state" / "store-stall"


def marker_path(state: pathlib.Path, volume: str) -> pathlib.Path:
    return state / ((re.sub(r"[^A-Za-z0-9]+", "_", volume).strip("_") or "root") + ".pids")


def _read(path: pathlib.Path) -> Optional[dict]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: pathlib.Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    os.replace(temporary, path)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pid_identity(pid: int) -> Optional[str]:
    """Return the process start identity used to reject PID reuse."""
    try:
        result = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value or None


def live_marker(state: pathlib.Path, volume: str,
                alive: Callable[[int], bool] = pid_alive,
                identity: Callable[[int], Optional[str]] = pid_identity) -> Optional[dict]:
    """The volume's marker when an earlier probe of it is still alive, else None."""
    marker = _read(marker_path(state, volume))
    pids = [pid for pid in (marker or {}).get("pids") or [] if isinstance(pid, int)]
    identities = marker.get("identities") if marker else None
    if not isinstance(identities, dict):
        return None
    live = [pid for pid in pids if alive(pid)
            and identities.get(str(pid))
            and identities.get(str(pid)) == identity(pid)]
    return marker if live else None


def probe(probes: list, *, timeout: float = PROBE_TIMEOUT_S, runner: Runner = run_bounded,
          ps_output: Optional[str] = None, state: Optional[pathlib.Path] = None,
          now: Optional[float] = None, alive: Callable[[int], bool] = pid_alive,
          identity: Callable[[int], Optional[str]] = pid_identity) -> dict:
    """Probe every volume whose earlier probe is not still alive, all at once
    (the caller waits at most about `timeout`), and cache the result."""
    state = state or state_dir()
    now = time.time() if now is None else now
    previous = (_read(state / "status.json") or {}).get("since") or {}
    held = {item["volume"]: live_marker(state, item["volume"], alive, identity)
            for item in probes}
    held = {volume: marker for volume, marker in held.items() if marker}
    run = [item for item in probes if item["volume"] not in held]
    results: list = [None] * len(run)
    lock = threading.Lock()
    spawned: dict = {}

    def one(index: int) -> None:
        item = run[index]

        def started(pid: int) -> None:
            # Recorded before waiting: a probe that hangs, or a caller killed
            # while it waits, still leaves its PID for the next run to find.
            with lock:
                spawned.setdefault(item["volume"], []).append(pid)
                _write(marker_path(state, item["volume"]),
                       {"since": previous.get(item["volume"]) or now,
                        "pids": spawned[item["volume"]],
                        "identities": {str(value): identity(value)
                                        for value in spawned[item["volume"]]}})
        results[index] = runner(item["argv"], timeout, item["env"], started)

    threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(len(run))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout + 5)
    stalled = [item for item, took in zip(run, results) if took is None]
    stalled_volumes = {item["volume"] for item in stalled} | set(held)
    for volume in {item["volume"] for item in run} - stalled_volumes:
        marker_path(state, volume).unlink(missing_ok=True)   # it answered: nothing left behind
    since = {volume: float(previous.get(volume) or (held.get(volume) or {}).get("since") or now)
             for volume in stalled_volumes}
    value = describe_result(probes, stalled, held, since, timeout, ps_output)
    value.update(probed_at=now, since=since)
    _write(state / "status.json", value)
    return value


def describe_result(probes: list, stalled: list, held: dict, since: dict, timeout: float,
                    ps_output: Optional[str]) -> dict:
    if not probes:
        return {"state": "unprobed", "detail": "the installed profile names no store to probe",
                "stalled": []}
    if not stalled and not held:
        return {"state": "ok", "detail": f"{len(probes)} store probe(s) answered within "
                f"{timeout:g}s", "stalled": []}
    volumes = sorted({item["volume"] for item in stalled} | set(held))
    stuck = stuck_processes(set(volumes), ps_output)
    lines = []
    for volume in volumes:
        if volume in held:
            what = (f"an earlier probe (pid {', '.join(map(str, held[volume].get('pids') or []))}) "
                    "is still hanging, so no new one was started")
        else:
            what = (", ".join(sorted({i["what"] for i in stalled if i["volume"] == volume}))
                    + f" did not finish within {timeout:g}s")
        hanging = (f"; an older {os.path.basename(stuck[volume][1].split()[0])} on it has been "
                   f"hanging for {_age(stuck[volume][0])}" if volume in stuck else "")
        lines.append(f"{volume}: still stalled since {_iso(since[volume])}: {what}{hanging}")
    return {"state": "stalled",
            "detail": ("store volume stalled: " + "; ".join(lines) + f". Cause: {CAUSE}. "
                       f"Operator step: {STEP}"),
            "stalled": [{"volume": volume, "since": since[volume],
                         "hanging_s": stuck.get(volume, (None,))[0],
                         "earlier_probe_alive": volume in held} for volume in volumes]}


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def installed_profile(home: pathlib.Path) -> pathlib.Path:
    return home / ".config" / "tartci" / "macos-fleet-profile.toml"


def status(home: Optional[pathlib.Path] = None, **kwargs: Any) -> dict:
    """Probe now (doctor, the watchdog): the volumes, guarded by their markers."""
    home = home or pathlib.Path.home()
    path = installed_profile(home)
    if tomllib is None:
        return {"state": "unprobed", "detail": "no TOML parser in this interpreter", "stalled": []}
    try:
        profile = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {"state": "unprobed", "detail": "no installed fleet profile", "stalled": []}
    except (OSError, ValueError) as exc:
        return {"state": "unprobed", "detail": f"installed profile unreadable: {exc}",
                "stalled": []}
    kwargs.setdefault("state", state_dir(home))
    return probe(targets(profile), **kwargs)


# Past this age the cached result says nothing about now: the watchdog probes
# every PROBE_INTERVAL_S, so one missed run is allowed and a second is not.
STALE_AFTER_S = 2 * PROBE_INTERVAL_S


def cached(home: Optional[pathlib.Path] = None, now: Optional[float] = None) -> dict:
    """The last probe's result, read only: what pool status shows. Spawns nothing.

    A result older than STALE_AFTER_S reads as unprobed, naming its age, the
    state it last had, and what should have refreshed it, so a watchdog that
    stopped probing never leaves an old "ok" standing in for now."""
    value = _read(state_dir(home) / "status.json")
    if value is None or not isinstance(value.get("probed_at"), (int, float)):
        return {"state": "unprobed", "detail": "no probe result yet (doctor or the watchdog "
                "writes one)", "stalled": []}
    age = (time.time() if now is None else now) - float(value["probed_at"])
    if age > STALE_AFTER_S:
        return {"state": "unprobed", "stalled": [], "probed_at": value["probed_at"],
                "stale_state": value.get("state"), "age_s": round(age),
                "detail": (f"last probe {_age(age)} ago, older than {_age(STALE_AFTER_S)}; "
                           "the launchd watchdog is not probing "
                           "(`tartci doctor fleet` probes now)")}
    return value


def watchdog_pass(home: Optional[pathlib.Path] = None, now: Optional[float] = None,
                  **kwargs: Any) -> Optional[str]:
    """At most every PROBE_INTERVAL_S: probe, and return a line when stalled."""
    now = time.time() if now is None else now
    last = (_read(state_dir(home) / "status.json") or {}).get("probed_at")
    if isinstance(last, (int, float)) and now - last < PROBE_INTERVAL_S:
        return None
    value = status(home, now=now, **kwargs)
    return f"store-stall: WARN {value['detail']}" if value.get("state") == "stalled" else None


def describe(value: dict) -> str:
    state = value.get("state")
    as_of = (f" (as of {_iso(value['probed_at'])})"
             if isinstance(value.get("probed_at"), (int, float)) else "")
    if state == "stalled":
        first = min((row.get("since") for row in value.get("stalled") or []
                     if isinstance(row.get("since"), (int, float))), default=None)
        since = f" since {_iso(first)}" if first is not None else ""
        return f"store: STALLED{since}{as_of}: " + str(value.get("detail"))
    if state == "ok":
        return f"store: ok{as_of} (" + str(value.get("detail")) + ")"
    return "store: not probed (" + str(value.get("detail")) + ")"


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--cached", action="store_true",
                      help="read the last result only; never probe (pool status)")
    mode.add_argument("--probe", action="store_true", help="probe now (doctor)")
    parser.add_argument("--timeout", type=float, default=PROBE_TIMEOUT_S)
    args = parser.parse_args(argv)
    value = cached() if args.cached else status(timeout=args.timeout)
    print(json.dumps(value, sort_keys=True) if args.json else describe(value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
