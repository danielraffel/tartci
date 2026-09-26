#!/usr/bin/env python3
"""The pre-mint proofs run beside the clone and boot, and decide exactly as before.

Shipyard's admission verdict and the runner group's repository-access proof
gate every JIT mint. Neither reads the VM, yet both started only after the
clone, the boot and the guest preflights, so their full duration sat on every
job's critical path. They now start once the VM lease is held and the boundary
consumes their results.

The tests drive the real library and the real boundary block against stub
`shipyard` and `gh` executables, and assert three things:

1. the proofs really overlap the boot (the stub records when it was asked);
2. a refusal still discards the booted VM with the same code and events;
3. a verdict too old to trust, a missing result or a disabled knob falls back
   to the synchronous call, so the parallel path can remove time but never a
   check.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "providers/tart-macos/runner.sh"
ADMISSION_LIB = ROOT / "providers/common/admission-clean.lib.sh"
PROOF_LIB = ROOT / "providers/tart-macos/boundary-proof.lib.sh"
REPO = "Generous-Corp/pulp"
LABELS = "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm"


def envelope(verdict: str, reason: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "command": "runner:admission-clean",
        "verdict": verdict,
        "reason": reason,
        "repo": REPO,
        "base": "main",
        "labels": sorted({label.lower() for label in LABELS.split(",")}),
        "observed_at": "2026-09-26T05:00:00Z",
        "blocker_run_ids": [] if verdict == "admit" else [1],
    }


def function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n(.*?)^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(1)


def boundary_gate_block(source: str) -> str:
    start = source.index(
        "  if tartci_admission_clean_enabled; then\n    local admission_json"
    )
    end = source.index("\n  fi\n", start) + len("\n  fi\n")
    return source[start:end]


class Harness:
    """A bash process with the real libraries, stub CLIs and a fake clock-free boot."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.state = tmp / "state"
        self.state.mkdir()
        self.events = tmp / "events.tsv"
        self.calls = tmp / "shipyard-calls"
        self.discards = tmp / "discards"

    def _exe(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def stub_shipyard(self, script: list[tuple[str, str, int]], delay: float = 0) -> None:
        """One verdict per call in order (the last repeats); each call logs its start."""
        payloads = self.tmp / "payloads"
        payloads.mkdir()
        for index, (verdict, reason, code) in enumerate(script):
            (payloads / f"{index}.json").write_text(
                json.dumps(envelope(verdict, reason)), encoding="utf-8"
            )
            (payloads / f"{index}.rc").write_text(str(code), encoding="utf-8")
        self._exe(
            "stub-shipyard",
            "#!/bin/bash\n"
            f"calls={str(self.calls)!r}\n"
            "n=$(wc -l <\"$calls\" 2>/dev/null | tr -d ' ' || true); n=${n:-0}\n"
            "python3 -c 'import time;print(time.time())' >>\"$calls\"\n"
            f"printf '%s\\n' $$ >{str(self.tmp / 'shipyard.pid')!r}\n"
            f"sleep {delay}\n"
            f"last={len(script) - 1}\n"
            "i=$n; [ \"$i\" -le \"$last\" ] || i=$last\n"
            f"cat {payloads}/$i.json; printf '\\n'\n"
            f"exit $(cat {payloads}/$i.rc)\n",
        )

    def stub_gh(self, *, fail: str | None = None) -> None:
        if fail is None:
            self._exe("stub-gh", "#!/bin/bash\necho '{}'\n")
        else:
            self._exe("stub-gh", f"#!/bin/bash\necho {fail!r} >&2\nexit 1\n")

    def run(self, script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        harness = self.tmp / "harness.sh"
        harness.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            f"TARTCI_ROOT={str(ROOT)!r}\n"
            f"source {str(ADMISSION_LIB)!r}\n"
            f"source {str(PROOF_LIB)!r}\n"
            "note(){ :; }\nheartbeat(){ :; }\n"
            f"event(){{ printf '%s\\t%s\\n' \"$1\" \"${{2:-}}\" >>{str(self.events)!r}; }}\n"
            f"discard_current_vm(){{ echo discard >>{str(self.discards)!r}; }}\n"
            "tartci_release_vm_lease(){ :; }\n"
            f"REPO={REPO!r}\n"
            f"STATE_DIR={str(self.state)!r}\n"
            "JIT_GH_CLI=stub-gh\n"
            "i=1\nvm='lane-vm-1'\n"
            f"selected_labels={LABELS!r}\n"
            + script,
            encoding="utf-8",
        )
        full_env = os.environ.copy()
        full_env.update(
            {
                "PATH": os.pathsep.join([str(self.bin), full_env.get("PATH", "/usr/bin:/bin")]),
                "TARTCI_ADMISSION_CLEAN_MODE": "required",
                "TARTCI_SHIPYARD_CLI": "stub-shipyard",
                "TARTCI_ADMISSION_CLEAN_STATE_DIR": str(self.tmp / "breaker"),
                "TARTCI_ADMISSION_CLEAN_CONTENTION_WAIT_SECS": "0",
            }
        )
        full_env.update(env or {})
        return subprocess.run(
            ["/bin/bash", str(harness)],
            env=full_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )

    def call_times(self) -> list[float]:
        if not self.calls.exists():
            return []
        return [float(line) for line in self.calls.read_text().split()]

    def event_names(self) -> list[str]:
        if not self.events.exists():
            return []
        return [line.split("\t", 1)[0] for line in self.events.read_text().splitlines()]


def access_block(source: str) -> str:
    """The run_one repository-access block, verbatim, up to its success exit."""
    start = source.index('  access_error="$STATE_DIR/$vm.repository-access-error"')
    end = source.index('  rm -f "$access_error"\n', start) + len('  rm -f "$access_error"\n')
    return source[start:end]


def access_function() -> str:
    block = access_block(RUNNER.read_text(encoding="utf-8"))
    return (
        "access(){\n"
        "  local access_json access_rc access_error\n"
        "  local selected_group_id=\"$1\"\n"
        f"{block}"
        "  echo passed\n"
        "}\n"
    )


def boundary_function() -> str:
    block = boundary_gate_block(RUNNER.read_text(encoding="utf-8"))
    return f"boundary(){{\n{block}}}\n"


class ProofsOverlapTheBootTests(unittest.TestCase):
    def test_admission_is_asked_before_the_boot_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = Harness(Path(raw))
            h.stub_shipyard([("admit", "clean", 0)], delay=1)
            h.stub_gh()
            booted = h.tmp / "booted"
            result = h.run(
                boundary_function()
                + "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 1\n"
                # The stand-in for clone + boot + preflights.
                + "sleep 2\n"
                + f"python3 -c 'import time;print(time.time())' >{str(booted)!r}\n"
                + "boundary\n"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = h.call_times()
            self.assertEqual(len(calls), 1, "the boundary asked Shipyard again")
            self.assertLess(
                calls[0],
                float(booted.read_text()),
                "admission was not asked until after the boot",
            )
            names = h.event_names()
            self.assertIn("admission_parallel_start", names)
            self.assertIn("source=parallel", h.events.read_text())
            self.assertNotIn("discard", h.discards.read_text() if h.discards.exists() else "")

    def test_repository_access_is_proved_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = Harness(Path(raw))
            h.stub_shipyard([("admit", "clean", 0)])
            h.stub_gh()
            err = h.tmp / "access.err"
            result = h.run(
                "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 1\n"
                f"tartci_boundary_proof_take_access {str(err)!r}\n"
                "printf '%s\\n' \"$BOUNDARY_ACCESS_RC\" \"$BOUNDARY_ACCESS_JSON\"\n"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rc, payload = result.stdout.splitlines()[:2]
            self.assertEqual(rc, "0")
            self.assertEqual(json.loads(payload)["verdict"], "admit")


class RefusalsStillDiscardTests(unittest.TestCase):
    def test_parallel_defer_discards_the_booted_vm_with_the_same_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = Harness(Path(raw))
            h.stub_shipyard([("defer", "stale_compatible_runs", 3)])
            h.stub_gh()
            result = h.run(
                boundary_function()
                + "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 1\n"
                + "rc=0; boundary || rc=$?\nexit $rc\n"
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn("admission_deferred", h.event_names())
            self.assertEqual(h.discards.read_text().split(), ["discard"])
            self.assertEqual(len(h.call_times()), 1)

    def test_parallel_access_denial_reaches_the_refusal_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = Harness(Path(raw))
            h.stub_shipyard([("admit", "clean", 0)])
            h.stub_gh(fail="HTTP 403: Resource not accessible by integration")
            err = h.tmp / "access.err"
            result = h.run(
                "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 7\n"
                f"tartci_boundary_proof_take_access {str(err)!r}\n"
                "printf '%s\\n' \"$BOUNDARY_ACCESS_RC\"\n"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "2")
            # The refusal path greps this file for 401/403/404.
            self.assertIn("HTTP 403", err.read_text())


class ProviderAccessBlockTests(unittest.TestCase):
    """The run_one access block itself, fed by the parallel proof."""

    def _run(self, fail: str | None, group: int = 7) -> tuple[subprocess.CompletedProcess, Harness]:
        raw = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, raw, True)
        h = Harness(Path(raw))
        h.stub_shipyard([("admit", "clean", 0)])
        h.stub_gh(fail=fail)
        denied = h.tmp / "denied"
        result = h.run(
            access_function()
            + f"record_jit_admission_denied(){{ echo \"$*\" >>{str(denied)!r}; }}\n"
            + f"tartci_boundary_proof_start \"$vm\" \"$selected_labels\" {group}\n"
            + "_tartci_boundary_proof_join\n"
            # The sync fallback must not be what answers: a gh that would now
            # time out proves the block used the parallel result.
            + f"printf '#!/bin/bash\\nsleep 60\\n' >{str(h.bin / 'stub-gh')!r}\n"
            + f"rc=0; access {group} || rc=$?\nexit $rc\n"
        )
        return result, h

    def test_a_parallel_denial_refuses_the_mint_and_blocks_the_contract(self) -> None:
        result, h = self._run("HTTP 403: Resource not accessible by integration")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("passed", result.stdout)
        self.assertEqual(h.discards.read_text().split(), ["discard"])
        self.assertTrue((h.tmp / "denied").exists(), "access denial was not recorded")
        self.assertIn("jit_repository_access_denied", h.event_names())

    def test_the_control_a_parallel_admit_passes(self) -> None:
        # Group 1 is repository-scoped and admitted without an API call.
        result, h = self._run(None, group=1)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("passed", result.stdout)
        self.assertFalse(h.discards.exists())


class FallbackToTheSynchronousCallTests(unittest.TestCase):
    def test_a_stale_verdict_is_asked_again_at_the_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = Harness(Path(raw))
            # The parallel answer admits; by the time the boundary reads it,
            # it is past the bound, and the fresh answer defers. The fresh
            # answer must win.
            h.stub_shipyard([("admit", "clean", 0), ("defer", "stale_compatible_runs", 3)])
            h.stub_gh()
            result = h.run(
                boundary_function()
                + "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 1\n"
                + "sleep 3\n"
                + "rc=0; boundary || rc=$?\nexit $rc\n",
                env={"TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS": "1"},
            )
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(len(h.call_times()), 2)
            names = h.event_names()
            self.assertIn("admission_parallel_stale", names)
            self.assertIn("source=boundary", h.events.read_text())
            self.assertEqual(h.discards.read_text().split(), ["discard"])

    def test_the_control_a_fresh_verdict_is_used_as_is(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = Harness(Path(raw))
            h.stub_shipyard([("admit", "clean", 0), ("defer", "stale_compatible_runs", 3)])
            h.stub_gh()
            result = h.run(
                boundary_function()
                + "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 1\n"
                + "rc=0; boundary || rc=$?\nexit $rc\n",
                env={"TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS": "60"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(h.call_times()), 1)

    def test_disabled_knob_runs_the_boundary_synchronously(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = Harness(Path(raw))
            h.stub_shipyard([("admit", "clean", 0)])
            h.stub_gh()
            result = h.run(
                boundary_function()
                + "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 1\n"
                + "sleep 1\n"
                + "boundary\n",
                env={"TARTCI_BOUNDARY_PROOF_PARALLEL": "0"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(h.call_times()), 1)
            self.assertNotIn("admission_parallel_start", h.event_names())
            self.assertIn("source=boundary", h.events.read_text())

    def test_an_abandoned_proof_is_killed_and_never_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            h = Harness(Path(raw))
            h.stub_shipyard([("admit", "clean", 0)], delay=30)
            h.stub_gh()
            started = time.monotonic()
            result = h.run(
                "tartci_boundary_proof_start \"$vm\" \"$selected_labels\" 1\n"
                f"while [ ! -s {str(h.tmp / 'shipyard.pid')!r} ]; do sleep 0.1; done\n"
                "dir=\"$BOUNDARY_PROOF_DIR\"\n"
                "tartci_boundary_proof_abandon\n"
                "[ ! -e \"$dir\" ] || { echo 'proof dir survived'; exit 9; }\n"
                "if tartci_boundary_proof_take_admission; then echo consumed; exit 8; fi\n"
                f"pid=$(cat {str(h.tmp / 'shipyard.pid')!r})\n"
                "for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 \"$pid\" 2>/dev/null || exit 0; sleep 0.2; done\n"
                "echo 'shipyard call outlived the abandon'; exit 7\n"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertLess(time.monotonic() - started, 20)

    def test_invalid_configuration_is_refused(self) -> None:
        for name, value in (
            ("TARTCI_BOUNDARY_PROOF_PARALLEL", "yes"),
            ("TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS", "601"),
            ("TARTCI_BOUNDARY_PROOF_MAX_AGE_SECS", "soon"),
        ):
            with self.subTest(name=name, value=value), tempfile.TemporaryDirectory() as raw:
                h = Harness(Path(raw))
                result = h.run("tartci_boundary_proof_validate\n", env={name: value})
                self.assertEqual(result.returncode, 2, result.stderr)


class ProviderWiringTests(unittest.TestCase):
    """Where the proofs start and where every exit path drops them."""

    def test_proofs_start_after_the_lease_and_before_the_clone(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        # A job boot leases, starts the proofs, clones and boots inside
        # boot_vm_to_ssh (shared with the warm-VM park, which starts none).
        boot = function_body(source, "boot_vm_to_ssh")
        lease = boot.index("tartci_acquire_vm_lease")
        start = boot.index("tartci_boundary_proof_start")
        clone = boot.index("event clone_start")
        self.assertIn('[ -z "$proof_group" ] ||', boot[start - 40:start])
        body = function_body(source, "run_one")
        self.assertIn('"$selected_group_id" || lease_rc=$?',
                      body[body.index('boot_vm_to_ssh "$i"'):])
        # A warm hand-off has nothing to overlap with and starts them itself.
        handoff = body.index("tartci_warm_handoff")
        self.assertIn("tartci_boundary_proof_start", body[handoff:body.index('boot_vm_to_ssh "$i"')])
        booted = body.index("t_booted=")
        consume = body.index("tartci_boundary_proof_take_admission")
        mint = body.index("generate-jitconfig")
        self.assertLess(lease, start)
        self.assertLess(start, clone)
        self.assertLess(booted, consume, "the verdict is consumed before the boot")
        self.assertLess(consume, mint)

    def test_every_exit_path_drops_an_unconsumed_proof(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("tartci_boundary_proof_abandon", function_body(source, "cleanup"))
        loop_call = source.index('run_one "$i" "$selected_labels" "$selected_tier" || run_rc=$?')
        self.assertIn(
            "tartci_boundary_proof_abandon",
            source[loop_call:loop_call + 2000],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
