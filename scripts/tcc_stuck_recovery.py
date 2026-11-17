#!/usr/bin/env python3
"""Restart user launchd jobs left blocked on the store by a TCC wedge.

While a privacy consent alert waits, or tccd is wedged, every launchd open()
of an external volume without a stored grant blocks, and the blocked jobs stay
blocked after the cause clears (m3, 2026-11-16: com.pulp.fleet.prewall in
open() from 22:18Z, three fleet-pass must-review.py children from about
22:16Z). Restarting tccd and sandboxd does not release them; a run that never
ends also never starts again, so a periodic job is silently gone.

When the watchdog's store-health reading names a wedge (``tcc_attribution_wedge``
or ``tcc_prompt_pending``, or a store probe that timed out past the failure
threshold), each pass:

  1. finds this user's loaded launchd jobs (``launchctl list``) whose process
     has run longer than about 3x its StartInterval, and never less than
     STUCK_FLOOR_S, and whose process tree (the job and its descendants, from
     one ``ps`` read) has a command argument or cwd (``lsof -b -w -d cwd``,
     which never stats the path) under /Volumes;
  2. ``launchctl kickstart -k gui/<uid>/<label>`` each, once per episode per
     label;
  3. after SETTLE_S, ``kill -KILL`` the job's own pid if it is still the same
     process (same start time, same uid);
  4. logs every action and outcome, and when a job or a /Volumes descendant
     still survives, logs ``WARN tcc-wedge needs reboot`` and pages through
     the host incident path (a GitHub issue with the tartci-incident marker).

It never touches another user's or root's process, never runs sudo, and
leaves alone the runner lanes (``.tart-runner``, ``.qemu-runner``,
``actions.runner.``: the drain path owns them), the watchdog itself, and
KeepAlive services with no StartInterval, which always look long-running.

Rollback: ``TARTCI_TCC_STUCK_RECOVERY=0`` in the watchdog's environment turns it
off (the pass then logs what it would have done). Default on.
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_off  # noqa: E402 - sibling module
import tartci_incident  # noqa: E402 - sibling module

ENV_SWITCH = "TARTCI_TCC_STUCK_RECOVERY"
CLASS = "tcc_wedge_needs_reboot"
TITLE = "[tartci] {host}: jobs stay stuck on the store after a TCC wedge; reboot needed"
WEDGE_KINDS = frozenset({"tcc_attribution_wedge", "tcc_prompt_pending"})
STUCK_FLOOR_S = 30 * 60
INTERVAL_FACTOR = 3
SETTLE_S = 10.0
KILL_SETTLE_S = 2.0
COMMAND_TIMEOUT_S = 15.0
EXCLUDED_MARKERS = (".tart-runner", ".qemu-runner")
EXCLUDED_PREFIXES = ("actions.runner.",)
SELF_LABEL = "com.danielraffel.tartci.launchd-watchdog"

Runner = Callable[[List[str]], Any]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def on_volumes(path: str) -> bool:
    return path == "/Volumes" or path.startswith("/Volumes/")


def _run(argv: List[str]) -> Any:
    import bounded_subprocess  # noqa: PLC0415 - sibling module
    return bounded_subprocess.run_bounded(argv, timeout=COMMAND_TIMEOUT_S,
                                          operation="tcc-stuck-recovery")


def wedge(store_state: Dict[str, Any]) -> Tuple[bool, str]:
    """(active, episode key) from the watchdog's store-health state."""
    if not isinstance(store_state, dict) or store_state.get("state") == "healthy":
        return False, ""
    classification = store_state.get("classification") or {}
    kind = classification.get("kind") if isinstance(classification, dict) else None
    last = store_state.get("last_result") or {}
    hung = bool(store_state.get("drain_required")) and str(
        (last.get("reason") if isinstance(last, dict) else "") or "").startswith("timeout")
    if kind not in WEDGE_KINDS and not hung:
        return False, ""
    episode = str(store_state.get("failing_since") or
                  (classification.get("classified_at") if isinstance(classification, dict) else "")
                  or "unknown")
    return True, episode


def parse_etime(text: str) -> Optional[int]:
    """`ps -o etime` ([[dd-]hh:]mm:ss) in seconds."""
    text = text.strip()
    days = 0
    if "-" in text:
        head, text = text.split("-", 1)
        if not head.isdigit():
            return None
        days = int(head)
    fields = text.split(":")
    if not 2 <= len(fields) <= 3 or not all(f.isdigit() for f in fields):
        return None
    seconds = 0
    for field in fields:
        seconds = seconds * 60 + int(field)
    return days * 86400 + seconds


