import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "leased-build"

FAKE_TARTCI = r"""#!/bin/bash
if [ "$2" = acquire ]; then
  printf '%s\n' "$*" >> "$FAKE_TARTCI_LOG"
  printf '{"ok": true, "lease": {"lease_size_cores": %s}}' "${FAKE_GRANT:-1}"
fi
exit 0
"""


class LeasedBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.log = root / "acquire.log"
        fake = root / "bin" / "tartci"
        fake.parent.mkdir()
        fake.write_text(FAKE_TARTCI)
        fake.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{fake.parent}:{os.environ['PATH']}",
                    "FAKE_TARTCI_LOG": str(self.log)}
        self.env.pop("LEASED_BUILD_ID", None)

    def run_wrapper(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(SCRIPT), *args], text=True, capture_output=True,
                              env={**self.env, **env}, timeout=60)

    def test_check_class_asks_for_one_core_with_a_one_core_floor(self) -> None:
        result = self.run_wrapper("--class", "check", "--cores", "8", "--", "sh", "-c",
                                  'echo "$CMAKE_BUILD_PARALLEL_LEVEL"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        line = self.log.read_text()
        self.assertIn("--cores 1 --min-cores 1", line)
        self.assertIn("--class check", line)
        self.assertNotIn("--allow-floor", line)

    def test_background_class_keeps_its_two_core_floor(self) -> None:
        result = self.run_wrapper("--", "true", FAKE_GRANT="4")
        self.assertEqual(result.returncode, 0, result.stderr)
        line = self.log.read_text()
        self.assertIn("--cores 8 --min-cores 2", line)
        self.assertIn("--class background", line)
        self.assertIn("--allow-floor", line)

    def test_unknown_class_is_refused(self) -> None:
        result = self.run_wrapper("--class", "bogus", "--", "true")
        self.assertEqual(result.returncode, 2)
        self.assertIn("background, interactive or check", result.stderr)

    def test_class_from_environment(self) -> None:
        result = self.run_wrapper("--", "true", LEASED_BUILD_CLASS="check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--cores 1 --min-cores 1", self.log.read_text())


if __name__ == "__main__":
    unittest.main()
