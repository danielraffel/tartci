#!/usr/bin/env python3
"""runner.sh refuses to boot a lane whose own cache root is not what it names."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
LIB = ROOT / "providers" / "tart-macos" / "lane-cache-root.lib.sh"


def refusal(root: Path, host: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", 'source "$1"; tartci_lane_cache_root_refusal "$2" "$3"',
         "_", str(LIB), str(root), host],
        text=True, capture_output=True, check=False, env={"PATH": "/usr/bin:/bin"},
    )


class LaneCacheRootRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # realpath: macOS temp dirs sit behind the /var -> /private/var link.
        self.base = Path(os.path.realpath(tmp.name))
        self.host = self.base / "host-ci"
        (self.host / "ccache").mkdir(parents=True)
        (self.host / "ccache" / "marker").write_text("planted-by-pr-job\n", encoding="utf-8")
        (self.host / "configure-checks").mkdir()

    def test_a_real_separate_root_boots(self) -> None:
        root = self.base / "trusted"
        (root / "ccache").mkdir(parents=True)
        (root / "configure-checks").mkdir()
        result = refusal(root, str(self.host))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_root_not_created_yet_boots(self) -> None:
        result = refusal(self.base / "trusted", str(self.host))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_symlinked_root_is_refused(self) -> None:
        root = self.base / "trusted"
        root.symlink_to(self.host)
        # Control: through the link the "trusted" lane sees the PR-planted file.
        self.assertTrue((root / "ccache" / "marker").exists())
        result = refusal(root, str(self.host))
        self.assertEqual(result.returncode, 1)
        self.assertIn("symlink", result.stdout)

    def test_a_symlinked_mount_subdirectory_is_refused(self) -> None:
        for sub in ("ccache", "configure-checks"):
            with self.subTest(sub=sub):
                root = self.base / f"trusted-{sub}"
                root.mkdir()
                (root / sub).symlink_to(self.host / sub)
                result = refusal(root, str(self.host))
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"{root / sub} is a symlink", result.stdout)

    def test_a_root_under_a_symlinked_parent_is_refused(self) -> None:
        (self.base / "link").symlink_to(self.host)
        result = refusal(self.base / "link" / "ccache" / "..", str(self.host))
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_overlap_with_the_host_root_is_refused(self) -> None:
        inside = self.host / "trusted"
        inside.mkdir()
        cases = {
            "same": (self.host, "is or sits inside"),
            "inside": (inside, "is or sits inside"),
            "containing": (self.base, "contains the host cache root"),
        }
        for name, (root, message) in cases.items():
            with self.subTest(name=name):
                result = refusal(root, str(self.host))
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stdout)

    def test_overlap_is_judged_on_physical_paths(self) -> None:
        # The host root named through a link still overlaps the real directory.
        (self.base / "host-link").symlink_to(self.host)
        result = refusal(self.host, str(self.base / "host-link"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("is or sits inside", result.stdout)

    def test_a_host_root_lane_is_not_checked(self) -> None:
        # Lanes on the host root carry no TARTCI_HOST_CI_CACHE; nothing changes for them.
        link = self.base / "linked-host"
        link.symlink_to(self.host)
        result = refusal(link, "")
        self.assertEqual(result.returncode, 0)


    def test_a_parent_link_to_a_non_host_dir_is_refused(self) -> None:
        # Not an overlap with the host root, so only the physical-path
        # comparison can see that the root is not the directory it names.
        elsewhere = self.base / "elsewhere"
        (elsewhere / "lane").mkdir(parents=True)
        (self.base / "link").symlink_to(elsewhere)
        result = refusal(self.base / "link" / "lane", str(self.host))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"resolves to {elsewhere / 'lane'}", result.stdout)
        # Control: the same directory named by its real path boots.
        self.assertEqual(refusal(elsewhere / "lane", str(self.host)).returncode, 0)


# Stubs for the functions the mount preparation calls, each recording its call.
STUBS = r"""
calls="$1"; shift
tartci_prepare_disk_root() { echo "prepare $1" >>"$calls"; mkdir -p "$1" || return 1; [ -z "${STUB_AFTER_PREPARE:-}" ] || eval "$STUB_AFTER_PREPARE"; }
tartci_ccache_guard() { echo "guard $1" >>"$calls"; }
note() { echo "note $*" >>"$calls"; }
discard_current_vm() { echo "discard" >>"$calls"; }
tartci_release_vm_lease() { echo "release" >>"$calls"; }
runtime_emit_complete() { echo "complete $*" >>"$calls"; }
"""


class MountPreparationTests(unittest.TestCase):
    """The mount preamble, executed: the lib function, and the runner's own
    lines that call it, with every side effect stubbed and recorded."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.host = self.base / "host-ci"
        (self.host / "ccache").mkdir(parents=True)
        (self.host / "configure-checks").mkdir()
        self.fetch = self.base / "fetchcontent"
        self.calls = self.base / "calls.log"

    def runner_block(self) -> str:
        # The whole statement around the call, taken as written: a guard that
        # was neutralized (`if false && ...`) runs here as it would in a boot.
        body = RUNNER.read_text(encoding="utf-8")
        call = body.index('tartci_prepare_lane_cache_mounts "$CACHE_ROOT"')
        start = body.rindex("\n", 0, call) + 1
        return body[start:body.index("\n  fi\n", start) + len("\n  fi\n")]

    def boot(self, root: Path, host: str, after_prepare: str = "") -> tuple[int, list[str], str]:
        """Run the runner's own preamble lines inside a boot function."""
        script = (STUBS + 'source "$1"; shift\n'
                  'CACHE_ROOT="$1" HOST_CACHE_ROOT="$2" FETCHCONTENT_SOURCE_ROOT="$3"\n'
                  'boot() {\n  local i=1 logdir=log\n' + self.runner_block()
                  + '  echo booted >>"$calls"\n}\nboot\n')
        result = subprocess.run(
            ["/bin/bash", "-c", script, "_", str(self.calls), str(LIB), str(root), host,
             str(self.fetch)],
            text=True, capture_output=True, check=False,
            env={"PATH": "/usr/bin:/bin", "STUB_AFTER_PREPARE": after_prepare},
        )
        calls = self.calls.read_text().splitlines() if self.calls.exists() else []
        return result.returncode, calls, result.stderr

    def test_a_clean_lane_root_is_prepared_in_order_and_boots(self) -> None:
        root = self.base / "trusted"
        rc, calls, err = self.boot(root, str(self.host))
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls, [f"prepare {root}/ccache", f"guard {root}/ccache",
                                 f"prepare {self.fetch}", f"prepare {root}/configure-checks",
                                 "booted"])

    def test_a_symlinked_root_stops_the_boot_before_anything_is_prepared(self) -> None:
        root = self.base / "trusted"
        root.symlink_to(self.host)
        rc, calls, err = self.boot(root, str(self.host))
        self.assertEqual(rc, 1, err)
        self.assertEqual(calls[1:], ["discard", "release", "complete fail cache_setup_failed 1  log"])
        self.assertTrue(calls[0].startswith(f"note [1] lane cache root: {root} is a symlink"), calls)
        self.assertTrue(calls[0].endswith("; refusing to boot"), calls)

    def test_a_link_that_appears_during_preparation_is_refused_by_the_second_check(self) -> None:
        # configure-checks becomes a link to the host's after the first check.
        root = self.base / "trusted"
        swap = (f'[ "$1" = "{root}/configure-checks" ] && rmdir "$1" '
                f'&& ln -s "{self.host}/configure-checks" "$1"; true')
        rc, calls, err = self.boot(root, str(self.host), after_prepare=swap)
        self.assertEqual(rc, 1, err)
        self.assertIn(f"prepare {root}/configure-checks", calls)
        self.assertNotIn("booted", calls)
        self.assertTrue(any(c.startswith(f"note [1] lane cache root: {root}/configure-checks is a symlink")
                            for c in calls), calls)
        self.assertEqual(calls[-3:], ["discard", "release", "complete fail cache_setup_failed 1  log"])

    def test_a_failed_preparation_stops_the_boot_without_a_refusal_note(self) -> None:
        root = self.base / "trusted"
        rc, calls, err = self.boot(root, str(self.host), after_prepare="false")
        self.assertEqual(rc, 1, err)
        self.assertEqual(calls, [f"prepare {root}/ccache", "discard", "release",
                                 "complete fail cache_setup_failed 1  log"])

    def test_a_host_root_lane_is_prepared_without_checks(self) -> None:
        # No TARTCI_HOST_CI_CACHE: a link in the host-root lane is not judged.
        root = self.base / "linked"
        root.symlink_to(self.host)
        rc, calls, err = self.boot(root, "")
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls[-1], "booted")


if __name__ == "__main__":
    unittest.main()
