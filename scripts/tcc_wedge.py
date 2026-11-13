#!/usr/bin/env python3
"""Name the cause when launchd-context opens of the Tart store keep timing out.

A host whose Tart store is an external volume can stop admitting VMs while the
volume itself is fine: every `open()` of the store from a launchd job needs a
Removable Volumes authorization from sandboxd, which asks the system tccd, and
when tccd stops answering those requests the opens block in the kernel. The
watchdog's store probe and every lane's probe then time out, the lanes log
"store health is unknown", and an interactive shell (whose decision is already
cached) still opens the same directory in a fraction of a millisecond.

What this module adds to the plain timeout:

  * ``classify``: once the watchdog has seen ``failure_threshold`` consecutive
    timeouts it names the cause from three independent readings:
      - a control probe of an internal path that must answer (a host whose
        boot volume also hangs is not a TCC verdict);
      - a bounded ``log show`` over the window since the streak began, looking
        for sandboxd/tccd Removable Volumes authorization failures ("Failed to
        create Attribution Chain", "missing 'auth_value'"). The failures are
        logged when a request is refused, at the onset and at release of a
        wedge; while requests merely hang nothing is logged, so the window is
        the whole streak (bounded), never only the last few minutes;
      - optionally, the same store probe from a context that has TCC access
        (``context_probe``); a store that does not answer there either is a
        stalled volume, not a wedged daemon.
    Only all three agreeing yield ``tcc_attribution_wedge``.

    A hang that logs no refusal at all (m3, 2026-11-13) is attributed only by
    positive readings: a fresh launchd job (``launchd_probe``, `launchctl
    submit`, always removed) hangs on the volume ROOT while its own internal
    open answers, the internal control path answers, and the configured
    context with TCC access answers for the same store. A bounded `log show`
    of sandboxd's ``tcc_send_request_authorization() IPC`` lines over that
    probe's window (``scan_ipc``) is recorded as corroboration. Readings that
    disagree (the launchd job opens the root, or fails fast on it) veto both
    paths: no root heal.
  * ``heal``: one scoped root action, ``sudo -n /usr/local/sbin/tartci-heal-tcc``,
    which restarts tccd and sandboxd and nothing else, at most once per
    ``HEAL_INTERVAL_S`` (the wrapper keeps its own root-owned stamp as well).
    A missing wrapper, a refused sudo, a rate limit and a run that did not
    recover the store are each a distinct outcome; none falls back to anything
    broader.

Every reading is recorded in the watchdog's ``store-health.json`` so the lane
log, ``tartci doctor fleet`` and the fleet check name the cause and the heal
outcome instead of "health unknown".
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable, Optional

from bounded_subprocess import ObservationError, require_success, run_bounded
import store_health

KIND = "tcc_attribution_wedge"
# The lane's heartbeat phase while it defers admission for this cause.
PHASE = "tcc-attribution-wedge"
WRAPPER = "/usr/local/sbin/tartci-heal-tcc"
SUDO = "/usr/bin/sudo"
LOG_SHOW = "/usr/bin/log"
HEAL_INTERVAL_S = 30 * 60
HEAL_TIMEOUT_S = 20.0
LOG_SCAN_TIMEOUT_S = 40.0   # a 180 min window takes ~3 s on m3
MIN_WINDOW_S = 10 * 60
MAX_WINDOW_S = 3 * 3600
# A classification older than this says nothing about now.
FRESH_S = 30 * 60
# The watchdog's cadence; seeds the streak start on a first pass that finds
# an older failure count but no recorded start (rollout mid-wedge).
WATCHDOG_INTERVAL_S = 300
STAMP_DIR = "/var/db/tartci-heal-tcc"
LOG_PREDICATE = (
    '(process == "sandboxd" OR process == "tccd") AND '
    '(eventMessage CONTAINS "Failed to create Attribution Chain" OR '
    'eventMessage CONTAINS "kTCCServiceSystemPolicyRemovableVolumes")'
)
SIGNATURES = (
    ("attribution_chain", re.compile(r"Failed to create Attribution Chain")),
    ("removable_volumes_auth_failed", re.compile(
        r"tcc_server_message_request_authorization\(kTCCServiceSystemPolicyRemovableVolumes\) failed")),
)
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
OUTCOMES = ("ran", "rate_limited", "wrapper_rate_limited", "wrapper_missing",
            "wrapper_insecure", "not_permitted", "timeout", "failed", "disabled",
            "escalated")
# Outcomes that reached sudo and so spend the 30-minute budget. A refused
# sudo spends it too: retrying it every pass would be a 300 s loop of nothing.
BUDGET_OUTCOMES = frozenset({"ran", "not_permitted", "timeout", "failed"})


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def local_stamp(epoch: float) -> str:
    """An epoch as `log show --style compact` prints its timestamps (local time)."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def signature_hits(text: str, *, before: Optional[str] = None) -> dict[str, Any]:
    """Count the wedge signatures in `log show --style compact` output.

    Lines stamped at or after `before` (a local_stamp) are ignored: the burst
    sandboxd writes as a wedge RELEASES after a heal must not be the evidence
    for the next heal.
    """
    counts = {name: 0 for name, _ in SIGNATURES}
    ignored = 0
    first: Optional[str] = None
    last: Optional[str] = None
    for line in text.splitlines():
        stamp = _TIMESTAMP.match(line)
        if before and stamp and stamp.group(1) >= before:
            ignored += 1
            continue
        matched = False
        for name, pattern in SIGNATURES:
            if pattern.search(line):
                counts[name] += 1
                matched = True
        if not matched:
            continue
        if stamp:
            first = first or stamp.group(1)
            last = stamp.group(1)
    return {"total": sum(counts.values()), "by_signature": counts,
            "first": first, "last": last, "ignored_after_heal": ignored}


