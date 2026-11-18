#!/usr/bin/env python3
"""Reach the owner for the few alerts only he can clear.

A pending macOS privacy prompt waits for a person to click Allow at the
host's screen. A GitHub issue alone sat unread for hours on 2026-11-18
(tartci #960, m5studio, 01:27Z to about 05:35Z). This posts to the fleet
Discord channel, the owner channel fleet-health and fleet-liveness already
use, through the same webhook file they restore from the Fleet Agents vault
(~/.config/pulp/secrets/fleet/discord-machine-room.webhook, mode 0600). It
never restores the file itself and never prints or logs the URL.

`notify_once` posts at most once per key (one wedge episode): the key is
recorded only after a post succeeds, so a failed post is retried on the next
pass and a sent one never repeats. Runs under the system python3 (3.9).

Nothing here touches /Volumes. During a privacy-prompt wedge every open on
the Tart store volume hangs, and this is the message that says so: the
webhook file and the receipt live under the home directory, the watchdog
that calls it runs from ~/.local/share/tartci-generations with its log
under ~/Library/Logs, and the post itself is a network call
(test_owner_notify.NoVolumesTests).
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Tuple

WEBHOOK_RELATIVE = Path(".config") / "pulp" / "secrets" / "fleet" / "discord-machine-room.webhook"
TIMEOUT_S = 20

Sender = Callable[[str], Tuple[bool, str]]


def webhook_file() -> Path:
    override = os.environ.get("TARTCI_OWNER_WEBHOOK_FILE")
    return Path(override) if override else Path.home() / WEBHOOK_RELATIVE


def enabled() -> bool:
    return os.environ.get("TARTCI_OWNER_NOTIFY", "1") != "0"


def _test_run() -> bool:
    import sys  # noqa: PLC0415
    import host_off  # noqa: PLC0415 - owns the scratch-home rule for every outbound write
    return host_off._scratch_home() or "unittest" in sys.modules


def post(text: str) -> Tuple[bool, str]:
    """POST `text` to the owner channel. (ok, detail); never raises."""
    if _test_run():
        # Tests inject a sender; a forgotten stub must never reach the owner.
        return False, "refused: owner notifications are never sent from a test run"
    path = webhook_file()
    try:
        url = path.read_text().strip()
    except (OSError, ValueError):  # ValueError covers a non-UTF-8 file
        return False, f"no readable owner webhook at {path}"
    if not url.startswith("https://"):
        return False, f"owner webhook at {path} is not an https URL"
    try:
        request = urllib.request.Request(
            url, data=json.dumps({"content": text[:1900]}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "tartci-owner-notify"},
            method="POST")
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 - https only
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except Exception as exc:  # noqa: BLE001 - best effort; the URL is never part of the detail
        return False, f"post failed: {type(exc).__name__}"


def notify_once(state: Path, key: str, text: str, *, sender: Optional[Sender] = None,
                now: Optional[float] = None) -> dict:
    """Post `text` unless `key` was already sent from `state`. Returns
    {"sent": bool, "skipped": reason | None, "detail": str}."""
    import host_off  # noqa: PLC0415
    import time  # noqa: PLC0415
    saved = host_off._read_json(state) or {}
    if saved.get("key") == key and saved.get("sent_at"):
        return {"sent": False, "skipped": "already sent for this episode", "detail": ""}
    if not enabled():
        return {"sent": False, "skipped": "TARTCI_OWNER_NOTIFY=0", "detail": ""}
    record = {"key": key, "text": text[:500], "detail": "sending"}
    try:
        # Proven writable before sending: a receipt that cannot be saved would
        # repeat the message on every pass.
        host_off._write_json(state, record)
    except OSError as exc:
        return {"sent": False, "skipped": f"receipt not writable: {type(exc).__name__}",
                "detail": ""}
    ok, detail = (sender or post)(text)
    record["detail"] = detail[:200]
    if ok:
        record["sent_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(time.time() if now is None else now))
    try:
        host_off._write_json(state, record)
    except OSError as exc:
        detail = f"{detail}; receipt not saved: {type(exc).__name__}"
    return {"sent": ok, "skipped": None, "detail": detail}
