#!/usr/bin/env python3
"""Per-job ccache write layers: verdicts, promotion rules, sweep, config."""

from __future__ import annotations

import testing_support  # noqa: E402
testing_support.skip_module_without_tomllib()
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import tomllib
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ccache_layer as cl  # noqa: E402
import macos_fleet_lanes as fleet  # noqa: E402

LIB = ROOT / "providers" / "tart-macos" / "ccache-layer.lib.sh"
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
PROMOTER = ROOT / "scripts" / "ccache_layer.py"
TRUSTED_RECEIPT = ('{"kind":"terminal","conclusion":"success","event":"push",'
                   '"head_branch":"main","run_id":1,"job_id":2}')
SUCCESS_LOG = "2026-09-27 01:00:00Z: Job macos completed with result: Succeeded\n"


def entry(kind: int, payload: bytes = b"payload") -> bytes:
    return b"\xcc\xac\x01" + bytes([kind]) + b"\x00" * 16 + payload


def write_entry(remote: Path, key: str, data: bytes, mtime: float | None = None) -> Path:
    path = remote / key[:2] / key[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def never_present(_vm: str) -> bool:
    return False


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ccache-layer-"))
        self.layout = cl.Layout(self.tmp / "ccache" / "tartci-layers-v1", self.tmp / "state")
        self.log = self.tmp / "runner.log"
        self.log.write_text(SUCCESS_LOG)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def attach(self, vm: str, owner: int | None = None) -> Path:
        return cl.attach(self.layout, vm, os.getpid() if owner is None else owner)

    def settle(self, vm: str, rc: int = 0, **kwargs: object) -> tuple[str, str, Path | None]:
        return cl.settle(self.layout, vm, rc, str(kwargs.get("capture", "terminal")),
                         str(kwargs.get("receipt", TRUSTED_RECEIPT)),
                         str(kwargs.get("quarantine", "none")),
                         kwargs.get("log", self.log))  # type: ignore[arg-type]

    def shared_keys(self) -> set[str]:
        return {f"{p.parent.name}{p.name}" for p in self.layout.shared.glob("*/*")
                if not p.name.startswith(".")}

    def audit_events(self) -> list[dict]:
        if not self.layout.audit.exists():
            return []
        return [json.loads(line) for line in self.layout.audit.read_text().splitlines()]


class VerdictTests(unittest.TestCase):
    def test_green_needs_a_trusted_host_receipt(self) -> None:
        for local in ("Succeeded", "SucceededWithIssues", None):
            self.assertEqual(cl.verdict(0, "terminal", TRUSTED_RECEIPT, "none", local,
                                        "1", "2"), ("green", "receipt=push:main"))
        merge_group = '{"conclusion":"success","event":"merge_group","head_branch":"gh-readonly-queue/main/x"}'
        self.assertEqual(cl.verdict(0, "terminal", merge_group, "none", None)[0], "green")

    def test_a_forged_runner_log_line_is_not_proof(self) -> None:
        # The guest writes the runner log, so "Succeeded" there proves nothing.
        for capture in ("", "active", "setup_error", "terminal_unknown"):
            with self.subTest(capture):
                self.assertEqual(cl.verdict(0, capture, "", "none", "Succeeded")[0], "red")

    def test_pull_request_and_unbound_receipts_are_red(self) -> None:
        pr = '{"conclusion":"success","event":"pull_request","head_branch":"main"}'
        fork_main = '{"conclusion":"success","event":"pull_request_target","head_branch":"main"}'
        for receipt in (pr, fork_main, '{"conclusion":"success","event":"push","head_branch":"feat/x"}'):
            self.assertIn("untrusted_ref", cl.verdict(0, "terminal", receipt, "none", None)[1])
        self.assertEqual(cl.verdict(0, "terminal", TRUSTED_RECEIPT, "none", None, "9", "2"),
                         ("red", "receipt_unbound"))
        self.assertEqual(cl.verdict(0, "terminal", TRUSTED_RECEIPT, "none", None, "1", "9"),
                         ("red", "receipt_unbound"))

    def test_everything_short_of_proof_is_red(self) -> None:
        t = TRUSTED_RECEIPT
        cases = {
            "timeout": (124, "terminal", t, "none", "Succeeded"),
            "retarget": (125, "terminal", t, "none", None),
            "killed": (143, "terminal", t, "none", None),
            "failed": (0, "terminal", t, "none", "Failed"),
            "cancelled": (0, "terminal", t, "none", "Canceled"),
            "no_receipt": (0, "", "", "none", None),
            "quarantine": (0, "active", "", "listener_exited_workflow_active", "Succeeded"),
            "api_failure": (0, "terminal", '{"conclusion":"failure"}', "none", "Succeeded"),
            "api_cancel": (0, "terminal", '{"conclusion":"cancelled"}', "none", "Succeeded"),
            "bad_receipt": (0, "terminal", "{not json", "none", "Succeeded"),
        }
        for name, args in cases.items():
            with self.subTest(name):
                self.assertEqual(cl.verdict(*args)[0], "red")

    def test_runner_log_result_takes_the_last_completion_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log"
            log.write_text("x: Job a completed with result: Failed\n" + SUCCESS_LOG)
            self.assertEqual(cl.runner_log_result(log), "Succeeded")
            self.assertIsNone(cl.runner_log_result(Path(td) / "missing"))


class PromotionTests(Base):
    def test_green_promotes_into_shared_and_removes_the_layer(self) -> None:
        layer = self.attach("vm-a")
        write_entry(layer / "remote", "ab" + "1" * 38, entry(0))
        write_entry(layer / "remote", "cd" + "2" * 38, entry(1))
        decision, _, target = self.settle("vm-a")
        self.assertEqual(decision, "green")
        self.assertEqual(target, self.layout.green / "vm-a")
        self.assertEqual(cl.process(self.layout, "vm-a", never_present, poll=0.01), "promoted")
        self.assertEqual(self.shared_keys(), {"ab" + "1" * 38, "cd" + "2" * 38})
        self.assertFalse((self.layout.green / "vm-a").exists())
        promote = [e for e in self.audit_events() if e["event"] == "promote"]
        self.assertEqual(promote[0]["promoted"], 2)
        self.assertEqual(len(list(self.layout.promotions.glob("*-vm-a.keys"))), 1)

    def test_red_cancel_and_timeout_discard_without_touching_shared(self) -> None:
        for vm, rc, log_text in (("vm-red", 0, "x: Job m completed with result: Failed\n"),
                                 ("vm-cancel", 0, "x: Job m completed with result: Canceled\n"),
                                 ("vm-timeout", 124, SUCCESS_LOG)):
            with self.subTest(vm):
                layer = self.attach(vm)
                write_entry(layer / "remote", "ef" + "3" * 38, entry(0))
                log = self.tmp / f"{vm}.log"
                log.write_text(log_text)
                decision, _, target = self.settle(vm, rc, log=log)
                self.assertEqual(decision, "red")
                self.assertEqual(target.parent, self.layout.discard)
                self.assertEqual(cl.process(self.layout, target.name, never_present), "discarded")
                self.assertFalse(target.exists())
        self.assertEqual(self.shared_keys(), set())
        self.assertFalse([e for e in self.audit_events() if e["event"] == "promote"])

    def test_existing_result_is_never_overwritten(self) -> None:
        key = "ab" + "4" * 38
        original = write_entry(self.layout.shared, key, entry(0, b"original"), mtime=1.0)
        layer = self.attach("vm-b")
        write_entry(layer / "remote", key, entry(0, b"newer-but-a-result"))
        self.settle("vm-b")
        cl.process(self.layout, "vm-b", never_present)
        self.assertEqual(original.read_bytes(), entry(0, b"original"))

    def test_manifest_replaced_only_when_strictly_newer(self) -> None:
        older_key, newer_key = "aa" + "5" * 38, "bb" + "6" * 38
        write_entry(self.layout.shared, older_key, entry(1, b"shared-old"), mtime=1000.0)
        write_entry(self.layout.shared, newer_key, entry(1, b"shared-new"), mtime=5000.0)
        layer = self.attach("vm-c")
        write_entry(layer / "remote", older_key, entry(1, b"job"), mtime=3000.0)
        write_entry(layer / "remote", newer_key, entry(1, b"job"), mtime=3000.0)
        self.settle("vm-c")
        cl.process(self.layout, "vm-c", never_present)
        self.assertEqual((self.layout.shared / "aa" / older_key[2:]).read_bytes(), entry(1, b"job"))
        self.assertEqual((self.layout.shared / "bb" / newer_key[2:]).read_bytes(),
                         entry(1, b"shared-new"))

    def test_malformed_symlinked_and_non_entry_files_are_rejected(self) -> None:
        outside = self.tmp / "outside-secret"
        outside.write_bytes(entry(0))
        layer = self.attach("vm-d")
        remote = layer / "remote"
        write_entry(remote, "ab" + "7" * 38, b"not a ccache entry at all")
        write_entry(remote, "ab" + "8" * 38 + ".tmp", entry(0))
        (remote / "zz").mkdir()
        (remote / "zz" / ("9" * 38)).symlink_to(outside)
        (remote / "yy").symlink_to(self.tmp)
        write_entry(remote, "QZ" + "1" * 38, entry(0))  # upper case is not a ccache key
        self.settle("vm-d")
        cl.process(self.layout, "vm-d", never_present)
        self.assertEqual(self.shared_keys(), set())
        promote = [e for e in self.audit_events() if e["event"] == "promote"][0]
        self.assertEqual(promote["rejected"], 5)

    def test_concurrent_promotions_are_safe(self) -> None:
        keys = [f"{i:02x}" + "c" * 38 for i in range(40)]
        names = [f"vm-par-{n}" for n in range(8)]
        for n, vm in enumerate(names):
            layer = self.attach(vm)
            for key in keys:
                write_entry(layer / "remote", key, entry(0, f"from-{n}".encode()))
            write_entry(layer / "remote", "ff" + "d" * 38, entry(1, f"m{n}".encode()),
                        mtime=1000.0 + n)
            self.settle(vm)
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            outcomes = list(pool.map(
                lambda vm: cl.process(self.layout, vm, never_present), names))
        self.assertEqual(outcomes, ["promoted"] * len(names))
        self.assertEqual(self.shared_keys(), set(keys) | {"ff" + "d" * 38})
        for key in keys:
            self.assertRegex((self.layout.shared / key[:2] / key[2:]).read_bytes()[20:].decode(),
                             r"^from-[0-7]$")
        self.assertEqual((self.layout.shared / "ff" / ("d" * 38)).read_bytes(), entry(1, b"m7"))
        self.assertFalse(list(self.layout.shared.glob(f"*/{cl.TMP_PREFIX}*")))
        promoted = sum(e["promoted"] for e in self.audit_events() if e["event"] == "promote")
        self.assertEqual(promoted, len(keys) + 1)

    def test_concurrent_cli_processes_on_one_layer_run_it_once(self) -> None:
        layer = self.attach("vm-cli")
        write_entry(layer / "remote", "ab" + "e" * 38, entry(0))
        self.settle("vm-cli")
        fake_tart = self.tmp / "tart"
        fake_tart.write_text("#!/bin/sh\nsleep 0.3; printf '[]'\n")
        fake_tart.chmod(0o755)
        cmd = [sys.executable, str(PROMOTER), "--root", str(self.layout.root),
               "--state", str(self.layout.state), "process", "--layer", "vm-cli",
               "--tart", str(fake_tart), "--poll", "0.05"]
        procs = [subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True) for _ in range(4)]
        results = sorted(p.communicate(timeout=30)[0].strip() for p in procs)
        self.assertEqual(results.count("promoted"), 1, results)
        self.assertEqual(self.shared_keys(), {"ab" + "e" * 38})

    def test_green_layer_is_discarded_when_the_vm_never_goes_away(self) -> None:
        layer = self.attach("vm-stuck")
        write_entry(layer / "remote", "ab" + "f" * 38, entry(0))
        self.settle("vm-stuck")
        outcome = cl.process(self.layout, "vm-stuck", lambda _vm: True,
                             vm_gone_timeout=0.05, poll=0.01)
        self.assertEqual(outcome, "discarded")
        self.assertEqual(self.shared_keys(), set())

    def test_unreadable_inventory_is_never_taken_as_absence(self) -> None:
        def broken(_vm: str) -> bool:
            raise ValueError("tart list failed")

        self.assertFalse(cl.wait_vm_gone("vm", broken, timeout=0.05, poll=0.01))


