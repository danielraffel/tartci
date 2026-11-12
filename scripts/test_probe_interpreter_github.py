from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import probe_interpreter_github as probe


class ProbeTests(unittest.TestCase):
    def fake_interpreter(self, exception: str) -> Path:
        root = Path(self.temp.name)
        path = root / "python"
        path.write_text(textwrap.dedent(f"""
            #!{sys.executable}
            import sys, urllib.error, urllib.request
            def fail(*args, **kwargs):
                raise {exception}
            urllib.request.urlopen = fail
            exec(sys.argv[2], {{}})
        """).lstrip())
        path.chmod(stat.S_IRWXU)
        return path

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_http_rate_limit_is_reachable(self) -> None:
        interpreter = self.fake_interpreter(
            "urllib.error.HTTPError('https://api.github.com', 403, 'rate limit', {}, None)"
        )
        result = subprocess.run([sys.executable, str(Path(probe.__file__)), str(interpreter)],
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_connection_error_still_fails(self) -> None:
        interpreter = self.fake_interpreter(
            "urllib.error.URLError('temporary DNS failure')"
        )
        result = subprocess.run([sys.executable, str(Path(probe.__file__)), str(interpreter)],
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("interpreter GitHub probe failed", result.stderr)

    def test_http_200_is_reachable(self) -> None:
        root = Path(self.temp.name)
        interpreter = root / "working-python"
        interpreter.write_text(textwrap.dedent(f"""
            #!{sys.executable}
            import sys, urllib.request
            class Response:
                def read(self, count):
                    return b'{{}}'
            urllib.request.urlopen = lambda *args, **kwargs: Response()
            exec(sys.argv[2], {{}})
        """).lstrip())
        interpreter.chmod(stat.S_IRWXU)
        result = subprocess.run([sys.executable, str(Path(probe.__file__)), str(interpreter)],
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_timeout_is_a_transport_failure(self) -> None:
        interpreter = self.fake_interpreter("TimeoutError('timed out')")
        result = subprocess.run([sys.executable, str(Path(probe.__file__)), str(interpreter)],
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("interpreter GitHub probe failed", result.stderr)


if __name__ == "__main__":
    unittest.main()
