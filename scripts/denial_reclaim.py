#!/usr/bin/env python3
"""Free regenerable caches when a VM lease is refused for disk, then retry once.

Why this exists: on 2026-11-08 m1's two Pulp gate slots sat idle for hours
with 15 macOS jobs queued. Every VM lease was refused `disk_capacity_exceeded`
(a 25 GiB floor plus 24 GiB growth per VM, about 52.6 GB, against 27 GiB
free) and nothing freed space on its own: the hourly `tartci reclaim --fix`
pass freed nothing, because the space was held by gate ccache entries older
than 14 days (28 GB down to 19 GB once evicted) and by OCI cache images that
no profile references, and neither belongs to that pass. A human freed both.

This is the same reclaim path (`tartci reclaim --on-lease-denial` delegates
here), narrowed to what is regenerable and fast enough to run while a lane
waits on a lease:

  * gate ccache entries unused for N days (default 14), through
    `gate_ccache_trim.evict`: the same command, the same "no VM running and
    no lease held" gate and the same pre-boot guard lock as the daily trim,
    without its opt-in or its 24 h interval;
  * Tart OCI cache images (`tart list --source oci`) that nothing references:
    not named by the installed fleet profile (every golden and every lane
    value), by any file under ~/.config/tartci or tartci's state, by a
    LaunchAgent plist, or by the profiles and manifests in this checkout.

Never touched: local VMs in any Tart store (only `--source oci` entries are
considered, and a name must carry a registry path), worktrees, checkouts and
build trees (the hourly pass owns those).

Bounded:
  * at most one run per `denial_reclaim_interval_minutes` (default 30) per
    host, stamped when a run STARTS so a crashed run also counts, under a
    non-blocking lock so two lanes refused at once run it once;
  * one overall budget (default 300 s) shared by every step, the reference
    scan included, so a reclaiming lane stays well inside the launchd
    watchdog's 900 s stale-boot bound (the lane heartbeats
    `disk-denial-reclaim` first);
  * a Tart store is touched (`tart list`, `tart delete`) only after its
    bounded storage-health probe says healthy, as the watchdog requires
    before its own `tart list`, so a hung kernel open cannot hang the lane;
  * an OCI image is kept while it runs, while it was accessed in the last
    `--oci-min-idle-hours` (default 24), and while any `tart pull` or
    `tart clone` is running on the host, checked again before EVERY
    delete: the first one seen stops the deletes.

Fail open: the lane retries admission only when this prints status `ran`.
Every other outcome, including an exception or a non-zero exit, leaves the
refusal exactly as it was.

Profile knobs, all optional, in `[reclaim]`:

    denial_reclaim = true                    # false turns it off
    denial_reclaim_interval_minutes = 30     # 5..1440
    denial_reclaim_ccache_max_age_days = 14  # 3..90 (else gate_ccache_max_age_days)

TARTCI_DISK_DENIAL_RECLAIM=0 turns it off for a lane without a profile edit.

Python 3.9-safe: lanes call it with a bare `python3`. Without tomllib the
profile is still scanned as text for references, and the defaults apply.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Iterator

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gate_ccache_trim  # noqa: E402
import store_health  # noqa: E402
import tart_image_prune  # noqa: E402

try:
    import fcntl
except ImportError:  # pragma: no cover - tartci hosts are POSIX
    fcntl = None  # type: ignore[assignment]

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.9 lanes
    tomllib = None  # type: ignore[assignment]

Runner = Callable[..., subprocess.CompletedProcess]

GIB = 1024 ** 3
DEFAULT_INTERVAL_MINUTES = 30
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 1440
DEFAULT_BUDGET_S = 300.0
DEFAULT_OCI_MIN_IDLE_HOURS = 24.0
STAMP_NAME = "denial-reclaim.json"
LOCK_NAME = "denial-reclaim.lock"
EVENTS_NAME = "events.jsonl"
SETTINGS_KEYS = frozenset({"denial_reclaim", "denial_reclaim_interval_minutes",
                           "denial_reclaim_ccache_max_age_days"})
# A registry reference: a host (with a dot or a port, or localhost), a slash,
# then a path. A local Tart VM name never has this shape, so a listing that
# ever mixed local VMs in cannot hand one to `tart delete`.
OCI_NAME_RE = re.compile(
    r"^(?:[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+|localhost|[A-Za-z0-9-]+:[0-9]+)"
    r"(?::[0-9]+)?/[A-Za-z0-9._/-]+(?::[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$")
TART_BUSY_RE = re.compile(r"(?:^|/)tart\s+(?:pull|clone)\b")


def validate(table: dict[str, Any]) -> list[str]:
    """Problems with the denial-reclaim keys of a `[reclaim]` table."""
    problems = []
    if type(table.get("denial_reclaim", True)) is not bool:
        problems.append("reclaim.denial_reclaim must be a boolean")
    interval = table.get("denial_reclaim_interval_minutes", DEFAULT_INTERVAL_MINUTES)
    if type(interval) is not int or not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES:
        problems.append("reclaim.denial_reclaim_interval_minutes must be an integer from "
                        f"{MIN_INTERVAL_MINUTES} through {MAX_INTERVAL_MINUTES}")
    age = table.get("denial_reclaim_ccache_max_age_days")
    if age is not None and (type(age) is not int or not gate_ccache_trim.MIN_MAX_AGE_DAYS
                            <= age <= gate_ccache_trim.MAX_MAX_AGE_DAYS):
        problems.append("reclaim.denial_reclaim_ccache_max_age_days must be an integer from "
                        f"{gate_ccache_trim.MIN_MAX_AGE_DAYS} through "
                        f"{gate_ccache_trim.MAX_MAX_AGE_DAYS}")
    return problems


def load_profile(profile: pathlib.Path) -> dict[str, Any]:
    """The parsed profile, or {} when it is absent, unreadable or unparseable here."""
    if tomllib is None:
        return {}
    try:
        with profile.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def settings(data: dict[str, Any], env: dict[str, str]) -> tuple[dict[str, Any] | None, str]:
    """The effective settings, or (None, why it is off)."""
    if env.get("TARTCI_DISK_DENIAL_RECLAIM", "1") == "0":
        return None, "TARTCI_DISK_DENIAL_RECLAIM=0"
    table = data.get("reclaim") if isinstance(data.get("reclaim"), dict) else {}
    problems = validate(table)
    if problems:
        return None, "; ".join(problems)
    if table.get("denial_reclaim", True) is False:
        return None, "[reclaim] denial_reclaim = false"
    host = data.get("host") if isinstance(data.get("host"), dict) else {}
    age = table.get("denial_reclaim_ccache_max_age_days")
    if age is None:
        fallback = table.get("gate_ccache_max_age_days", gate_ccache_trim.DEFAULT_MAX_AGE_DAYS)
        age = (fallback if type(fallback) is int and gate_ccache_trim.MIN_MAX_AGE_DAYS
               <= fallback <= gate_ccache_trim.MAX_MAX_AGE_DAYS
               else gate_ccache_trim.DEFAULT_MAX_AGE_DAYS)
    return {
        "interval_minutes": table.get("denial_reclaim_interval_minutes",
                                      DEFAULT_INTERVAL_MINUTES),
        "ccache_max_age_days": age,
        "cache": gate_ccache_trim.cache_dir(host.get("cache_root"), env),
    }, "enabled"


def profile_values(data: dict[str, Any]) -> set[str]:
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return found


class ScanDeadline(Exception):
    """The reference scan ran out of budget: nothing can be proven unreferenced."""


def reference_files(roots: Iterable[pathlib.Path], skip: Iterable[pathlib.Path],
                    deadline: float | None = None) -> list[pathlib.Path]:
    """Configuration-like files under roots: never logs, event journals, or our own receipts.

    A log that once printed an image name is history, not a reference; the
    reclaim receipts name what this module deleted, and reading them would
    keep a re-pulled image forever.
    """
    skipped = [str(path) for path in skip]
    files: list[pathlib.Path] = []
    for root in roots:
        if root.is_file():
            candidates: Iterable[pathlib.Path] = [root]
        elif root.is_dir():
            candidates = (p for p in root.rglob("*") if p.is_file())
        else:
            continue
        for path in candidates:
            if deadline is not None and time.monotonic() > deadline:
                raise ScanDeadline("reference scan ran out of budget")
            text = str(path)
            if path.suffix in (".log", ".jsonl") or any(
                    text == s or text.startswith(s + os.sep) for s in skipped):
                continue
            files.append(path)
    return files


def reference_text(files: list[pathlib.Path], deadline: float) -> str:
    """tart_image_prune.reference_text, one file at a time against the deadline."""
    chunks = []
    for path in files:
        if time.monotonic() > deadline:
            raise ScanDeadline("reference scan ran out of budget")
        chunks.append(tart_image_prune.reference_text([path]))
    return "\n".join(chunks)


StoreProbe = Callable[[pathlib.Path], "str | None"]


def store_unhealthy(home: pathlib.Path, state_path: pathlib.Path | None = None,
                    timeout_s: float = store_health.DEFAULT_TIMEOUT_S) -> str | None:
    """Why this Tart store must not be touched, or None when it is proven healthy.

    The same rule the launchd watchdog applies before its `tart list`: the
    bounded metadata probe, folded into the persisted health state for that
    store, must read `healthy`. Anything else (a timeout, a device change, an
    earlier failure still on record) keeps every Tart call off the store.
    """
    previous = store_health.load(str(state_path)) if state_path else {}
    if previous.get("path") not in (None, str(home)):
        previous = {}
    result = store_health.probe(str(home), timeout_s=timeout_s)
    state = store_health.next_state(previous, result)
    if state.get("state") == "healthy":
        return None
    return str((state.get("last_result") or {}).get("reason") or state.get("state"))


def split_oci(name: str) -> tuple[str, str | None, str | None]:
    """(repository, tag, digest) of an OCI cache name."""
    if "@" in name:
        repo, digest = name.split("@", 1)
        return repo, None, digest
    head, _, last = name.rpartition("/")
    if ":" in last:
        base, tag = last.split(":", 1)
        return f"{head}/{base}", tag, None
    return name, None, None


def tag_digest(oci_root: pathlib.Path, name: str) -> str | None:
    """The `repo@sha256:...` a cached tag points at, read from Tart's symlink."""
    repo, tag, _ = split_oci(name)
    if tag is None:
        return None
    link = oci_root / repo / tag
    try:
        target = os.readlink(link)
    except OSError:
        return None
    digest = os.path.basename(target.rstrip("/"))
    return f"{repo}@{digest}" if digest.startswith("sha256:") else None


