#!/usr/bin/env python3
"""Per-job ccache write layers, promoted into the shared cache only on green.

The problem this solves: every macOS guest mounted the host ccache read-write,
so a job torn down mid-build (a Tart hang, a timeout, a cancel) could leave
half-written direct-mode manifests that every later job then trusted. With
write isolation on, a guest reads the shared cache but writes only to its own
job layer, and this module is the host side that decides what happens to that
layer after the job:

  settle   decide the verdict (green or red) from the supervisor's own job
           evidence and move the layer to ``green/`` or ``discard/``;
  process  for a green layer, wait until the VM is provably gone, then promote
           each entry into ``shared/``; for a discarded layer, delete it;
  sweep    reclaim what an interrupted supervisor left behind: an active layer
           whose owner is dead is discarded (never promoted), a green layer
           whose promoter died is promoted again (promotion is idempotent);
  trim     keep ``shared/`` under a size cap, oldest entries first.

The guest has NO writable ccache mount on the host. It mounts the shared store
read-only and writes its new entries to a guest-local directory; when the job
ends green, the host pulls those entries over ssh as a tar stream (``receive``)
and validates every member before it lands in a job layer. A guest therefore
holds no path, link or inode on the host cache filesystem it could write.

Layout under the layers root ($CACHE_ROOT/ccache-layers-v1, a sibling of the
legacy ccache directory, so no legacy read-write mount reaches it):

  shared/<k0k1>/<rest>      ccache ``file:`` remote-storage layout; guests mount
                            it read-only
  jobs/<vm>/remote/         entries received from a green guest (host-only)
  jobs/<vm>/stats.json      the guest's own hit/miss counters, as data
  green/<vm>/  discard/<vm>/

Host-only state (never mounted into a guest) lives in a separate state
directory: owner pids, per-layer locks, and ``audit.jsonl``.

Promotion rules (the tests in test_ccache_layer.py pin each one):
  * only a green verdict promotes; red, cancelled, timed-out, orphaned or
    unproved-teardown layers are deleted without touching ``shared/``;
  * a result entry is published with a hard link, which fails if the key
    already exists, so an existing entry is never overwritten;
  * a manifest may replace an existing manifest only when it is strictly newer,
    under a lock, through a temp file and an atomic rename;
  * anything that is not a regular file with a ccache entry header in a
    well-formed key path is rejected, and nothing is ever followed through a
    symlink.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

ENTRY_MAGIC = b"\xcc\xac"
ENTRY_HEADER_MIN = 15
ENTRY_TYPE_RESULT = 0
ENTRY_TYPE_MANIFEST = 1
KEY_DIR = re.compile(r"^[0-9a-z]{2}$")
KEY_NAME = re.compile(r"^[0-9a-z]{8,128}$")
VM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
GREEN_RESULTS = ("Succeeded", "SucceededWithIssues")
TMP_PREFIX = ".tartci-tmp-"
TRIM_STAMP = ".tartci-last-trim"
DEFAULT_VM_GONE_TIMEOUT = 1800.0
DEFAULT_TRIM_INTERVAL = 3600.0
STALE_TMP_SECONDS = 3600.0


# ── Verdict ────────────────────────────────────────────────────────────────


def runner_log_result(path: Path | None) -> str | None:
    """The Actions runner's own ``Job … completed with result: X`` value."""
    if path is None:
        return None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    result = None
    for line in text.splitlines():
        marker = "completed with result: "
        if ": Job " in line and marker in line:
            result = line.rsplit(marker, 1)[1].strip()
    return result


def verdict(runner_rc: int, capture_status: str, receipt: str, quarantine: str,
            local_result: str | None) -> tuple[str, str]:
    """(``green``|``red``, reason). Anything short of positive proof is red."""
    if runner_rc != 0:
        return "red", f"runner_rc={runner_rc}"
    if quarantine not in ("", "none"):
        return "red", f"quarantine={quarantine}"
    if local_result is None:
        return "red", "no_completion_line"
    if local_result not in GREEN_RESULTS:
        return "red", f"result={local_result}"
    if capture_status == "terminal":
        try:
            conclusion = json.loads(receipt or "{}").get("conclusion")
        except (json.JSONDecodeError, AttributeError):
            return "red", "receipt_unreadable"
        if conclusion != "success":
            return "red", f"conclusion={conclusion}"
    return "green", f"result={local_result}"


