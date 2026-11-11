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
LOG_SCAN_TIMEOUT_S = 25.0
MIN_WINDOW_S = 10 * 60
MAX_WINDOW_S = 3 * 3600
# A classification older than this says nothing about now.
FRESH_S = 30 * 60
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
            "not_permitted", "timeout", "failed", "disabled")


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def signature_hits(text: str) -> dict[str, Any]:
    """Count the wedge signatures in `log show --style compact` output."""
    counts = {name: 0 for name, _ in SIGNATURES}
    first: Optional[str] = None
    last: Optional[str] = None
    for line in text.splitlines():
        matched = False
        for name, pattern in SIGNATURES:
            if pattern.search(line):
                counts[name] += 1
                matched = True
        if not matched:
            continue
        stamp = _TIMESTAMP.match(line)
        if stamp:
            first = first or stamp.group(1)
            last = stamp.group(1)
    return {"total": sum(counts.values()), "by_signature": counts,
            "first": first, "last": last}


def window_seconds(failing_since: Optional[float], now: float) -> int:
    """How far back to scan: the whole streak plus a margin, within bounds."""
    if not isinstance(failing_since, (int, float)) or failing_since > now:
        return MIN_WINDOW_S
    return int(min(MAX_WINDOW_S, max(MIN_WINDOW_S, now - failing_since + MIN_WINDOW_S)))


def scan_log(window_s: int, *, runner: Callable[..., Any] = run_bounded) -> dict[str, Any]:
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
    hits = signature_hits(completed.stdout or "")
    hits.update(ok=True, window_s=window_s)
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
             store_timeout_s: float = 10.0,
             scan: Optional[Callable[[int], dict]] = None,
             control: Optional[Callable[[str, float], dict]] = None,
             context_probe: Optional[Callable[[], dict]] = None) -> dict[str, Any]:
    """Classify a failing store probe. Returns {kind, reason, evidence, classified_at}."""
    now = time.time() if now is None else now
    scan = scan_log if scan is None else scan
    control = control_probe if control is None else control
    evidence: dict[str, Any] = {"consecutive_failures": consecutive_failures,
                                "failure_threshold": failure_threshold,
                                "last_reason": last_reason}

    def verdict(kind: str, reason: str) -> dict[str, Any]:
        return {"kind": kind, "reason": reason, "evidence": evidence,
                "classified_at": _iso(now)}

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
    window = window_seconds(failing_since, now)
    log = scan(window)
    evidence["log_scan"] = log
    minutes = max(1, int(math.ceil(window / 60.0)))
    if not log.get("ok"):
        return verdict("stalled_unclassified",
                       f"log show unavailable ({log.get('error')}); the store is stalled "
                       "but the cause is unproven")
    if int(log.get("total") or 0) == 0:
        return verdict("stalled_unclassified",
                       f"no sandboxd/tccd Removable Volumes failure in the last {minutes} min; "
                       "the store is stalled but the cause is unproven")
    if context_probe is not None:
        ctx = context_probe()
        evidence["tcc_context_probe"] = ctx
        if not ctx.get("ok"):
            return verdict("stalled_unclassified",
                           "the store did not answer from a context with TCC access either "
                           f"({ctx.get('reason')}); the volume, not tccd, is stalled")
    return verdict(KIND,
                   f"{log.get('total')} sandboxd/tccd Removable Volumes authorization "
                   f"failure(s) in the last {minutes} min while the internal control path "
                   "answers: launchd-context opens of the store hang on tccd")


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


def heal(*, now: Optional[float] = None, last_heal: Optional[dict] = None,
         runner: Optional[Callable[..., Any]] = None, wrapper: Optional[str] = None,
         sudo: Optional[str] = None) -> dict[str, Any]:
    """Run the scoped root wrapper once. Never anything broader."""
    now = time.time() if now is None else now
    runner = subprocess.run if runner is None else runner
    wrapper = WRAPPER if wrapper is None else wrapper
    sudo = SUDO if sudo is None else sudo
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
    try:
        completed = runner([sudo, "-n", wrapper], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=HEAL_TIMEOUT_S,
                           check=False)
    except subprocess.TimeoutExpired:
        record.update(outcome="timeout", rc=None,
                      detail=f"{wrapper} did not return within {HEAL_TIMEOUT_S:g}s")
        return record
    except OSError as exc:
        record.update(outcome="failed", rc=None, detail=str(exc)[:300])
        return record
    output = ((completed.stderr or "") + (completed.stdout or "")).strip()[-300:]
    record["rc"] = completed.returncode
    record["detail"] = output
    if completed.returncode == 0:
        record["outcome"] = "ran"
    elif completed.returncode == 75:
        record["outcome"] = "wrapper_rate_limited"
    elif completed.returncode == 1 and "password" in output.lower():
        record["outcome"] = "not_permitted"
    else:
        record["outcome"] = "failed"
    return record


def watchdog_step(state: dict, previous: dict, *, control_path: str, store_timeout_s: float,
                  dry_run: bool, now: Optional[float] = None,
                  heal_enabled: bool = True,
                  classify_fn: Callable[..., dict] = classify,
                  heal_fn: Callable[..., dict] = heal,
                  reprobe: Optional[Callable[..., dict]] = None) -> Optional[dict]:
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
    state["failing_since"] = since if isinstance(since, (int, float)) and since <= now else now
    if isinstance(previous.get("last_heal"), dict):
        state["last_heal"] = previous["last_heal"]
    if not state.get("drain_required"):
        return None
    classification = classify_fn(
        consecutive_failures=int(state.get("consecutive_failures") or 0),
        failure_threshold=int(state.get("failure_threshold") or 2),
        last_reason=last_result.get("reason"), failing_since=state["failing_since"],
        control_path=control_path, now=now, store_timeout_s=store_timeout_s)
    state["classification"] = classification
    if classification.get("kind") != KIND:
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
    if record.get("outcome") in ("ran", "wrapper_rate_limited", "timeout", "failed"):
        # These all reached the wrapper: they spend the heal budget.
        state["last_heal"] = {"at": record["at"], "at_epoch": now,
                              "outcome": record["outcome"]}
    if record.get("outcome") != "ran":
        return None
    result = reprobe(str(state.get("path") or ""), timeout_s=store_timeout_s)
    record["reprobe"] = {"ok": result.get("ok") is True, "reason": result.get("reason"),
                         "latency_ms": result.get("latency_ms")}
    return result if result.get("ok") is True else None


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
    parser.add_argument("--last-minutes", type=int, default=MIN_WINDOW_S // 60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.lane_cause:
        text = lane_cause(read_state())
        if text:
            print(text)
        return 0
    if args.scan_log:
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
