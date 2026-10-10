#!/usr/bin/env python3
"""Start fleet timer jobs that launchd has stopped starting.

A macOS automatic install that stalls part-way (m3, 2026-10-04: 27.0.1 began
at 03:14 and never finished) can leave launchd's gui domain refusing every
non-demand spawn. `launchctl print` then shows each StartInterval agent as
`pended nondemand spawn = interval` with its `runs` counter frozen, and a
KeepAlive agent that exits sits at `pended nondemand spawn` instead of being
respawned. The VM reaper, the launchd watchdog, self-update and the reclaimer
all stopped for 16 h while the long-running lane supervisors kept serving, so
nothing looked down. Only a demand spawn (`launchctl kickstart`) still works.

This guard therefore runs inside a process that is already alive: the lane
supervisor starts it as a background child (providers/tart-macos/
interval-guard.lib.sh). Every supervisor on a host starts one; an exclusive
lock picks the single one that acts, and another takes over within one cadence
when that supervisor exits. Each pass reads the fleet's LaunchAgents
(com.danielraffel.*, com.pulp.*) and, with per-call timeouts:

  * kicks a StartInterval agent that is not running and whose `runs` counter
    has not moved for 2x its interval (event `interval_agent_kicked`);
  * kicks a fleet runner lane (KeepAlive) that launchd owes a respawn
    (event `keepalive_agent_kicked`): it exited 75, the code a lane uses only
    after its fail-closed restart contract ran, it is not running, it has
    stayed that way for KEEPALIVE_PENDED_S, pool participation is on and
    launchd does not list it as disabled. The rule is the watchdog's own
    (`owes_exit75_respawn`), and unknown enablement kicks nothing. A pended
    spawn line is recorded as evidence, never required. A lane stopped with
    any other exit code did not vouch for its own state: it is reported once
    (`keepalive_agent_stopped`) and left to the watchdog's verdict;
  * stops after KICK_CEILING kicks in a row that did not take (the agent
    neither ran nor advanced its run count by the next pass), emitting
    `interval_kick_ceiling` / `keepalive_kick_ceiling` once, so a job that
    cannot start fails loud instead of being kicked forever. The ceiling
    clears when the agent next runs or advances on its own;
  * does not kick self-update (PAUSE_DURING_STALL) while a domain stall
    episode is open: an update stops every supervisor, so no guard runs and
    any lane restart it leaves to launchd would pend unattended. Outside an
    episode launchd starts it on its own, and an overdue one is kicked like
    any other. It still counts toward the domain stall;
  * opens a domain-wide episode when two or more interval agents have gone
    2x their interval without launchd starting them on its own (event
    `launchd_interval_spawns_stalled`, once per episode), and closes it
    (`launchd_interval_spawns_recovered`) when launchd resumes.

A run the guard caused does not count as launchd making progress, so the
episode stays open for as long as launchd is broken even though the guard
keeps the jobs running. Every pass writes a receipt that `tartci pool status`
and `tartci doctor fleet` read.

Python 3.9-safe: the launchd python on fleet hosts is /usr/bin/python3.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import pathlib
import plistlib
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import tartci_launchd_watchdog as watchdog  # noqa: E402

try:
    import fcntl
except ImportError:  # pragma: no cover - not on macOS/Linux
    fcntl = None  # type: ignore[assignment]

LABEL_PREFIXES = ("com.danielraffel.", "com.pulp.")
# Not kicked while a domain stall episode is open (still observed, and still
# counted toward the stall). Self-update stops every supervisor, so during the
# update no guard runs, and a lane restart left to launchd would pend.
PAUSE_DURING_STALL = ("self-update",)
PAUSED_HINT = "self-update paused: launchd timers stalled; a reboot resumes it"
# The only KeepAlive agents kicked: the fleet runner lanes.
KEEPALIVE_KICK = ("tart-runner-macos-fleet",)
STALL_FACTOR = 2
DOMAIN_STALL_MIN_AGENTS = 2
KEEPALIVE_PENDED_S = 120
KICK_CEILING = 3
STOPPED_STATES = ("not running", "spawn scheduled")
CADENCE_S = 60
CALL_TIMEOUT_S = 5.0
KICK_TIMEOUT_S = 10.0
PASS_BUDGET_S = 40.0
# A receipt this old means no supervisor on the host is running the guard.
RECEIPT_STALE_S = 10 * 60
STALL_HINT = ("macOS launchd stopped starting timer jobs, likely a stalled automatic "
              "macOS install; the supervisor is starting them; a reboot clears it")

Run = Callable[[List[str], float], Tuple[int, str, str]]


def default_dir() -> pathlib.Path:
    override = os.environ.get("TARTCI_INTERVAL_GUARD_DIR")
    if override:
        return pathlib.Path(override).expanduser()
    home = os.environ.get("TARTCI_HOME", str(pathlib.Path.home() / ".tartci"))
    return pathlib.Path(home).expanduser() / "state" / "launchd-interval-guard"


def run_command(argv: List[str], timeout: float) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:g}s"
    except OSError as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def fleet_agents(agents_dir: pathlib.Path) -> List[Dict[str, Any]]:
    """Fleet LaunchAgents with a StartInterval or KeepAlive, from their plists."""
    out: List[Dict[str, Any]] = []
    try:
        paths = sorted(agents_dir.glob("*.plist"))
    except OSError:
        return out
    for path in paths:
        if not path.name.startswith(LABEL_PREFIXES):
            continue
        try:
            with path.open("rb") as handle:
                spec = plistlib.load(handle)
        except Exception:  # noqa: BLE001 - an unreadable plist is not ours to judge
            continue
        if not isinstance(spec, dict):
            continue
        label = spec.get("Label")
        if not isinstance(label, str) or not label.startswith(LABEL_PREFIXES):
            continue
        interval = spec.get("StartInterval")
        if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
            interval = None
        keepalive = spec.get("KeepAlive")
        keepalive = keepalive is True or (isinstance(keepalive, dict) and bool(keepalive))
        if interval is None and not keepalive:
            continue
        out.append({"label": label, "interval": interval,
                    "keepalive": keepalive and interval is None})
    return out


_FIELD = re.compile(r"^\t([a-z][a-z ]*[a-z]) = (.*)$")


def parse_print(text: str) -> Dict[str, str]:
    """The top-level `key = value` fields of `launchctl print` (one tab deep)."""
    fields: Dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD.match(line)
        if match and match.group(1) not in fields:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def observe(label: str, domain: str, run: Run) -> Optional[Dict[str, Any]]:
    """runs/state/pended/last exit for one label, or None when launchd does not hold it.

    A failed print is not read at all, whatever text came with it.
    """
    rc, out, _ = run(["launchctl", "print", f"{domain}/{label}"], CALL_TIMEOUT_S)
    if rc != 0:
        return None
    fields = parse_print(out)
    try:
        runs = int(fields.get("runs", ""))
    except ValueError:
        return None
    state, last_exit = watchdog.parse_launchctl_print(out)
    return {"runs": runs, "running": fields.get("state") == "running",
            "state": state, "last_exit": last_exit,
            "pended": fields.get("pended nondemand spawn")}


class Guard:
    def __init__(self, state_dir: pathlib.Path, agents_dir: pathlib.Path, *,
                 domain: str, run: Run = run_command,
                 clock: Callable[[], float] = time.time,
                 event_log: Optional[pathlib.Path] = None, runner: str = "",
                 owner_pid: Optional[int] = None,
                 participation_file: Optional[pathlib.Path] = None) -> None:
        self.state_dir = state_dir
        self.agents_dir = agents_dir
        self.domain = domain
        self.run = run
        self.clock = clock
        self.event_log = event_log
        self.runner = runner
        self.owner_pid = owner_pid if owner_pid is not None else os.getpid()
        self._deferred: List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], float]] = []
        self.participation_file = participation_file or (
            pathlib.Path.home() / ".config" / "tartci" / "native-build-participation")
        self.disabled: Optional[set] = None

    # -- persistence -------------------------------------------------------
    @property
    def state_path(self) -> pathlib.Path:
        return self.state_dir / "state.json"

    @property
    def receipt_path(self) -> pathlib.Path:
        return self.state_dir / "status.json"

    def _load(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {"agents": {}, "episode": None}
        if not isinstance(value, dict) or not isinstance(value.get("agents"), dict):
            return {"agents": {}, "episode": None}
        value.setdefault("episode", None)
        return value

    def _write(self, path: pathlib.Path, value: Dict[str, Any]) -> None:
        tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(value, sort_keys=True) + "\n")
        os.replace(tmp, path)

    def emit(self, kind: str, detail: str, fields: Dict[str, Any]) -> None:
        line = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock())),
            "event": kind, "runner": self.runner, "vm": "", "detail": detail,
            "fields": fields}, sort_keys=True)
        for path in filter(None, (self.state_dir / "events.jsonl", self.event_log)):
            try:
                with open(path, "a") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    # -- one pass ------------------------------------------------------------
    def kick(self, label: str) -> Tuple[bool, str]:
        rc, _, err = self.run(["launchctl", "kickstart", f"{self.domain}/{label}"],
                              KICK_TIMEOUT_S)
        return rc == 0, (err or "").strip()[:200]

    def pass_once(self) -> Dict[str, Any]:
        started = self.clock()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        state = self._load()
        memory: Dict[str, Any] = state["agents"]
        agents = fleet_agents(self.agents_dir)
        seen = {agent["label"] for agent in agents}
        stalled: List[Dict[str, Any]] = []
        kicked: List[Dict[str, Any]] = []
        errors: List[str] = []
        self._deferred = []
        checked = 0
        # Enablement is read once per pass; None (unreadable) kicks no lane.
        rc, out, _ = self.run(["launchctl", "print-disabled", self.domain], CALL_TIMEOUT_S)
        self.disabled = watchdog.parse_disabled_services(out) if rc == 0 else None
        for agent in agents:
            if self.clock() - started > PASS_BUDGET_S:
                errors.append("pass budget exhausted; remaining agents skipped")
                break
            label = agent["label"]
            try:
                obs = observe(label, self.domain, self.run)
            except Exception as exc:  # noqa: BLE001 - one agent never stops the pass
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
                continue
            if obs is None:
                continue
            checked += 1
            now = self.clock()
            if agent["interval"] is not None:
                self._interval(agent, obs, memory, now, stalled, kicked, errors)
            else:
                self._keepalive(agent, obs, memory, now, kicked, errors)
        # Forget only agents whose plist is gone; a failed read keeps the history.
        for label in list(memory):
            if label not in seen:
                del memory[label]
        now = self.clock()
        if checked or not agents:
            episode = self._episode(state, stalled, now)
        else:
            # Nothing could be read this pass: neither open nor close an episode.
            current = state.get("episode")
            episode = {"active": isinstance(current, dict),
                       "labels": current.get("labels", []) if isinstance(current, dict) else []}
            if isinstance(current, dict):
                episode["started_ts"] = current.get("started_ts")
        # Decided after the episode, so a pass that opens one never kicks.
        paused: List[Dict[str, Any]] = []
        for agent, obs, mem, since in self._deferred:
            if episode.get("active"):
                paused.append({"label": agent["label"], "interval": agent["interval"],
                               "seconds_since_progress": int(since)})
            else:
                self._kick_interval(agent, obs, mem, now, since, kicked, errors)
        self._write(self.state_path, state)
        receipt = {
            "ts": now, "owner_pid": self.owner_pid, "runner": self.runner,
            "agents_checked": checked, "stalled": stalled, "kicked": kicked,
            "errors": errors, "episode": episode, "paused": paused,
        }
        self._write(self.receipt_path, receipt)
        return receipt

    def _interval(self, agent: Dict[str, Any], obs: Dict[str, Any],
                  memory: Dict[str, Any], now: float, stalled: List[Dict[str, Any]],
                  kicked: List[Dict[str, Any]], errors: List[str]) -> None:
        label, interval = agent["label"], agent["interval"]
        mem = memory.get(label)
        if not isinstance(mem, dict) or not isinstance(mem.get("runs"), int) \
                or obs["runs"] < mem["runs"]:
            # First sight, or the counter reset (re-registered): start measuring now.
            memory[label] = {"runs": obs["runs"], "progress_ts": now,
                             "natural_ts": now, "pending_kicks": 0}
            return
        delta = obs["runs"] - mem["runs"]
        if "kicked_runs" in mem:
            # Did the last kick take? It ran (or is running) by now, or it did not.
            if obs["runs"] > int(mem.pop("kicked_runs")) or obs["running"]:
                mem["unconfirmed"] = 0
            else:
                mem["unconfirmed"] = int(mem.get("unconfirmed", 0)) + 1
                # A kick that started nothing caused no run, so it must not
                # claim a later run launchd starts on its own.
                mem["pending_kicks"] = max(0, int(mem.get("pending_kicks", 0)) - 1)
        if delta > 0:
            ours = min(delta, int(mem.get("pending_kicks", 0)))
            mem["pending_kicks"] = int(mem.get("pending_kicks", 0)) - ours
            if delta > ours:
                # launchd started it on its own. A run the guard caused was
                # already counted as progress when it was kicked.
                mem["progress_ts"] = now
                mem["natural_ts"] = now
                mem["unconfirmed"] = 0
                mem.pop("ceiling_reported", None)
            mem["runs"] = obs["runs"]
        limit = STALL_FACTOR * interval
        natural_age = now - float(mem.get("natural_ts", now))
        if obs["running"]:
            # Running is progress for the kick decision, never for launchd.
            mem["progress_ts"] = now
        if natural_age > limit:
            stalled.append({"label": label, "interval": interval,
                            "seconds_since_launchd_start": int(natural_age)})
        since = now - float(mem.get("progress_ts", now))
        if obs["running"] or since < limit:
            return
        if any(part in label for part in PAUSE_DURING_STALL):
            self._deferred.append((agent, obs, mem, since))
            return
        self._kick_interval(agent, obs, mem, now, since, kicked, errors)

    def _kick_interval(self, agent: Dict[str, Any], obs: Dict[str, Any],
                       mem: Dict[str, Any], now: float, since: float,
                       kicked: List[Dict[str, Any]], errors: List[str]) -> None:
        label, interval = agent["label"], agent["interval"]
        if self._at_ceiling(mem, label, "interval_kick_ceiling"):
            return
        ok, err = self.kick(label)
        if ok:
            mem["pending_kicks"] = int(mem.get("pending_kicks", 0)) + 1
            mem["kicked_runs"] = obs["runs"]
            mem["progress_ts"] = now
            kicked.append({"label": label, "interval": interval,
                           "seconds_since_progress": int(since)})
            self.emit("interval_agent_kicked",
                      f"label={label} interval={interval} seconds_since_progress={int(since)}",
                      {"label": label, "interval": interval,
                       "seconds_since_progress": int(since), "pended": obs["pended"] or ""})
        else:
            errors.append(f"{label}: kickstart failed: {err}")
            self.emit("interval_agent_kick_failed", f"label={label} error={err}",
                      {"label": label, "interval": interval})

    def _at_ceiling(self, mem: Dict[str, Any], label: str, event: str) -> bool:
        """Whether KICK_CEILING kicks in a row did not take; reported once."""
        unconfirmed = int(mem.get("unconfirmed", 0))
        if unconfirmed < KICK_CEILING:
            return False
        if not mem.get("ceiling_reported"):
            mem["ceiling_reported"] = True
            self.emit(event, f"label={label} unconfirmed={unconfirmed}",
                      {"label": label, "unconfirmed": unconfirmed})
        return True

    def _keepalive(self, agent: Dict[str, Any], obs: Dict[str, Any],
                   memory: Dict[str, Any], now: float, kicked: List[Dict[str, Any]],
                   errors: List[str]) -> None:
        label = agent["label"]
        if not any(part in label for part in KEEPALIVE_KICK) \
                or any(part in label for part in PAUSE_DURING_STALL):
            return
        mem = memory.get(label)
        if not isinstance(mem, dict):
            mem = memory[label] = {"runs": obs["runs"]}
        if "kicked_runs" in mem:
            if obs["runs"] > int(mem.pop("kicked_runs")) or obs["running"]:
                mem["unconfirmed"] = 0
            else:
                mem["unconfirmed"] = int(mem.get("unconfirmed", 0)) + 1
        if obs["running"]:
            # Seen running: whatever stopped it has cleared.
            memory[label] = {"runs": obs["runs"]}
            return
        if obs["state"] not in STOPPED_STATES:
            return
        last_exit = obs["last_exit"]
        if last_exit not in (None, 0, 75):
            if mem.get("reported_exit") != last_exit:
                mem["reported_exit"] = last_exit
                self.emit("keepalive_agent_stopped", f"label={label} last_exit={last_exit}",
                          {"label": label, "last_exit": last_exit})
            return
        if mem.get("runs") != obs["runs"] or "stopped_since" not in mem:
            mem["runs"], mem["stopped_since"] = obs["runs"], now
        since = now - float(mem["stopped_since"])
        if self.disabled is None:
            return
        expected_loaded = (watchdog.pool_participating(str(self.participation_file))
                           and label not in self.disabled)
        if not watchdog.owes_exit75_respawn(obs["state"], last_exit, since,
                                            expected_loaded, KEEPALIVE_PENDED_S):
            return
        if self._at_ceiling(mem, label, "keepalive_kick_ceiling"):
            return
        pended = obs["pended"] or ""
        ok, err = self.kick(label)
        if ok:
            mem["stopped_since"] = now
            mem["kicked_runs"] = obs["runs"]
            kicked.append({"label": label, "keepalive": True,
                           "seconds_stopped": int(since)})
            self.emit("keepalive_agent_kicked",
                      f"label={label} last_exit=75 seconds_stopped={int(since)} "
                      f"pended={pended or 'no'}",
                      {"label": label, "seconds_stopped": int(since), "last_exit": 75,
                       "pended": pended})
        else:
            errors.append(f"{label}: kickstart failed: {err}")
            self.emit("keepalive_agent_kick_failed", f"label={label} error={err}",
                      {"label": label})

    def _episode(self, state: Dict[str, Any], stalled: List[Dict[str, Any]],
                 now: float) -> Dict[str, Any]:
        labels = sorted(row["label"] for row in stalled)
        current = state.get("episode")
        if len(labels) >= DOMAIN_STALL_MIN_AGENTS:
            if not isinstance(current, dict):
                current = {"started_ts": now, "labels": labels}
                state["episode"] = current
                self.emit("launchd_interval_spawns_stalled",
                          f"agents={len(labels)} labels={','.join(labels)}",
                          {"agents": len(labels), "labels": ",".join(labels)})
            else:
                current["labels"] = labels
            return {"active": True, "started_ts": current["started_ts"], "labels": labels}
        if isinstance(current, dict):
            self.emit("launchd_interval_spawns_recovered",
                      f"seconds={int(now - float(current.get('started_ts', now)))}",
                      {"seconds": int(now - float(current.get("started_ts", now)))})
        state["episode"] = None
        return {"active": False, "labels": labels}


# -- ownership + loop ----------------------------------------------------------
def acquire_lock(path: pathlib.Path) -> Optional[Any]:
    """An exclusive, non-blocking lock held for this process's life, or None."""
    if fcntl is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def owner_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def loop(guard: Guard, *, owner_pid: int, cadence: float = CADENCE_S,
         sleep: Callable[[float], None] = time.sleep, max_passes: Optional[int] = None,
         log: Callable[[str], None] = lambda text: print(text, file=sys.stderr, flush=True)
         ) -> int:
    """Run passes once a cadence while the owning supervisor lives.

    A process that cannot take the lock keeps trying, so the duty moves to
    another supervisor within one cadence of the owner exiting.
    """
    lock = None
    passes = 0
    while owner_alive(owner_pid):
        if lock is None:
            try:
                lock = acquire_lock(guard.state_dir / "owner.lock")
            except OSError as exc:
                log(f"interval guard: lock error: {exc}")
        if lock is not None:
            try:
                guard.pass_once()
            except Exception as exc:  # noqa: BLE001 - the loop must outlive any pass
                log(f"interval guard: pass failed: {type(exc).__name__}: {exc}")
                # The lane's event log, not only this process's log file, so a
                # guard that keeps failing is visible where its kicks would be.
                guard.emit("launchd_guard_pass_failed", f"{type(exc).__name__}: {exc}"[:300],
                           {"error": f"{type(exc).__name__}: {exc}"[:300]})
        passes += 1
        if max_passes is not None and passes >= max_passes:
            break
        sleep(cadence)
    return 0


