#!/usr/bin/env python3
"""Behavioral tests for the TARTCI_GH_CLI knob in the runner providers.

The providers poll GitHub every VM_POLL seconds on every host; on a shared
personal PAT that polling is the dominant secondary-rate-limit source. The knob
lets a host route all provider API traffic through a GitHub-App CLI wrapper
(TARTCI_GH_CLI=ghapp) instead of bare `gh`, onto the App's separate bucket.

The decisive property — "does the provider actually call the configured CLI, not
hard-coded `gh`?" — is tested by behavior: we drive the tart-macos provider's
`--print-queue` path (which runs `queued_work`, the per-poll API call) with a
stub CLI on PATH and assert the stub was invoked. Stubs for `tart` and the CLI
mean no real gh/tart/network is needed, so this runs anywhere.

Run:  python3 scripts/test_gh_cli_knob.py
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = [
    ROOT / "providers" / "tart-macos" / "runner.sh",
    ROOT / "providers" / "tart-linux" / "runner.sh",
    ROOT / "providers" / "qemu-windows" / "runner.sh",
]
MACOS = ROOT / "providers" / "tart-macos" / "runner.sh"


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class GhCliKnobBehavior(unittest.TestCase):
    """Drive the real provider with a stub CLI and prove the knob is honored."""

    def _run_print_queue(self, tmp: Path, gh_cli_env: dict) -> subprocess.CompletedProcess:
        # Minimal PATH: only our stub dir + the dirs holding bash/python3.
        # Deliberately EXCLUDES any real `gh`, so a hard-coded `gh` call would
        # fail to resolve — making "the knob worked" unambiguous.
        base = [d for d in ("/bin", "/usr/bin", "/opt/homebrew/bin", "/usr/local/bin")
                if Path(d).exists()]
        env = {
            "HOME": str(tmp),  # provider runs under `set -u`; needs HOME defined
            "PATH": os.pathsep.join([str(tmp), *base]),
            "TART_HOME": str(tmp / "vms"),
            "TARTCI_STATE_DIR": str(tmp / "state"),
            **gh_cli_env,
        }
        return subprocess.run(
            ["bash", str(MACOS), "--print-queue",
             "--labels", "self-hosted,macOS,ARM64,pulp-build-vm"],
            capture_output=True, text=True, check=False, env=env,
        )

    def test_knob_routes_api_calls_through_configured_cli(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            marker = tmp / "called"
            # Stub `tart` so `command -v tart` + any tart calls succeed.
            _write_exec(tmp / "tart", "#!/usr/bin/env bash\nexit 0\n")
            # Stub CLI: records that it ran, then prints empty runs JSON so the
            # queued_work python parses cleanly and prints 0.
            _write_exec(tmp / "myghapp",
                        "#!/usr/bin/env bash\n"
                        f'echo "$@" >> "{marker}"\n'
                        'printf \'{"workflow_runs": []}\'\n')
            r = self._run_print_queue(tmp, {"TARTCI_GH_CLI": "myghapp"})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "0", r.stdout + r.stderr)
            self.assertTrue(marker.exists(),
                            "configured TARTCI_GH_CLI stub was never invoked — "
                            "the provider is not honoring the knob")
            # And it was used for the actual API call.
            self.assertIn("api", marker.read_text())
            # Default value is `gh` — asserted structurally in GhCliKnobWiring
            # (`${TARTCI_GH_CLI:-gh}`); not re-run here to avoid a real network
            # `gh` call when the host PATH already has gh.


class GhCliKnobWiring(unittest.TestCase):
    """All three providers expose the knob and route every call through it."""

    def test_all_providers_define_and_default_the_knob(self) -> None:
        for p in PROVIDERS:
            body = p.read_text(encoding="utf-8")
            self.assertIn('TARTCI_GH_CLI="${TARTCI_GH_CLI:-gh}"', body, p.name)

    def test_no_provider_calls_bare_gh_api(self) -> None:
        # The whole point: nothing may hard-code `gh` for an API call anymore.
        for p in PROVIDERS:
            body = p.read_text(encoding="utf-8")
            self.assertNotIn("gh api ", body, f"{p.name} still has a bare `gh api` call")
            self.assertNotIn('["gh", "api"', body,
                             f"{p.name} still has a bare gh call in python")

    def test_macos_jit_override_is_explicit_and_denial_blocks_reboot(self) -> None:
        body = MACOS.read_text(encoding="utf-8")
        self.assertIn('JIT_GH_CLI="${TARTCI_JIT_GH_CLI:-$GH_CLI}"', body)
        self.assertIn('generate-jitconfig', body)
        self.assertIn('SHIPYARD_GH_APP_REPO="$REPO" GH_REPO="$REPO"', body)
        self.assertIn('"$JIT_GH_CLI" api -X POST', body)
        self.assertIn('jit_admission_denied', body)
        self.assertIn('JIT admission remains blocked', body)

    def test_unattended_linux_and_reap_agents_pin_app_wrapper(self) -> None:
        templates = (
            ROOT / "launchd" / "com.danielraffel.pulp.tart-runner-macos.plist.template",
            ROOT / "launchd" / "com.danielraffel.pulp.tart-runner-macos-release.plist.template",
            ROOT / "launchd" / "com.danielraffel.pulp.tart-runner-linux.plist.template",
            ROOT / "launchd" / "com.danielraffel.pulp.qemu-runner-windows.plist.template",
            ROOT / "launchd" / "com.danielraffel.tartci.reap.plist.template",
            ROOT / "launchd" / "com.danielraffel.pulp.queue-saturation.plist.template",
        )
        for template in templates:
            body = template.read_text(encoding="utf-8")
            key = "PULP_SAT_GH_CLI" if "queue-saturation" in template.name else "TARTCI_GH_CLI"
            self.assertIn(f"<key>{key}</key>", body, template.name)
            self.assertIn("<string>ghapp</string>", body, template.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
