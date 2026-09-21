#!/usr/bin/env python3
"""Behavioral tests for the pre-clone admission probe and the reason it reports.

Two defects motivated these, both measured on the Pulp macOS gate during the
2026-09-21 outage:

1. The admission verdict was only ever asked AFTER a 47 GB CoW clone and a full
   boot, so a lane whose admission authority was unreachable minted and then
   discarded a VM every cycle - 103 VMs in one hour, zero jobs served. The
   verdict is a function of (repo, labels), so nothing required paying for a VM
   to learn it.
2. The refusal event carried `rc=1 unregistered=true` and nothing else. The
   typed reason and the underlying error were written to an envelope on disk
   and nowhere else.

Both are asserted by DRIVING the real `run_one` body with a stub `shipyard` on
PATH rather than by matching source text, so a test cannot pass against a
provider that no longer probes or no longer reports. The clone is observed via
the `clone_start` event, which the harness turns into a distinguishable exit.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MACOS_RUNNER = ROOT / "providers/tart-macos/runner.sh"
LIB = ROOT / "providers/common/admission-clean.lib.sh"
DETAIL = ROOT / "scripts/admission_clean_detail.py"

# `run_one` reaching this event means a VM clone was about to start.
CLONE_REACHED_EXIT = 17

LABELS = "self-hosted,macOS,ARM64,pulp-build,pulp-build-vm"
REPO = "Generous-Corp/pulp"

# The real error text observed on the gate, verbatim. It happens to be an HTTP
# 504; near the failure threshold the same inconclusive outcome arrives as
# `unexpected end of JSON input` instead, which is why nothing classifies on
# the message and the tests assert against the typed reason.
LIVE_ERROR = (
    "open PR list failed: gh pr list --repo Generous-Corp/pulp --state open "
    "--base main --limit 1000 --json id,number,isDraft,baseRefName,headRefOid,"
    "headRefName,mergeStateStatus,autoMergeRequest,labels,statusCheckRollup "
    "failed with status 1: HTTP 504: We couldn't respond to your request in "
    "time. Sorry about that."
)


def function_body(source: str, name: str) -> str:
    match = re.search(rf"^{name}\(\)\{{\n(.*?)^\}}$", source, re.MULTILINE | re.DOTALL)
    if match is None:
        raise AssertionError(f"missing function {name}")
    return match.group(1)


def boundary_gate_block(source: str) -> str:
    """The at-boundary gate block, verbatim.

    Sliced rather than matched so the harness below runs the SAME text the
    provider runs. The block is the only one that declares `admission_json`,
    and it closes at the first `fi` back at its own two-space indent.
    """
    start = source.index(
        "  if tartci_admission_clean_enabled; then\n    local admission_json"
    )
    end = source.index("\n  fi\n", start) + len("\n  fi\n")
    return source[start:end]


def make_envelope(
    verdict: str, reason: str, *, error: str | None = None
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": 1,
        "command": "runner:admission-clean",
        "verdict": verdict,
        "reason": reason,
        "repo": REPO,
        "base": "main",
        "labels": sorted({label.lower() for label in LABELS.split(",")}),
        "observed_at": "2026-09-21T05:34:56.027586+00:00",
        "blocker_run_ids": [],
    }
    if error is not None:
        value["error"] = error
    return value


class RunOneHarness:
    """Runs the real `run_one` body far enough to see whether it clones."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.events = tmp / "events.tsv"
        self.notes = tmp / "notes.log"
        self.heartbeats = tmp / "heartbeats.log"
        self.state = tmp / "state"
        self.state.mkdir()

    def _exe(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path

    def stub_shipyard(self, envelope: dict[str, object], exit_code: int) -> None:
        # Via a file, never an inlined literal: the real error text carries both
        # quote characters, and a stub that mangles its own payload would make
        # the adapter reject the envelope and report a configuration error that
        # reads exactly like a refusal.
        payload = self.tmp / "verdict.json"
        payload.write_text(
            json.dumps(envelope, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        self._exe(
            "stub-shipyard",
            "#!/bin/bash\n"
            f"cat {str(payload)!r}\n"
            "printf '\\n'\n"
            f"exit {exit_code}\n",
        )

    def run(self, *, source_override: str | None = None) -> subprocess.CompletedProcess:
        source = (
            MACOS_RUNNER.read_text(encoding="utf-8")
            if source_override is None
            else source_override
        )
        body = function_body(source, "run_one")
        harness = self.tmp / "harness.sh"
        harness.write_text(
            "#!/bin/bash\n"
            # Match the provider's own strictness. Under a laxer shell an
            # errexit slip in the new block - a substitution on the refusal
            # path, say - would never surface here.
            "set -euo pipefail\n"
            f"TARTCI_ROOT={str(ROOT)!r}\n"
            f"source {str(LIB)!r}\n"
            # Stubs for everything `run_one` touches before the clone. The
            # admission chain itself is NOT stubbed: the real library, the real
            # adapter and the real renderer run against a stub `shipyard`.
            "ephemeral_boot_name(){ printf 'lane-vm-%s' \"$1\"; }\n"
            "now_epoch(){ printf '0'; }\n"
            "runner_group_id_for_tier(){ printf '11'; }\n"
            "runner_api_root_for_group(){ printf 'repos/%s' \"$REPO\"; }\n"
            "jit_admission_denied(){ return 1; }\n"
            "tartci_pool_lock_absent(){ return 0; }\n"
            "tartci_check_macos_disk_floor_with_cleanup_once(){ return 0; }\n"
            "tartci_prepare_and_check_disk_root_observed(){ return 0; }\n"
            "tartci_prepare_disk_root(){ return 0; }\n"
            "reclaim_runner_name(){ :; }\n"
            "sweep_lane_ghost_runners(){ :; }\n"
            "tartci_vm_lease_cores(){ printf '4'; }\n"
            "tartci_vm_lease_mem_mb(){ printf '8192'; }\n"
            "tartci_vm_lease_priority(){ printf '0'; }\n"
            "tartci_acquire_vm_lease(){ return 0; }\n"
            "tartci_release_vm_lease(){ :; }\n"
            "discard_current_vm(){ :; }\n"
            "runtime_emit_complete(){ :; }\n"
            f"note(){{ printf '%s\\n' \"$*\" >>{str(self.notes)!r}; }}\n"
            f"heartbeat(){{ printf '%s\\n' \"$1\" >>{str(self.heartbeats)!r}; }}\n"
            "event(){\n"
            f"  printf '%s\\t%s\\n' \"$1\" \"${{2:-}}\" >>{str(self.events)!r}\n"
            f"  [ \"$1\" = clone_start ] && exit {CLONE_REACHED_EXIT}\n"
            "  return 0\n"
            "}\n"
            f"REPO={REPO!r}\n"
            f"LABELS={LABELS!r}\n"
            "GOLDEN='pulp-build-runner:latest'\n"
            "RUNNER_NAME='lane-01'\n"
            "SLOT=1\n"
            f"STATE_DIR={str(self.state)!r}\n"
            f"TART_HOME={str(self.tmp / 'vms')!r}\n"
            f"CACHE_ROOT={str(self.tmp / 'cache')!r}\n"
            f"MACOS_LOGROOT={str(self.tmp / 'logs')!r}\n"
            "FETCHCONTENT_SOURCE_ROOT=''\n"
            "CHROME_MOUNT_ARG=''\n"
            "ASSIGNMENT_MODE='off'\n"
            "CURRENT_VM=''\n"
            "CURRENT_IP=''\n"
            "CURRENT_RESV=''\n"
            "CURRENT_RPID=''\n"
            "CURRENT_LABELS=''\n"
            "CURRENT_RUNNER_API_ROOT=''\n"
            "SERVING_BLOCKED_SINCE=''\n"
            "RUNNER_VERSION='2.336.0'\n"
            f"run_one(){{\n{body}}}\n"
            f"run_one 1 {LABELS!r} 0\n"
            "exit $?\n",
            encoding="utf-8",
        )
        harness.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "PATH": os.pathsep.join([str(self.bin), env.get("PATH", "/usr/bin:/bin")]),
                "TARTCI_ROOT": str(ROOT),
                "TARTCI_ADMISSION_CLEAN_MODE": "required",
                "TARTCI_SHIPYARD_CLI": "stub-shipyard",
                # Isolate the inconclusive breaker's counter: a shared one would
                # let an earlier test degrade a later test into an admit.
                "TARTCI_ADMISSION_CLEAN_STATE_DIR": str(self.tmp / "breaker"),
            }
        )
        return subprocess.run(
            ["/bin/bash", str(harness)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=120,
        )

    def event_names(self) -> list[str]:
        if not self.events.exists():
            return []
        return [
            line.split("\t", 1)[0]
            for line in self.events.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def detail_for(self, name: str) -> str:
        for line in self.events.read_text(encoding="utf-8").splitlines():
            kind, _, detail = line.partition("\t")
            if kind == name:
                return detail
        raise AssertionError(f"no {name} event in {self.event_names()}")


class PrecheckSkipsTheCloneTests(unittest.TestCase):
    """A refused verdict must cost a shell-out, not a VM."""

    def test_inconclusive_error_never_reaches_the_clone(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            harness = RunOneHarness(Path(raw))
            harness.stub_shipyard(
                make_envelope("error", "observation_failed", error=LIVE_ERROR), 1
            )
            result = harness.run()
            self.assertEqual(result.returncode, 1, result.stderr)
            names = harness.event_names()
            self.assertIn("admission_precheck", names)
            self.assertIn("admission_precheck_error", names)
            self.assertNotIn(
                "clone_start",
                names,
                "a refused admission verdict still paid for a VM clone",
            )

    def test_defer_never_reaches_the_clone_and_keeps_its_typed_rc(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            harness = RunOneHarness(Path(raw))
            harness.stub_shipyard(
                make_envelope("defer", "stale_compatible_runs"), 3
            )
            result = harness.run()
            self.assertEqual(result.returncode, 3, result.stderr)
            names = harness.event_names()
            self.assertIn("admission_precheck_deferred", names)
            self.assertNotIn("clone_start", names)
            self.assertIn("admission-precheck-deferred", harness.heartbeats.read_text())

    def test_admit_reaches_the_clone(self) -> None:
        """The control. Without it, a probe that refused everything would pass
        both tests above while starving the lane."""
        with tempfile.TemporaryDirectory() as raw:
            harness = RunOneHarness(Path(raw))
            harness.stub_shipyard(make_envelope("admit", "clean"), 0)
            result = harness.run()
            self.assertEqual(
                result.returncode,
                CLONE_REACHED_EXIT,
                f"an admitted verdict must still clone: {result.stderr}",
            )
            self.assertIn("clone_start", harness.event_names())

    def test_probe_runs_before_the_clone_and_the_gate_still_runs_after_boot(
        self,
    ) -> None:
        source = MACOS_RUNNER.read_text(encoding="utf-8")
        body = function_body(source, "run_one")
        probe = body.index('precheck_json="$(tartci_admission_clean')
        clone = body.index("event clone_start")
        booted = body.index("t_booted=")
        gate = body.index('admission_json="$(tartci_admission_clean')
        mint = body.index("generate-jitconfig")
        self.assertLess(probe, clone, "the probe no longer precedes the clone")
        self.assertLess(clone, booted)
        self.assertLess(booted, gate, "the authoritative gate moved before boot")
        self.assertLess(gate, mint)


class RefusalReportsItsReasonTests(unittest.TestCase):
    """`rc=1 unregistered=true` named the exit code and nothing else."""

    def test_precheck_event_carries_reason_and_bounded_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            harness = RunOneHarness(Path(raw))
            harness.stub_shipyard(
                make_envelope("error", "observation_failed", error=LIVE_ERROR), 1
            )
            self.assertEqual(harness.run().returncode, 1)
            detail = harness.detail_for("admission_precheck_error")
            self.assertIn("reason=observation_failed", detail)
            self.assertIn("error=open PR list failed:", detail)
            self.assertIn("...", detail)
            self.assertNotIn(
                "Sorry about that.",
                detail,
                "the error was not bounded and will bloat every event",
            )
            self.assertEqual(len(detail.splitlines()), 1)

    def test_note_also_carries_the_reason(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            harness = RunOneHarness(Path(raw))
            harness.stub_shipyard(
                make_envelope("error", "observation_failed", error=LIVE_ERROR), 1
            )
            self.assertEqual(harness.run().returncode, 1)
            self.assertIn(
                "reason=observation_failed", harness.notes.read_text(encoding="utf-8")
            )

    def test_boundary_refusal_event_renders_the_same_detail(self) -> None:
        # The at-boundary refusal is downstream of a full boot, so its block is
        # sliced out and RUN rather than driven through `run_one`. Asserting
        # the renderer merely appears somewhere in the block is not enough: the
        # note interpolates it too, so such a test stays green while the event
        # goes back to reporting only its exit code. Read the event.
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            events = tmp / "events.tsv"
            notes = tmp / "notes.log"
            block = boundary_gate_block(MACOS_RUNNER.read_text(encoding="utf-8"))
            verdict = tmp / "verdict.json"
            verdict.write_text(
                json.dumps(
                    make_envelope(
                        "error", "observation_failed", error=LIVE_ERROR
                    ),
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            stub = tmp / "stub-shipyard"
            stub.write_text(
                f"#!/bin/bash\ncat {str(verdict)!r}\nprintf '\\n'\nexit 1\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            harness = tmp / "boundary.sh"
            harness.write_text(
                "#!/bin/bash\nset -euo pipefail\n"
                f"TARTCI_ROOT={str(ROOT)!r}\n"
                f"source {str(LIB)!r}\n"
                f"note(){{ printf '%s\\n' \"$*\" >>{str(notes)!r}; }}\n"
                "heartbeat(){ :; }\n"
                f"event(){{ printf '%s\\t%s\\n' \"$1\" \"${{2:-}}\" >>{str(events)!r}; }}\n"
                "discard_current_vm(){ :; }\n"
                "tartci_release_vm_lease(){ :; }\n"
                f"REPO={REPO!r}\n"
                "i=1\n"
                "vm='lane-vm-1'\n"
                f"selected_labels={LABELS!r}\n"
                f"STATE_DIR={str(tmp)!r}\n"
                f"boundary(){{\n{block}}}\n"
                "boundary\n"
                "exit $?\n",
                encoding="utf-8",
            )
            harness.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": os.pathsep.join(
                        [str(tmp), env.get("PATH", "/usr/bin:/bin")]
                    ),
                    "TARTCI_ROOT": str(ROOT),
                    "TARTCI_ADMISSION_CLEAN_MODE": "required",
                    "TARTCI_SHIPYARD_CLI": "stub-shipyard",
                    "TARTCI_ADMISSION_CLEAN_STATE_DIR": str(tmp / "breaker"),
                }
            )
            result = subprocess.run(
                ["/bin/bash", str(harness)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=120,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            emitted = dict(
                line.split("\t", 1)
                for line in events.read_text(encoding="utf-8").splitlines()
                if "\t" in line
            )
            self.assertIn("admission_error", emitted, emitted)
            self.assertIn("reason=observation_failed", emitted["admission_error"])
            self.assertIn("error=open PR list failed:", emitted["admission_error"])
            self.assertIn(
                "reason=observation_failed", notes.read_text(encoding="utf-8")
            )

    def test_every_provider_reports_a_reason_when_it_refuses(self) -> None:
        # The invariant that did not transfer between call sites is the whole
        # failure mode; assert it at all three rather than only the one that
        # burned an hour.
        for provider in (
            ROOT / "providers/tart-linux/runner.sh",
            ROOT / "providers/tart-macos/runner.sh",
            ROOT / "providers/qemu-windows/runner.sh",
        ):
            with self.subTest(provider=provider.parent.name):
                self.assertIn(
                    'tartci_admission_clean_detail "$admission_json"',
                    provider.read_text(encoding="utf-8"),
                )


class DetailRendererTests(unittest.TestCase):
    """The renderer classifies on the typed reason, never on the message."""

    def render(self, payload: str, *, limit: int | None = None) -> str:
        argv = [str(DETAIL)]
        if limit is not None:
            argv += ["--max-error-chars", str(limit)]
        result = subprocess.run(
            ["python3", "-B", *argv],
            input=payload,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_truncates_at_the_requested_bound(self) -> None:
        rendered = self.render(
            json.dumps(make_envelope("error", "observation_failed", error="x" * 500)),
            limit=120,
        )
        self.assertEqual(rendered, f"reason=observation_failed error={'x' * 120}...")

    def test_untruncated_error_carries_no_marker(self) -> None:
        rendered = self.render(
            json.dumps(make_envelope("error", "mutation_failed", error="short"))
        )
        self.assertEqual(rendered, "reason=mutation_failed error=short")

    def test_distinct_inconclusive_spellings_share_one_classification(self) -> None:
        # The same outcome arrives as a 504, as a truncated body, or as a
        # timeout. Anything keyed on the message would split them; the typed
        # reason does not.
        spellings = (
            "HTTP 504: We couldn't respond to your request in time.",
            "unexpected end of JSON input",
            "context deadline exceeded",
        )
        rendered = [
            self.render(
                json.dumps(
                    make_envelope("error", "observation_failed", error=spelling)
                )
            )
            for spelling in spellings
        ]
        self.assertEqual(
            {line.split(" ", 1)[0] for line in rendered},
            {"reason=observation_failed"},
        )

    def test_multiline_error_becomes_one_line(self) -> None:
        rendered = self.render(
            json.dumps(
                make_envelope("error", "authority_failed", error="a\nb\tc\r\n  d")
            )
        )
        self.assertEqual(rendered, "reason=authority_failed error=a b c d")

    def test_defer_without_an_error_reports_only_its_reason(self) -> None:
        rendered = self.render(
            json.dumps(make_envelope("defer", "cancellation_pending"))
        )
        self.assertEqual(rendered, "reason=cancellation_pending")

    def test_malformed_input_renders_rather_than_fails(self) -> None:
        self.assertEqual(self.render(""), "reason=missing")
        self.assertEqual(self.render("not json"), "reason=unreadable")
        self.assertEqual(self.render("[1,2,3]"), "reason=unreadable")
        self.assertEqual(self.render('{"reason": 7}'), "reason=unknown")
        self.assertEqual(self.render('{"reason": "Bad Reason"}'), "reason=unknown")

    def test_shell_wrapper_fails_open_without_python(self) -> None:
        # The renderer runs on the failure path. If it can abort, it takes the
        # refusal's own reporting down with it.
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f"set -u; TARTCI_ROOT={str(ROOT)!r}; source {str(LIB)!r}; "
                'tartci_admission_clean_detail \'{"reason":"observation_failed"}\'',
            ],
            env={"PATH": "/nonexistent", "HOME": os.environ.get("HOME", "/tmp")},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "reason=unreadable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
