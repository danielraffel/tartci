#!/usr/bin/env python3
"""The fallback peer read must never consume the supervisor's stdin.

`tartci_assignment_v2_select_live` walks the slot's classes with
`while read ... done <<< "$ASSIGNMENT_V2_ORDER_LABELS"`, so the loop's stdin IS
the rest of the class list. When a class has young demand, the loop asks
`gate_supply.py decide`, which reads each peer over ssh. An ssh client forwards
its stdin to the remote end, so it drained that list: the loop ended after the
first class with young demand and every later class went unobserved. A slot
whose order put merge-group before pr-head therefore idled while old pr-head
jobs queued, every time a young merge-group job existed.

Both layers are covered, each with a negative control that reinstates the
defect and must observe the failure, so a pass here is not the test grading a
path it never reaches.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V2_LIB = ROOT / "providers/tart-macos/assignment-v2.lib.sh"
GATE_SUPPLY = ROOT / "scripts/gate_supply.py"
BASE = "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm"

LIB_FUNCTIONS = (
    "tartci_assignment_v2_tier_index",
    "tartci_assignment_v2_tier_labels",
    "tartci_assignment_v2_select_live",
    "tartci_fallback_enabled",
    "tartci_fallback_grant_file",
    "tartci_fallback_clear_grant",
    "tartci_fallback_decision",
    "tartci_fallback_grant",
)

# Stands in for the real helper's ssh peer read: drains stdin exactly as an ssh
# client without `-n` does, then holds (peers cover the young demand).
FAKE_DECIDE = """#!/usr/bin/env python3
import sys
sys.stdin.read()
print("hold demand=1 peer_cover=1 local_cover=0 excess=0 studio:free=0,in_flight=1")
"""


def function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n(.*?)^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(1)


def select_live(lib_source: str) -> str:
    """Run the real class walk with young merge-group demand and old pr-head demand."""
    with tempfile.TemporaryDirectory(prefix="tartci-fallback-stdin-") as tmp_text:
        tmp = Path(tmp_text)
        (tmp / "scripts").mkdir()
        (tmp / "scripts/gate_supply.py").write_text(FAKE_DECIDE, encoding="utf-8")
        state = tmp / "state"
        state.mkdir()
        pieces = "".join(f"{name}(){{\n{function_body(lib_source, name)}}}\n"
                         for name in LIB_FUNCTIONS)
        script = textwrap.dedent(f"""\
            set -euo pipefail
            TARTCI_ROOT={str(tmp)!r}
            STATE_DIR={str(state)!r}
            RUNNER_NAME=lane
            REPO=Generous-Corp/pulp
            SLOT=2
            FALLBACK_PEERS=studio=m3
            FALLBACK_PEER_MAX_AGE=60
            ASSIGNMENT_MODE=event-class-v2
            MIN_QUEUED_AGE=600
            ASSIGNMENT_V2_BASE_LABELS={BASE!r}
            TIER_LABELS_CONFIG=$'pulp-build-merge-group\\npulp-build-pr-head'
            ASSIGNMENT_V2_ORDER_LABELS="$TIER_LABELS_CONFIG"
            event(){{ :; }}
            # Merge-group: one job, too young for this lane's 600s minimum.
            # PR-head: three jobs past the minimum.
            tartci_assignment_v2_tier_demand(){{
              case "$1:${{3:-$MIN_QUEUED_AGE}}" in
                pulp-build-merge-group:0) printf '1\\n' ;;
                pulp-build-merge-group:*) printf '0\\n' ;;
                pulp-build-pr-head:*) printf '3\\n' ;;
              esac
            }}
            """) + pieces + "tartci_assignment_v2_select_live\n"
        proc = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                              stdin=subprocess.DEVNULL, check=False, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(f"harness failed rc={proc.returncode}: {proc.stderr}")
        return proc.stdout.strip()


class ClassWalkSurvivesPeerRead(unittest.TestCase):
    def test_young_demand_on_an_earlier_class_does_not_hide_a_later_class(self) -> None:
        self.assertEqual(
            select_live(V2_LIB.read_text(encoding="utf-8")),
            f"3|{BASE},pulp-build-pr-head|1",
        )

    def test_control_without_the_redirect_the_later_class_is_lost(self) -> None:
        source = V2_LIB.read_text(encoding="utf-8")
        fixed = '--max-age-seconds "$FALLBACK_PEER_MAX_AGE" </dev/null 2>/dev/null)"'
        self.assertEqual(source.count(fixed), 1, "the fix this control removes is gone")
        broken = source.replace(fixed, '--max-age-seconds "$FALLBACK_PEER_MAX_AGE" 2>/dev/null)"')
        # The walk ends after merge-group: nothing selected, one class counted.
        self.assertEqual(select_live(broken), f"0|{BASE}|1")


# A fake ssh that records its argv and whatever stdin it was able to read.
FAKE_SSH = """#!/usr/bin/env python3
import json, sys
from pathlib import Path
record = Path(sys.argv[0]).with_suffix(".record")
record.write_text(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}))
print("{}")
"""

PEER_READ = """
import json, subprocess, sys
sys.path.insert(0, {scripts!r})
import gate_supply
if {broken!r}:
    real_run = subprocess.run
    def run(command, **kwargs):
        kwargs.pop("stdin", None)
        return real_run([part for part in command if part != "-n"], **kwargs)
    gate_supply.subprocess.run = run
gate_supply.fetch_peer("studio", "m3", "Generous-Corp/pulp", "pulp-build-pr-head",
                       ssh={ssh!r}, timeout=10)
print(json.dumps({{"left": sys.stdin.read()}}))
"""


def peer_read(*, broken: bool) -> tuple[dict, dict]:
    with tempfile.TemporaryDirectory(prefix="tartci-fetch-peer-") as tmp_text:
        ssh = Path(tmp_text) / "ssh"
        ssh.write_text(FAKE_SSH, encoding="utf-8")
        ssh.chmod(0o755)
        code = PEER_READ.format(scripts=str(GATE_SUPPLY.parent), broken=broken, ssh=str(ssh))
        proc = subprocess.run([sys.executable, "-c", code], input="pulp-build-pr-head\n",
                              text=True, capture_output=True, check=False, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(f"peer read failed rc={proc.returncode}: {proc.stderr}")
        import json
        return (json.loads(proc.stdout.strip().splitlines()[-1]),
                json.loads(ssh.with_suffix(".record").read_text()))


class PeerReadLeavesStdinAlone(unittest.TestCase):
    def test_ssh_neither_reads_nor_drains_the_callers_stdin(self) -> None:
        caller, ssh = peer_read(broken=False)
        self.assertEqual(caller["left"], "pulp-build-pr-head\n")
        self.assertEqual(ssh["stdin"], "")
        self.assertEqual(ssh["argv"][0], "-n")

    def test_control_an_ssh_given_the_callers_stdin_drains_it(self) -> None:
        caller, ssh = peer_read(broken=True)
        self.assertEqual(ssh["stdin"], "pulp-build-pr-head\n")
        self.assertEqual(caller["left"], "")


if __name__ == "__main__":
    unittest.main()
