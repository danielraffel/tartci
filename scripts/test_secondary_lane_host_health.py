#!/usr/bin/env python3
"""host_health_yield parity for the SECONDARY lanes (tart-linux + qemu-windows).

The macOS gate got host-health auto-yield first (test_idle_gate.py). But a Mac
Studio also runs the tart-linux and qemu-windows lanes on the SAME cores, so if
only macOS backs off under load the host can still be oversubscribed — that is the
2026-07-06/07 "load 135 on 28 cores" incident: the required macOS gate degraded
because the other lanes kept booting VMs on an already-saturated host. These lanes
now carry the SAME fail-open host_health_yield as tart-macos, exercised here
through each provider's `--print-host-health` hook. No gh / tart / qemu / golden is
needed because the hook exits before any VM requirement, so this runs on any
platform in CI.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROVIDERS = {
    "tart-linux": Path(__file__).resolve().parents[1] / "providers" / "tart-linux" / "runner.sh",
    "qemu-windows": Path(__file__).resolve().parents[1] / "providers" / "qemu-windows" / "runner.sh",
}

_CRIT = "#!/usr/bin/env bash\nexit 20\n"
_WARN = "#!/usr/bin/env bash\nexit 10\n"
_GREEN = "#!/usr/bin/env bash\nexit 0\n"


def _run(script: Path, *, vitals_body: str | None, env_extra: dict) -> subprocess.CompletedProcess:
    d = tempfile.mkdtemp()
    tmp = Path(d)
    # Stubs keep each script's top-level happy even though --print-host-health
    # exits before the VM path (belt-and-suspenders; the hook precedes them).
    for stub in ("tart", "qemu-system-aarch64", "qemu-img"):
        (tmp / stub).write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (tmp / stub).chmod(0o755)
    if vitals_body is not None:
        hv = tmp / "host_vitals.sh"
        hv.write_text(vitals_body, encoding="utf-8")
        hv.chmod(0o755)
    base = [b for b in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
            if Path(b).exists()]
    env = {
        "HOME": str(tmp),
        "PATH": os.pathsep.join([str(tmp), *base]),
        "TART_HOME": str(tmp / "vms"),
        "TARTCI_STATE_DIR": str(tmp / "state"),
        **env_extra,
    }
    return subprocess.run(
        ["bash", str(script), "--print-host-health"],
        capture_output=True, text=True, check=False, env=env,
    )


# (label, vitals_body, env_extra, expected_stdout) — identical policy to the
# tart-macos HostHealthYieldTests, asserted for every secondary provider.
CASES = [
    ("feature off short-circuits to 0", _CRIT, {}, "0"),
    ("missing probe fails open (0)", None, {"TARTCI_HOST_VITALS_YIELD": "1"}, "0"),
    ("critical yields (1)", _CRIT, {"TARTCI_HOST_VITALS_YIELD": "1"}, "1"),
    ("green boots (0)", _GREEN, {"TARTCI_HOST_VITALS_YIELD": "1"}, "0"),
    ("warn does not yield by default (0)", _WARN, {"TARTCI_HOST_VITALS_YIELD": "1"}, "0"),
    ("warn yields when opted in (1)", _WARN,
     {"TARTCI_HOST_VITALS_YIELD": "1", "TARTCI_HOST_VITALS_YIELD_ON_WARN": "1"}, "1"),
    ("critical still yields with warn opt-in (1)", _CRIT,
     {"TARTCI_HOST_VITALS_YIELD": "1", "TARTCI_HOST_VITALS_YIELD_ON_WARN": "1"}, "1"),
]


class SecondaryLaneHostHealthTests(unittest.TestCase):
    def test_print_host_health_matrix(self) -> None:
        for lane, script in PROVIDERS.items():
            for label, body, env_extra, expected in CASES:
                with self.subTest(lane=lane, case=label):
                    r = _run(script, vitals_body=body, env_extra=env_extra)
                    self.assertEqual(r.returncode, 0, f"{lane}: {r.stderr}")
                    self.assertEqual(r.stdout.strip(), expected, f"{lane}: {label}")


class SecondaryLaneWiringTests(unittest.TestCase):
    """Pin the contract in source: feature defaults OFF and the loop consults it."""

    def test_feature_defaults_off(self) -> None:
        for lane, script in PROVIDERS.items():
            with self.subTest(lane=lane):
                body = script.read_text(encoding="utf-8")
                self.assertIn('HOST_VITALS_YIELD="${TARTCI_HOST_VITALS_YIELD:-}"', body)
                self.assertIn(
                    '[ -n "$HOST_VITALS_YIELD" ] && [ "$HOST_VITALS_YIELD" != 0 ] '
                    "|| { printf '%s\\n' 0; return 0; }",
                    body,
                )

    def test_loop_gate_consults_host_health(self) -> None:
        for lane, script in PROVIDERS.items():
            with self.subTest(lane=lane):
                body = script.read_text(encoding="utf-8")
                self.assertIn('hh="$(host_health_yield)"', body)
                self.assertIn('[ "${hh:-0}" -eq 0 ]', body)

    def test_syntax_is_valid(self) -> None:
        for lane, script in PROVIDERS.items():
            with self.subTest(lane=lane):
                r = subprocess.run(["bash", "-n", str(script)],
                                   capture_output=True, text=True, check=False)
                self.assertEqual(r.returncode, 0, f"{lane}: {r.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