# ── Paths, locks, audit ────────────────────────────────────────────────────


class Layout:
    def __init__(self, root: Path, state: Path) -> None:
        self.root = root
        self.state = state
        self.shared = root / "shared"
        self.jobs = root / "jobs"
        self.green = root / "green"
        self.discard = root / "discard"
        self.owners = state / "owners"
        self.locks = state / "locks"
        self.promotions = state / "promotions"
        self.audit = state / "audit.jsonl"

    def ensure(self) -> None:
        # Tart mounts shared/ and jobs/<vm> by path, and every move below is a
        # rename by path: a symlink anywhere on the way would mount, promote
        # into or delete whatever it points at. mkdir(exist_ok) accepts a link
        # to a directory, so check before and after creating.
        self.refuse_links()
        for path in (self.shared, self.jobs, self.green, self.discard,
                     self.owners, self.locks, self.promotions):
            path.mkdir(parents=True, exist_ok=True)
        self.refuse_links()

    def refuse_links(self) -> None:
        for path in (self.root, self.shared, self.jobs, self.green, self.discard,
                     self.state):
            if path.is_symlink():
                raise ValueError(f"{path} is a symlink; refusing to use it")
            if path.exists() and not path.is_dir():
                raise ValueError(f"{path} is not a directory")


def check_vm(vm: str) -> str:
    if not VM_NAME.fullmatch(vm):
        raise ValueError(f"invalid VM name: {vm!r}")
    return vm