# -- status ----------------------------------------------------------------------
def status(state_dir: Optional[pathlib.Path] = None, *,
           now: Optional[float] = None) -> Dict[str, Any]:
    path = (state_dir or default_dir()) / "status.json"
    now = time.time() if now is None else now
    try:
        receipt = json.loads(path.read_text())
    except FileNotFoundError:
        return classify(None, now, receipt_path=str(path))
    except (OSError, ValueError) as exc:
        return {"receipt": str(path), "stale_after_s": RECEIPT_STALE_S,
                "state": "unreadable", "error": str(exc)}
    return classify(receipt, now, receipt_path=str(path))


def classify(receipt: Any, now: float, *, receipt_path: str = "") -> Dict[str, Any]:
    """The guard's state from a parsed receipt (None: never written), judged at
    `now`. Pure, so a peer that read the receipt over SSH can judge it on the
    receipt host's own clock (scripts/peer_stall_alert.py)."""
    out: Dict[str, Any] = {"receipt": receipt_path, "stale_after_s": RECEIPT_STALE_S}
    if receipt is None:
        out["state"] = "never"
        return out
    if not isinstance(receipt, dict) or not isinstance(receipt.get("ts"), (int, float)):
        out.update(state="unreadable", error="receipt has no ts")
        return out
    episode = receipt.get("episode") if isinstance(receipt.get("episode"), dict) else {}
    age = max(0.0, now - float(receipt["ts"]))
    out.update(age_seconds=int(age), agents_checked=receipt.get("agents_checked"),
               stalled=receipt.get("stalled") or [], kicked=receipt.get("kicked") or [],
               errors=receipt.get("errors") or [], episode=episode,
               paused=receipt.get("paused") or [],
               owner_pid=receipt.get("owner_pid"), runner=receipt.get("runner"))
    if age > RECEIPT_STALE_S:
        out["state"] = "stale"
    elif episode.get("active"):
        out["state"] = "stalled"
    else:
        out["state"] = "ok"
    return out


