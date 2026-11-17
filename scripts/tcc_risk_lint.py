#!/usr/bin/env python3
"""Report LaunchAgents whose macOS privacy identity is unstable, or that keep
their script or working directory on /Volumes. Report only.

launchd attributes a job's privacy (TCC) requests to its program. A program
whose identity changes (an ad-hoc binary is identified by its code hash; a
Homebrew Cellar interpreter moves and is re-signed ad hoc on every
`brew upgrade`) is a new identity each time and can raise a new consent alert.
While one alert waits on an unattended host, every launchd open() of an
external volume without a stored grant blocks, and the blocked jobs stay
blocked after the alert goes away (m3, 2026-11-16: com.pulp.fleet.prewall and
three fleet-pass must-review.py children held in open() on /Volumes/Example
for hours). A job that keeps its script or working directory on /Volumes
touches the store on every start, so it is the first to hang.

For each LOADED agent in ~/Library/LaunchAgents this flags:

  * ``cellar``: the program, or the interpreter it resolves to (symlinks
    followed, a venv's pyvenv.cfg read), is under a Homebrew Cellar;
  * ``adhoc`` / ``unsigned``: that binary is ad-hoc signed or not signed;
  * ``volumes``: the program, a script argument or the WorkingDirectory is
    under /Volumes;

unless the program is a signed launcher bundle (TartCILauncher.app or
ShipyardLauncher.app), whose grant is keyed to its bundle ID and team.

Nothing under /Volumes is ever stat'ed, read or signature-checked: those
checks are lexical, because a stat on a wedged store hangs. Symlinks are
followed one hop at a time with readlink, stopping at the first hop that lands
on /Volumes. `codesign -dv` results are cached by path and mtime, and at most
MAX_CODESIGN_PER_PASS uncached binaries are read in one pass.

Each flagged agent is printed as ``WARN tcc-risk <label>: <reason>``; a newly
flagged label is an event, and the host carries one ``tcc_risk`` incident (a
GitHub issue with the tartci-incident marker) while anything is flagged.
Nothing is unloaded or changed.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import host_off  # noqa: E402 - sibling module
import launchd_identity_lint  # noqa: E402 - sibling module (codesign_info)
import tartci_incident  # noqa: E402 - sibling module

CLASS = "tcc_risk"
TITLE = "[tartci] {host} runs LaunchAgents that can raise a privacy prompt"
INTERVAL_S = 15 * 60
MAX_CODESIGN_PER_PASS = 8
MAX_LINK_HOPS = 40
LAUNCHER_BUNDLES = ("TartCILauncher.app", "ShipyardLauncher.app")
PLATFORM_ROOTS = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/libexec/", "/System/")
LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

Codesign = Callable[[str], Dict[str, Any]]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def on_volumes(path: str) -> bool:
    """Lexical only: never stat a path that may sit on a wedged store."""
    normal = os.path.normpath(path) if path.startswith("/") else path
    return normal == "/Volumes" or normal.startswith("/Volumes/")


def safe_realpath(path: str, readlink: Callable[[str], str] = os.readlink) -> Tuple[str, bool]:
    """`path` with every symlink resolved, and whether resolution stopped
    because a hop landed under /Volumes (which is then never touched)."""
    parts = [p for p in path.split("/") if p]
    resolved = ""
    hops = 0
    while parts:
        part = parts.pop(0)
        if part == ".":
            continue
        if part == "..":
            resolved = resolved.rsplit("/", 1)[0]
            continue
        candidate = f"{resolved}/{part}"
        if on_volumes(candidate):
            rest = "/".join(parts)
            return (f"{candidate}/{rest}" if rest else candidate), True
        try:
            target = readlink(candidate)
        except OSError:
            resolved = candidate
            continue
        hops += 1
        if hops > MAX_LINK_HOPS:
            return candidate, False
        if target.startswith("/"):
            resolved = ""
        parts = [p for p in target.split("/") if p] + parts
    return resolved or "/", False


def venv_base(program: str, read_text: Callable[[str], str],
              readlink: Callable[[str], str] = os.readlink) -> Optional[str]:
    """The base interpreter of a venv's bin/python, from its pyvenv.cfg."""
    if on_volumes(program):
        return None
    cfg = os.path.join(os.path.dirname(os.path.dirname(program)), "pyvenv.cfg")
    # pyvenv.cfg may itself be a symlink onto /Volumes: open() would follow it.
    resolved, stopped = safe_realpath(cfg, readlink)
    if stopped or on_volumes(resolved):
        return None
    try:
        text = read_text(cfg)
    except OSError:
        return None
    values: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    if values.get("executable", "").startswith("/"):
        return values["executable"]
    home = values.get("home", "")
    if home.startswith("/"):
        return os.path.join(home, os.path.basename(program))
    return None


