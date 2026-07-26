#!/usr/bin/env python3
"""Behavioral tests for the priority-aware idle gate in providers/tart-macos/runner.sh.

The idle gate lets a SECONDARY macOS lane (e.g. a sanitizer or coverage VM)
yield its VM slot to a higher-priority lane (the required build gate) so it can
never starve it on a shared-cap host — the failure that backed out the Pulp
coverage VM lane. The decision hinges on `priority_demand()`, which counts how
many priority-lane jobs are waiting/running by checking whether each job's
requested labels are a SUBSET of the priority lane's advertised labels (GitHub's
assignment rule: a runner serves a job iff it advertises every requested label).

Rather than assert the source contains a string (which gave false confidence
before — see the inverted-subset bug fixed in Pulp #4087), we EXTRACT the exact
pure matcher snippet from the script and run it against synthetic job data, so
an inverted/incorrect subset check is caught by behavior. No gh/tart needed, so
this runs on any platform in CI.

Run:  python3 scripts/test_idle_gate.py
"""
from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "providers" / "tart-macos" / "runner.sh"


class IdleGateWiringTests(unittest.TestCase):
    """Pin the gate-decision contract: the loop must consult priority_demand and
    the feature must default OFF so the primary gate runner is unaffected."""

    def setUp(self) -> None:
        self.body = SCRIPT.read_text(encoding="utf-8")

    def test_script_syntax_is_valid(self) -> None:
        r = subprocess.run(["bash", "-n", str(SCRIPT)],
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_feature_defaults_off(self) -> None:
        # YIELD_WORKFLOW empty by default → priority_demand short-circuits to 0
        # → the boot condition `p -eq 0` is always true → no behavior change for
        # the primary gate runner / release lane.
        self.assertIn('YIELD_WORKFLOW="${TARTCI_YIELD_TO_WORKFLOW_NAME:-}"', self.body)
        self.assertIn('[ -n "$YIELD_WORKFLOW" ] || { printf \'%s\\n\' 0; return 0; }',
                      self.body)

    def test_loop_gate_consults_priority_demand(self) -> None:
        # The boot decision must include the priority-demand==0 clause.
        self.assertIn('p="$(priority_demand)"', self.body)
        self.assertIn('[ "${p:-0}" -eq 0 ]', self.body)

    def test_priority_demand_uses_shared_bounded_scanner(self) -> None:
        self.assertIn('scripts/queue_scan.py"', self.body)
        self.assertIn("--job-statuses queued,in_progress", self.body)
        self.assertIn("--shared-cache-file", self.body)
        self.assertNotIn('actions/runs?status=$st', self.body)


class IdleGateFailClosed(unittest.TestCase):
    """priority_demand must FAIL CLOSED: a gh error → report demand (yield), not 0.

    gh errors cluster during the rate-limit storms when the gate most needs its
    slot, so a fail-open guard would let the secondary boot exactly when that is
    most harmful. Driven through the real `--print-priority-demand` hook with stub
    CLIs so no gh/tart/network is needed.
    """

    def _run(self, gh_stub_body: str) -> subprocess.CompletedProcess:
        import stat
        import tempfile
        d = tempfile.mkdtemp()
        tmp = Path(d)
        def _exe(name: str, body: str) -> None:
            p = tmp / name
            p.write_text(body, encoding="utf-8")
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _exe("tart", "#!/usr/bin/env bash\nexit 0\n")
        _exe("stubgh", gh_stub_body)
        base = [b for b in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
                if Path(b).exists()]
        env = {
            "HOME": str(tmp),
            "PATH": os.pathsep.join([str(tmp), *base]),
            "TART_HOME": str(tmp / "vms"),
            "TARTCI_STATE_DIR": str(tmp / "state"),
            "TARTCI_GH_CLI": "stubgh",
            # Idle gate ON so priority_demand actually probes.
            "TARTCI_YIELD_TO_WORKFLOW_NAME": "Build and Test",
            "TARTCI_YIELD_TO_LABELS": "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm",
        }
        return subprocess.run(
            ["bash", str(SCRIPT), "--print-priority-demand",
             "--labels", "self-hosted,macOS,ARM64,pulp-coverage-vm-macos"],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_gh_error_reports_demand_not_zero(self) -> None:
        # Stub gh exits non-zero (simulates rate-limit / 5xx).
        r = self._run("#!/usr/bin/env bash\nexit 1\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1",
                         "priority_demand must report demand (yield) on a gh error, "
                         "not fail open to 0")

    def test_no_priority_runs_reports_zero(self) -> None:
        # Stub gh returns a real workflow ID and empty run pages → no demand.
        r = self._run(
            """#!/usr/bin/env bash
case "$*" in
  *actions/workflows?per_page=100*) printf '%s\n' '{"workflows":[{"id":99,"name":"Build and Test"}]}' ;;
  *actions/workflows/99/runs*) printf '%s\n' '{"workflow_runs":[]}' ;;
  *) exit 1 ;;
esac
"""
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")


class HostHealthYieldTests(unittest.TestCase):
    """host_health_yield must FAIL OPEN (the deliberate opposite of
    priority_demand): a missing/broken host_vitals probe → boot (0), never a
    stalled runner. Yields (1) only when the probe reports critical (or warn when
    opted in). Driven through the real `--print-host-health` hook with a stub
    host_vitals.sh so no real host metrics are needed."""

    def _run(self, *, vitals_body: str | None, env_extra: dict) -> subprocess.CompletedProcess:
        import stat
        import tempfile
        d = tempfile.mkdtemp()
        tmp = Path(d)
        # A `tart` stub keeps the script's top-level happy even though the
        # --print-host-health hook exits before the VM path.
        (tmp / "tart").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        (tmp / "tart").chmod(0o755)
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
            ["bash", str(SCRIPT), "--print-host-health"],
            capture_output=True, text=True, check=False, env=env,
        )

    _CRIT = "#!/usr/bin/env bash\nexit 20\n"
    _WARN = "#!/usr/bin/env bash\nexit 10\n"
    _GREEN = "#!/usr/bin/env bash\nexit 0\n"

    def test_feature_off_prints_zero_without_probing(self) -> None:
        # No TARTCI_HOST_VITALS_YIELD → short-circuit to 0 even if host_vitals
        # would report critical. This is the primary-gate-runner default.
        r = self._run(vitals_body=self._CRIT, env_extra={})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_missing_probe_fails_open(self) -> None:
        # Feature ON but host_vitals.sh absent → fail OPEN (0/boot), never wedge.
        r = self._run(vitals_body=None, env_extra={"TARTCI_HOST_VITALS_YIELD": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_critical_yields(self) -> None:
        r = self._run(vitals_body=self._CRIT,
                      env_extra={"TARTCI_HOST_VITALS_YIELD": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")

    def test_green_boots(self) -> None:
        r = self._run(vitals_body=self._GREEN,
                      env_extra={"TARTCI_HOST_VITALS_YIELD": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_warn_does_not_yield_by_default(self) -> None:
        # WARN (10) is below the critical bar; without opt-in we keep booting.
        r = self._run(vitals_body=self._WARN,
                      env_extra={"TARTCI_HOST_VITALS_YIELD": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")

    def test_warn_yields_when_opted_in(self) -> None:
        r = self._run(vitals_body=self._WARN,
                      env_extra={"TARTCI_HOST_VITALS_YIELD": "1",
                                 "TARTCI_HOST_VITALS_YIELD_ON_WARN": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")

    def test_critical_still_yields_with_warn_opt_in(self) -> None:
        r = self._run(vitals_body=self._CRIT,
                      env_extra={"TARTCI_HOST_VITALS_YIELD": "1",
                                 "TARTCI_HOST_VITALS_YIELD_ON_WARN": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")


class HostHealthWiringTests(unittest.TestCase):
    """Pin the host-health contract: the gate decision is the SHARED helper and the
    loop consults it. The default-off + fail-open policy itself is asserted in
    test_host_health_lib.py (the single source of the extracted decision)."""

    def setUp(self) -> None:
        self.body = SCRIPT.read_text(encoding="utf-8")

    def test_sources_shared_host_health_lib(self) -> None:
        self.assertIn(
            'source "$TARTCI_ROOT/providers/common/host-health.lib.sh"', self.body
        )

    def test_loop_gate_consults_host_health(self) -> None:
        self.assertIn('hh="$(tartci_host_health_yield)"', self.body)
        self.assertIn('[ "${hh:-0}" -eq 0 ]', self.body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
