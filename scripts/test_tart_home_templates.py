#!/usr/bin/env python3
"""LaunchAgent templates must preserve each host's declared Tart VM store."""

from __future__ import annotations

import unittest
import plistlib
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TART_TEMPLATES = (
    ROOT / "launchd/com.danielraffel.pulp.tart-runner-macos.plist.template",
    ROOT / "launchd/com.danielraffel.pulp.tart-runner-macos-release.plist.template",
    ROOT / "launchd/com.danielraffel.pulp.tart-runner-linux.plist.template",
    ROOT / "launchd/com.danielraffel.tartci.reap.plist.template",
    # The watchdog probes the Tart inventory to decide whether a supervisor is
    # crash-looping or merely quiet under a long build, and it deletes nothing
    # itself but triggers a bootout that does. A LaunchAgent inherits no login
    # shell, so without a rendered TART_HOME its `tart list` reads the default
    # store, comes back empty on any host that keeps VMs elsewhere, and the
    # probe is blind exactly when it is load-bearing.
    ROOT / "launchd/com.danielraffel.tartci.launchd-watchdog.plist.template",
)


class TartHomeTemplateTests(unittest.TestCase):
    def test_launch_agents_render_a_host_declared_store(self) -> None:
        for template in TART_TEMPLATES:
            body = template.read_text(encoding="utf-8")
            self.assertIn(
                "<key>TART_HOME</key>\n        <string>$TART_HOME</string>",
                body,
                template.name,
            )
            self.assertNotIn(
                "<key>TART_HOME</key>\n        <string>$HOME/VMs</string>",
                body,
                f"{template.name} would reset an external-store host on reinstall",
            )

    def test_documented_install_shape_substitutes_the_store(self) -> None:
        # A template that carries $TART_HOME but documents an install command
        # that never substitutes it ships a literal "$TART_HOME" into launchd,
        # which is the same blindness as omitting the key entirely.
        for template in TART_TEMPLATES:
            body = template.read_text(encoding="utf-8")
            header = body.split("-->", 1)[0]
            substituted = (
                "render_launchd_template.py" in header
                or "$TART_HOME|" in header
            )
            self.assertTrue(
                substituted,
                f"{template.name} documents an install that leaves "
                "$TART_HOME unsubstituted",
            )

    def test_fresh_gate_migration_requires_and_renders_tart_home(self) -> None:
        body = (ROOT / "scripts/migrate_macos_gate_agent.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("${TART_HOME:?fresh migration requires", body)
        self.assertIn("render_launchd_template.py", body)

    def test_renderer_preserves_legal_path_metacharacters(self) -> None:
        template = TART_TEMPLATES[0]
        tart_home = "/Volumes/Builds & VMs/pipe|slash\\VMs"
        result = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts/render_launchd_template.py"),
                str(template),
                "--set",
                f"TART_HOME={tart_home}",
                "--set",
                "HOME=/Users/tester",
            ],
            check=True,
            capture_output=True,
        )
        value = plistlib.loads(result.stdout)
        self.assertEqual(value["EnvironmentVariables"]["TART_HOME"], tart_home)

    def test_renderer_injects_host_specific_environment(self) -> None:
        result = subprocess.run(
            [
                "python3",
                str(ROOT / "scripts/render_launchd_template.py"),
                str(TART_TEMPLATES[0]),
                "--set",
                "TART_HOME=/tmp/VMs",
                "--set",
                "HOME=/Users/tester",
                "--environment",
                "HTTP_PROXY=http://127.0.0.1:49125",
                "--environment",
                "TARTCI_GUEST_HTTP_PROXY=http://192.168.64.1:49125",
            ],
            check=True,
            capture_output=True,
        )
        environment = plistlib.loads(result.stdout)["EnvironmentVariables"]
        self.assertEqual(environment["HTTP_PROXY"], "http://127.0.0.1:49125")
        self.assertEqual(
            environment["TARTCI_GUEST_HTTP_PROXY"],
            "http://192.168.64.1:49125",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