def parse_ps(text: str) -> Dict[int, Dict[str, Any]]:
    """`ps -axo pid=,ppid=,uid=,etime=,lstart=,command=` rows by pid.
    lstart is always five words (`Mon Nov 16 22:18:01 2026`)."""
    rows: Dict[int, Dict[str, Any]] = {}
    for line in text.splitlines():
        parts = line.split(None, 9)
        if len(parts) < 9 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        etime = parse_etime(parts[3])
        if etime is None or not parts[2].lstrip("-").isdigit():
            continue
        rows[int(parts[0])] = {"pid": int(parts[0]), "ppid": int(parts[1]), "uid": int(parts[2]),
                               "etime": etime, "lstart": " ".join(parts[4:9]),
                               "command": parts[9] if len(parts) > 9 else ""}
    return rows


def ps_snapshot(run: Runner) -> Optional[Dict[int, Dict[str, Any]]]:
    try:
        proc = run(["/bin/ps", "-axo", "pid=,ppid=,uid=,etime=,lstart=,command="])
    except Exception:  # noqa: BLE001 - no snapshot, no action
        return None
    if proc.returncode != 0:
        return None
    return parse_ps(proc.stdout or "")


def launchd_pids(run: Runner) -> Optional[Dict[str, int]]:
    """label -> pid for this user's running launchd jobs."""
    try:
        proc = run(["/bin/launchctl", "list"])
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    out: Dict[str, int] = {}
    for line in (proc.stdout or "").splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) >= 3 and fields[0].strip().isdigit():
            out[fields[2].strip()] = int(fields[0])
    return out


def cwds(pids: Iterable[int], run: Runner) -> Dict[int, str]:
    """Each pid's cwd from one `lsof -b -w`, which never stats the path."""
    wanted = sorted(set(pids))
    if not wanted:
        return {}
    try:
        proc = run(["/usr/sbin/lsof", "-b", "-w", "-a", "-p", ",".join(map(str, wanted)),
                    "-d", "cwd", "-Fpn"])
    except Exception:  # noqa: BLE001 - argv evidence still applies
        return {}
    out: Dict[int, str] = {}
    pid = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            out[pid] = line[1:]
    return out


