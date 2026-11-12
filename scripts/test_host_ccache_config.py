#!/usr/bin/env python3
"""Host-side ccache never reads the guest-writable cache's ccache.conf.

Every gate VM mounts the shared ccache directory read-write, so a guest can
write its `ccache.conf`, and ccache acts on keys such as `prefix_command`. The
host's calls (the pre-boot guard, the reclaim trim, the layer stats, the
per-job stats line) read entries and counters only, so each runs with
`CCACHE_CONFIGPATH` pointing at an empty file.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ccache_guard  # noqa: E402
import ccache_layer  # noqa: E402
import gate_ccache_trim as trim  # noqa: E402


def planted_cache(root: Path) -> Path:
    """A shared cache whose ccache.conf a guest wrote."""
    cache = root / "ccache"
    cache.mkdir()
    (cache / "ccache.conf").write_text(
        f"prefix_command = {root / 'planted.sh'}\n", encoding="utf-8")
    return cache


class Captured:
    """A subprocess.run stand-in that records each call's environment."""

    def __init__(self) -> None:
        self.envs: list[dict[str, str] | None] = []

    def __call__(self, command, **kwargs):
        self.envs.append(kwargs.get("env"))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


class EveryHostCallReadsNoSharedConfig(unittest.TestCase):
    def assert_isolated(self, captured: Captured) -> None:
        self.assertTrue(captured.envs, "control: the call reached ccache")
        for env in captured.envs:
            self.assertIsNotNone(env, "the call inherited the caller's environment")
            self.assertEqual(env["CCACHE_CONFIGPATH"], os.devnull)

    def test_the_guard_inspects_and_extracts_without_the_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            entry = Path(tmp) / "entry"
            entry.write_bytes(b"x")
            captured = Captured()
            with mock.patch.object(ccache_guard.subprocess, "run", captured):
                ccache_guard.inspect_manifest("ccache", entry)
                with mock.patch.object(ccache_guard, "result_file", return_value=entry):
                    ccache_guard.result_verdict("ccache", Path(tmp), "key")
            self.assertEqual(len(captured.envs), 2)
            self.assert_isolated(captured)

    def test_the_trim_counts_and_evicts_without_the_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = planted_cache(Path(tmp))
            captured = Captured()
            trim.counters("ccache", cache, captured)
            with mock.patch.object(trim, "measure", return_value={}):
                trim.evict(cache=cache, max_age_days=14, fix=True, ccache="ccache",
                           busy_probe=lambda: None, runner=captured)
            # The count and the eviction, plus any recount after it.
            self.assertGreaterEqual(len(captured.envs), 2)
            self.assert_isolated(captured)

    def test_the_layer_stats_read_without_the_layers_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            captured = Captured()
            with mock.patch.object(ccache_layer.subprocess, "run", captured):
                ccache_layer.layer_stats(Path(tmp), "ccache")
            self.assert_isolated(captured)
            self.assertEqual(captured.envs[0]["CCACHE_DIR"], str(Path(tmp) / "local"))

    def test_the_per_job_stats_line_reads_no_shared_config(self) -> None:
        text = (ROOT / "providers/tart-macos/run.sh").read_text(encoding="utf-8")
        host = text.split("\nGUEST\n", 1)[1]
        lines = [line for line in host.splitlines()
                 if re.search(r"(^|\s)ccache\s+-", line) and not line.lstrip().startswith("#")]
        self.assertTrue(lines, "control: the host stats line was found")
        for line in lines:
            self.assertIn("CCACHE_CONFIGPATH=/dev/null", line)


@unittest.skipUnless(shutil.which("ccache"), "needs ccache on PATH")
class RealCcache(unittest.TestCase):
    def shown(self, cache: Path, env: dict[str, str]) -> str:
        return subprocess.run(["ccache", "-d", str(cache), "--show-config"], env=env,
                              capture_output=True, text=True, encoding="utf-8",
                              check=True, timeout=60).stdout

    def test_a_planted_prefix_command_is_not_in_the_hosts_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = planted_cache(Path(tmp))
            planted = str(Path(tmp) / "planted.sh")
            # Negative control: ccache reads the planted file by default.
            self.assertIn(planted, self.shown(cache, dict(os.environ)))
            self.assertNotIn(planted, self.shown(cache, ccache_guard.host_ccache_env()))


if __name__ == "__main__":
    unittest.main()