def window_seconds(failing_since: Optional[float], now: float) -> int:
    """How far back to scan: the whole streak plus a margin, within bounds."""
    if not isinstance(failing_since, (int, float)) or failing_since > now:
        return MIN_WINDOW_S
    return int(min(MAX_WINDOW_S, max(MIN_WINDOW_S, now - failing_since + MIN_WINDOW_S)))


def scan_log(window_s: int, *, before: Optional[str] = None,
             runner: Callable[..., Any] = run_bounded) -> dict[str, Any]:
    """Bounded `log show` for the signatures over the last `window_s` seconds."""
    minutes = max(1, int(math.ceil(window_s / 60.0)))
    argv = [LOG_SHOW, "show", "--last", f"{minutes}m", "--predicate", LOG_PREDICATE,
            "--style", "compact"]
    try:
        completed = runner(argv, timeout=LOG_SCAN_TIMEOUT_S, operation="tcc_log_scan")
        require_success(completed, operation="tcc_log_scan")
    except ObservationError as exc:
        return {"ok": False, "window_s": window_s, "total": 0,
                "error": f"{exc.kind}: {exc.detail}"[:300]}
    except (OSError, ValueError) as exc:
        return {"ok": False, "window_s": window_s, "total": 0, "error": str(exc)[:300]}
    hits = signature_hits(completed.stdout or "", before=before)
    hits.update(ok=True, window_s=window_s, before=before)
    return hits


# ── The fresh-launchd-job probe and the IPC signature ───────────────────────
#
# A wedge does not always log the refusals above: on 2026-11-13 m3's
# launchd-context opens of its store volume hung for hours with no error line
# at all, because a request that merely hangs logs nothing. The positive
# reading that does not depend on error lines is to open the volume ROOT from a
# context that has no cached decision, a fresh launchd job, while an internal
# path answers in the same job and a context that already holds authorization
# answers for the same volume.

LAUNCHCTL = "/bin/launchctl"
LAUNCHCTL_TIMEOUT_S = 10.0
LAUNCHD_LABEL_PREFIX = "com.danielraffel.tartci.tcc-probe"
LAUNCHD_PROBE_DIR = "tcc-probe"
LAUNCHD_POLL_S = 0.1
CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tcc_launchd_probe_child.py")
# Outcomes of the launchd probe that say the root answered (or failed fast):
# the store is not hanging on an authorization from that context, so the
# readings disagree with a wedge and nothing privileged may run.
LAUNCHD_DISAGREES = frozenset({"answered", "root_error"})
# An IPC line sandboxd logs as it asks tccd, and how long its own thread has to
# take the next step (the "activating connection" to tccd, a SEND, or the second
# IPC to the per-user tccd) before the request counts as unanswered. Answered
# requests take that step within ~20 ms on m3.
IPC_ANSWER_S = 5.0
IPC_SCAN_TIMEOUT_S = 30.0
IPC_PREDICATE = ('(process == "sandboxd" AND eventMessage CONTAINS[c] "tcc") OR '
                 '(process == "tccd" AND eventMessage CONTAINS "REQUEST")')
_LOG_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(\.\d+)?\s+\S+\s+"
    r"(sandboxd|tccd)\[(\d+):([0-9a-fA-F]+)\]\s?(.*)$")
_IPC = "tcc_send_request_authorization() IPC"


def volume_root(path: str) -> str:
    """The mount point of an external volume, lexically ("/" when internal).

    Never a stat: the path may be the one that hangs."""
    parts = os.path.normpath(str(path)).split(os.sep)
    if len(parts) >= 3 and parts[0] == "" and parts[1] == "Volumes" and parts[2]:
        return os.sep.join(parts[:3])
    return "/"


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path) as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _remove_job(label: str, runner: Callable[..., Any]) -> tuple[bool, str]:
    try:
        completed = runner([LAUNCHCTL, "remove", label], timeout=LAUNCHCTL_TIMEOUT_S,
                           operation="tcc_launchd_remove")
    except (OSError, ValueError) as exc:
        return False, str(exc)[:200]
    if completed.returncode == 0:
        return True, ""
    detail = (completed.stderr or completed.stdout or "").strip()
    # A job that is already gone is removed.
    if "No such process" in detail or completed.returncode == 3:
        return True, detail[:200]
    return False, (detail or f"exit {completed.returncode}")[:200]