def descendants(root: int, rows: Dict[int, Dict[str, Any]]) -> List[int]:
    children: Dict[int, List[int]] = {}
    for row in rows.values():
        children.setdefault(row["ppid"], []).append(row["pid"])
    out, stack = [], list(children.get(root, []))
    while stack and len(out) < 2000:
        pid = stack.pop()
        if pid in out or pid == root:
            continue
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def job_specs(agents_dir: Path) -> Dict[str, Dict[str, Any]]:
    """label -> {interval, keepalive} from the user's LaunchAgents plists."""
    specs: Dict[str, Dict[str, Any]] = {}
    for path in sorted(Path(agents_dir).glob("*.plist")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = plistlib.loads(path.read_bytes())
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(value, dict) or not value.get("Label"):
            continue
        interval = value.get("StartInterval")
        specs[str(value["Label"])] = {
            "interval": interval if isinstance(interval, int) and interval > 0 else None,
            "calendar": "StartCalendarInterval" in value,
            "keepalive": bool(value.get("KeepAlive")),
        }
    return specs


def excluded(label: str) -> Optional[str]:
    if label == SELF_LABEL:
        return "the watchdog itself"
    if any(marker in label for marker in EXCLUDED_MARKERS) or label.startswith(EXCLUDED_PREFIXES):
        return "a runner lane (the drain path owns it)"
    return None


def threshold_s(spec: Dict[str, Any]) -> Optional[int]:
    """How long a run may take before it counts as stuck, or None to skip."""
    if spec.get("interval"):
        return max(INTERVAL_FACTOR * int(spec["interval"]), STUCK_FLOOR_S)
    if spec.get("keepalive"):
        return None  # a service: always long-running, never "stuck" by age
    return STUCK_FLOOR_S


def find_stuck(specs: Dict[str, Dict[str, Any]], pids: Dict[str, int],
               rows: Dict[int, Dict[str, Any]], uid: int, run: Runner) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(candidates, skip notes). A candidate names the job, its pid and the
    /Volumes evidence; only jobs owned by `uid` are ever candidates."""
    aged: List[Tuple[str, int, int, List[int]]] = []
    notes: List[str] = []
    for label, pid in sorted(pids.items()):
        spec = specs.get(label)
        row = rows.get(pid)
        if spec is None or row is None or excluded(label):
            continue
        if row["uid"] != uid or uid == 0:
            continue
        limit = threshold_s(spec)
        if limit is None or row["etime"] < limit:
            continue
        aged.append((label, pid, limit, [pid] + descendants(pid, rows)))
    if not aged:
        return [], notes
    cwd = cwds([p for _, _, _, tree in aged for p in tree], run)
    out = []
    for label, pid, limit, tree in aged:
        evidence = []
        for member in tree:
            row = rows.get(member)
            if row is None or row["uid"] != uid:
                continue
            if "/Volumes/" in row["command"]:
                evidence.append({"pid": member, "lstart": row["lstart"], "why": "argument on /Volumes",
                                 "command": row["command"][:200]})
            elif on_volumes(cwd.get(member, "")):
                evidence.append({"pid": member, "lstart": row["lstart"],
                                 "why": f"cwd {cwd[member]}", "command": row["command"][:200]})
        if not evidence:
            notes.append(f"{label} (pid {pid}) is past {limit}s but nothing in its tree is on /Volumes")
            continue
        out.append({"label": label, "pid": pid, "lstart": rows[pid]["lstart"],
                    "etime": rows[pid]["etime"], "limit": limit, "evidence": evidence})
    return out, notes


def same_process(pid: int, lstart: str, uid: int, rows: Optional[Dict[int, Dict[str, Any]]]) -> bool:
    if rows is None:
        return True  # cannot tell: assume it survived, so a reboot is named
    row = rows.get(pid)
    return bool(row and row["lstart"] == lstart and row["uid"] == uid)


def render(host: str, stuck: List[Dict[str, Any]], episode: str, detected: str) -> Tuple[str, str]:
    rows = [f"- `{item['label']}`: " + ", ".join(
        f"pid {e['pid']} ({e['why']})" for e in item["survivors"]) for item in stuck]
    body = "\n".join([
        "## What happened",
        f"During a TCC wedge on `{host}` (store failing since {episode}), the watchdog "
        "kickstarted launchd jobs held in open() on /Volumes and sent SIGKILL to the job's own "
        "pid. These processes survived:", "", *rows, "",
        "## Fix", "Drain the host, let running jobs finish, and reboot it.", "",
        "## Safe to auto-apply",
        "No. A reboot is the owner's call; the watchdog never runs sudo.", "",
        "## How to verify",
        "After the reboot the store probe is healthy; this issue then closes itself.",
    ])
    marker = tartci_incident.marker(host, CLASS, episode, detected)
    return TITLE.format(host=host), tartci_incident.with_marker(body, marker)


def _load(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(path: Path, value: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value, sort_keys=True))
        os.replace(tmp, path)
    except OSError:
        pass


def _host() -> str:
    import vm_boot_alert  # noqa: PLC0415 - owns this host's id
    return vm_boot_alert._alert_host()[0]


def recovery_pass(store_state: Dict[str, Any], *, now: Optional[float] = None,
                  dry_run: bool = False, enabled: Optional[bool] = None,
                  home: Optional[str] = None, state_dir: Optional[Path] = None,
                  uid: Optional[int] = None, run: Runner = _run,
                  kill: Callable[[int, int], None] = os.kill,
                  sleep: Callable[[float], None] = time.sleep,
                  host: Optional[str] = None, issue: Optional[Callable[..., Any]] = None,
                  close: Optional[Callable[..., Any]] = None) -> List[str]:
    """One pass. Returns the lines to log. Never raises on a probe failure."""
    now = time.time() if now is None else now
    home = home or os.path.expanduser("~")
    uid = os.getuid() if uid is None else uid
    enabled = (os.environ.get(ENV_SWITCH, "1") != "0") if enabled is None else enabled
    sdir = state_dir or Path(os.environ.get("TARTCI_HOME") or os.path.join(home, ".tartci")) / "state"
    state_path = sdir / "tcc-stuck-recovery.json"
    alert_path = sdir / "tcc-wedge-reboot-alert.json"
    stamp = _iso(now)
    prefix = f"{stamp} launchd-watchdog: tcc-stuck-recovery:"
    active, episode = wedge(store_state)
    host_name = host or ""

    def page(stuck: List[Dict[str, Any]]) -> Dict[str, Any]:
        name = host_name or _host()
        return host_off.episode_alert(
            alert_path, active=bool(stuck), resolved=not active, since=episode or None,
            raise_event=lambda: host_off.event(sdir, CLASS, "; ".join(s["label"] for s in stuck),
                                               {"labels": [s["label"] for s in stuck]}, now),
            render=lambda: render(name, stuck, episode, stamp), issue=issue, close=close)

    if not active:
        if not dry_run:
            if state_path.exists():
                state_path.unlink(missing_ok=True)
            if alert_path.exists():
                page([])
        return []
    if uid == 0:
        return [f"{prefix} refused: running as root; only a user's own jobs are restarted"]
    state = _load(state_path)
    if state.get("episode") != episode:
        state = {"episode": episode, "kicked": {}}
    kicked: Dict[str, Any] = state.setdefault("kicked", {})
    specs = job_specs(Path(home) / "Library" / "LaunchAgents")
    pids = launchd_pids(run)
    rows = ps_snapshot(run)
    if pids is None or rows is None:
        return [f"{prefix} WARN could not read launchctl list or ps; no job was restarted"]
    candidates, _notes = find_stuck(specs, pids, rows, uid, run)
    lines: List[str] = []
    todo = []
    for item in candidates:
        evidence = item["evidence"][0]
        what = (f"{item['label']} (pid {item['pid']}, up {item['etime']}s > {item['limit']}s; "
                f"pid {evidence['pid']} {evidence['why']})")
        if item["label"] in kicked:
            continue
        if not enabled:
            lines.append(f"{prefix} would kickstart {what} ({ENV_SWITCH}=0: report only)")
            continue
        if dry_run:
            lines.append(f"{prefix} would kickstart {what} (dry run)")
            continue
        todo.append((item, what))
    if not todo:
        if not dry_run and enabled:
            _save(state_path, state)
        return lines
    for item, what in todo:
        target = f"gui/{uid}/{item['label']}"
        try:
            proc = run(["/bin/launchctl", "kickstart", "-k", target])
            ok, detail = proc.returncode == 0, (proc.stderr or proc.stdout or "").strip()[:200]
        except Exception as exc:  # noqa: BLE001 - recorded, then the kill decides
            ok, detail = False, str(exc)[:200]
        kicked[item["label"]] = {"at": stamp, "pid": item["pid"], "lstart": item["lstart"],
                                 "kickstart": "ok" if ok else f"failed: {detail}"}
        host_off.event(sdir, "tcc_stuck_kickstart", what,
                       {"label": item["label"], "pid": item["pid"], "ok": ok}, now)
        lines.append(f"{prefix} kickstarted {what}" + ("" if ok else f" FAILED ({detail})"))
    _save(state_path, state)
    sleep(SETTLE_S)
    after = ps_snapshot(run)
    killed = []
    for item, _what in todo:
        if after is None:
            # Without a fresh snapshot the pid may already be someone else's:
            # never signal it. Name it as stuck so a person looks.
            kicked[item["label"]]["outcome"] = "unverified"
            lines.append(f"{prefix} {item['label']}: WARN ps unreadable after the kickstart; "
                         f"pid {item['pid']} not signalled")
            continue
        if not same_process(item["pid"], item["lstart"], uid, after):
            kicked[item["label"]]["outcome"] = "restarted"
            lines.append(f"{prefix} {item['label']}: pid {item['pid']} exited after the kickstart")
            continue
        try:
            kill(item["pid"], signal.SIGKILL)
            kicked[item["label"]]["outcome"] = "killed"
            killed.append(item)
            lines.append(f"{prefix} {item['label']}: pid {item['pid']} survived the kickstart; sent SIGKILL")
        except ProcessLookupError:
            kicked[item["label"]]["outcome"] = "restarted"
        except OSError as exc:
            kicked[item["label"]]["outcome"] = f"kill failed: {exc}"
            lines.append(f"{prefix} {item['label']}: kill -KILL {item['pid']} FAILED ({exc})")
    if killed:
        sleep(KILL_SETTLE_S)
        after = ps_snapshot(run)
    stuck = []
    for item, _what in todo:
        survivors = [e for e in item["evidence"]
                     if same_process(e["pid"], e["lstart"], uid, after)]
        if survivors:
            kicked[item["label"]]["outcome"] = "stuck"
            stuck.append(dict(item, survivors=survivors))
    _save(state_path, state)
    if stuck:
        names = ", ".join(f"{s['label']} (pid " + ",".join(str(e["pid"]) for e in s["survivors"]) + ")"
                          for s in stuck)
        out = page(stuck)
        lines.append(f"{stamp} launchd-watchdog: WARN tcc-wedge needs reboot: still stuck on "
                     f"/Volumes after kickstart and SIGKILL: {names}"
                     + (f" (issue #{out.get('issue')})" if out.get("issue")
                        else f" (issue not opened: {out.get('issue_error') or 'pending'})"))
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    import argparse  # noqa: PLC0415
    import tcc_wedge  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="say what would be restarted")
    args = parser.parse_args(argv)
    for line in recovery_pass(tcc_wedge.read_state(), dry_run=args.dry_run):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