def shebang(path: str, read_head: Callable[[str], bytes]) -> Optional[List[str]]:
    """The interpreter argv of a `#!` script, or None for a binary."""
    if on_volumes(path):
        return None
    try:
        head = read_head(path)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    line = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    return line.split() or None


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read(4096)


def _read_head(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(256)


def _mtime(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


class CodesignCache:
    """`codesign -dv` per (path, mtime), with a per-pass budget."""

    def __init__(self, entries: Optional[Dict[str, Any]] = None, *,
                 codesign: Optional[Codesign] = None, mtime: Callable[[str], Optional[float]] = _mtime,
                 budget: int = MAX_CODESIGN_PER_PASS) -> None:
        self.entries = dict(entries or {})
        self.codesign = codesign or launchd_identity_lint.codesign_info
        self.mtime = mtime
        self.budget = budget
        self.calls = 0

    def info(self, path: str) -> Optional[Dict[str, Any]]:
        """The signature of `path`, or None when this pass's budget is spent."""
        stamp = self.mtime(path)
        cached = self.entries.get(path)
        if isinstance(cached, dict) and cached.get("mtime") == stamp and isinstance(cached.get("info"), dict):
            return cached["info"]
        if self.calls >= self.budget:
            return None
        self.calls += 1
        info = self.codesign(path)
        self.entries[path] = {"mtime": stamp, "info": info}
        return info


def _is_launcher(path: str) -> bool:
    return any(f"/{bundle}/Contents/MacOS/" in path for bundle in LAUNCHER_BUNDLES)


def _which(name: str, path_value: str,
           readlink: Callable[[str], str] = os.readlink) -> Optional[str]:
    """The program `name` names on `path_value`. Nothing on /Volumes, and no
    symlink that resolves there, is ever stat'ed: such a hit is returned
    unchecked so judge_binary flags it without touching the volume."""
    if "/" in name:
        # An absolute target is not searched for; guard it as is.
        resolved, stopped = safe_realpath(name, readlink)
        if name.startswith("/") and (stopped or on_volumes(name) or on_volumes(resolved)):
            return name
        return shutil.which(name, path="")
    for entry in path_value.split(":"):
        if not entry or on_volumes(entry):
            continue
        candidate = os.path.join(entry, name)
        resolved, stopped = safe_realpath(candidate, readlink)
        if stopped or on_volumes(resolved):
            return candidate
        found = shutil.which(name, path=entry)
        if found:
            return found
    return None


def judge_binary(program: str, cache: CodesignCache, *,
                 readlink: Callable[[str], str] = os.readlink,
                 read_text: Callable[[str], str] = _read_text,
                 read_head: Callable[[str], bytes] = _read_head,
                 path_value: str = LAUNCHD_PATH, depth: int = 0) -> Dict[str, str]:
    """{verdict: ok|flag|pending|launcher, reason} for the binary launchd runs."""
    if on_volumes(program):
        return {"verdict": "flag", "kind": "volumes", "reason": f"program {program} is on /Volumes"}
    resolved, stopped = safe_realpath(program, readlink)
    if stopped:
        return {"verdict": "flag", "kind": "volumes",
                "reason": f"program {program} resolves onto /Volumes ({resolved})"}
    if _is_launcher(resolved):
        return {"verdict": "launcher", "kind": "launcher", "reason": "a signed launcher bundle"}
    shown = program if resolved == program else f"{program} -> {resolved}"
    base = venv_base(program, read_text, readlink)
    if base and depth < 2:
        verdict = judge_binary(base, cache, readlink=readlink, read_text=read_text,
                               read_head=read_head, path_value=path_value, depth=depth + 1)
        if verdict["verdict"] in ("flag", "pending"):
            verdict = dict(verdict, reason=f"venv {program} runs {verdict['reason']}")
        return verdict
    if "/Cellar/" in resolved:
        return {"verdict": "flag", "kind": "cellar",
                "reason": f"{shown} is a Homebrew Cellar binary (ad-hoc, a new privacy "
                          "identity on every brew upgrade)"}
    if resolved.startswith(PLATFORM_ROOTS):
        return {"verdict": "ok", "kind": "platform", "reason": "an Apple binary"}
    interpreter = shebang(resolved, read_head)
    if interpreter and depth < 2:
        # A script's privacy identity is its interpreter's.
        target = interpreter[0]
        if os.path.basename(target) == "env" and len(interpreter) > 1:
            found = _which(interpreter[1], path_value, readlink)
            if not found:
                return {"verdict": "ok", "kind": "unresolved",
                        "reason": f"{shown} runs `{interpreter[1]}`, not on the job's PATH"}
            target = found
        verdict = judge_binary(target, cache, readlink=readlink, read_text=read_text,
                               read_head=read_head, path_value=path_value, depth=depth + 1)
        if verdict["verdict"] in ("flag", "pending"):
            verdict = dict(verdict, reason=f"script {shown} runs {verdict['reason']}")
        return verdict
    info = cache.info(resolved)
    if info is None:
        return {"verdict": "pending", "kind": "pending",
                "reason": f"{shown}: signature not read yet (per-pass budget)"}
    if not info.get("signed"):
        return {"verdict": "flag", "kind": "unsigned",
                "reason": f"{shown} is not signed ({info.get('error') or 'no signature'})"}
    if info.get("adhoc"):
        return {"verdict": "flag", "kind": "adhoc",
                "reason": f"{shown} is ad-hoc signed (its identity is its code hash)"}
    return {"verdict": "ok", "kind": "signed", "reason": f"signed by team {info.get('team')}"}


def judge_agent(value: Dict[str, Any], cache: CodesignCache, **kwargs: Any) -> List[Dict[str, str]]:
    """Every tcc-risk finding for one plist (empty when it is fine)."""
    program = launchd_identity_lint.program_of(value)
    if not program:
        return []
    env = value.get("EnvironmentVariables") if isinstance(value.get("EnvironmentVariables"), dict) else {}
    path_value = str(env.get("PATH") or LAUNCHD_PATH)
    binary = judge_binary(program, cache, path_value=path_value, **kwargs)
    if binary["verdict"] == "launcher":
        return []
    findings = [binary] if binary["verdict"] in ("flag", "pending") else []
    arguments = value.get("ProgramArguments") if isinstance(value.get("ProgramArguments"), list) else []
    for argument in arguments[1:]:
        if isinstance(argument, str) and on_volumes(argument):
            findings.append({"verdict": "flag", "kind": "volumes",
                             "reason": f"script argument {argument} is on /Volumes"})
            break
    cwd = value.get("WorkingDirectory")
    if isinstance(cwd, str) and on_volumes(cwd):
        findings.append({"verdict": "flag", "kind": "volumes",
                         "reason": f"WorkingDirectory {cwd} is on /Volumes"})
    return findings


def loaded_labels(run: Optional[Callable[..., Any]] = None) -> Optional[set]:
    """Labels `launchctl list` reports in this user's domain, or None."""
    try:
        if run is None:
            import bounded_subprocess  # noqa: PLC0415 - sibling module
            proc = bounded_subprocess.run_bounded(["/bin/launchctl", "list"], timeout=15,
                                                  operation="launchctl-list")
        else:
            proc = run(["/bin/launchctl", "list"])
    except Exception:  # noqa: BLE001 - an unreadable domain scans every plist
        return None
    if proc.returncode != 0:
        return None
    labels = set()
    for line in (proc.stdout or "").splitlines()[1:]:
        fields = line.split("\t")
        if len(fields) >= 3:
            labels.add(fields[2].strip())
    return labels


def scan(agents_dir: Path, cache: CodesignCache, loaded: Optional[Iterable[str]] = None,
         **kwargs: Any) -> Dict[str, List[Dict[str, str]]]:
    """label -> findings, for every loaded plist with at least one."""
    wanted = None if loaded is None else set(loaded)
    out: Dict[str, List[Dict[str, str]]] = {}
    for path in sorted(Path(agents_dir).glob("*.plist")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = plistlib.loads(path.read_bytes())
        except Exception:  # noqa: BLE001 - one unreadable plist must not stop the scan
            continue
        if not isinstance(value, dict):
            continue
        label = str(value.get("Label") or path.stem)
        if wanted is not None and label not in wanted:
            continue
        findings = judge_agent(value, cache, **kwargs)
        if findings:
            out[label] = findings
    return out


def render(host: str, flagged: Dict[str, List[Dict[str, str]]], since: str,
           detected: str) -> Tuple[str, str]:
    rows = [f"- `{label}`: " + "; ".join(f["reason"] for f in findings)
            for label, findings in sorted(flagged.items())]
    body = "\n".join([
        "## What happened",
        f"`{host}` has loaded LaunchAgents whose macOS privacy identity is unstable, or that "
        "keep their script or working directory on /Volumes. Any of them can raise a privacy "
        "consent alert (Removable Volumes, Full Disk Access); while it waits unanswered, every "
        "launchd open of the Tart store blocks and the blocked jobs stay blocked.", "",
        "## Evidence", *rows, "",
        "## Fix",
        "Run the agent through a stable identity: an Apple binary (/bin/bash, /usr/bin/python3), "
        "the python.org framework python at its versioned path, or a signed launcher bundle "
        "(TartCILauncher.app or ShipyardLauncher.app). Move scripts and WorkingDirectory off "
        "/Volumes onto the internal volume.", "",
        "## Safe to auto-apply",
        "No. The watchdog reports only; it never unloads an agent.", "",
        "## How to verify",
        "The watchdog log stops printing `WARN tcc-risk`; this issue then closes itself.",
    ])
    marker = tartci_incident.marker(host, CLASS, since, detected)
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


def watchdog_pass(now: Optional[float] = None, *, home: Optional[str] = None,
                  state_dir: Optional[Path] = None, loaded: Optional[Iterable[str]] = None,
                  codesign: Optional[Codesign] = None, host: Optional[str] = None,
                  issue: Optional[Callable[..., Any]] = None,
                  close: Optional[Callable[..., Any]] = None,
                  interval_s: int = INTERVAL_S, **kwargs: Any) -> List[str]:
    """One watchdog pass: WARN lines for each flagged agent (at most every
    `interval_s`), an event per newly flagged label, one host incident."""
    now = time.time() if now is None else now
    home = home or os.path.expanduser("~")
    sdir = state_dir or Path(os.environ.get("TARTCI_HOME") or os.path.join(home, ".tartci")) / "state"
    state_path = sdir / "tcc-risk-lint.json"
    state = _load(state_path)
    if now - float(state.get("checked_at") or 0) < interval_s:
        return []
    cache = CodesignCache(state.get("codesign"), codesign=codesign)
    labels = loaded if loaded is not None else loaded_labels()
    flagged = scan(Path(home) / "Library" / "LaunchAgents", cache, labels, **kwargs)
    hard = {label: f for label, f in flagged.items() if any(x["verdict"] == "flag" for x in f)}
    previous = state.get("flagged") if isinstance(state.get("flagged"), dict) else {}
    since = state.get("since") if hard and state.get("since") else (_iso(now) if hard else None)
    for label in sorted(set(hard) - set(previous)):
        host_off.event(sdir, "tcc_risk", f"{label}: " + "; ".join(f["reason"] for f in hard[label]),
                       {"label": label}, now)
    host_name = host or _host()
    detected = _iso(now)
    out = host_off.episode_alert(
        sdir / "tcc-risk-alert.json", active=bool(hard), resolved=not hard, since=since,
        raise_event=lambda: None,
        render=lambda: render(host_name, hard, since or detected, detected),
        issue=issue, close=close)
    lines = []
    for label, findings in sorted(flagged.items()):
        for finding in findings:
            lines.append(f"{detected} launchd-watchdog: WARN tcc-risk {label}: {finding['reason']}"
                         + (" (pending)" if finding["verdict"] == "pending" else ""))
    if hard and out.get("issue"):
        lines.append(f"{detected} launchd-watchdog: tcc-risk: {len(hard)} agent(s) flagged "
                     f"(issue #{out['issue']}; report only)")
    _save(state_path, {"checked_at": now, "since": since, "codesign": cache.entries,
                       "flagged": {label: [f["reason"] for f in findings]
                                   for label, findings in hard.items()}})
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agents-dir", default=os.path.expanduser("~/Library/LaunchAgents"))
    parser.add_argument("--all", action="store_true", help="also scan agents that are not loaded")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    cache = CodesignCache(budget=10_000)
    flagged = scan(Path(args.agents_dir), cache, None if args.all else loaded_labels())
    if args.json:
        print(json.dumps(flagged, indent=2, sort_keys=True))
    else:
        for label, findings in sorted(flagged.items()):
            for finding in findings:
                print(f"WARN tcc-risk {label}: {finding['reason']}")
    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