def tart_busy(runner: Runner) -> str | None:
    """Why the OCI cache may be being written now, or None when provably not."""
    try:
        proc = runner(["ps", "-axo", "command="], capture_output=True, text=True,
                      timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"cannot read the process table: {exc}"
    if proc.returncode != 0:
        return f"ps exited {proc.returncode}"
    for line in proc.stdout.splitlines():
        if TART_BUSY_RE.search(line.strip()):
            return f"a tart pull or clone is running: {line.strip()[:120]}"
    return None


def plan_oci(entries: list[dict[str, Any]], *, values: set[str], text: str,
             oci_root: pathlib.Path, now: float, min_idle_hours: float) -> list[dict[str, Any]]:
    """A verdict per OCI cache entry: tags first, then digests."""
    rows: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("Name") or "")
        row = {"name": name, "size_gb": entry.get("Size"), "verdict": "keep", "reason": ""}
        rows.append(row)
        if str(entry.get("Source") or "").lower() != "oci":
            row["reason"] = "not an OCI cache entry"
        elif not OCI_NAME_RE.match(name):
            row["reason"] = "not a registry reference"
        elif entry.get("Running") is True:
            row["reason"] = "running"
    kept_digests: set[str] = set()
    unresolved_repos: set[str] = set()
    referenced_repos: set[str] = set()
    for row in rows:
        if row["reason"]:
            continue
        repo, tag, _ = split_oci(row["name"])
        if tart_image_prune.named_in(repo, text) or repo in values:
            referenced_repos.add(repo)
        if row["name"] in values or tart_image_prune.named_in(row["name"], text):
            row["reason"] = "referenced"
            if tag is not None:
                digest = tag_digest(oci_root, row["name"])
                if digest is None:
                    unresolved_repos.add(repo)
                else:
                    kept_digests.add(digest)
    by_name = {str(e.get("Name") or ""): e for e in entries}
    for row in rows:
        if row["reason"]:
            continue
        repo, tag, digest = split_oci(row["name"])
        accessed = tart_image_prune.accessed_ts(by_name[row["name"]].get("Accessed"))
        if repo in referenced_repos:
            row["reason"] = "its repository is referenced without a tag"
        elif digest is not None and row["name"] in kept_digests:
            row["reason"] = "the digest of a referenced tag"
        elif digest is not None and repo in unresolved_repos:
            row["reason"] = "a referenced tag of this repository could not be resolved"
        elif accessed is None:
            row["reason"] = "last access unreadable"
        elif now - accessed < min_idle_hours * 3600:
            row["reason"] = f"accessed within {min_idle_hours:g} h"
        else:
            row["verdict"] = "delete"
            row["reason"] = "unreferenced"
    rows.sort(key=lambda r: (r["verdict"] != "delete", "@" in r["name"], r["name"]))
    return rows