class KilledJobRegressionTests(Base):
    """A zero-include manifest from a job torn down mid-build never reaches shared.

    The 2026-09-26 m3 incident: VMs killed mid-build during a Tart hang left
    direct-mode manifests with no include paths in the shared cache, and every
    later build linked the wrong objects.
    """

    ZERO_INCLUDE_MANIFEST = b"\xcc\xac\x01\x01\x00\x00\x00\x00\x00\x00\x00\x6a\xb8\x6f\x72"

    def plant(self, vm: str) -> Path:
        layer = self.attach(vm, owner=os.getpid())
        write_entry(layer / "remote", "6c" + "0" * 38, self.ZERO_INCLUDE_MANIFEST)
        return layer

    def test_killed_timed_out_and_signalled_jobs_never_promote(self) -> None:
        scenarios = {
            "vm-timeout": dict(rc=124, log=self.log),
            "vm-signal": dict(rc=0, quarantine="signal_teardown_unknown", log=self.log),
            "vm-no-receipt": dict(rc=0, capture=""),
            "vm-forged-line": dict(rc=0, capture="setup_error"),
            "vm-pr-job": dict(rc=0, receipt=TRUSTED_RECEIPT.replace('"push"', '"pull_request"')),
        }
        for vm, kwargs in scenarios.items():
            with self.subTest(vm):
                self.plant(vm)
                rc = kwargs.pop("rc")
                self.settle(vm, rc, **kwargs)
        cl.sweep(self.layout, never_present)
        self.assertEqual(self.shared_keys(), set())

    def test_orphaned_layer_of_a_dead_supervisor_is_discarded_not_promoted(self) -> None:
        dead = subprocess.Popen(["true"])
        dead.wait()
        layer = self.plant("vm-orphan")
        (self.layout.owners / "vm-orphan").write_text(f"{dead.pid}\n")
        live = self.plant("vm-live")
        counts = cl.sweep(self.layout, never_present)
        self.assertEqual(counts["orphaned"], 1)
        self.assertFalse(layer.exists())
        self.assertTrue(live.exists(), "a live owner's layer must survive a sweep")
        self.assertEqual(self.shared_keys(), set())


