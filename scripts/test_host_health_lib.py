#!/usr/bin/env python3
"""Behavioral tests for the SHARED host-health auto-yield helper.

providers/common/host-health.lib.sh is the single implementation of the
"should we boot a NEW VM right now, or is the host too saturated?" decision,
sourced by all three provider supervisors (tart-macos, tart-linux,
qemu-windows) so the policy can never drift between lanes. Before this
extraction each runner carried a byte-identical copy of the logic.

This file is the AUTHORITATIVE test of that decision: it sources ONLY the lib
(no runner, no gh/tart/qemu/golden) and drives `tartci_host_health_yield`
directly against a stub `host_vitals.sh`, asserting the full matrix. It also
asserts every provider runner sources the shared lib and consults the shared
function, so parity across lanes holds by construction rather than by three
hand-maintained copies. The per-runner end-to-end matrix through each
`--print-host-health` hook still lives in test_idle_gate.py (macOS) and
test_secondary_lane_host_health.py (linux + windows).

The helper is deliberately FAIL-OPEN (the opposite of the priority gate): a
missing / non-executable / erroring probe must yield 0 (boot), never wedge a
required lane. It yields 1 only on CRITICAL (>=20) always, and on WARN (>=10)
when TARTCI_HOST_VITALS_YIELD_ON_WARN is set.

Run:  python3 scripts/test_host_health_lib.py
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "providers" / "common" / "host-health.lib.sh"
PROVIDERS = {
    "tart-macos": ROOT / "providers" / "tart-macos" / "runner.sh",
    "tart-linux": ROOT / "providers" / "tart-linux" / "runner.sh",
    "qemu-windows": ROOT / "providers" / "qemu-windows" / "runner.sh",
}


def _yield(env_extra: dict, *, vitals_body: str | None = None,
           vitals_name: str = "host_vitals.sh") -> str:
    """Source the shared lib under `set -euo pipefail` (matching every runner's
    top-level) and return `tartci_host_health_yield`'s stdout. Running under
    `set -e` also proves the helper never aborts on a non-zero probe exit."""
    d = tempfile.mkdtemp()
    tmp = Path(d)
    if vitals_body is not None:
        probe = tmp / vitals_name
        probe.write_text(vitals_body, encoding="utf-8")
        probe.chmod(probe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    base = [b for b in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
            if Path(b).exists()]
    env = {
        "HOME": str(tmp),
        "PATH": os.pathsep.join([str(tmp), *base]),
        **env_extra,
    }
    r = subprocess.run(
        ["bash", "-c",
         f'set -euo pipefail; source "{LIB}"; tartci_host_health_yield'],
        capture_output=True, text=True, check=False, env=env,
    )
    # A non-zero rc here would mean the helper aborted the caller under set -e —
    # itself a fail-open violation, so surface it loudly.
    if r.returncode != 0:
        raise AssertionError(f"helper aborted under set -e (rc={r.returncode}): {r.stderr}")
    return r.stdout.strip()


_CRIT = "#!/usr/bin/env bash\nexit 20\n"
_WARN = "#!/usr/bin/env bash\nexit 10\n"
_GREEN = "#!/usr/bin/env bash\nexit 0\n"
_GARBAGE = "#!/usr/bin/env bash\nexit 1\n"  # not one of 0/10/20 → treat as boot


class HostHealthLibMatrix(unittest.TestCase):
    """Drive the extracted decision directly, no runner in the loop."""

    def test_feature_off_short_circuits_without_probing(self) -> None:
        # No TARTCI_HOST_VITALS_YIELD → 0 even though the probe would report crit.
        self.assertEqual(_yield({}, vitals_body=_CRIT), "0")

    def test_feature_explicit_zero_is_off(self) -> None:
        self.assertEqual(_yield({"TARTCI_HOST_VITALS_YIELD": "0"}, vitals_body=_CRIT), "0")

    def test_missing_probe_fails_open(self) -> None:
        self.assertEqual(_yield({"TARTCI_HOST_VITALS_YIELD": "1"}, vitals_body=None), "0")

    def test_garbage_exit_code_fails_open(self) -> None:
        # A probe that IS runnable but exits with something other than 0/10/20 (a
        # broken/degraded probe) is below the critical bar → boot, never wedge.
        # (A probe entirely absent from PATH is covered by test_missing_probe_*;
        # the executability of a present-but-non-exec file is privilege/OS-dependent
        # and not a contract point, so it is deliberately not asserted here.)
        self.assertEqual(_yield({"TARTCI_HOST_VITALS_YIELD": "1"}, vitals_body=_GARBAGE), "0")

    def test_green_boots(self) -> None:
        self.assertEqual(_yield({"TARTCI_HOST_VITALS_YIELD": "1"}, vitals_body=_GREEN), "0")

    def test_critical_yields(self) -> None:
        self.assertEqual(_yield({"TARTCI_HOST_VITALS_YIELD": "1"}, vitals_body=_CRIT), "1")

    def test_warn_does_not_yield_by_default(self) -> None:
        self.assertEqual(_yield({"TARTCI_HOST_VITALS_YIELD": "1"}, vitals_body=_WARN), "0")

    def test_warn_yields_when_opted_in(self) -> None:
        self.assertEqual(
            _yield({"TARTCI_HOST_VITALS_YIELD": "1", "TARTCI_HOST_VITALS_YIELD_ON_WARN": "1"},
                   vitals_body=_WARN), "1")

    def test_warn_opt_in_explicit_zero_is_off(self) -> None:
        self.assertEqual(
            _yield({"TARTCI_HOST_VITALS_YIELD": "1", "TARTCI_HOST_VITALS_YIELD_ON_WARN": "0"},
                   vitals_body=_WARN), "0")

    def test_critical_still_yields_with_warn_opt_in(self) -> None:
        self.assertEqual(
            _yield({"TARTCI_HOST_VITALS_YIELD": "1", "TARTCI_HOST_VITALS_YIELD_ON_WARN": "1"},
                   vitals_body=_CRIT), "1")

    def test_boundary_just_below_warn_boots_with_opt_in(self) -> None:
        # exit 9 (< 10) is below even the WARN bar → boot regardless of opt-in.
        self.assertEqual(
            _yield({"TARTCI_HOST_VITALS_YIELD": "1", "TARTCI_HOST_VITALS_YIELD_ON_WARN": "1"},
                   vitals_body="#!/usr/bin/env bash\nexit 9\n"), "0")

    def test_boundary_just_below_critical_yields_only_with_opt_in(self) -> None:
        # exit 19 (>=10, <20): yields only when WARN opt-in is set.
        body = "#!/usr/bin/env bash\nexit 19\n"
        self.assertEqual(_yield({"TARTCI_HOST_VITALS_YIELD": "1"}, vitals_body=body), "0")
        self.assertEqual(
            _yield({"TARTCI_HOST_VITALS_YIELD": "1", "TARTCI_HOST_VITALS_YIELD_ON_WARN": "1"},
                   vitals_body=body), "1")

    def test_above_critical_still_yields(self) -> None:
        # A probe that reports beyond the critical code (>=20) still yields.
        self.assertEqual(
            _yield({"TARTCI_HOST_VITALS_YIELD": "1"},
                   vitals_body="#!/usr/bin/env bash\nexit 30\n"), "1")

    def test_custom_probe_name_is_honored(self) -> None:
        # TARTCI_HOST_VITALS_BIN overrides the default probe name.
        self.assertEqual(
            _yield({"TARTCI_HOST_VITALS_YIELD": "1", "TARTCI_HOST_VITALS_BIN": "my_vitals"},
                   vitals_body=_CRIT, vitals_name="my_vitals"), "1")


class HostHealthLibParity(unittest.TestCase):
    """Every provider runner must delegate to the shared helper — the whole point
    of the extraction is that there is exactly one decision, not three copies."""

    def test_lib_defines_the_function(self) -> None:
        self.assertIn("tartci_host_health_yield()", LIB.read_text(encoding="utf-8"))

    def test_all_providers_source_the_shared_lib(self) -> None:
        for lane, script in PROVIDERS.items():
            with self.subTest(lane=lane):
                body = script.read_text(encoding="utf-8")
                self.assertIn(
                    'source "$TARTCI_ROOT/providers/common/host-health.lib.sh"', body,
                    f"{lane} does not source the shared host-health lib")

    def test_no_provider_redefines_the_function(self) -> None:
        # A local `tartci_host_health_yield(){` / `host_health_yield(){` definition
        # in a runner would reintroduce the drift this extraction removed.
        for lane, script in PROVIDERS.items():
            with self.subTest(lane=lane):
                body = script.read_text(encoding="utf-8")
                self.assertNotIn("host_health_yield(){", body,
                                 f"{lane} redefines the host-health helper locally")

    def test_all_providers_call_the_shared_helper(self) -> None:
        for lane, script in PROVIDERS.items():
            with self.subTest(lane=lane):
                body = script.read_text(encoding="utf-8")
                self.assertIn("tartci_host_health_yield", body)

    def test_lib_syntax_is_valid(self) -> None:
        r = subprocess.run(["bash", "-n", str(LIB)],
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