def free_bytes(path: pathlib.Path) -> int | None:
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    return stats.f_bavail * stats.f_frsize


@contextlib.contextmanager
def try_lock(path: pathlib.Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield False
        return
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_stamp(state_dir: pathlib.Path) -> float | None:
    try:
        value = json.loads((state_dir / STAMP_NAME).read_text()).get("started_at")
    except (OSError, ValueError, AttributeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def append_event(state_dir: pathlib.Path, report: dict[str, Any]) -> None:
    oci = report.get("oci") or {}
    ccache = report.get("ccache") or {}
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "reclaim_on_lease_denial",
        "detail": (f"status={report.get('status')} freed={int(report.get('freed_bytes') or 0) / GIB:.1f}GiB "
                   f"ccache={ccache.get('status')} oci_deleted={oci.get('deleted', 0)} "
                   f"lane={report.get('lane') or '-'}"),
        "fields": {k: report.get(k) for k in (
            "status", "reason", "lane", "free_before", "free_after", "freed_bytes",
            "required_bytes", "elapsed_s")},
    }
    try:
        with (state_dir / EVENTS_NAME).open("a") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError:
        pass


def run_oci(*, tart_homes: list[pathlib.Path], values: set[str], text: str, now: float,
            min_idle_hours: float, fix: bool, deadline: float, runner: Runner,
            store_probe: StoreProbe | None = None) -> dict[str, Any]:
    """Plan, then (with fix) delete, unreferenced OCI cache images. Never raises."""
    report: dict[str, Any] = {"deleted": 0, "planned": 0, "stores": []}
    busy = tart_busy(runner)
    if busy:
        return {**report, "status": "skipped", "reason": busy}
    errors: list[str] = []
    for home in tart_homes:
        if report.get("stopped"):
            break
        store: dict[str, Any] = {"tart_home": str(home)}
        report["stores"].append(store)
        env = {**os.environ, "TART_HOME": str(home)}
        remaining = deadline - time.monotonic()
        if remaining <= 5:
            store["status"] = "skipped"
            store["reason"] = "budget spent"
            continue
        sick = (store_probe or store_unhealthy)(home)
        if sick:
            store["status"] = "skipped"
            store["reason"] = f"Tart store health is not healthy: {sick}"
            continue
        try:
            listing = runner(["tart", "list", "--source", "oci", "--format", "json"],
                             capture_output=True, text=True, timeout=min(120.0, remaining),
                             check=False, env=env)
            entries = json.loads(listing.stdout) if listing.returncode == 0 else None
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            entries, listing = None, None
            store["reason"] = f"tart list failed: {exc}"
        if not isinstance(entries, list):
            store["status"] = "skipped"
            store.setdefault("reason", "tart list failed: "
                             + ((listing.stderr or "").strip()[:200] if listing else ""))
            continue
        rows = plan_oci(entries, values=values, text=text,
                        oci_root=home / "cache" / "OCIs", now=now,
                        min_idle_hours=min_idle_hours)
        store["rows"] = rows
        planned = [row for row in rows if row["verdict"] == "delete"]
        report["planned"] += len(planned)
        store["status"] = "planned"
        if not fix:
            continue
        store["status"] = "applied"
        for row in planned:
            remaining = deadline - time.monotonic()
            if remaining <= 5:
                row["deleted"] = False
                errors.append("budget spent before deleting " + row["name"])
                break
            # A pull or clone that started after the plan may be writing (or
            # reading) the very entry about to go: look again every time.
            busy = tart_busy(runner)
            if busy:
                row["deleted"] = False
                report["stopped"] = busy
                break
            try:
                proc = runner(["tart", "delete", row["name"]], capture_output=True, text=True,
                              timeout=min(300.0, remaining), check=False, env=env)
            except (OSError, subprocess.SubprocessError) as exc:
                row["deleted"] = False
                errors.append(f"tart delete {row['name']}: {exc}")
                continue
            row["deleted"] = proc.returncode == 0
            if proc.returncode == 0:
                report["deleted"] += 1
            else:
                errors.append(f"tart delete {row['name']}: {(proc.stderr or '').strip()[:200]}")
    report["status"] = "applied" if fix else "planned"
    if report.get("stopped"):
        report["status"] = "stopped"
    if errors:
        report["errors"] = errors
    return report


def default_tart_homes(data: dict[str, Any], env: dict[str, str]) -> list[pathlib.Path]:
    """The Tart stores whose OCI caches are judged: the configured one and Tart's default."""
    host = data.get("host") if isinstance(data.get("host"), dict) else {}
    homes = []
    for candidate in (env.get("TART_HOME"), host.get("tart_home"),
                      str(pathlib.Path.home() / ".tart")):
        if isinstance(candidate, str) and candidate:
            path = pathlib.Path(candidate).expanduser()
            if path.is_dir() and path.resolve() not in [h.resolve() for h in homes]:
                homes.append(path)
    return homes


def run(args: argparse.Namespace, *, env: dict[str, str] | None = None,
        runner: Runner = subprocess.run, now: float | None = None,
        busy_probe: Callable[[], str | None] | None = None,
        ccache: str | None = None,
        store_probe: StoreProbe | None = None) -> dict[str, Any]:
    """One lease-denial reclaim. Never raises; the caller retries only on status `ran`."""
    env = dict(os.environ if env is None else env)
    now = time.time() if now is None else now
    started = time.monotonic()
    deadline = started + float(args.budget_seconds)
    data = load_profile(pathlib.Path(args.profile).expanduser())
    effective, why = settings(data, env)
    report: dict[str, Any] = {"lane": args.lane, "required_bytes": args.required_bytes,
                              "mode": "fix" if args.fix else "plan"}
    if effective is None:
        return {**report, "status": "disabled", "reason": why}
    state_dir = pathlib.Path(args.state_dir).expanduser()
    with try_lock(state_dir / LOCK_NAME) as held:
        if not held:
            return {**report, "status": "busy", "reason": "another lane is running it"}
        last = read_stamp(state_dir)
        interval_s = effective["interval_minutes"] * 60
        if args.fix and last is not None and 0 <= now - last < interval_s:
            return {**report, "status": "cooldown", "last_started_at": last,
                    "interval_minutes": effective["interval_minutes"]}
        if args.fix:
            write_json(state_dir / STAMP_NAME, {"started_at": now, "lane": args.lane})
        measure_path = pathlib.Path(args.measure_path).expanduser() if args.measure_path else None
        report["free_before"] = free_bytes(measure_path) if measure_path else None
        home = pathlib.Path.home()
        tartci_root = pathlib.Path(__file__).resolve().parents[1]
        if store_probe is None:
            health_state = pathlib.Path(env.get("TARTCI_HOME", str(home / ".tartci"))
                                        ).expanduser() / "state" / "store-health.json"
            store_probe = lambda path: store_unhealthy(path, health_state)  # noqa: E731
        try:
            # Half the budget at most: the ccache step after it needs the rest.
            scan_deadline = min(deadline, started + float(args.budget_seconds) / 2)
            files = reference_files(
                [home / ".config/tartci", home / ".tartci/state", home / "Library/LaunchAgents",
                 tartci_root / "profiles", tartci_root / "manifests",
                 pathlib.Path(args.profile).expanduser()],
                skip=[state_dir], deadline=scan_deadline)
            text = reference_text(files, scan_deadline)
        except ScanDeadline as exc:
            text = None
            report["oci"] = {"status": "skipped", "reason": str(exc), "deleted": 0}
        try:
            if text is None:
                raise ScanDeadline("skipped")
            report["oci"] = run_oci(
                tart_homes=[pathlib.Path(p) for p in args.tart_home] if args.tart_home
                else default_tart_homes(data, env),
                values=profile_values(data), text=text, now=now,
                min_idle_hours=args.oci_min_idle_hours, fix=args.fix, deadline=deadline,
                runner=runner, store_probe=store_probe)
        except ScanDeadline:
            pass
        except Exception as exc:  # noqa: BLE001 - one step must not take the run down
            report["oci"] = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        remaining = deadline - time.monotonic()
        if remaining <= 10:
            report["ccache"] = {"status": "skipped", "reason": "budget spent"}
        else:
            try:
                report["ccache"] = gate_ccache_trim.evict(
                    cache=effective["cache"], max_age_days=effective["ccache_max_age_days"],
                    fix=args.fix, ccache=ccache or gate_ccache_trim.ccache_guard.resolve_ccache(),
                    busy_probe=busy_probe or gate_ccache_trim.ccache_guard.host_busy,
                    runner=runner, timeout=remaining, measure_entries=False)
            except Exception as exc:  # noqa: BLE001
                report["ccache"] = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}
        report["free_after"] = free_bytes(measure_path) if measure_path else None
        if report["free_before"] is not None and report["free_after"] is not None:
            report["freed_bytes"] = max(0, report["free_after"] - report["free_before"])
        report["elapsed_s"] = round(time.monotonic() - started, 1)
        report["status"] = "ran" if args.fix else "planned"
        if args.fix:
            write_json(state_dir / STAMP_NAME, {
                "started_at": now, "lane": args.lane, "finished_at": time.time(),
                "freed_bytes": report.get("freed_bytes"),
                "ccache": (report.get("ccache") or {}).get("status"),
                "oci_deleted": (report.get("oci") or {}).get("deleted", 0)})
            append_event(state_dir, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--fix", action="store_true",
                        help="delete; without it, only plan (and no cooldown is spent)")
    parser.add_argument("--lane", default="", help="the lane whose lease was refused")
    parser.add_argument("--required-bytes", type=int, default=None)
    parser.add_argument("--measure-path", default=os.environ.get("TART_HOME"),
                        help="report free space before and after on this path's volume "
                             "(default: $TART_HOME)")
    parser.add_argument("--tart-home", action="append", default=[],
                        help="a Tart store whose OCI cache is judged (repeatable; default: "
                             "$TART_HOME, the profile's [host].tart_home and ~/.tart)")
    parser.add_argument("--profile", default=os.environ.get(
        "TARTCI_FLEET_PROFILE",
        str(pathlib.Path.home() / ".config/tartci/macos-fleet-profile.toml")))
    parser.add_argument("--state-dir", default=os.environ.get("TARTCI_RECLAIM_STATE_DIR") or str(
        pathlib.Path(os.environ.get("TARTCI_HOME", str(pathlib.Path.home() / ".tartci")))
        .expanduser() / "state" / "reclaim"))
    parser.add_argument("--budget-seconds", type=float, default=float(os.environ.get(
        "TARTCI_DISK_DENIAL_RECLAIM_BUDGET_SECS", str(DEFAULT_BUDGET_S))))
    parser.add_argument("--oci-min-idle-hours", type=float, default=DEFAULT_OCI_MIN_IDLE_HOURS)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.budget_seconds <= 0 or args.oci_min_idle_hours < 1:
        print(json.dumps({"status": "error",
                          "reason": "--budget-seconds must be > 0 and --oci-min-idle-hours >= 1"}))
        return 2
    try:
        report = run(args)
    except Exception as exc:  # noqa: BLE001 - the caller fails open on anything else
        print(json.dumps({"status": "error", "reason": f"{type(exc).__name__}: {exc}"}))
        return 1
    if args.json:
        print(json.dumps(report, sort_keys=True, default=str))
    else:
        freed = report.get("freed_bytes")
        print(f"denial-reclaim: {report.get('status')}"
              + (f" ({report.get('reason')})" if report.get("reason") else "")
              + (f"; freed {freed / GIB:.1f} GiB" if isinstance(freed, int) else ""))
        for store in (report.get("oci") or {}).get("stores", []):
            for row in store.get("rows", []):
                print(f"  oci {row['verdict']:6} {row['name']}  ({row['reason']})")
        ccache_report = report.get("ccache") or {}
        if ccache_report:
            print(f"  ccache {ccache_report.get('status')}"
                  + (f" ({ccache_report.get('reason')})" if ccache_report.get("reason") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