class SweepAndTrimTests(Base):
    def test_sweep_resumes_an_interrupted_green_promotion(self) -> None:
        layer = self.attach("vm-resume")
        write_entry(layer / "remote", "ab" + "a" * 38, entry(0))
        self.settle("vm-resume")
        counts = cl.sweep(self.layout, never_present)
        self.assertEqual(counts["resumed"], 1)
        self.assertEqual(self.shared_keys(), {"ab" + "a" * 38})

    def test_a_remote_dir_swapped_for_a_link_promotes_nothing(self) -> None:
        # The guest owns jobs/<vm> read-write and can replace remote/ with a
        # link to any host directory full of entry-shaped files.
        outside = self.tmp / "host-dir"
        write_entry(outside, "ab" + "e" * 38, entry(0))
        layer = self.attach("vm-link")
        shutil.rmtree(layer / "remote")
        (layer / "remote").symlink_to(outside)
        self.settle("vm-link")
        self.assertEqual(cl.process(self.layout, "vm-link", never_present), "promoted")
        self.assertEqual(self.shared_keys(), set())
        promote = [e for e in self.audit_events() if e["event"] == "promote"]
        self.assertEqual(promote[-1]["rejected"], 1)
        self.assertTrue((outside / "ab" / ("e" * 38)).exists(), "the link target is untouched")

    def test_a_symlinked_store_directory_is_refused(self) -> None:
        for name in ("shared", "jobs", "green", "discard"):
            with self.subTest(dir=name):
                tmp = Path(tempfile.mkdtemp(prefix="ccache-layer-link-"))
                self.addCleanup(shutil.rmtree, tmp, True)
                layout = cl.Layout(tmp / "layers", tmp / "state")
                (tmp / "target").mkdir()
                (tmp / "layers").mkdir()
                (tmp / "layers" / name).symlink_to(tmp / "target")
                with self.assertRaises(ValueError):
                    cl.attach(layout, "vm-x", os.getpid())
                self.assertEqual(list((tmp / "target").iterdir()), [])

    def test_trim_evicts_oldest_until_under_the_cap_and_reaps_stale_tmp(self) -> None:
        for i in range(10):
            write_entry(self.layout.shared, f"{i:02x}" + "b" * 38, entry(0, b"x" * 1000),
                        mtime=1000.0 + i)
        stale = self.layout.shared / "00" / f"{cl.TMP_PREFIX}junk"
        stale.write_bytes(b"x")
        os.utime(stale, (1.0, 1.0))
        result = cl.trim(self.layout, max_size=5000, interval=0, now=10_000.0)
        self.assertEqual(result["removed_tmp"], 1)
        remaining = sorted(self.shared_keys())
        self.assertLessEqual(result["bytes_after"], 4500)
        self.assertEqual(remaining[0][:2], f"{10 - len(remaining):02x}")

    def test_parse_size(self) -> None:
        self.assertEqual(cl.parse_size("40G"), 40 * 1024 ** 3)
        self.assertEqual(cl.parse_size("5M"), 5 * 1024 ** 2)


@unittest.skipUnless(shutil.which("ccache") and shutil.which("cc"), "needs a real ccache and cc")
class RealCcacheTests(Base):
    """The guest configuration, run against a real ccache, end to end."""

    def compile(self, vm: str, source: Path) -> dict[str, int]:
        layer = self.layout.jobs / vm
        env = dict(os.environ,
                   CCACHE_DIR=str(layer / "local"), CCACHE_REMOTE_ONLY="true",
                   CCACHE_REMOTE_STORAGE=f"file:{self.layout.shared}|read-only "
                                         f"file:{layer / 'remote'}",
                   CCACHE_NODEPEND="true", CCACHE_COMPILERCHECK="content")
        subprocess.run(["ccache", "cc", "-c", str(source), "-o", str(self.tmp / "out.o")],
                       env=env, check=True, cwd=self.tmp)
        return cl.layer_stats(layer, shutil.which("ccache")) or {}

    def test_promoted_entries_serve_the_next_job_and_shared_stays_read_only(self) -> None:
        (self.tmp / "h.h").write_text("#define X 3\n")
        source = self.tmp / "a.c"
        source.write_text('#include "h.h"\nint f(void){return X;}\n')
        self.layout.ensure()
        before = set(self.layout.shared.rglob("*"))
        self.attach("vm-cold")
        cold = self.compile("vm-cold", source)
        self.assertEqual(set(self.layout.shared.rglob("*")), before,
                         "a guest compile must never write the shared store")
        self.assertEqual(cold.get("cache_miss"), 1)
        self.settle("vm-cold")
        self.assertEqual(cl.process(self.layout, "vm-cold", never_present), "promoted")
        self.assertEqual(len(self.shared_keys()), 2)
        self.attach("vm-warm")
        warm = self.compile("vm-warm", source)
        self.assertEqual(warm.get("direct_cache_hit"), 1, warm)
        self.assertEqual(list((self.layout.jobs / "vm-warm" / "remote").glob("*/*")), [],
                         "a remote hit must not be re-written into the job layer")