def describe(value: Dict[str, Any]) -> str:
    state = value.get("state")
    if state == "never":
        return ("launchd timers: guard has not run on this host yet "
                f"(no receipt at {value['receipt']})")
    if state == "unreadable":
        return f"launchd timers: guard receipt UNREADABLE ({value.get('error')})"
    if state == "stale":
        return (f"launchd timers: guard NOT RUNNING, last pass "
                f"{value['age_seconds'] / 60:.0f}m ago (no lane supervisor is running it)")
    if state == "stalled":
        episode = value.get("episode") or {}
        labels = episode.get("labels") or []
        since = episode.get("started_ts")
        age = "" if not isinstance(since, (int, float)) else \
            f" for {(time.time() - since) / 3600:.1f}h"
        return (f"launchd timers: STALLED{age}, {len(labels)} agents not started by "
                f"launchd ({', '.join(labels)}); {STALL_HINT}"
                + (f"; {PAUSED_HINT}" if value.get("paused") else ""))
    kicked = value.get("kicked") or []
    tail = f", kicked {len(kicked)} this pass" if kicked else ""
    return (f"launchd timers: ok ({value.get('agents_checked')} fleet agents checked "
            f"{value.get('age_seconds')}s ago{tail})")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="launchd_interval_guard")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("loop", "once"):
        p = sub.add_parser(name)
        p.add_argument("--state-dir", default=None)
        p.add_argument("--agents-dir",
                       default=str(pathlib.Path.home() / "Library" / "LaunchAgents"))
        p.add_argument("--domain", default=f"gui/{os.getuid()}")
        p.add_argument("--event-log", default=None)
        p.add_argument("--runner", default="")
        p.add_argument("--owner-pid", type=int, default=None)
        p.add_argument("--cadence", type=float, default=CADENCE_S)
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    st.add_argument("--state-dir", default=None)
    args = parser.parse_args(argv)
    if args.cmd == "status":
        value = status(pathlib.Path(args.state_dir) if args.state_dir else None)
        print(json.dumps(value, sort_keys=True) if args.json else describe(value))
        return 0
    owner = args.owner_pid if args.owner_pid is not None else os.getppid()
    guard = Guard(pathlib.Path(args.state_dir) if args.state_dir else default_dir(),
                  pathlib.Path(args.agents_dir), domain=args.domain,
                  event_log=pathlib.Path(args.event_log) if args.event_log else None,
                  runner=args.runner, owner_pid=owner)
    if args.cmd == "once":
        lock = acquire_lock(guard.state_dir / "owner.lock")
        if lock is None:
            print("interval guard: another supervisor owns the duty", file=sys.stderr)
            return 3
        print(json.dumps(guard.pass_once(), sort_keys=True))
        return 0
    return loop(guard, owner_pid=owner, cadence=args.cadence)


if __name__ == "__main__":
    sys.exit(main())
