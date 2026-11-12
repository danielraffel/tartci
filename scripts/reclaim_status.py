#!/usr/bin/env python3
"""The disk reclaimer's last pass: how long ago, and how it ended.

Read from the receipt every tartci reclaim pass writes when it finishes
($TARTCI_HOME/state/reclaim/last-run.json), never from launchd. launchd can say
a job with the right label is loaded while the job it holds is somebody else's:
m3's reclaimer was shadowed for a day by a leaked registration that exited 127
every hour, and every launchd-based view reported it loaded. A receipt that has
stopped getting fresher is the one signal that does not depend on what launchd
believes.

States:
  ok          the last pass finished recently and exited 0
  low_space   the last pass ran (exit 3) and a scanned volume is still below
              the free-space floor: the reclaimer worked, the disk is still full
  boot_low    the last pass ran (exit 5): the Tart store's volume is fine but
              the boot data volume, which no lease floor covers, is still
              below its own floor
  failed      the last pass finished recently and did not exit 0, 3 or 5
  stale       no pass has finished within STALE_AFTER_S: the agent is dead,
              shadowed, wedged, or never scheduled, whatever launchd says
  never       no receipt yet (a host that has not run a pass since this shipped)
  unreadable  the receipt exists and could not be parsed
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from typing import Any

GIB = 1024 ** 3
# The agent runs hourly and a pass that runs both Pulp reapers can take up to
# ~80 minutes under their own timeouts, so a healthy host's newest receipt is
# at most ~2.5 hours old. Four hours is two missed passes, not one slow one.
STALE_AFTER_S = 4 * 3600


def default_state_dir() -> pathlib.Path:
    override = os.environ.get("TARTCI_RECLAIM_STATE_DIR")
    if override:
        return pathlib.Path(override).expanduser()
    home = os.environ.get("TARTCI_HOME", str(pathlib.Path.home() / ".tartci"))
    return pathlib.Path(home).expanduser() / "state" / "reclaim"


def default_log_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get(
        "TARTCI_RECLAIM_LOG",
        str(pathlib.Path.home() / "Library" / "Logs" / "tartci" / "tartci-reclaim.log"),
    )).expanduser()


def status(state_dir: pathlib.Path | None = None, *, now: float | None = None,
           stale_after_s: float = STALE_AFTER_S,
           log_path: pathlib.Path | None = None) -> dict[str, Any]:
    path = (state_dir or default_state_dir()) / "last-run.json"
    now = time.time() if now is None else now
    out: dict[str, Any] = {"receipt": str(path), "stale_after_s": stale_after_s}
    # Secondary evidence only: the log's mtime moves whenever anything writes
    # it, so it is reported beside the receipt and never decides the state.
    try:
        out["log_age_seconds"] = int(max(0.0, now - (log_path or default_log_path()).stat().st_mtime))
    except OSError:
        out["log_age_seconds"] = None
    try:
        receipt = json.loads(path.read_text())
    except FileNotFoundError:
        out["state"] = "never"
        return out
    except (OSError, ValueError) as exc:
        out.update(state="unreadable", error=str(exc))
        return out
    if not isinstance(receipt, dict) or not isinstance(
            receipt.get("finished_ts"), (int, float)):
        out.update(state="unreadable", error="receipt has no finished_ts")
        return out
    pulp = receipt.get("pulp_reapers") or {}
    age = max(0.0, now - float(receipt["finished_ts"]))
    out.update(
        age_seconds=int(age),
        finished_ts=receipt["finished_ts"],
        exit_code=receipt.get("exit_code"),
        mode=receipt.get("mode"),
        reclaimed_bytes=receipt.get("reclaimed_bytes", 0),
        free_bytes_after=receipt.get("free_bytes_after"),
        pulp_reapers_enabled=bool(pulp.get("enabled")),
        pulp_reclaimed_bytes=pulp.get("reclaimed_bytes", 0),
        pulp_error=pulp.get("error"),
        vitals_refresh=(pulp.get("host_vitals_sensor") or {})
        if isinstance(pulp.get("host_vitals_sensor"), dict) else {},
        fail_below_gb=receipt.get("fail_below_gb"),
        tightest_root=receipt.get("tightest_root"),
        boot_volume=receipt.get("boot_volume")
        if isinstance(receipt.get("boot_volume"), dict) else None,
        scratch_removed_bytes=(receipt.get("scratch_dirs") or {}).get("removed_bytes")
        if isinstance(receipt.get("scratch_dirs"), dict) else None,
        boot_usage=receipt.get("boot_usage")
        if isinstance(receipt.get("boot_usage"), dict) else None,
    )
    if age > stale_after_s:
        out["state"] = "stale"
    elif receipt.get("exit_code") == 3:
        out["state"] = "low_space"
    elif receipt.get("exit_code") == 5:
        out["state"] = "boot_low"
    elif receipt.get("exit_code") != 0:
        out["state"] = "failed"
    else:
        out["state"] = "ok"
    return out


def describe(value: dict[str, Any]) -> str:
    state = value.get("state")
    if state == "never":
        log_age = value.get("log_age_seconds")
        log = ("no reclaim log either" if log_age is None
               else f"reclaim log last written {log_age / 3600:.1f}h ago")
        return f"reclaim: no pass recorded yet (no receipt at {value['receipt']}; {log})"
    if state == "unreadable":
        return f"reclaim: receipt UNREADABLE ({value.get('error')})"
    hours = value["age_seconds"] / 3600
    pulp = (f", pulp reapers {value['pulp_reclaimed_bytes'] / GIB:.1f} GiB"
            if value.get("pulp_reapers_enabled") else ", pulp reapers off")
    warnings = degraded(value)
    body = (f"last pass {hours:.1f}h ago, exit {value.get('exit_code')}, "
            f"reclaimed {(value.get('reclaimed_bytes') or 0) / GIB:.1f} GiB{pulp}")
    if warnings:
        body = "; ".join(warnings) + "; " + body
    if state == "stale":
        return (f"reclaim: STALE, no pass finished in {hours:.1f}h "
                f"(> {value['stale_after_s'] / 3600:g}h); the reclaim agent is not "
                f"running whatever launchd says; {body}")
    if state == "low_space":
        free = value.get("free_bytes_after")
        where = value.get("tightest_root") or "a scanned volume"
        amount = "unknown" if free is None else f"{free / GIB:.1f} GiB"
        floor = value.get("fail_below_gb")
        floor_text = f" < {floor:g} GiB floor" if isinstance(floor, (int, float)) else ""
        return (f"reclaim: FREE SPACE STILL LOW after the pass: {amount} on {where}"
                f"{floor_text} (nothing left that the reclaimer may delete); {body}")
    if state == "boot_low":
        boot = value.get("boot_volume") or {}
        free = boot.get("free_bytes_after")
        amount = "unknown" if free is None else f"{free / GIB:.1f} GiB"
        floor = boot.get("floor_gb")
        floor_text = f" < {floor:g} GiB floor" if isinstance(floor, (int, float)) else ""
        return (f"reclaim: BOOT VOLUME LOW after the pass: {amount} on "
                f"{boot.get('path') or 'the boot volume'}{floor_text} (leases are judged on "
                f"the Tart store and unaffected; look in /private/tmp and the per-user "
                f"temp dir); {body}")
    if state == "failed":
        return f"reclaim: LAST PASS FAILED; {body}"
    if warnings:
        return f"reclaim: WARN {body}"
    return f"reclaim: ok; {body}"


def degraded(value: dict[str, Any]) -> list[str]:
    """What the pass could not do although it exited 0, loudest first.

    A pass whose Pulp reapers never ran (m5studio's profile names a
    worktrees_root that does not exist) exited 0 and printed reclaim: ok,
    with the reason in a parenthesis nobody reads.
    """
    out = []
    error = value.get("pulp_error")
    if value.get("pulp_reapers_enabled") and error:
        if "worktrees_root" in str(error) and "is not a directory" in str(error):
            out.append(f"pulp reapers: NOT RUNNING (worktrees_root missing: {error})")
        else:
            out.append(f"pulp reapers: NOT RUNNING ({error})")
    vitals = value.get("vitals_refresh") or {}
    if vitals.get("state") == "refresh_failed":
        out.append(f"host-vitals sensor: REFRESH FAILED ({vitals.get('detail')})")
    # CI and agent data piling up on the boot volume, by path (boot_usage.py).
    usage = value.get("boot_usage") or {}
    out.extend(w for w in usage.get("warnings") or [] if isinstance(w, str))
    if usage.get("error"):
        out.append(f"boot usage: NOT MEASURED ({usage['error']})")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reclaim_status")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args(argv)
    value = status(pathlib.Path(args.state_dir) if args.state_dir else None)
    if args.json:
        print(json.dumps(value, sort_keys=True))
    else:
        print(describe(value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