def _forget(probe_dir: str, label: str) -> None:
    for suffix in (".label", ".json", ".json.tmp"):
        try:
            os.unlink(os.path.join(probe_dir, label + suffix))
        except OSError:
            pass


def _remove_leftovers(probe_dir: str, runner: Callable[..., Any]) -> list:
    """Remove jobs an earlier pass submitted and could not remove (a killed
    watchdog, a failed remove). Each submit leaves a `<label>.label` marker
    until its job is gone."""
    removed = []
    try:
        names = sorted(os.listdir(probe_dir))
    except OSError:
        return removed
    for name in names:
        if not (name.endswith(".label") and name.startswith(LAUNCHD_LABEL_PREFIX + ".")):
            continue
        label = name[:-len(".label")]
        ok, _ = _remove_job(label, runner)
        if ok:
            _forget(probe_dir, label)
            removed.append(label)
    return removed


def launchd_probe(store_path: str, internal_dir: str, timeout_s: float, *,
                  runner: Callable[..., Any] = run_bounded,
                  python: Optional[str] = None,
                  sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic,
                  now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Open the store's volume root from a fresh launchd job, bounded.

    Outcomes: answered, root_error (a fast error, not a hang), hung_on_root
    (the internal path answered in the same job, the root did not within
    `timeout_s`), did_not_start (no phase recorded: launchd did not run it, or
    even its internal open hung), inconclusive (launchctl failed), not_external
    (the store is on the boot volume; nothing to probe). The job is removed on
    every path, including an exception.
    """
    root = volume_root(store_path)
    record: dict[str, Any] = {"context": "fresh launchd job (launchctl submit)",
                              "volume_root": root, "timeout_s": timeout_s}
    if root == "/":
        record.update(outcome="not_external",
                      detail=f"{store_path} is not on an external volume")
        return record
    probe_dir = os.path.join(internal_dir, LAUNCHD_PROBE_DIR)
    try:
        os.makedirs(probe_dir, exist_ok=True)
    except OSError as exc:
        record.update(outcome="inconclusive", detail=f"{probe_dir}: {exc}"[:300])
        return record
    leftovers = _remove_leftovers(probe_dir, runner)
    if leftovers:
        record["removed_leftover_jobs"] = leftovers
    started_epoch = now()
    label = f"{LAUNCHD_LABEL_PREFIX}.{os.getpid()}.{int(started_epoch * 1000)}"
    result_path = os.path.join(probe_dir, label + ".json")
    if python is None:
        from python_child import child_python
        python = child_python()
    argv = [LAUNCHCTL, "submit", "-l", label, "--", python, CHILD,
            "--root", root, "--internal", probe_dir, "--result", result_path,
            "--hold", str(int(math.ceil(timeout_s)) + 30)]
    record.update(label=label, started_at=_iso(started_epoch), started_epoch=started_epoch)
    seen: Optional[dict] = None
    submitted = False
    began = clock()
    try:
        with open(os.path.join(probe_dir, label + ".label"), "w") as marker:
            marker.write(f"{started_epoch}\n")
        try:
            completed = runner(argv, timeout=LAUNCHCTL_TIMEOUT_S, operation="tcc_launchd_submit")
            require_success(completed, operation="tcc_launchd_submit")
            submitted = True
        except ObservationError as exc:
            record.update(outcome="inconclusive",
                          detail=f"launchctl submit {exc.kind}: {exc.detail}"[:300])
        except (OSError, ValueError) as exc:
            record.update(outcome="inconclusive", detail=f"launchctl submit: {exc}"[:300])
        if submitted:
            deadline = began + timeout_s
            while True:
                seen = _read_json(result_path)
                if seen and seen.get("phase") in ("root_ok", "root_error"):
                    break
                if clock() >= deadline:
                    break
                sleep(LAUNCHD_POLL_S)
    finally:
        removed, why = _remove_job(label, runner)
        record["job_removed"] = removed
        if removed:
            _forget(probe_dir, label)
        else:
            record["remove_error"] = why
    record["waited_s"] = round(clock() - began, 3)
    if not submitted:
        return record
    phase = (seen or {}).get("phase")
    record["phase"] = phase
    for key in ("internal_latency_ms", "root_latency_ms", "errno", "error"):
        if seen and key in seen:
            record[key] = seen[key]
    if phase == "root_ok":
        record["outcome"] = "answered"
    elif phase == "root_error":
        record["outcome"] = "root_error"
    elif phase == "internal_ok":
        record["outcome"] = "hung_on_root"
    else:
        record["outcome"] = "did_not_start"
    return record


def launchd_probe_from_env(store_path: str, internal_dir: str, timeout_s: float,
                           env: Optional[dict] = None) -> Optional[Callable[[], dict]]:
    """The probe the watchdog runs, unless TARTCI_TCC_LAUNCHD_PROBE=0."""
    env = os.environ if env is None else env
    if str(env.get("TARTCI_TCC_LAUNCHD_PROBE", "1")) == "0":
        return None
    return lambda: launchd_probe(store_path, internal_dir, timeout_s)


def _local_epoch(stamp: str, fraction: Optional[str]) -> float:
    return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")) + float(fraction or 0)


def ipc_hits(text: str, *, end_epoch: Optional[float] = None,
             answer_s: float = IPC_ANSWER_S) -> dict[str, Any]:
    """Unanswered sandboxd authorization requests in `log show --style compact` text.

    A sandboxd thread asks tccd with a `tcc_send_request_authorization() IPC`
    line and, when the request goes out, logs its next step on the same thread
    within milliseconds. A thread whose first IPC is followed by nothing from
    that thread within `answer_s` sent a request that never reached tccd.
    tccd's REQUEST lines carry only a msgID that sandboxd prints on few threads,
    so the thread's own next step stands in for "a matching tccd REQUEST";
    tccd's REQUEST lines are counted to show tccd itself is alive. An IPC within
    `answer_s` of `end_epoch` is pending: the log ends before it can be judged.
    """
    threads: dict[tuple, list] = {}
    order = 0
    tccd_requests = 0
    for line in text.splitlines():
        match = _LOG_LINE.match(line)
        if not match:
            continue
        stamp, fraction, process, pid, tid, message = match.groups()
        if process == "tccd":
            if "REQUEST" in message:
                tccd_requests += 1
            continue
        epoch = _local_epoch(stamp, fraction)
        threads.setdefault((pid, tid), []).append((epoch, order, _IPC in message, stamp))
        order += 1
    answered = unanswered = pending = 0
    first_unanswered: Optional[str] = None
    for rows in threads.values():
        rows.sort(key=lambda row: (row[0], row[1]))
        for index, (epoch, _, is_ipc, stamp) in enumerate(rows):
            if not is_ipc:
                continue
            if index and epoch - rows[index - 1][0] <= answer_s:
                continue        # a later step of a request already judged
            if any(later[0] - epoch <= answer_s for later in rows[index + 1:]):
                answered += 1
            elif end_epoch is not None and epoch + answer_s > end_epoch:
                pending += 1
            else:
                unanswered += 1
                first_unanswered = first_unanswered or stamp
    if unanswered:
        state = "unanswered"
    elif answered:
        state = "all_answered"
    elif pending:
        state = "pending"
    else:
        state = "absent"
    return {"state": state, "answered": answered, "unanswered": unanswered,
            "pending": pending, "tccd_requests": tccd_requests,
            "first_unanswered": first_unanswered, "answer_s": answer_s}


def scan_ipc(start_epoch: float, end_epoch: float, *,
             runner: Callable[..., Any] = run_bounded) -> dict[str, Any]:
    """Bounded `log show` of sandboxd's authorization IPC over one probe's window."""
    argv = [LOG_SHOW, "show", "--start", local_stamp(start_epoch),
            "--end", local_stamp(end_epoch + 1), "--info", "--debug",
            "--predicate", IPC_PREDICATE, "--style", "compact"]
    try:
        completed = runner(argv, timeout=IPC_SCAN_TIMEOUT_S, operation="tcc_ipc_scan")
        require_success(completed, operation="tcc_ipc_scan")
    except ObservationError as exc:
        return {"state": "unavailable", "error": f"{exc.kind}: {exc.detail}"[:300]}
    except (OSError, ValueError) as exc:
        return {"state": "unavailable", "error": str(exc)[:300]}
    hits = ipc_hits(completed.stdout or "", end_epoch=end_epoch + 1)
    hits.update(start=local_stamp(start_epoch), end=local_stamp(end_epoch + 1))
    return hits


def control_probe(path: str, timeout_s: float, *,
                  probe: Callable[..., dict] = store_health.probe) -> dict[str, Any]:
    """The same bounded open+fstat, on an internal path that must answer."""
    result = probe(path, timeout_s=timeout_s)
    return {"ok": result.get("ok") is True, "path": path,
            "reason": result.get("reason"), "latency_ms": result.get("latency_ms")}


def is_timeout(reason: Any) -> bool:
    return str(reason or "").startswith("timeout")


def classify(*, consecutive_failures: int, failure_threshold: int, last_reason: Any,
             failing_since: Optional[float], control_path: str, now: Optional[float] = None,
             store_timeout_s: float = 10.0, healed_at: Optional[float] = None,
             scan: Optional[Callable[..., dict]] = None,
             control: Optional[Callable[[str, float], dict]] = None,
             context_probe: Optional[Callable[[], dict]] = None,
             launchd_probe: Optional[Callable[[], dict]] = None,
             ipc_scan: Optional[Callable[[float, float], dict]] = None) -> dict[str, Any]:
    """Classify a failing store probe. Returns {kind, reason, evidence, classified_at}.

    Two paths reach `tcc_attribution_wedge`; every other combination is
    `stalled_unclassified` (or `host_unresponsive`) and runs nothing privileged:

      * error_signature: sandboxd/tccd logged Removable Volumes refusals over
        the streak (the 10-25 and 11-11 shape), and no reading contradicts it.
      * launchd_root_hang: no error lines, but a fresh launchd job hangs on the
        volume root while its own internal open answers, AND a context that
        already holds authorization (TARTCI_TCC_CONTEXT_PROBE) answers for the
        same store (the 11-13 shape). Without that configured reading the
        verdict stays unproven.

    Disagreement fails closed: a fresh launchd job that opens the root, or
    fails fast on it, and an authorized context that hangs too, each veto both
    paths. The sandboxd IPC signature (an authorization request that never
    left sandboxd) is recorded and named as corroboration, never required and
    never a veto: on 2026-11-13 the same wedge showed an unanswered IPC on one
    probe and no IPC line at all on the next, so its absence proves nothing.
    """
    now = time.time() if now is None else now
    scan = scan_log if scan is None else scan
    control = control_probe if control is None else control
    ipc_scan = scan_ipc if ipc_scan is None else ipc_scan
    evidence: dict[str, Any] = {"consecutive_failures": consecutive_failures,
                                "failure_threshold": failure_threshold,
                                "last_reason": last_reason}

    def verdict(kind: str, reason: str, path: Optional[str] = None) -> dict[str, Any]:
        value = {"kind": kind, "reason": reason, "evidence": evidence,
                 "classified_at": _iso(now)}
        if path:
            value["path"] = path
        return value

    if consecutive_failures < failure_threshold:
        return verdict("not_classified",
                       f"{consecutive_failures} failure(s), below the threshold of {failure_threshold}")
    if not is_timeout(last_reason):
        return verdict("not_classified",
                       f"the last probe failed without timing out ({last_reason}); not a hang")
    ctl = control(control_path, store_timeout_s)
    evidence["control_probe"] = ctl
    if not ctl.get("ok"):
        return verdict("host_unresponsive",
                       f"the internal control path {control_path} also fails "
                       f"({ctl.get('reason')}); not a TCC verdict")
    launchd: dict[str, Any] = {"outcome": "not_run"}
    if launchd_probe is not None:
        launchd = launchd_probe()
        evidence["launchd_probe"] = launchd
        started = launchd.get("started_epoch")
        if launchd.get("outcome") == "hung_on_root" and isinstance(started, (int, float)):
            evidence["ipc_scan"] = ipc_scan(started, started + float(
                launchd.get("waited_s") or launchd.get("timeout_s") or store_timeout_s))
    window = window_seconds(failing_since, now)
    before = local_stamp(healed_at) if isinstance(healed_at, (int, float)) else None
    log = scan(window, before=before)
    evidence["log_scan"] = log
    minutes = max(1, int(math.ceil(window / 60.0)))
    outcome = launchd.get("outcome")
    root = launchd.get("volume_root")
    if outcome in LAUNCHD_DISAGREES:
        how = ("opened" if outcome == "answered"
               else f"failed fast on ({launchd.get('error')})")
        return verdict("stalled_unclassified",
                       f"a fresh launchd job {how} the volume root {root} while the store probe "
                       "timed out: the readings disagree, so the cause is unproven and nothing "
                       "privileged runs")
    removable = 0
    if log.get("ok"):
        removable = int((log.get("by_signature") or {}).get("removable_volumes_auth_failed") or 0)
    ctx: Optional[dict] = None

    def authorized_context() -> Optional[dict]:
        nonlocal ctx
        if ctx is None and context_probe is not None:
            ctx = context_probe()
            evidence["tcc_context_probe"] = ctx
        return ctx

    if removable > 0:
        # "Failed to create Attribution Chain" is logged for any TCC service;
        # only the Removable Volumes request line ties the failure to the store.
        reading = authorized_context()
        if reading is not None and not reading.get("ok"):
            return verdict("stalled_unclassified",
                           "the store did not answer from a context with TCC access either "
                           f"({reading.get('reason')}); the volume, not tccd, is stalled")
        return verdict(KIND,
                       f"{removable} sandboxd/tccd Removable Volumes authorization "
                       f"failure(s) in the last {minutes} min while the internal control path "
                       "answers: launchd-context opens of the store hang on tccd",
                       path="error_signature")
    if not log.get("ok"):
        signature_text = f"log show unavailable ({log.get('error')})"
    else:
        signature_text = (f"no sandboxd/tccd Removable Volumes failure in the last {minutes} min "
                          f"({log.get('total')} other TCC failure line(s))")
    if outcome != "hung_on_root":
        detail = {"not_run": "", "not_external": "; the store is not on an external volume",
                  "did_not_start": "; the fresh launchd probe recorded nothing",
                  "inconclusive": f"; the fresh launchd probe was inconclusive "
                                  f"({launchd.get('detail')})"}.get(str(outcome), "")
        return verdict("stalled_unclassified",
                       f"{signature_text}{detail}; the store is stalled but the cause is unproven")
    reading = authorized_context()
    if reading is None:
        return verdict("stalled_unclassified",
                       f"a fresh launchd job hangs on the volume root {root} while its internal "
                       f"open answers, and {signature_text}; no context with TCC access is "
                       "configured (TARTCI_TCC_CONTEXT_PROBE), so the cause is unproven and "
                       "nothing privileged runs")
    if not reading.get("ok"):
        return verdict("stalled_unclassified",
                       "the store did not answer from a context with TCC access either "
                       f"({reading.get('reason')}); the volume, not tccd, is stalled")
    ipc = evidence.get("ipc_scan") or {}
    corroboration = (f"; sandboxd sent {ipc.get('unanswered')} authorization request(s) tccd "
                     f"never received (first at {ipc.get('first_unanswered')})"
                     if ipc.get("state") == "unanswered" else
                     f"; sandboxd IPC signature {ipc.get('state') or 'not read'}")
    return verdict(KIND,
                   f"a fresh launchd job hangs on the volume root {root} while its internal "
                   "open and the internal control path answer, and the store answers from a "
                   f"context with TCC access{corroboration}: launchd-context opens wait on a "
                   "Removable Volumes authorization", path="launchd_root_hang")


def heal_allowed(last_heal: Optional[dict], now: float) -> tuple[bool, str]:
    """tartci's own rate limit, independent of the wrapper's root-owned stamp."""
    if not isinstance(last_heal, dict):
        return True, ""
    at = last_heal.get("at_epoch")
    if not isinstance(at, (int, float)) or at > now:
        return True, ""
    if now - at < HEAL_INTERVAL_S:
        return False, (f"last heal {int(now - at)}s ago, under the "
                       f"{HEAL_INTERVAL_S}s interval")
    return True, ""


def wrapper_insecurity(wrapper: str, *, lstat: Callable[[str], Any] = os.lstat) -> str:
    """Why the wrapper must not be run with NOPASSWD root, or "" when it may.

    Every directory above it and the file itself must be owned by root and
    writable by nobody else; on a host where Homebrew owns /usr/local the login
    user could otherwise replace the wrapper and own root.
    """
    import stat as stat_module
    if not os.path.isabs(wrapper):
        return f"{wrapper} is not an absolute path"
    # Walk the REAL chain: on macOS /var and /tmp are symlinks into /private,
    # and a symlink above the wrapper is only acceptable when the resolved
    # chain is itself root-owned.
    path = os.path.realpath(wrapper)
    parts = path.split(os.sep)
    chain = [os.sep] + [os.sep.join(parts[:index + 1]) for index in range(1, len(parts))]
    for node in chain:
        try:
            info = lstat(node)
        except FileNotFoundError:
            return f"{node} does not exist"
        except OSError as exc:
            return f"{node}: {exc}"
        if stat_module.S_ISLNK(info.st_mode):
            return f"{node} is a symlink"
        if info.st_uid != 0:
            return f"{node} is owned by uid {info.st_uid}, not root"
        if info.st_mode & (stat_module.S_IWGRP | stat_module.S_IWOTH):
            return f"{node} is writable by group or other (mode {oct(info.st_mode & 0o777)})"
    if not stat_module.S_ISREG(info.st_mode):
        return f"{path} is not a regular file"
    if not info.st_mode & stat_module.S_IXUSR:
        return f"{path} is not executable"
    return ""


def run_privileged_bounded(argv: list, timeout: float) -> tuple[Optional[int], str]:
    """Run sudo with a deadline the caller survives.

    The login user cannot signal a root process, so a plain subprocess.run
    timeout would raise on kill() and then wait forever. Here a timeout gives
    up on the child without waiting for it; the wrapper's own stamp still
    bounds what it can do.
    """
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except (PermissionError, ProcessLookupError, OSError):
            pass
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return None, ""
    return proc.returncode, output or ""


def heal(*, now: Optional[float] = None, last_heal: Optional[dict] = None,
         runner: Optional[Callable[[list, float], tuple]] = None,
         wrapper: Optional[str] = None, sudo: Optional[str] = None,
         insecurity: Optional[Callable[[str], str]] = None) -> dict[str, Any]:
    """Run the scoped root wrapper once. Never anything broader."""
    now = time.time() if now is None else now
    runner = run_privileged_bounded if runner is None else runner
    wrapper = WRAPPER if wrapper is None else wrapper
    sudo = SUDO if sudo is None else sudo
    insecurity = wrapper_insecurity if insecurity is None else insecurity
    record: dict[str, Any] = {"at": _iso(now), "at_epoch": now, "command": [sudo, "-n", wrapper]}
    allowed, why = heal_allowed(last_heal, now)
    if not allowed:
        record.update(outcome="rate_limited", detail=why)
        return record
    if not (os.path.isfile(wrapper) and os.access(wrapper, os.X_OK)):
        record.update(outcome="wrapper_missing",
                      detail=f"{wrapper} is not installed; install fleet/ops/tartci-heal-tcc "
                             "(sudo fleet/ops/install-tartci-heal-tcc.sh)")
        return record
    unsafe = insecurity(wrapper)
    if unsafe:
        record.update(outcome="wrapper_insecure",
                      detail=f"refusing NOPASSWD root through a path that is not root-owned alone: "
                             f"{unsafe}")
        return record
    try:
        rc, output = runner([sudo, "-n", wrapper], HEAL_TIMEOUT_S)
    except OSError as exc:
        record.update(outcome="failed", rc=None, detail=str(exc)[:300])
        return record
    if rc is None:
        record.update(outcome="timeout", rc=None,
                      detail=f"{wrapper} did not return within {HEAL_TIMEOUT_S:g}s")
        return record
    output = output.strip()[-300:]
    record["rc"] = rc
    record["detail"] = output
    if rc == 0:
        record["outcome"] = "ran"
    elif rc == 75:
        record["outcome"] = "wrapper_rate_limited"
    elif rc == 1 and "password" in output.lower():
        record["outcome"] = "not_permitted"
    else:
        record["outcome"] = "failed"
    return record


def watchdog_step(state: dict, previous: dict, *, control_path: str, store_timeout_s: float,
                  dry_run: bool, now: Optional[float] = None,
                  heal_enabled: bool = True,
                  classify_fn: Callable[..., dict] = classify,
                  heal_fn: Callable[..., dict] = heal,
                  reprobe: Optional[Callable[..., dict]] = None,
                  context_probe: Optional[Callable[[], dict]] = None,
                  launchd_probe: Optional[Callable[[], dict]] = None) -> Optional[dict]:
    """Classify, heal once, and reprobe. Mutates `state`; returns a recovered
    probe result when the heal brought the store back, else None.

    `failing_since`, `last_heal` and the latest classification ride along in
    the state file so the rate limit and the lane's cause line survive passes.
    """
    now = time.time() if now is None else now
    reprobe = store_health.probe if reprobe is None else reprobe
    last_result = state.get("last_result") or {}
    if last_result.get("ok"):
        state.pop("failing_since", None)
        state.pop("classification", None)
        if isinstance(previous.get("last_heal"), dict):
            state["last_heal"] = previous["last_heal"]
        return None
    since = previous.get("failing_since")
    if isinstance(since, (int, float)) and since <= now:
        state["failing_since"] = since
    else:
        # No recorded start (first pass after a rollout mid-wedge): the earlier
        # failures this pass inherited happened one watchdog interval apart.
        earlier = max(0, int(state.get("consecutive_failures") or 1) - 1)
        state["failing_since"] = now - earlier * WATCHDOG_INTERVAL_S
    last_heal = previous.get("last_heal") if isinstance(previous.get("last_heal"), dict) else None
    if last_heal:
        state["last_heal"] = last_heal
    if not state.get("drain_required"):
        return None
    healed_at = last_heal.get("at_epoch") if last_heal else None
    classification = classify_fn(
        consecutive_failures=int(state.get("consecutive_failures") or 0),
        failure_threshold=int(state.get("failure_threshold") or 2),
        last_reason=last_result.get("reason"), failing_since=state["failing_since"],
        control_path=control_path, now=now, store_timeout_s=store_timeout_s,
        healed_at=healed_at if isinstance(healed_at, (int, float)) else None,
        context_probe=context_probe, launchd_probe=launchd_probe)
    state["classification"] = classification
    if classification.get("kind") != KIND:
        return None
    if (last_heal and last_heal.get("outcome") == "ran"
            and last_heal.get("failing_since") == state["failing_since"]):
        # One heal per streak. The restart did not bring the store back, so a
        # second one would only spend the budget on a cause this is not.
        state["heal"] = {"outcome": "escalated", "at": _iso(now),
                         "detail": "tccd and sandboxd were already restarted in this streak "
                                   f"(at {last_heal.get('at')}) and the store still hangs: "
                                   "drain the host, let jobs finish, and reboot"}
        return None
    if dry_run:
        state["heal"] = {"outcome": "disabled", "at": _iso(now),
                         "detail": "dry run: the wrapper was not invoked"}
        return None
    if not heal_enabled:
        state["heal"] = {"outcome": "disabled", "at": _iso(now),
                         "detail": "TARTCI_TCC_HEAL=0: classify only"}
        return None
    record = heal_fn(now=now, last_heal=state.get("last_heal"))
    state["heal"] = record
    if record.get("outcome") in BUDGET_OUTCOMES:
        state["last_heal"] = {"at": record["at"], "at_epoch": now,
                              "outcome": record["outcome"],
                              "failing_since": state["failing_since"]}
    if record.get("outcome") != "ran":
        return None
    result = reprobe(str(state.get("path") or ""), timeout_s=store_timeout_s)
    record["reprobe"] = {"ok": result.get("ok") is True, "reason": result.get("reason"),
                         "latency_ms": result.get("latency_ms")}
    return result if result.get("ok") is True else None


def context_probe_from_env(env: Optional[dict] = None,
                           runner: Callable[..., Any] = run_bounded) -> Optional[Callable[[], dict]]:
    """A probe from a context with TCC access, when the host configures one.

    TARTCI_TCC_CONTEXT_PROBE is an argv (shell-split) that exits 0 when the
    store answers from that context, for example an `ssh localhost ...` whose
    sshd holds Full Disk Access. Unset, the reading is skipped and recorded
    as not configured.
    """
    import shlex
    env = os.environ if env is None else env
    text = str(env.get("TARTCI_TCC_CONTEXT_PROBE") or "").strip()
    if not text:
        return None
    argv = shlex.split(text)

    def probe() -> dict:
        try:
            completed = runner(argv, timeout=LOG_SCAN_TIMEOUT_S, operation="tcc_context_probe")
            require_success(completed, operation="tcc_context_probe")
        except ObservationError as exc:
            return {"ok": False, "reason": f"{exc.kind}: {exc.detail}"[:200], "argv": argv}
        except (OSError, ValueError) as exc:
            return {"ok": False, "reason": str(exc)[:200], "argv": argv}
        return {"ok": True, "argv": argv}
    return probe


def describe(state: dict) -> str:
    """One bracketed clause for the watchdog line: the cause and the heal outcome."""
    classification = state.get("classification") or {}
    kind = classification.get("kind")
    if not kind:
        return ""
    heal_record = state.get("heal") or {}
    outcome = heal_record.get("outcome")
    text = kind
    if outcome:
        text += f": heal {outcome}"
        reprobe = heal_record.get("reprobe")
        if isinstance(reprobe, dict):
            if reprobe.get("ok"):
                text += f", store answered in {reprobe.get('latency_ms')} ms"
            else:
                text += f", store still {reprobe.get('reason')}"
    return text


def read_state(home: Optional[str] = None) -> dict:
    """The watchdog's store-health state, read only (internal volume)."""
    root = os.environ.get("TARTCI_HOME") or os.path.join(home or os.path.expanduser("~"), ".tartci")
    return store_health.load(os.path.join(root, "state", "store-health.json"))


def lane_cause(state: dict, now: Optional[float] = None) -> str:
    """What a lane prints while it defers: `tcc_attribution_wedge <heal outcome>`,
    or nothing when the watchdog has no fresh wedge verdict."""
    now = time.time() if now is None else now
    classification = state.get("classification") or {}
    if classification.get("kind") != KIND or state.get("state") == "healthy":
        return ""
    try:
        stamp = time.strptime(str(classification.get("classified_at")), "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return ""
    if now - calendar.timegm(stamp) > FRESH_S:
        return ""
    outcome = (state.get("heal") or {}).get("outcome") or "not_attempted"
    return f"{KIND} {outcome}"


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--lane-cause", action="store_true",
                      help="print the fresh wedge verdict for a lane's deferral line, or nothing")
    mode.add_argument("--scan-log", action="store_true",
                      help="run the bounded signature scan now and print it")
    mode.add_argument("--state", action="store_true",
                      help="print the watchdog's classification and heal records")
    mode.add_argument("--launchd-probe", metavar="STORE",
                      help="open STORE's volume root from a fresh launchd job now (bounded, "
                           "the job is always removed) and print the reading")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--last-minutes", type=int, default=MIN_WINDOW_S // 60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.lane_cause:
        text = lane_cause(read_state())
        if text:
            print(text)
        return 0
    if args.launchd_probe:
        root = os.environ.get("TARTCI_HOME") or os.path.join(os.path.expanduser("~"), ".tartci")
        value = launchd_probe(args.launchd_probe, os.path.join(root, "state"),
                              args.timeout_seconds)
    elif args.scan_log:
        value = scan_log(max(60, args.last_minutes * 60))
    else:
        state = read_state()
        value = {"state": state.get("state"), "failing_since": state.get("failing_since"),
                 "classification": state.get("classification"), "heal": state.get("heal"),
                 "last_heal": state.get("last_heal")}
    print(json.dumps(value, sort_keys=True) if args.json else json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
