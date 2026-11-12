#!/usr/bin/env python3
"""Tests for scripts/ccache_guard.py.

Most tests drive the guard through a stub `ccache` that reads JSON fixture
entries, so they run on any CI host. RealCcacheTests reproduce the incident
with the real ccache and clang when both are installed: a zero-include
manifest serving another source's object, and the guard's quarantine curing it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import ccache_guard as guard  # noqa: E402

STUB = r'''#!/usr/bin/env python3
import json, os, sys
MAGIC = b"\xcc\xac"
def load(path):
    raw = open(path, "rb").read()
    if raw[:2] != MAGIC:
        sys.exit(1)
    body = json.loads(raw[4:])
    if body.get("broken"):
        sys.exit(1)
    return raw[3], body
cmd, path = sys.argv[1], sys.argv[2]
kind, body = load(path)
if cmd == "--inspect":
    if kind == 1:
        print("Entry type: 1 (manifest)")
        print("Creation time: %d" % body.get("created", 0))
        print("File paths (%d):" % len(body["paths"]))
        for i, p in enumerate(body["paths"]):
            print("  %d: %s" % (i, p))
        print("Results (%d):" % len(body["results"]))
        for i, k in enumerate(body["results"]):
            print("  %d:" % i)
            print("    Key: %s" % k)
    else:
        print("Entry type: 0 (result)")
elif cmd == "--extract-result":
    if kind != 0:
        sys.exit(1)
    open("ccache-result.o", "w").write("o")
    if "dep" in body:
        open("ccache-result.d", "w").write(body["dep"])
else:
    sys.exit(2)
'''


class Fixture:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.cache = tmp / "cache"
        self.cache.mkdir()
        (self.cache / "ccache.conf").write_text("max_size = 1G\n")
        self.qroot = tmp / "cache-quarantine"
        self.ccache = tmp / "ccache"
        self.ccache.write_text(STUB)
        self.ccache.chmod(0o755)

    def entry(self, key: str, kind: int, body: dict) -> Path:
        path = self.cache / key[0] / key[1] / key[2:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(guard.MAGIC + bytes([1, kind]) + json.dumps(body).encode())
        return path

    def manifest(self, key: str, paths: list[str], results: list[str]) -> Path:
        return self.entry(key, 1, {"paths": paths, "results": results, "created": 1790000000})

    def result(self, key: str, dep: str | None) -> Path:
        return self.entry(key, 0, {} if dep is None else {"dep": dep})

    def run(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = guard.main([argv[0], "--cache", str(self.cache), "--ccache", str(self.ccache),
                             "--json", *argv[1:]])
        return rc, json.loads(out.getvalue().strip().splitlines()[-1])


HEADER_DEP = "a.o: src/audio_doctor.cpp include/audio_doctor.hpp /usr/include/stdio.h\n"
SOURCE_ONLY_DEP = ("m.o: \\\n  /Library/Developer/SDKs/MacOSX.sdk/SDKSettings.json \\\n"
                   "  gen/control_shipping_marker.cpp\n")

POISON = "11" + "a" * 38        # zero includes, names an object with headers
LEGIT_ZERO = "22" + "b" * 38    # zero includes, names an include-less TU's object
HEALTHY = "33" + "c" * 38       # lists includes
DANGLING = "44" + "d" * 38      # zero includes, names a missing result
BROKEN = "55" + "e" * 38        # ccache cannot inspect it
R_HEADERS = "a1" + "0" * 38
R_SOURCE_ONLY = "b1" + "0" * 38
R_HEALTHY = "c1" + "0" * 38


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self._tmp.name))
        fx = self.fx
        fx.result(R_HEADERS, HEADER_DEP)
        fx.result(R_SOURCE_ONLY, SOURCE_ONLY_DEP)
        fx.result(R_HEALTHY, HEADER_DEP)
        self.poison = fx.manifest(POISON, [], [R_HEADERS])
        self.legit = fx.manifest(LEGIT_ZERO, [], [R_SOURCE_ONLY])
        self.healthy = fx.manifest(HEALTHY, ["/usr/include/stdio.h"], [R_HEALTHY])
        self.dangling = fx.manifest(DANGLING, [], ["f1" + "0" * 38])
        broken = fx.cache / BROKEN[0] / BROKEN[1] / BROKEN[2:]
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_bytes(guard.MAGIC + bytes([1, 1]) + b'{"broken": true}')
        self.broken = broken
        self.poison_bytes = self.poison.read_bytes()
        guard.BUSY_PROBE = lambda: None

    def tearDown(self) -> None:
        guard.BUSY_PROBE = guard.host_busy
        self._tmp.cleanup()

    def quarantined(self, report: dict) -> set[str]:
        batch = Path(report["quarantine_dir"])
        return {str(p.relative_to(batch)) for p in batch.rglob("*")
                if p.is_file() and p.name != "report.json"}

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.fx.cache))

    def test_dep_inputs_skip_implicit_sdk_settings(self) -> None:
        self.assertEqual(guard.dep_inputs(SOURCE_ONLY_DEP),
                         ["/Library/Developer/SDKs/MacOSX.sdk/SDKSettings.json",
                          "gen/control_shipping_marker.cpp"])

    def test_scan_classifies_and_changes_nothing(self) -> None:
        before = sorted(p for p in self.fx.cache.rglob("*") if p.is_file())
        rc, report = self.fx.run("scan")
        self.assertEqual(rc, guard.EXIT_OK)
        c = report["counts"]
        self.assertEqual(c["zero_include"], 3)
        self.assertEqual(c["zero_include_suspect"], 2)      # poison + dangling
        self.assertEqual(c["zero_include_consistent"], 1)   # the include-less TU
        self.assertEqual(c["uninspectable"], 1)
        self.assertEqual(c["quarantined"], 0)
        verdicts = {row["path"]: row["results"] for row in report["flagged"]}
        self.assertEqual(verdicts[self.rel(self.poison)], {R_HEADERS: "has_headers"})
        self.assertEqual(list(verdicts[self.rel(self.dangling)].values()), ["missing"])
        self.assertEqual(before, sorted(p for p in self.fx.cache.rglob("*") if p.is_file()))
        self.assertFalse(self.fx.qroot.exists())

    def test_all_zero_include_moves_every_zero_include_manifest_intact(self) -> None:
        rc, report = self.fx.run("quarantine", "--all-zero-include")
        self.assertEqual(rc, guard.EXIT_OK)
        moved = self.quarantined(report)
        self.assertEqual(moved, {self.rel(self.poison), self.rel(self.legit),
                                 self.rel(self.dangling)})
        self.assertFalse(self.poison.exists())
        self.assertEqual((Path(report["quarantine_dir"]) / self.rel(self.poison)).read_bytes(),
                         self.poison_bytes)
        # Healthy manifests, results and anything unreadable stay in the cache.
        for kept in (self.healthy, self.broken):
            self.assertTrue(kept.exists(), kept)
        for key in (R_HEADERS, R_SOURCE_ONLY, R_HEALTHY):
            self.assertIsNotNone(guard.result_file(self.fx.cache, key))
        self.assertTrue((self.fx.cache / "ccache.conf").exists())
        log = [json.loads(line) for line in (self.fx.qroot / "guard.log").read_text().splitlines()]
        self.assertEqual(log[-1]["counts"]["quarantined"], 3)
        self.assertEqual(log[-1]["counts"]["zero_include_suspect"], 2)
        batch_report = json.loads((Path(report["quarantine_dir"]) / "report.json").read_text())
        self.assertEqual(len(batch_report["flagged"]), 3)

    def test_default_quarantines_only_suspects_and_counts_both(self) -> None:
        rc, report = self.fx.run("quarantine")
        self.assertEqual(rc, guard.EXIT_OK)
        self.assertEqual(report["mode"], "suspect-only")
        self.assertEqual(self.quarantined(report),
                         {self.rel(self.poison), self.rel(self.dangling)})
        self.assertTrue(self.legit.exists())
        self.assertEqual(report["counts"]["zero_include_suspect"], 2)
        self.assertEqual(report["counts"]["zero_include_consistent"], 1)
        log = json.loads((self.fx.qroot / "guard.log").read_text().splitlines()[-1])
        self.assertEqual((log["counts"]["zero_include_suspect"],
                          log["counts"]["zero_include_consistent"]), (2, 1))

    def test_a_consistent_verdict_is_remembered_until_the_entry_changes(self) -> None:
        self.fx.run("quarantine")
        rc, report = self.fx.run("quarantine")
        self.assertEqual(rc, guard.EXIT_OK)
        self.assertEqual(report["counts"]["consistent_cached"], 1)
        self.assertEqual(report["counts"]["zero_include_consistent"], 1)
        # Only the healthy and the uninspectable manifests are opened again.
        self.assertEqual(report["counts"]["manifests_checked"], 2)
        # Rewritten in place (new mtime/size): checked again, and now suspect.
        self.legit.write_bytes(guard.MAGIC + bytes([1, 1]) + json.dumps(
            {"paths": [], "results": [R_HEADERS], "created": 1}).encode() + b" ")
        rc, report = self.fx.run("quarantine")
        self.assertEqual(report["counts"]["consistent_cached"], 0)
        self.assertFalse(self.legit.exists())

    def test_per_job_layers_are_never_scanned_but_the_shared_layer_is(self) -> None:
        layers = self.fx.cache / guard.LAYER_DIR
        placed = {}
        for layer in ("jobs/vm-1", "green/vm-2", "discard/vm-3", "shared"):
            root = layers / layer
            for key, body in ((POISON, {"paths": [], "results": [R_HEADERS]}),
                              (R_HEADERS, {"dep": HEADER_DEP})):
                path = root / key[0] / key[1] / key[2:]
                path.parent.mkdir(parents=True, exist_ok=True)
                kind = 1 if key == POISON else 0
                path.write_bytes(guard.MAGIC + bytes([1, kind]) + json.dumps(body).encode())
            placed[layer] = root / POISON[0] / POISON[1] / POISON[2:]
        rc, report = self.fx.run("quarantine")
        self.assertEqual(rc, guard.EXIT_OK)
        moved = self.quarantined(report)
        self.assertIn(str(placed["shared"].relative_to(self.fx.cache)), moved)
        self.assertFalse(placed["shared"].exists())
        for layer in ("jobs/vm-1", "green/vm-2", "discard/vm-3"):
            self.assertTrue(placed[layer].exists(), layer)
            self.assertFalse(any(layer in m for m in moved), layer)
        self.assertIn(self.rel(self.poison), moved)  # the legacy root too

    def test_second_run_finds_nothing(self) -> None:
        self.fx.run("quarantine", "--all-zero-include")
        rc, report = self.fx.run("quarantine", "--all-zero-include")
        self.assertEqual(rc, guard.EXIT_OK)
        self.assertEqual(report["counts"]["zero_include"], 0)
        self.assertNotIn("quarantine_dir", report)

    def test_lock_held_skips_without_moving(self) -> None:
        lock = guard.Lock(self.fx.qroot / ".guard.lock")
        self.assertTrue(lock.acquire())
        try:
            rc, report = self.fx.run("quarantine")
        finally:
            lock.release()
        self.assertEqual(rc, guard.EXIT_SKIPPED)
        self.assertEqual(report["status"], "skipped")
        self.assertTrue(self.poison.exists())

    def hold_lock_for(self, seconds: float) -> threading.Thread:
        lock = guard.Lock(self.fx.qroot / ".guard.lock")
        self.assertTrue(lock.acquire())
        thread = threading.Thread(target=lambda: (time.sleep(seconds), lock.release()))
        thread.start()
        self.addCleanup(thread.join)
        return thread

    def test_wait_takes_the_lock_once_the_other_guard_lets_go(self) -> None:
        # m5, 2026-09-27: an operator quarantine landed while a pre-boot guard
        # held the lock and skipped. With --wait it runs once the lock frees.
        self.hold_lock_for(1.0)
        rc, report = self.fx.run("quarantine", "--wait=10")
        self.assertEqual(rc, guard.EXIT_OK, report)
        self.assertGreaterEqual(report["lock_waited_s"], 0.5)
        self.assertFalse(self.poison.exists())

    def test_wait_is_bounded_and_still_skips_a_lock_that_is_never_released(self) -> None:
        self.hold_lock_for(3.0)
        started = time.monotonic()
        rc, report = self.fx.run("quarantine", "--wait=0.5")
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual((rc, report["status"]), (guard.EXIT_SKIPPED, "skipped"))
        self.assertIn("waited 0.5s", report["detail"])
        self.assertTrue(self.poison.exists())

    def test_wait_rejects_out_of_range_values(self) -> None:
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            guard.main(["quarantine", "--cache", str(self.fx.cache), "--wait=-1"])
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            guard.main(["quarantine", "--cache", str(self.fx.cache), "--wait=99999"])

    def test_operator_cli_waits_by_default_and_the_runner_does_not(self) -> None:
        body = (ROOT / "tartci").read_text()
        section = body[body.index("cmd_ccache()"):body.index("cmd_windows()")]
        self.assertIn('--wait=${TARTCI_CCACHE_WAIT_SECS:-120}', section)
        runner = (ROOT / "providers" / "tart-macos" / "runner.sh").read_text()
        start = runner.index('ccache_guard.py" quarantine')
        call = runner[start:runner.index("--json", start)]
        self.assertIn("--budget", call)  # control: this IS the runner's call
        self.assertNotIn("--wait", call)

    def test_missing_ccache_skips(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = guard.main(["quarantine", "--cache", str(self.fx.cache),
                             "--ccache", str(self.fx.tmp / "absent"), "--json"])
        self.assertEqual(rc, guard.EXIT_SKIPPED)
        self.assertTrue(self.poison.exists())

    def test_budget_exhausted_is_partial_and_reported(self) -> None:
        rc, report = self.fx.run("quarantine", "--budget", "0.000001")
        self.assertEqual(rc, guard.EXIT_BUDGET)
        self.assertEqual(report["status"], "budget_exhausted")

    def fake_clock(self) -> None:
        """Advance the budget clock by one per check: a budget of N admits N entries."""
        ticks = iter(range(10**6))
        guard.CLOCK = lambda: float(next(ticks))
        self.addCleanup(setattr, guard, "CLOCK", time.monotonic)

    def tail_fixture(self) -> Path:
        """Filler entries in every fan-out ahead of a poisoned manifest in `f`."""
        for top in "0123456789abcde":
            for n in range(3):
                path = self.fx.cache / top / "0" / f"filler{n}"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"not a manifest")
        return self.fx.manifest("f3" + "9" * 38, [], [R_HEADERS])

    def test_exhausted_run_then_resume_covers_the_tail(self) -> None:
        late = self.tail_fixture()
        self.fake_clock()
        rc, first = self.fx.run("quarantine", "--budget", "30")
        self.assertEqual(rc, guard.EXIT_BUDGET)
        self.assertTrue(late.exists(), "the first run must stop before the tail")
        self.assertIsNotNone(first["cursor"])
        self.assertNotIn("resumed_from", first)
        rc, second = self.fx.run("quarantine", "--budget", "30")
        self.assertFalse(late.exists(), "the resumed run must reach the tail")
        self.assertIn(self.rel(late), self.quarantined(second))
        self.assertEqual(second.get("resumed_from"), first["cursor"])

    def test_without_the_cursor_every_exhausted_run_stops_at_the_head(self) -> None:
        # Control for the test above: the same budget, cursor removed between
        # runs, never reaches the tail.
        late = self.tail_fixture()
        self.fake_clock()
        for _ in range(3):
            rc, _report = self.fx.run("quarantine", "--budget", "30")
            self.assertEqual(rc, guard.EXIT_BUDGET)
            (self.fx.qroot / guard.CURSOR_FILE).unlink()
        self.assertTrue(late.exists())

    def test_resumed_run_wraps_to_the_head_and_a_complete_run_clears_the_cursor(self) -> None:
        late = self.tail_fixture()
        self.fake_clock()
        self.fx.run("quarantine", "--budget", "30")
        total = sum(1 for _ in guard.iter_all_entries(self.fx.cache))
        rc, report = self.fx.run("quarantine", "--budget", "100000")
        self.assertEqual(rc, guard.EXIT_OK)
        self.assertIsNone(report["cursor"])
        self.assertFalse((self.fx.qroot / guard.CURSOR_FILE).exists())
        self.assertFalse(late.exists())
        # The wrapped part reached every unit, so the run counted every entry.
        self.assertEqual(report["counts"]["entries"], total)
        self.assertFalse(self.poison.exists())

    def test_cursor_for_a_vanished_unit_starts_at_the_next_one(self) -> None:
        self.tail_fixture()
        guard.save_cursor(self.fx.qroot, [".", "e", "zz"])
        units = [key for key, _root, _entry in guard.iter_from(self.fx.cache, [".", "e", "zz"])]
        self.assertEqual(units[0][:2], [".", "f"])
        # Every entry is still visited exactly once.
        self.assertEqual(len(units), sum(1 for _ in guard.iter_all_entries(self.fx.cache)))

    def test_unreadable_or_unknown_cursor_starts_at_the_beginning(self) -> None:
        self.fx.qroot.mkdir(parents=True)
        (self.fx.qroot / guard.CURSOR_FILE).write_text("{not json")
        self.assertIsNone(guard.load_cursor(self.fx.qroot))
        plain = [str(e) for _r, e in guard.iter_all_entries(self.fx.cache)]
        unknown = [str(e) for _k, _r, e in guard.iter_from(self.fx.cache, ["elsewhere", "1", ""])]
        self.assertEqual(plain, unknown)

    def test_scan_neither_reads_nor_moves_the_cursor(self) -> None:
        self.tail_fixture()
        guard.save_cursor(self.fx.qroot, [".", "f", "3"])
        before = (self.fx.qroot / guard.CURSOR_FILE).read_text()
        rc, report = self.fx.run("scan")
        self.assertEqual(rc, guard.EXIT_OK)
        self.assertNotIn("resumed_from", report)
        self.assertEqual((self.fx.qroot / guard.CURSOR_FILE).read_text(), before)

    def test_reset_refuses_while_busy(self) -> None:
        guard.BUSY_PROBE = lambda: "a Tart VM is running on this host"
        rc, report = self.fx.run("reset", "--reset")
        self.assertEqual(rc, guard.EXIT_BUSY)
        self.assertEqual(report["status"], "refused_busy")
        self.assertTrue(self.poison.exists())
        self.assertTrue(self.healthy.exists())
        self.assertFalse(self.fx.qroot.exists())

    def test_reset_force_overrides_busy(self) -> None:
        guard.BUSY_PROBE = lambda: "a Tart VM is running on this host"
        rc, report = self.fx.run("reset", "--force")
        self.assertEqual(rc, guard.EXIT_OK)
        self.assertEqual(report["forced_over"], "a Tart VM is running on this host")
        self.assertFalse(self.poison.exists())
        self.assertTrue(self.healthy.exists())

    def test_reset_plan_changes_nothing(self) -> None:
        rc, report = self.fx.run("reset", "--reset", "--plan")
        self.assertEqual(rc, guard.EXIT_OK)
        self.assertEqual(report["status"], "planned")
        self.assertEqual(report["before"], report["after"])
        self.assertEqual(report["reset_moved"], report["before"])
        self.assertTrue(self.poison.exists())
        self.assertFalse(self.fx.qroot.exists())

    def test_reset_empties_the_cache_into_quarantine(self) -> None:
        before = len(list(guard.iter_entries(self.fx.cache)))
        rc, report = self.fx.run("reset", "--reset")
        self.assertEqual(rc, guard.EXIT_OK)
        self.assertEqual(report["before"], before)
        self.assertEqual(report["after"], 0)
        batch = Path(report["quarantine_dir"])
        self.assertTrue((batch / "reset" / self.rel(self.healthy)).exists())
        self.assertTrue((batch / self.rel(self.poison)).exists())
        self.assertTrue((self.fx.cache / "ccache.conf").exists())
        log = json.loads((self.fx.qroot / "guard.log").read_text().splitlines()[-1])
        self.assertEqual((log["before"], log["after"]), (before, 0))

    def test_prune_removes_only_old_batches(self) -> None:
        self.fx.qroot.mkdir()
        old = self.fx.qroot / "20200101T000000Z"
        new = self.fx.qroot / guard.utc_stamp()
        other = self.fx.qroot / "keep-me"
        for d in (old, new, other):
            d.mkdir()
        removed = guard.prune_batches(self.fx.qroot, 30)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists() and other.exists())

    def test_host_busy_refuses_when_probe_cannot_prove_idle(self) -> None:
        import tartci_launchd_watchdog as watchdog
        original = watchdog.probe_tart_vm_running
        watchdog.probe_tart_vm_running = lambda: watchdog.TartVMProbe(None, "no tart", None, None)
        try:
            self.assertIn("cannot prove the host idle", guard.host_busy())
        finally:
            watchdog.probe_tart_vm_running = original

    def test_host_busy_counts_vm_leases(self) -> None:
        import lane_busy
        import tartci_launchd_watchdog as watchdog
        originals = (watchdog.probe_tart_vm_running, lane_busy.lease_records)
        watchdog.probe_tart_vm_running = lambda: watchdog.TartVMProbe(False, "idle", "tart", "/x")
        lane_busy.lease_records = lambda: [{"command_kind": "tart-macos-vm", "pid": 1},
                                           {"command_kind": "build", "pid": 2}]
        try:
            self.assertEqual(guard.host_busy(), "1 VM lease(s) held on this host")
            lane_busy.lease_records = lambda: [{"command_kind": "build", "pid": 2}]
            self.assertIsNone(guard.host_busy())
        finally:
            watchdog.probe_tart_vm_running, lane_busy.lease_records = originals


class RunnerWiringTests(unittest.TestCase):
    def test_macos_runner_guards_the_cache_before_the_vm_boots(self) -> None:
        body = (ROOT / "providers/tart-macos/runner.sh").read_text()
        lib = (ROOT / "providers/tart-macos/lane-cache-root.lib.sh").read_text()
        # The guard runs inside the mount preparation, which runs before boot
        # (its order is executed in test_macos_lane_cache_root).
        self.assertIn('tartci_ccache_guard "$root/ccache"', lib)
        prepare = body.index('tartci_prepare_lane_cache_mounts "$CACHE_ROOT"')
        boot = body.index('tartci_vm_lease_guard_exec tart run')
        self.assertLess(prepare, boot)
        self.assertIn('scripts/ccache_guard.py" quarantine', body)

    def test_runner_hook_is_fail_open(self) -> None:
        body = (ROOT / "providers/tart-macos/runner.sh").read_text()
        start = body.index("tartci_ccache_guard(){")
        fn = body[start:body.index("\n}\n", start)]
        self.assertIn("|| rc=$?", fn)
        self.assertTrue(fn.rstrip().endswith("return 0"))

    def test_tartci_dispatches_ccache(self) -> None:
        proc = subprocess.run([str(ROOT / "tartci"), "ccache", "nope"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("tartci ccache <scan|quarantine|reset>", proc.stdout)


def _real_tools() -> tuple[str | None, str | None]:
    return guard.resolve_ccache(), shutil.which("clang") or shutil.which("cc")


@unittest.skipUnless(all(_real_tools()), "real ccache and a C compiler are not installed")
class RealCcacheTests(unittest.TestCase):
    """The incident, end to end, with the real ccache."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cache = self.tmp / "cache"
        self.ccache, self.cc = _real_tools()
        (self.tmp / "marker.c").write_text("int marker(void) { return 7; }\n")
        (self.tmp / "wav_bridge.c").write_text(
            "#include <stdio.h>\nint wav_bridge(void) { return printf(\"x\"); }\n")
        self.env = dict(os.environ, CCACHE_DIR=str(self.cache), CCACHE_NODEPEND="true",
                        CCACHE_DIRECT="true", CCACHE_TEMPDIR=str(self.tmp / "cctmp"))
        for name in ("CCACHE_DEPEND", "CCACHE_NODIRECT", "CCACHE_DISABLE", "CCACHE_RECACHE"):
            self.env.pop(name, None)
        for src in ("marker", "wav_bridge"):
            self.compile(src)
        guard.BUSY_PROBE = lambda: None

    def tearDown(self) -> None:
        guard.BUSY_PROBE = guard.host_busy
        self._tmp.cleanup()

    def compile(self, src: str) -> str:
        obj = self.tmp / f"{src}.o"
        obj.unlink(missing_ok=True)
        subprocess.run([self.ccache, self.cc, "-MD", "-MF", str(obj) + ".d", "-c",
                        str(self.tmp / f"{src}.c"), "-o", str(obj)],
                       env=self.env, check=True, cwd=self.tmp)
        return subprocess.run(["nm", str(obj)], capture_output=True, text=True).stdout

    def manifests(self) -> dict[int, Path]:
        """File-path count -> manifest path."""
        found = {}
        for entry in guard.iter_entries(self.cache):
            if guard.is_manifest_header(entry):
                info = guard.inspect_manifest(self.ccache, entry)
                if info is not None:
                    found[info["file_paths"] > 0] = entry
        return found

    def test_zero_include_manifest_serves_a_foreign_object_until_quarantined(self) -> None:
        found = self.manifests()
        zero, real = found[False], found[True]
        # Poison: wav_bridge's manifest replaced by the include-less marker's.
        shutil.copyfile(zero, real)
        self.assertIn("marker", self.compile("wav_bridge"))  # the incident

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = guard.main(["quarantine", "--cache", str(self.cache), "--all-zero-include",
                             "--json"])
        report = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(rc, guard.EXIT_OK)
        self.assertEqual(report["counts"]["quarantined"], 2)

        symbols = self.compile("wav_bridge")
        self.assertIn("wav_bridge", symbols)
        self.assertNotIn("marker", symbols)

    def test_default_cannot_see_a_foreign_include_less_object(self) -> None:
        found = self.manifests()
        shutil.copyfile(found[False], found[True])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            guard.main(["scan", "--cache", str(self.cache), "--json"])
        report = json.loads(out.getvalue().strip().splitlines()[-1])
        # Both manifests look consistent, so the default flags nothing: the
        # documented blind spot that --all-zero-include (and reset) close.
        self.assertEqual(report["counts"]["zero_include_consistent"], 2)
        self.assertEqual(report["counts"]["flagged"], 0)

    def test_legitimate_include_less_manifest_is_classified_consistent(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            guard.main(["scan", "--cache", str(self.cache), "--json"])
        report = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(report["counts"]["zero_include_consistent"], 1)
        self.assertEqual(report["counts"]["zero_include_suspect"], 0)


if __name__ == "__main__":
    unittest.main()