def audit(layout: Layout, event: str, **fields: object) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "event": event, **fields}
    line = (json.dumps(record, sort_keys=True) + "\n").encode()
    layout.state.mkdir(parents=True, exist_ok=True)
    fd = os.open(layout.audit, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


@contextlib.contextmanager
def flock(path: Path, blocking: bool = True) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def remove_tree(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return True
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


# ── Attach / settle ────────────────────────────────────────────────────────


def attach(layout: Layout, vm: str, owner_pid: int) -> Path:
    """Create an empty job layer and record which supervisor owns it."""
    check_vm(vm)
    layout.ensure()
    layer = layout.jobs / vm
    if layer.exists() or layer.is_symlink():
        # A reused name can only be residue from a supervisor that died before
        # settling it; nothing in it has a verdict, so it is never promoted.
        discard_path = unique_target(layout.discard, vm)
        os.rename(layer, discard_path)
        audit(layout, "discard", vm=vm, reason="stale_layer_on_attach")
        remove_tree(discard_path)
    # Owner first: a concurrent sweep that can see the layer can see its owner.
    (layout.owners / vm).write_text(f"{owner_pid}\n")
    (layer / "local").mkdir(parents=True)
    (layer / "remote").mkdir()
    audit(layout, "attach", vm=vm, owner_pid=owner_pid)
    return layer


def unique_target(parent: Path, vm: str) -> Path:
    target = parent / vm
    if not target.exists():
        return target
    return parent / f"{vm}.{uuid.uuid4().hex[:8]}"


def settle(layout: Layout, vm: str, runner_rc: int, capture_status: str,
           receipt: str, quarantine: str, runner_log: Path | None,
           run_id: str = "", job_id: str = "") -> tuple[str, str, Path | None]:
    """Record the verdict and move the job layer out of ``jobs/``."""
    check_vm(vm)
    layout.ensure()
    local_result = runner_log_result(runner_log)
    decision, reason = verdict(runner_rc, capture_status, receipt, quarantine,
                               local_result)
    layer = layout.jobs / vm
    if not layer.is_dir() or layer.is_symlink():
        audit(layout, "settle", vm=vm, verdict=decision, reason=reason,
              layer="missing", run_id=run_id, job_id=job_id)
        return decision, reason, None
    target = unique_target(layout.green if decision == "green" else layout.discard, vm)
    os.rename(layer, target)
    with contextlib.suppress(OSError):
        (layout.owners / vm).unlink()
    if decision == "green":
        (layout.state / "green-meta").mkdir(parents=True, exist_ok=True)
        (layout.state / "green-meta" / f"{target.name}.json").write_text(
            json.dumps({"vm": vm, "run_id": run_id, "job_id": job_id,
                        "reason": reason}))
    audit(layout, "settle", vm=vm, verdict=decision, reason=reason,
          layer=target.name, run_id=run_id, job_id=job_id)
    return decision, reason, target


# ── Promotion ──────────────────────────────────────────────────────────────


def entry_type(path: Path) -> int | None:
    """The ccache entry type byte, or None when the file is not an entry."""
    try:
        with path.open("rb") as handle:
            header = handle.read(ENTRY_HEADER_MIN)
    except OSError:
        return None
    if len(header) < ENTRY_HEADER_MIN or header[:2] != ENTRY_MAGIC:
        return None
    return header[3]


def iter_layer_entries(remote: Path) -> Iterator[tuple[str, str, Path]]:
    """(key-dir, name, path) for every candidate file; rejects are yielded too."""
    try:
        subdirs = sorted(os.scandir(remote), key=lambda item: item.name)
    except FileNotFoundError:
        return
    for sub in subdirs:
        if sub.name == "CACHEDIR.TAG":
            continue
        if not sub.is_dir(follow_symlinks=False):
            yield sub.name, "", Path(sub.path)
            continue
        for item in sorted(os.scandir(sub.path), key=lambda entry: entry.name):
            yield sub.name, item.name, Path(item.path)


def publish_result(src: Path, dest: Path) -> str:
    """Hard-link a result entry into place; an existing key always wins."""
    try:
        os.link(src, dest)
        return "promoted"
    except FileExistsError:
        return "existing"
    except OSError as exc:
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EMLINK):
            raise
    tmp = dest.parent / f"{TMP_PREFIX}{dest.name}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        copy_synced(src, tmp)
        try:
            os.link(tmp, dest)
        except FileExistsError:
            return "existing"
        return "promoted"
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def copy_synced(src: Path, dest: Path) -> None:
    with src.open("rb") as reader, dest.open("xb") as writer:
        shutil.copyfileobj(reader, writer)
        writer.flush()
        os.fsync(writer.fileno())


def publish_manifest(src: Path, dest: Path, lock_path: Path) -> str:
    """Publish a manifest, replacing an existing one only when strictly newer."""
    with flock(lock_path):
        try:
            existing = dest.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if src.lstat().st_mtime_ns <= existing.st_mtime_ns:
                return "existing"
        tmp = dest.parent / f"{TMP_PREFIX}{dest.name}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            try:
                os.link(src, tmp)
            except OSError:
                copy_synced(src, tmp)
                stat = src.lstat()
                os.utime(tmp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            os.replace(tmp, dest)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        return "replaced" if existing is not None else "promoted"


def promote_layer(layout: Layout, layer: Path) -> dict[str, int | list[str]]:
    """Publish every valid entry of a green layer into ``shared/``."""
    counts: dict[str, int] = {"promoted": 0, "replaced": 0, "existing": 0,
                              "rejected": 0, "bytes": 0}
    keys: list[str] = []
    lock_path = layout.locks / "shared-manifests.lock"
    remote = layer / "remote"
    # The guest owns jobs/<vm> read-write, so it can swap remote/ for a link
    # to any host directory; scandir would follow it. Promote nothing then.
    if layer.is_symlink() or remote.is_symlink():
        counts["rejected"] += 1
        return {**counts, "keys": keys}
    for key_dir, name, path in iter_layer_entries(remote):
        if not KEY_DIR.fullmatch(key_dir) or not KEY_NAME.fullmatch(name):
            counts["rejected"] += 1
            continue
        try:
            info = path.lstat()
        except OSError:
            counts["rejected"] += 1
            continue
        if path.is_symlink() or not path.is_file():
            counts["rejected"] += 1
            continue
        kind = entry_type(path)
        if kind not in (ENTRY_TYPE_RESULT, ENTRY_TYPE_MANIFEST):
            counts["rejected"] += 1
            continue
        dest_dir = layout.shared / key_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        if kind == ENTRY_TYPE_MANIFEST:
            outcome = publish_manifest(path, dest, lock_path)
        else:
            outcome = publish_result(path, dest)
        counts[outcome] += 1
        if outcome in ("promoted", "replaced"):
            counts["bytes"] += info.st_size
            keys.append(f"{key_dir}{name}")
    return {**counts, "keys": keys}


STATS_KEYS = {"direct_cache_hit", "preprocessed_cache_hit", "cache_miss",
              "remote_storage_hit", "remote_storage_miss", "remote_storage_error"}


def parse_stats(text: str) -> dict[str, int]:
    """Whitelisted integer counters from ``ccache --print-stats`` output."""
    stats: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] in STATS_KEYS and parts[1].isdigit() \
                and len(parts[1]) <= 15:
            stats[parts[0]] = int(parts[1])
    return stats


def layer_stats(layer: Path, ccache: str | None) -> dict[str, int] | None:
    """Hit/miss counters for a layer: the guest's own (received as data), or,
    for a host-side layer with a real ``local/`` directory, the host ccache's."""
    received = layer / "stats.json"
    if received.is_file() and not received.is_symlink():
        try:
            data = json.loads(received.read_text())
        except (OSError, ValueError):
            return None
        return {k: v for k, v in data.items() if k in STATS_KEYS and type(v) is int} or None
    local = layer / "local"
    if not ccache or local.is_symlink() or not local.is_dir():
        return None
    # Read no ccache.conf from the layer (ccache_guard.HOST_CCACHE_CONFIG).
    env = dict(os.environ, CCACHE_DIR=str(local), CCACHE_CONFIGPATH=os.devnull)
    try:
        output = subprocess.run([ccache, "--print-stats"], env=env, text=True,
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_stats(output.stdout) or None


# ── Receive (pull a green guest's entries) ─────────────────────────────────

MEMBER = re.compile(r"^remote/([0-9a-z]{2})/([0-9a-z]{8,128})$")
STATS_MEMBER = "stats.txt"
STATS_MAX = 64 * 1024


class ReceiveError(Exception):
    pass


def receive_stream(layout: Layout, vm: str, owner_pid: int, stream,
                   max_bytes: int, now: float | None = None) -> dict[str, int]:
    """Validate a guest's tar stream into a fresh ``jobs/<vm>`` layer.

    Only regular files named ``remote/<k0k1>/<rest>`` that start with a ccache
    entry header, and an optional small ``stats.txt``, are written. Nothing a
    guest names is followed or trusted: no links, no devices, no other paths,
    and every file is created here (O_EXCL|O_NOFOLLOW, 0644, mtime = now), so
    the guest's mtimes and modes never reach the shared store.
    """
    import tarfile  # noqa: PLC0415

    layer = attach(layout, vm, owner_pid)
    remote = layer / "remote"
    stamp = time.time() if now is None else now
    counts = {"received": 0, "rejected": 0, "bytes": 0}
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                name = name.rstrip("/")
                if member.isdir() and (name in ("", "remote") or
                                       re.fullmatch(r"remote/[0-9a-z]{2}", name)):
                    continue
                if name == STATS_MEMBER and member.isfile() and member.size <= STATS_MAX:
                    handle = archive.extractfile(member)
                    text = handle.read(STATS_MAX).decode("utf-8", "replace") if handle else ""
                    (layer / "stats.json").write_text(json.dumps(parse_stats(text)))
                    continue
                match = MEMBER.fullmatch(name)
                if not match or not member.isfile():
                    counts["rejected"] += 1
                    continue
                if counts["bytes"] + member.size > max_bytes:
                    raise ReceiveError(f"stream exceeds {max_bytes} bytes")
                handle = archive.extractfile(member)
                header = handle.read(ENTRY_HEADER_MIN) if handle else b""
                if len(header) < ENTRY_HEADER_MIN or header[:2] != ENTRY_MAGIC \
                        or header[3] not in (ENTRY_TYPE_RESULT, ENTRY_TYPE_MANIFEST):
                    counts["rejected"] += 1
                    continue
                key_dir = remote / match.group(1)
                key_dir.mkdir(exist_ok=True)
                dest = key_dir / match.group(2)
                try:
                    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o644)
                except FileExistsError:
                    counts["rejected"] += 1
                    continue
                with os.fdopen(fd, "wb") as out:
                    out.write(header)
                    shutil.copyfileobj(handle, out, 1 << 20)
                os.chmod(dest, 0o644)
                os.utime(dest, (stamp, stamp))
                counts["received"] += 1
                counts["bytes"] += member.size
    except (tarfile.TarError, OSError, ReceiveError) as exc:
        discard_path = unique_target(layout.discard, vm)
        os.rename(layer, discard_path)
        with contextlib.suppress(OSError):
            (layout.owners / vm).unlink()
        remove_tree(discard_path)
        audit(layout, "receive_failed", vm=vm, reason=str(exc)[:200], **counts)
        raise ReceiveError(str(exc)) from exc
    audit(layout, "receive", vm=vm, **counts)
    return counts


def receive(layout: Layout, vm: str, owner_pid: int, command: list[str],
            max_bytes: int, timeout: float) -> dict[str, int]:
    """Run ``command`` (the ssh that tars the guest's layer) under a wall-clock
    bound and receive its stdout."""
    check_vm(vm)
    proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    timer = threading.Timer(timeout, lambda: _kill_group(proc))
    timer.start()
    try:
        counts = receive_stream(layout, vm, owner_pid, proc.stdout, max_bytes)
    except ReceiveError:
        _kill_group(proc)
        proc.wait()
        raise
    finally:
        timer.cancel()
    rc = proc.wait()
    if rc != 0:
        settle_path = layout.jobs / vm
        if settle_path.exists():
            target = unique_target(layout.discard, vm)
            os.rename(settle_path, target)
            remove_tree(target)
        with contextlib.suppress(OSError):
            (layout.owners / vm).unlink()
        audit(layout, "receive_failed", vm=vm, reason=f"rc={rc}", **counts)
        raise ReceiveError(f"pull command exited {rc}")
    return counts


def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def wait_vm_gone(vm: str, is_present: Callable[[str], bool], timeout: float,
                 poll: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if not is_present(vm):
                return True
        except Exception:  # noqa: BLE001 - an unreadable inventory proves nothing
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def tart_presence(tart: str) -> Callable[[str], bool]:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tart_inventory import vm_is_present  # noqa: PLC0415

    return lambda vm: vm_is_present(vm, 10.0, tart)


def process(layout: Layout, name: str, is_present: Callable[[str], bool],
            vm_gone_timeout: float = DEFAULT_VM_GONE_TIMEOUT, poll: float = 5.0,
            ccache: str | None = None, max_size: int | None = None,
            trim_interval: float = DEFAULT_TRIM_INTERVAL) -> str:
    """Finish one settled layer. Returns what happened, for tests and logs."""
    layout.ensure()
    green = layout.green / name
    discard = layout.discard / name
    with flock(layout.locks / f"{name}.lock", blocking=False) as held:
        if not held:
            return "busy"
        if discard.is_dir():
            ok = remove_tree(discard)
            audit(layout, "discard", layer=name, removed=ok)
            return "discarded"
        if not green.is_dir():
            return "missing"
        meta_path = layout.state / "green-meta" / f"{name}.json"
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError):
            meta = {"vm": name.split(".", 1)[0]}
        vm = str(meta.get("vm") or name.split(".", 1)[0])
        if not wait_vm_gone(vm, is_present, vm_gone_timeout, poll):
            # The guest may still be able to write into this layer. Promotion
            # needs proof of no writer, so an unproved teardown discards.
            target = unique_target(layout.discard, name)
            os.rename(green, target)
            audit(layout, "discard", vm=vm, layer=name, reason="vm_still_present")
            remove_tree(target)
            with contextlib.suppress(OSError):
                meta_path.unlink()
            return "discarded"
        stats = layer_stats(green, ccache)
        result = promote_layer(layout, green)
        keys = result.pop("keys")
        if keys:
            keys_file = layout.promotions / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{name}.keys"
            keys_file.write_text("\n".join(keys) + "\n")
        audit(layout, "promote", vm=vm, layer=name, run_id=meta.get("run_id", ""),
              job_id=meta.get("job_id", ""), stats=stats, **result)
        remove_tree(green)
        with contextlib.suppress(OSError):
            meta_path.unlink()
    if max_size:
        trim(layout, max_size, trim_interval)
    return "promoted"


# ── Sweep and trim ─────────────────────────────────────────────────────────


def sweep(layout: Layout, is_present: Callable[[str], bool], **process_args: object) -> dict[str, int]:
    """Reclaim layers an interrupted supervisor or promoter left behind."""
    layout.ensure()
    counts = {"orphaned": 0, "resumed": 0, "discarded": 0}
    for layer in sorted(layout.jobs.iterdir()):
        owner_file = layout.owners / layer.name
        try:
            owner = int(owner_file.read_text().strip())
        except (OSError, ValueError):
            owner = 0
        if pid_alive(owner):
            continue
        target = unique_target(layout.discard, layer.name)
        os.rename(layer, target)
        with contextlib.suppress(OSError):
            owner_file.unlink()
        audit(layout, "discard", vm=layer.name, layer=target.name,
              reason="orphaned_active_layer", owner_pid=owner)
        counts["orphaned"] += 1
    for layer in sorted(layout.discard.iterdir()):
        if process(layout, layer.name, is_present, **process_args) == "discarded":
            counts["discarded"] += 1
    for layer in sorted(layout.green.iterdir()):
        if process(layout, layer.name, is_present, **process_args) == "promoted":
            counts["resumed"] += 1
    return counts


def trim(layout: Layout, max_size: int, interval: float = DEFAULT_TRIM_INTERVAL,
         now: float | None = None) -> dict[str, int] | None:
    """Evict the oldest shared entries until under 90% of ``max_size``."""
    now = time.time() if now is None else now
    stamp = layout.shared / TRIM_STAMP
    with flock(layout.locks / "trim.lock", blocking=False) as held:
        if not held:
            return None
        try:
            if interval > 0 and now - stamp.stat().st_mtime < interval:
                return None
        except FileNotFoundError:
            pass
        entries: list[tuple[float, int, Path]] = []
        removed_tmp = 0
        total = 0
        for sub in os.scandir(layout.shared):
            if not sub.is_dir(follow_symlinks=False):
                continue
            for item in os.scandir(sub.path):
                try:
                    info = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                if item.name.startswith(TMP_PREFIX):
                    if now - info.st_mtime > STALE_TMP_SECONDS:
                        with contextlib.suppress(OSError):
                            os.unlink(item.path)
                            removed_tmp += 1
                    continue
                entries.append((info.st_mtime, info.st_size, Path(item.path)))
                total += info.st_size
        evicted = 0
        if total > max_size:
            target = int(max_size * 0.9)
            for _, size, path in sorted(entries, key=lambda row: row[0]):
                if total <= target:
                    break
                with contextlib.suppress(OSError):
                    path.unlink()
                    total -= size
                    evicted += 1
        stamp.touch()
        audit(layout, "trim", evicted=evicted, removed_tmp=removed_tmp,
              bytes_after=total, max_size=max_size)
        return {"evicted": evicted, "removed_tmp": removed_tmp, "bytes_after": total}


# ── CLI ────────────────────────────────────────────────────────────────────


def parse_size(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([KMGT])", value)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid size {value!r}; expected e.g. 40G")
    return int(match.group(1)) * 1024 ** "KMGT".index(match.group(2)) * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccache_layer")
    parser.add_argument("--root", required=True, type=Path,
                        help="layers root ($CACHE_ROOT/ccache-layers-v1)")
    parser.add_argument("--state", required=True, type=Path,
                        help="host-only state directory (never mounted)")
    sub = parser.add_subparsers(dest="command", required=True)

    attach_cmd = sub.add_parser("attach")
    attach_cmd.add_argument("--vm", required=True)
    attach_cmd.add_argument("--owner-pid", required=True, type=int)

    settle_cmd = sub.add_parser("settle")
    settle_cmd.add_argument("--vm", required=True)
    settle_cmd.add_argument("--runner-rc", required=True, type=int)
    settle_cmd.add_argument("--capture-status", default="")
    settle_cmd.add_argument("--receipt", default="")
    settle_cmd.add_argument("--quarantine", default="")
    settle_cmd.add_argument("--runner-log", type=Path)
    settle_cmd.add_argument("--run-id", default="")
    settle_cmd.add_argument("--job-id", default="")

    for name in ("process", "sweep"):
        cmd = sub.add_parser(name)
        if name == "process":
            cmd.add_argument("--layer", required=True)
        cmd.add_argument("--tart", default=os.environ.get("TARTCI_CCACHE_LAYER_TART", "tart"))
        cmd.add_argument("--vm-gone-timeout", type=float, default=DEFAULT_VM_GONE_TIMEOUT)
        cmd.add_argument("--poll", type=float, default=5.0)
        cmd.add_argument("--ccache", default=shutil.which("ccache"))
        cmd.add_argument("--max-size", type=parse_size)
        cmd.add_argument("--trim-interval", type=float, default=DEFAULT_TRIM_INTERVAL)

    sub.add_parser("prepare")

    verdict_cmd = sub.add_parser("verdict")
    verdict_cmd.add_argument("--runner-rc", required=True, type=int)
    verdict_cmd.add_argument("--capture-status", default="")
    verdict_cmd.add_argument("--receipt", default="")
    verdict_cmd.add_argument("--quarantine", default="")
    verdict_cmd.add_argument("--runner-log", type=Path)

    receive_cmd = sub.add_parser("receive")
    receive_cmd.add_argument("--vm", required=True)
    receive_cmd.add_argument("--owner-pid", required=True, type=int)
    receive_cmd.add_argument("--max-bytes", type=parse_size, default=parse_size("20G"))
    receive_cmd.add_argument("--timeout", type=float, default=900.0)
    receive_cmd.add_argument("pull", nargs=argparse.REMAINDER)

    trim_cmd = sub.add_parser("trim")
    trim_cmd.add_argument("--max-size", required=True, type=parse_size)
    trim_cmd.add_argument("--trim-interval", type=float, default=0.0)

    args = parser.parse_args(argv)
    layout = Layout(args.root, args.state)
    try:
        if args.command == "prepare":
            layout.ensure()
            print(layout.root)
        elif args.command == "verdict":
            decision, reason = verdict(args.runner_rc, args.capture_status, args.receipt,
                                       args.quarantine, runner_log_result(args.runner_log))
            print(f"{decision}\t{reason}")
        elif args.command == "receive":
            pull = args.pull[1:] if args.pull[:1] == ["--"] else args.pull
            if not pull:
                parser.error("receive needs the pull command after --")
            print(json.dumps(receive(layout, args.vm, args.owner_pid, pull,
                                     args.max_bytes, args.timeout), sort_keys=True))
        elif args.command == "attach":
            print(attach(layout, args.vm, args.owner_pid))
        elif args.command == "settle":
            decision, reason, target = settle(
                layout, args.vm, args.runner_rc, args.capture_status, args.receipt,
                args.quarantine, args.runner_log, args.run_id, args.job_id)
            print(f"{decision}\t{reason}\t{target.name if target else ''}")
        elif args.command in ("process", "sweep"):
            options = {"vm_gone_timeout": args.vm_gone_timeout, "poll": args.poll,
                       "ccache": args.ccache, "max_size": args.max_size,
                       "trim_interval": args.trim_interval}
            presence = tart_presence(args.tart)
            if args.command == "process":
                print(process(layout, args.layer, presence, **options))
            else:
                print(json.dumps(sweep(layout, presence, **options), sort_keys=True))
        else:
            print(json.dumps(trim(layout, args.max_size, args.trim_interval)))
    except (OSError, ValueError, ReceiveError) as exc:
        print(f"ccache_layer: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