class ShellLibTests(unittest.TestCase):
    def fragments(self, value: str) -> subprocess.CompletedProcess[str]:
        script = textwrap.dedent(f"""
            set -euo pipefail
            TARTCI_ROOT={ROOT}; CACHE_ROOT=/tmp/cache-root
            TARTCI_CCACHE_WRITE_ISOLATION='{value}'
            source {LIB}
            tartci_ccache_layer_guest_fragments vm-x
            printf 'PREP<%s>\\nENV<%s>\\nROOT<%s>\\n' "$CCACHE_LAYER_GUEST_PREP" "$CCACHE_LAYER_GUEST_ENV" "$CCACHE_LAYER_ROOT"
        """)
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    def test_unset_and_zero_produce_no_guest_change(self) -> None:
        for value in ("", "0"):
            with self.subTest(value=value):
                result = self.fragments(value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("PREP<>\nENV<>", result.stdout)

    def test_enabled_points_the_guest_at_its_own_layer_and_a_read_only_share(self) -> None:
        result = self.fragments("1")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        # A sibling of the legacy ccache directory, never inside it: a legacy
        # read-write `ccache` mount must not reach the shared store.
        self.assertIn("ROOT</tmp/cache-root/ccache-layers-v1>", out)
        self.assertNotIn("/tmp/cache-root/ccache/", out)
        self.assertIn("'/Volumes/My Shared Files/ccache-shared' ~/.tartci-ccache/shared", out)
        # The job layer is guest-local: no host share is linked for it.
        self.assertIn("mkdir -p ~/.tartci-ccache/job/local ~/.tartci-ccache/job/remote", out)
        self.assertEqual(out.count("/Volumes/My Shared Files/"), 1, out)
        self.assertIn('file:$HOME/.tartci-ccache/shared|read-only file:$HOME/.tartci-ccache/job/remote', out)
        self.assertIn("CCACHE_REMOTE_ONLY=true", out)
        self.assertIn("CCACHE_REMOTE_STORAGE=file:%s|read-only file:%s", out)

    def test_invalid_value_fails_closed(self) -> None:
        result = self.fragments("yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid TARTCI_CCACHE_WRITE_ISOLATION", result.stderr)

    def test_runner_splices_fragments_without_changing_the_legacy_command(self) -> None:
        source = RUNNER.read_text()
        self.assertIn("ln -sfn '/Volumes/My Shared Files/ccache' ~/Library/Caches/ccache && \\\n"
                      "     ${CCACHE_LAYER_GUEST_PREP}export CCACHE_NODEPEND=true", source)
        self.assertIn("     ${CCACHE_LAYER_GUEST_ENV}mv .env.tartci .env && \\", source)
        wrapper = source[source.index("run_runner_until_done(){"):
                         source.index("run_runner_until_done_unlayered(){")]
        self.assertLess(wrapper.index("tartci_ccache_layer_attach"),
                        wrapper.index("run_runner_until_done_unlayered"))
        self.assertLess(wrapper.index("run_runner_until_done_unlayered"),
                        wrapper.index("tartci_ccache_layer_settle"))

    def test_boot_mounts_the_layer_instead_of_the_legacy_writable_cache(self) -> None:
        source = RUNNER.read_text()
        boot = source[source.index("boot_vm_to_ssh(){"):source.index("tartci_vm_ssh_wait(){")]
        isolated = boot.index('if [ "$CURRENT_SHARED_CACHE" = 1 ] && [ "$CCACHE_LAYER_ENABLED" = 1 ]; then')
        legacy = boot.index('tart_dirs+=(--dir="ccache:$CACHE_ROOT/ccache")')
        # The legacy writable mount is only reached on the branch where
        # isolation is off; a failed attach drops the shared cache instead.
        self.assertLess(isolated, legacy)
        self.assertIn('elif [ "$CURRENT_SHARED_CACHE" = 1 ]; then\n'
                      '    tart_dirs+=(--dir="ccache:$CACHE_ROOT/ccache")', boot)
        branch = boot[isolated:legacy]
        self.assertIn("tartci_ccache_layer_boot", branch)
        self.assertIn("CURRENT_SHARED_CACHE=0", branch)
        # The layer is attached before the configure-checks mount reads the flag
        # and before `tart run` starts the guest.
        self.assertLess(isolated, boot.index('--dir="configure-checks:'))
        self.assertLess(isolated, boot.index("tart run --no-graphics"))

    def test_teardown_abandons_an_unsettled_layer_after_the_guest_stops(self) -> None:
        source = RUNNER.read_text()
        discard = source[source.index("discard_current_vm(){"):]
        discard = discard[:discard.index("\n}\n")]
        self.assertLess(discard.index("terminate_current_guardian"),
                        discard.index('tartci_ccache_layer_abandon "$CURRENT_VM"'))


class BootTests(Base):
    """The host-side hooks, driven through the shell library."""

    def run_lib(self, snippet: str, value: str = "1",
                extra_env: dict | None = None) -> subprocess.CompletedProcess[str]:
        tart = self.tmp / "bin" / "tart"
        tart.parent.mkdir(parents=True, exist_ok=True)
        tart.write_text('#!/bin/sh\nprintf "[]"\n')
        tart.chmod(0o755)
        script = textwrap.dedent(f"""
            set -euo pipefail
            TARTCI_ROOT={ROOT}; CACHE_ROOT={self.tmp}/cache; CCACHE_MAX_SIZE=1G
            STATE_DIR={self.tmp}/events; mkdir -p "$STATE_DIR"
            TARTCI_CCACHE_WRITE_ISOLATION='{value}'
            event(){{ printf '%s %s\n' "$1" "${{2:-}}" >>"$STATE_DIR/events"; }}
            source {LIB}
        """) + textwrap.dedent(snippet) + "\nwait\n"
        env = dict(os.environ, TARTCI_CCACHE_LAYER_TART=str(tart))
        env.update(extra_env or {})
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                              check=False, env=env)

    @property
    def root(self) -> Path:
        return self.tmp / "cache" / "ccache-layers-v1"

    def events(self) -> str:
        path = self.tmp / "events" / "events"
        return path.read_text() if path.exists() else ""

    def guest_tar(self, members: dict[str, bytes]) -> Path:
        """A tar the fake pull command streams, as the guest would."""
        import tarfile
        path = self.tmp / "guest.tar"
        with tarfile.open(path, "w") as archive:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = 4_000_000_000  # a guest-chosen future mtime
                info.mode = 0o000
                archive.addfile(info, io.BytesIO(data))
        return path

    def test_boot_mounts_only_the_shared_store_read_only(self) -> None:
        result = self.run_lib('''
            tartci_ccache_layer_boot vm-a
            printf '%s\n' "${CCACHE_LAYER_TART_DIRS[@]}"
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         [f"--dir=ccache-shared:{self.root}/shared:ro"])
        self.assertTrue((self.root / "shared").is_dir())
        self.assertFalse((self.root / "jobs" / "vm-a").exists(),
                         "no host directory is created for the guest to write")
        self.assertFalse((self.tmp / "cache" / "ccache").exists(),
                         "the boot hook must not create or use the legacy directory")

    def test_off_boot_is_a_no_op(self) -> None:
        result = self.run_lib('''
            tartci_ccache_layer_boot vm-a
            printf 'n=%s\n' "${#CCACHE_LAYER_TART_DIRS[@]}"
        ''', value="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("n=0", result.stdout)
        self.assertFalse(self.root.exists())

    def test_symlinked_shared_store_refuses_the_boot(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.root.mkdir(parents=True)
        (self.root / "shared").symlink_to(elsewhere)
        result = self.run_lib('''
            if tartci_ccache_layer_boot vm-a; then echo attached; else echo refused; fi
            printf 'n=%s\n' "${#CCACHE_LAYER_TART_DIRS[@]}"
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refused", result.stdout)
        self.assertIn("n=0", result.stdout)
        self.assertIn("ccache_layer_attach_failed", self.events())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def settle_with_pull(self, members: dict[str, bytes], runner_rc: int = 0,
                         pull_cmd: str | None = None) -> subprocess.CompletedProcess[str]:
        tar = self.guest_tar(members)
        marker = self.tmp / "pulled"
        cmd = pull_cmd or f"touch {marker}; cat {tar}"
        return self.run_lib(f'''
            tartci_ccache_layer_boot vm-j
            tartci_ccache_layer_attach vm-j
            CURRENT_ASSIGNMENT_QUARANTINE=none
            CURRENT_JOB_CAPTURE_STATUS=terminal; CURRENT_JOB_RECEIPT='{TRUSTED_RECEIPT}'
            tartci_ccache_layer_settle vm-j {runner_rc} "{self.log}" 192.0.2.1
        ''', extra_env={"TARTCI_CCACHE_LAYER_PULL_CMD": cmd})

    def test_green_job_entries_are_pulled_validated_and_promoted(self) -> None:
        good = "ab" + "1" * 38
        manifest = "cd" + "2" * 38
        result = self.settle_with_pull({
            "./stats.txt": b"cache_miss\t7\nremote_storage_hit\t3\nbogus\t9\n",
            f"./remote/{good[:2]}/{good[2:]}": entry(0, b"obj"),
            f"./remote/{manifest[:2]}/{manifest[2:]}": entry(1, b"man"),
            "./remote/ef/" + "3" * 38: b"not an entry",
            "../escape": entry(0),
            "./remote/ab/../../../escape2": entry(0),
            "/etc/abs" : entry(0),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.shared_keys_at(self.root), {good, manifest})
        for key in (good, manifest):
            info = (self.root / "shared" / key[:2] / key[2:]).stat()
            self.assertEqual(info.st_mode & 0o777, 0o644, "the guest's mode never reaches shared")
            self.assertLess(info.st_mtime, 4_000_000_000, "the guest's mtime never reaches shared")
        self.assertFalse((self.tmp / "escape").exists())
        self.assertFalse((self.tmp / "cache" / "escape2").exists())
        audit = [json.loads(line) for line in
                 (self.tmp / "cache" / "ccache-layer-state" / "audit.jsonl").read_text().splitlines()]
        received = [e for e in audit if e["event"] == "receive"][-1]
        self.assertEqual(received["received"], 2)
        self.assertEqual(received["rejected"], 4)
        promote = [e for e in audit if e["event"] == "promote"][-1]
        self.assertEqual(promote["stats"], {"cache_miss": 7, "remote_storage_hit": 3})

    def shared_keys_at(self, root: Path) -> set[str]:
        return {f"{p.parent.name}{p.name}" for p in (root / "shared").glob("*/*")
                if not p.name.startswith(".")}

    def test_links_and_devices_in_the_stream_are_rejected(self) -> None:
        import tarfile
        target = self.tmp / "host-secret"
        target.write_bytes(entry(0, b"secret"))
        path = self.tmp / "guest.tar"
        with tarfile.open(path, "w") as archive:
            for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info = tarfile.TarInfo(f"remote/ab/{'4' * 37}{kind.decode()}"
                                       if kind == tarfile.SYMTYPE else f"remote/ab/{'5' * 38}")
                info.type = kind
                info.linkname = str(target)
                archive.addfile(info)
        result = self.settle_with_pull({}, pull_cmd=f"cat {path}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.shared_keys_at(self.root), set())
        self.assertEqual(target.read_bytes(), entry(0, b"secret"))

    def test_red_job_is_never_pulled(self) -> None:
        good = "ab" + "1" * 38
        result = self.settle_with_pull({f"remote/{good[:2]}/{good[2:]}": entry(0)}, runner_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.tmp / "pulled").exists(), "a red job must not even be pulled")
        self.assertEqual(self.shared_keys_at(self.root), set())

    def test_failed_or_oversized_pull_settles_red(self) -> None:
        good = "ab" + "1" * 38
        for name, cmd, env in (("pull-fails", "exit 3", {}),
                               ("oversized", None, {"TARTCI_CCACHE_LAYER_PULL_MAX": "1K"})):
            with self.subTest(case=name):
                shutil.rmtree(self.tmp / "cache", ignore_errors=True)
                tar = self.guest_tar({f"remote/{good[:2]}/{good[2:]}": entry(0, b"x" * 4096)})
                result = self.run_lib(f'''
                    tartci_ccache_layer_boot vm-j
                    tartci_ccache_layer_attach vm-j
                    CURRENT_ASSIGNMENT_QUARANTINE=none
            CURRENT_JOB_CAPTURE_STATUS=terminal; CURRENT_JOB_RECEIPT='{TRUSTED_RECEIPT}'
                    tartci_ccache_layer_settle vm-j 0 "{self.log}" 192.0.2.1
                ''', extra_env={"TARTCI_CCACHE_LAYER_PULL_CMD": cmd or f"cat {tar}", **env})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.shared_keys_at(self.root), set())
                self.assertFalse((self.root / "jobs" / "vm-j").exists())
                self.assertIn("ccache_layer_pull_failed", self.events())
                self.assertIn("verdict=red", self.events())

    def test_pull_is_bounded_in_time(self) -> None:
        started = time.monotonic()
        result = self.run_lib(f'''
            tartci_ccache_layer_boot vm-j
            tartci_ccache_layer_attach vm-j
            CURRENT_ASSIGNMENT_QUARANTINE=none
            CURRENT_JOB_CAPTURE_STATUS=terminal; CURRENT_JOB_RECEIPT='{TRUSTED_RECEIPT}'
            tartci_ccache_layer_settle vm-j 0 "{self.log}" 192.0.2.1
        ''', extra_env={"TARTCI_CCACHE_LAYER_PULL_CMD": "sleep 60",
                          "TARTCI_CCACHE_LAYER_PULL_SECS": "2"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.monotonic() - started, 30)
        self.assertIn("ccache_layer_pull_failed", self.events())

    def test_receive_bound_covers_a_command_that_closes_stdout_and_lingers(self) -> None:
        self.layout.ensure()
        started = time.monotonic()
        with self.assertRaises(cl.ReceiveError):
            cl.receive(self.layout, "vm-linger", os.getpid(),
                       ["sh", "-c", "head -c 10240 /dev/zero; exec 1>&-; sleep 60"],
                       max_bytes=1 << 20, timeout=2)
        self.assertLess(time.monotonic() - started, 30)
        self.assertFalse((self.layout.jobs / "vm-linger").exists())

    def tar_stream(self, members: list[tuple[str, bytes]]) -> io.BytesIO:
        import tarfile  # noqa: PLC0415
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as archive:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        buf.seek(0)
        return buf

    def test_receive_cap_counts_rejected_members(self) -> None:
        # Junk the validator rejects still costs stream bytes; it must hit the cap.
        self.layout.ensure()
        stream = self.tar_stream([(f"junk/{i}", b"x" * 4096) for i in range(8)])
        with self.assertRaises(cl.ReceiveError):
            cl.receive_stream(self.layout, "vm-junk", os.getpid(), stream, max_bytes=8192)
        self.assertFalse((self.layout.jobs / "vm-junk").exists())

    def test_receive_rejects_a_repeated_stats_member(self) -> None:
        self.layout.ensure()
        stream = self.tar_stream([("stats.txt", b"cache_miss\t1\n")] * 2)
        with self.assertRaises(cl.ReceiveError):
            cl.receive_stream(self.layout, "vm-stats", os.getpid(), stream, max_bytes=1 << 20)
        self.assertFalse((self.layout.jobs / "vm-stats").exists())

    def test_isolated_boot_mounts_configure_checks_read_only(self) -> None:
        runner = (Path(__file__).resolve().parents[1] / "providers/tart-macos/runner.sh").read_text()
        self.assertIn('--dir="configure-checks:$CACHE_ROOT/configure-checks:ro"', runner)
        self.assertIn('rsync -a "/Volumes/My Shared Files/configure-checks/"', runner)
        # The only writable configure-checks mount is the non-isolated branch.
        writable = '--dir="configure-checks:$CACHE_ROOT/configure-checks")'
        before = runner[:runner.index(writable)]
        self.assertTrue(before.endswith('elif [ "$CURRENT_SHARED_CACHE" = 1 ]; then\n    tart_dirs+=('))

    def test_interrupted_pull_layer_is_deleted_on_teardown(self) -> None:
        key = "ab" + "c" * 38
        layout = cl.Layout(self.root, self.tmp / "cache" / "ccache-layer-state")
        layer = cl.attach(layout, "vm-gone", os.getpid())
        write_entry(layer / "remote", key, entry(0))
        result = self.run_lib('''
            tartci_ccache_layer_attach vm-gone
            tartci_ccache_layer_abandon vm-gone
            printf 'vm=<%s>\n' "$CCACHE_LAYER_VM"
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("vm=<>", result.stdout)
        self.assertFalse((self.root / "jobs" / "vm-gone").exists())
        self.assertEqual(self.shared_keys_at(self.root), set())
        self.assertNotIn('"event": "promote"', layout.audit.read_text())

    def test_abandon_without_a_layer_is_a_no_op(self) -> None:
        result = self.run_lib('''
            tartci_ccache_layer_abandon vm-never-attached
            echo done
        ''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("done", result.stdout)
        self.assertEqual(self.events(), "")


class SeedTests(Base):
    """Warming an empty host store from the keys a trusted peer promoted."""

    IDENTITY = {"schema": 1, "golden": "pulp-build-runner:latest",
                "golden_config_sha256": "a" * 64, "compiler": "Apple clang version 17.0.0"}

    def setUp(self) -> None:
        super().setUp()
        # Outside Base's "ccache-layer-*" temp dir: the guard refuses any root
        # with a component starting "ccache".
        peer_tmp = Path(tempfile.mkdtemp(prefix="seed-peer-"))
        self.addCleanup(shutil.rmtree, peer_tmp, True)
        self.peer_root = peer_tmp / "peer-root"
        self.peer_shared = self.peer_root / "ccache-layers-v1" / "shared"
        self.peer_promotions = self.peer_root / "ccache-layer-state" / "promotions"
        self.peer_shared.mkdir(parents=True)
        self.peer_promotions.mkdir(parents=True)
        # Runs the remote script locally, as ssh would on the peer.
        self.ssh = self.tmp / "fake-ssh"
        self.ssh.write_text('#!/bin/sh\nfor a; do last=$a; done\nexec sh -c "$last"\n')
        self.ssh.chmod(0o755)

    def identity_file(self, name: str, **changes: object) -> Path:
        path = self.tmp / f"{name}.identity.json"
        path.write_text(json.dumps({**self.IDENTITY, **changes}))
        return path

    def peer_entry(self, key: str, data: bytes, promoted: bool = True) -> Path:
        path = write_entry(self.peer_shared, key, data)
        if promoted:
            with (self.peer_promotions / "20261117T000000Z-vm.keys").open("a") as handle:
                handle.write(key + "\n")
        return path

    def run_seed(self, **kwargs: object) -> dict:
        options: dict = {"peer": "m5studio", "local_identity": self.identity_file("local"),
                         "peer_identity": self.identity_file("peer"), "max_bytes": 1 << 20,
                         "timeout": 30, "max_size": 1 << 30,
                         "peer_cache_root": str(self.peer_root), "ssh": str(self.ssh)}
        options.update(kwargs)
        return cl.seed(self.layout, options.pop("peer"), options.pop("local_identity"),
                       options.pop("peer_identity"), **options)

    def seed_events(self) -> list[dict]:
        return [e for e in self.audit_events() if e["event"] == "ccache_seed"]

    def assert_refused(self, reason: str, **kwargs: object) -> None:
        with self.assertRaises(cl.SeedError):
            self.run_seed(**kwargs)
        event = self.seed_events()[-1]
        self.assertEqual(event["verdict"], "refused")
        self.assertIn(reason, event["reason"])

    def test_promoted_peer_keys_land_and_the_audit_names_source_and_bytes(self) -> None:
        result_key, manifest_key = "ab" + "1" * 38, "cd" + "2" * 38
        self.peer_entry(result_key, entry(0))
        self.peer_entry(manifest_key, entry(1))
        counts = self.run_seed()
        self.assertEqual(counts["promoted"], 2)
        self.assertEqual(self.shared_keys(), {result_key, manifest_key})
        [event] = self.seed_events()
        self.assertEqual(event["verdict"], "seeded")
        self.assertEqual(event["peer"], "m5studio")
        self.assertEqual(event["bytes_received"], 2 * len(entry(0)))
        self.assertIsNotNone(event["trimmed"], "the store is trimmed after seeding")
        self.assertEqual(list(self.layout.jobs.iterdir()), [], "the seed layer is removed")
        keys = list(self.layout.promotions.glob("seed-m5studio-*.keys"))
        self.assertEqual(len(keys), 1, "the rollback list is written")

    def test_only_keys_the_peer_promoted_are_exported(self) -> None:
        promoted, stray = "ab" + "1" * 38, "ab" + "9" * 38
        self.peer_entry(promoted, entry(0))
        self.peer_entry(stray, entry(0), promoted=False)
        self.run_seed()
        self.assertEqual(self.shared_keys(), {promoted})

    def test_a_source_under_a_ccache_mount_is_refused(self) -> None:
        # The September opt-in's store: $CACHE_ROOT/ccache/tartci-layers-v1/shared,
        # inside the guest-writable legacy mount.
        legacy = self.tmp / "legacy-root" / "ccache" / "tartci-layers-v1"
        write_entry(legacy / "shared", "ab" + "1" * 38, entry(0))
        marker = self.tmp / "pulled"
        self.ssh.write_text(f"#!/bin/sh\ntouch {marker}\n")
        for root in (str(self.tmp / "legacy-root" / "ccache"), str(legacy),
                     "~/.cache/pulp-ci/ccache", "~/x/ccache-layers-v1", "~/a/../b", "rel/path",
                     # The Linux guests' read-write mount, and the legacy mount
                     # spelled in another case (APFS is case-insensitive).
                     "~/.cache/pulp-ci/ccache-linux", "~/.cache/pulp-ci/CCache",
                     "~/x/TARTCI-LAYERS-V1"):
            with self.subTest(root=root):
                self.assert_refused("peer cache root", peer_cache_root=root)
                self.assertFalse(marker.exists(), "a refused source is never pulled")
        self.assertEqual(self.shared_keys(), set())

    def test_a_mismatched_image_or_compiler_is_refused_before_any_pull(self) -> None:
        marker = self.tmp / "pulled"
        self.ssh.write_text(f"#!/bin/sh\ntouch {marker}\n")
        for field, value in (("golden_config_sha256", "b" * 64),
                             ("compiler", "Apple clang version 16.0.0"),
                             ("golden", "pulp-build-runner:old")):
            with self.subTest(field):
                self.assert_refused(f"'{field}'",
                                    peer_identity=self.identity_file("peer-bad", **{field: value}))
                self.assertFalse(marker.exists(), "a refused seed must not run the pull")
        self.assertEqual(self.shared_keys(), set())

    def test_a_malformed_identity_is_refused_even_when_both_sides_agree(self) -> None:
        # The same bad file on both sides, so equality cannot be what refuses it.
        for name, text in (("json", "{not json"),
                           ("extra", json.dumps({**self.IDENTITY, "extra": 1})),
                           ("hex64", json.dumps({**self.IDENTITY, "golden_config_sha256": "XYZ"})),
                           ("blank", json.dumps({**self.IDENTITY, "compiler": " "}))):
            with self.subTest(name):
                bad = self.tmp / f"bad-{name}.json"
                bad.write_text(text)
                self.assert_refused("identity", local_identity=bad, peer_identity=bad)
        self.assertEqual(self.shared_keys(), set())

    def test_links_and_bad_headers_are_rejected_as_in_receive(self) -> None:
        good = "ab" + "1" * 38
        self.peer_entry(good, entry(0))
        self.peer_entry("ab" + "2" * 38, b"not a ccache entry at all")
        self.peer_entry("ab" + "3" * 38, b"\xcc\xac\x01\x07" + b"\x00" * 16)
        secret = self.tmp / "host-secret"
        secret.write_bytes(entry(0, b"secret"))
        link = self.peer_shared / "ab" / ("4" * 38)
        link.symlink_to(secret)
        with (self.peer_promotions / "x.keys").open("a") as handle:
            handle.write("ab" + "4" * 38 + "\n../../etc\n")
        self.run_seed()
        self.assertEqual(self.shared_keys(), {good})
        [event] = self.seed_events()
        self.assertEqual(event["received_rejected"], 2)
        self.assertEqual(secret.read_bytes(), entry(0, b"secret"))

    def test_max_bytes_is_honoured(self) -> None:
        for i in range(4):
            self.peer_entry(f"ab{i:038d}", entry(0, b"x" * 4096))
        self.assert_refused("exceeds", max_bytes=8192)
        self.assertEqual(self.shared_keys(), set())
        self.assertEqual(list(self.layout.jobs.iterdir()), [])

    def test_a_seed_never_replaces_a_local_manifest_or_result(self) -> None:
        manifest, result = "cd" + "2" * 38, "ab" + "1" * 38
        self.layout.ensure()
        local_manifest = write_entry(self.layout.shared, manifest, entry(1, b"local"), mtime=1.0)
        local_result = write_entry(self.layout.shared, result, entry(0, b"local"), mtime=1.0)
        self.peer_entry(manifest, entry(1, b"peer"))
        self.peer_entry(result, entry(0, b"peer"))
        counts = self.run_seed(force_warm=True)
        self.assertEqual(counts["existing"], 2)
        self.assertEqual(counts["replaced"], 0)
        self.assertEqual(local_manifest.read_bytes(), entry(1, b"local"))
        self.assertEqual(local_result.read_bytes(), entry(0, b"local"))

    def test_a_warm_store_is_refused_without_force_warm(self) -> None:
        self.layout.ensure()
        write_entry(self.layout.shared, "ef" + "3" * 38, entry(0))
        self.peer_entry("ab" + "1" * 38, entry(0))
        self.assert_refused("store_not_empty")
        self.assertEqual(self.shared_keys(), {"ef" + "3" * 38})
        self.run_seed(force_warm=True)
        self.assertEqual(self.shared_keys(), {"ef" + "3" * 38, "ab" + "1" * 38})

    def test_a_pending_green_layer_refuses_the_seed(self) -> None:
        self.layout.ensure()
        (self.layout.green / "vm-pending").mkdir()
        self.peer_entry("ab" + "1" * 38, entry(0))
        self.assert_refused("green_pending")
        self.assertEqual(self.shared_keys(), set())

    def test_every_refusal_is_audited(self) -> None:
        self.layout.ensure()
        with self.subTest("busy"):
            with cl.flock(self.layout.locks / "seed.lock"):
                self.assert_refused("busy")
        with self.subTest("invalid peer"):
            self.assert_refused("invalid peer name", peer="m5studio;rm")
        with self.subTest("ssh missing"):
            self.assert_refused("FileNotFoundError", ssh=str(self.tmp / "no-such-ssh"))
        with self.subTest("promote ENOSPC"):
            import errno as errno_mod  # noqa: PLC0415
            from unittest import mock  # noqa: PLC0415
            self.peer_entry("ab" + "1" * 38, entry(0))
            with mock.patch.object(cl, "promote_layer",
                                   side_effect=OSError(errno_mod.ENOSPC, "No space left")):
                self.assert_refused("No space left")
            self.assertEqual(list(self.layout.jobs.iterdir()), [])
        self.assertEqual(self.shared_keys(), set())

    def test_a_partial_promotion_still_records_every_linked_key(self) -> None:
        import errno as errno_mod  # noqa: PLC0415
        from unittest import mock  # noqa: PLC0415
        first, second = "ab" + "1" * 38, "ab" + "2" * 38
        self.peer_entry(first, entry(0))
        self.peer_entry(second, entry(0))
        real = cl.publish_result
        calls = {"n": 0}

        def publish_then_fill_disk(src: Path, dest: Path) -> str:
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError(errno_mod.ENOSPC, "No space left on device")
            return real(src, dest)

        with mock.patch.object(cl, "publish_result", side_effect=publish_then_fill_disk):
            self.assert_refused("No space left")
        self.assertEqual(self.shared_keys(), {first})
        [keys_file] = self.layout.promotions.glob("seed-m5studio-*.keys")
        self.assertEqual(keys_file.read_text().split(), [first])

    def test_the_remote_key_filter_drops_anything_but_a_lowercase_key(self) -> None:
        import tarfile  # noqa: PLC0415
        good = "ab" + "1" * 38
        self.peer_entry(good, entry(0))
        for key in ("AB" + "1" * 38, "ab" + "1" * 37 + "Z"):
            write_entry(self.peer_shared, key, entry(0))
        (self.peer_shared / "x").mkdir()
        (self.peer_shared / "x" / "y").write_bytes(entry(0))
        (self.peer_promotions / "odd.keys").write_text("\n".join([
            "AB" + "1" * 38, "ab" + "1" * 37 + "Z", "../../etc/passwd", "ab/" + "1" * 37,
            "a", "", "xy", "ab*", "ab " + "1" * 36, "$(touch pwned)"]) + "\n")
        out = subprocess.run(cl.seed_pull_command("m5studio", str(self.peer_root), str(self.ssh)),
                             capture_output=True, check=True, cwd=self.tmp)
        with tarfile.open(fileobj=io.BytesIO(out.stdout)) as archive:
            names = [member.name for member in archive]
        self.assertEqual(names, [f"remote/ab/{'1' * 38}"])
        self.assertFalse((self.tmp / "pwned").exists())

    def test_identity_hashes_the_golden_configuration(self) -> None:
        fake = self.tmp / "tart"
        fake.write_text('#!/bin/sh\nprintf \'{"cpuCount":8,"os":"darwin"}\'\n')
        fake.chmod(0o755)
        value = cl.identity("pulp-build-runner:latest", "Apple clang version 17.0.0\n", str(fake))
        path = self.tmp / "id.json"
        path.write_text(json.dumps(value))
        self.assertEqual(cl.load_identity(path)["compiler"], "Apple clang version 17.0.0")
        import hashlib  # noqa: PLC0415
        self.assertEqual(value["golden_config_sha256"],
                         hashlib.sha256(b'{"cpuCount":8,"os":"darwin"}').hexdigest())


class ProfileTests(unittest.TestCase):
    def test_every_fleet_lane_enables_isolation(self) -> None:
        # Every gate lane on every fleet host: the shared store is the one
        # cross-job channel the gate VMs have, so no lane keeps the legacy
        # writable mount.
        profiles = sorted((ROOT / "profiles").glob("*-macos-fleet.toml"))
        self.assertGreaterEqual(len(profiles), 4)
        for path in profiles:
            with self.subTest(profile=path.name):
                data = fleet.load(path)
                self.assertTrue(data["lane"])
                for lane in data["lane"]:
                    self.assertIs(lane.get("ccache_write_isolation"), True, lane["id"])
                rendered = fleet.rendered_plists(data)
                self.assertTrue(rendered)
                for name, body in rendered.items():
                    self.assertIn(b"<key>TARTCI_CCACHE_WRITE_ISOLATION</key>", body, name)

    def profile_with(self, value: str) -> Path:
        base = (ROOT / "profiles" / "m5-macos-fleet.toml").read_text()
        base = base.replace("ccache_write_isolation = true\n", "")
        marker = 'id = "pulp-gate"\n'
        text = base.replace(marker, marker + f"ccache_write_isolation = {value}\n", 1)
        tomllib.loads(text)
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def test_enabled_lane_renders_the_flag_for_that_lane_only(self) -> None:
        data = fleet.load(self.profile_with("true"))
        rendered = fleet.rendered_plists(data)
        flagged = sorted(name for name, body in rendered.items()
                         if b"TARTCI_CCACHE_WRITE_ISOLATION" in body)
        self.assertTrue(flagged)
        self.assertTrue(all("pulp-gate" in name for name in flagged), flagged)

    def test_non_boolean_is_rejected(self) -> None:
        with self.assertRaises((ValueError, SystemExit)):
            fleet.load(self.profile_with('"yes"'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
