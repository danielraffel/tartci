#!/usr/bin/env python3
"""tartci-peer-read: the forced command for the fleet's read-only peer key.

sshd runs this in place of whatever the peer asked for when the key is listed
in ``authorized_keys`` as ``restrict,command="…/tartci-peer-read" <pub>``. The
asked-for command arrives in ``SSH_ORIGINAL_COMMAND``. Exactly the commands
other fleet hosts already send to read this host run; each also has a short
verb a client may send instead:

  pool-status           cd ~ && ~/.local/bin/tartci pool status --json
                        (fleet_self_update peer probe, capacity_floor)
  self-update-marker    fleet_self_update._PEER_MARKER (same bytes, imported)
  fleet-source-verified the canary's fleet-source-verified.json read
                        (fleet_self_update.read_canary_verified)
  job-claim-status      cd ~ && ~/.local/bin/tartci job-claim status --publish
                        (job_claim.gather_peers)

An allowed command runs through /bin/sh exactly as the full-shell key ran it,
so its output is byte-identical. Anything else (an empty command, a shell, a
command with one byte changed) exits EX_REFUSED with one audit line on stderr
and in ``$TARTCI_HOME/state/peer-read/audit.log``.

Not installed by anything yet: see docs/peer-read-key.md for the owner step.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EX_REFUSED = 77
AUDIT_MAX_BYTES = 1 << 20
SHELL = "/bin/sh"

POOL_STATUS = "cd ~ && ~/.local/bin/tartci pool status --json"
JOB_CLAIM_STATUS = "cd ~ && ~/.local/bin/tartci job-claim status --publish"


def allowed_commands() -> dict[str, str]:
    """verb -> the exact command string a peer sends today."""
    import fleet_self_update as su  # noqa: PLC0415 - the marker's single source

    return {
        "pool-status": POOL_STATUS,
        "self-update-marker": su._PEER_MARKER,  # noqa: SLF001 - same bytes as the client
        "fleet-source-verified": ('date +%s; cat "${TARTCI_HOME:-$HOME/.tartci}/state/self-update/'
                                  + su.FLEET_SOURCE_VERIFIED + '" 2>/dev/null || true'),
        "job-claim-status": JOB_CLAIM_STATUS,
    }


def resolve(asked: str, allowed: dict[str, str]) -> str | None:
    """The command to run for `asked`: a verb or a byte-exact legacy string."""
    if asked in allowed:
        return allowed[asked]
    if asked in allowed.values():
        return asked
    return None


def audit(asked: str | None, why: str) -> None:
    """One line on stderr and in the audit log; the log is best-effort."""
    text = asked or ""
    client = (os.environ.get("SSH_CONNECTION") or "?").split(" ")[0][:64]
    line = (f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
            f"tartci-peer-read refused from={client} reason={why} len={len(text)} "
            f"sha256={hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:16]} "
            f"command={text[:64]!r}")
    print(line, file=sys.stderr)
    home = os.environ.get("TARTCI_HOME") or os.path.expanduser("~/.tartci")
    log = Path(home) / "state" / "peer-read" / "audit.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.exists() and log.stat().st_size > AUDIT_MAX_BYTES:
            os.replace(log, log.with_name(log.name + ".1"))
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main(environ: dict[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    asked = environ.get("SSH_ORIGINAL_COMMAND")
    if not asked:
        audit(asked, "no_command")
        return EX_REFUSED
    try:
        allowed = allowed_commands()
    except Exception:  # noqa: BLE001 - an unloadable allowlist refuses everything
        audit(asked, "allowlist_unavailable")
        return EX_REFUSED
    command = resolve(asked, allowed)
    if command is None:
        audit(asked, "not_allowed")
        return EX_REFUSED
    sys.stdout.flush()
    os.execv(SHELL, [SHELL, "-c", command])
    return EX_REFUSED  # not reached


if __name__ == "__main__":
    sys.exit(main())
