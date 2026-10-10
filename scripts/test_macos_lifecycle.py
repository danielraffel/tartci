#!/usr/bin/env python3
"""Per-VM lifecycle timings on macOS lanes (lifecycle.lib.sh, pool_usage lifecycle).

Every number is computed by hand from the fixture: a served VM and an unserved
one whose boundaries are set as run_one sets them, so a change in what a phase
spans fails here instead of shifting the overhead split nobody re-derives.
"""
from __future__ import annotations

import io
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import pool_usage as pu  # noqa: E402
from test_pool_usage import UNTIL, ev, report, write_lane  # noqa: E402

LIB = ROOT / "providers" / "tart-macos" / "lifecycle.lib.sh"
RUNNER = ROOT / "providers" / "tart-macos" / "runner.sh"
T = 2_000_000_000

# A served VM: run_one starts at T. Phases by hand: pre_clone 60, clone 30,
# boot_ip 60, ip_ssh 30, prep 60, register 20, idle 40, job 1200, teardown 40,
# total 1540.
SERVED = dict(LC_CLONE_START=T + 60, LC_CLONED=T + 90, LC_IP=T + 150, LC_SSH=T + 180,
              LC_MINTED=T + 240, LC_LISTENING=T + 260, LC_ASSIGNED=T + 300, LC_WARM=0)
SERVED_TIMES = (T, T + 1500, T + 1540)
# The same VM never assigned: idle runs to the runner's exit at T + 860.
UNSERVED = {**SERVED, "LC_ASSIGNED": 0}
UNSERVED_TIMES = (T, T + 860, T + 900)


def emit(stamps: dict, times: tuple, rc: int = 0) -> list[str]:
    """Run tartci_lifecycle_emit with `event` captured; returns its argv."""
    assigns = " ".join(f"{k}={v}" for k, v in stamps.items())
    script = (f'source "{LIB}"\n'
              'event(){ printf "%s\\n" "$@"; }\n'
              f'tartci_lifecycle_reset; {assigns}\n'
              f'tartci_lifecycle_emit {times[0]} {times[1]} {times[2]} {rc}\n')
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def fields(argv: list[str]) -> dict:
    """The event's structured fields as runner.sh's `event` would write them."""
    out = {}
    for pair in argv[2:]:
        key, _, value = pair.partition("=")
        out[key] = int(value) if value.isdigit() else value
    return out


class LifecycleLib(unittest.TestCase):
    def test_a_served_vm_reports_every_phase(self):
        argv = emit(SERVED, SERVED_TIMES)
        self.assertEqual(argv[0], "vm_lifecycle")
        self.assertEqual(fields(argv), {
            "served": 1, "warm": 0, "rc": 0, "pre_clone_s": 60, "clone_s": 30,
            "boot_ip_s": 60, "ip_ssh_s": 30, "prep_s": 60, "register_s": 20,
            "idle_s": 40, "job_s": 1200, "teardown_s": 40, "total_s": 1540})
        self.assertTrue(argv[1].startswith("served=1 warm=0 rc=0 pre_clone_s=60 "), argv[1])

    def test_an_unserved_vm_is_idle_to_the_runner_exit_and_has_no_job(self):
        got = fields(emit(UNSERVED, UNSERVED_TIMES, rc=124))
        self.assertEqual((got["served"], got["idle_s"], got["teardown_s"], got["rc"]),
                         (0, 600, 40, 124))
        self.assertNotIn("job_s", got)

    def test_a_warm_handoff_has_no_clone_or_boot_and_prep_starts_at_run_one(self):
        warm = {**SERVED, "LC_CLONE_START": 0, "LC_CLONED": 0, "LC_IP": 0, "LC_SSH": 0,
                "LC_WARM": 1}
        got = fields(emit(warm, SERVED_TIMES))
        for phase in ("pre_clone_s", "clone_s", "boot_ip_s", "ip_ssh_s"):
            self.assertNotIn(phase, got)
        self.assertEqual((got["warm"], got["prep_s"]), (1, 240))

    def test_mark_keeps_the_first_sighting(self):
        script = (f'source "{LIB}"\ntartci_lifecycle_reset\n'
                  f'tartci_lifecycle_unmarked listening && echo unmarked\n'
                  f'tartci_lifecycle_mark listening {T + 5}\n'
                  f'tartci_lifecycle_mark listening {T + 9}\n'
                  'tartci_lifecycle_unmarked listening || echo marked\n'
                  'tartci_lifecycle_mark warm\n'
                  'printf "%s %s\\n" "$LC_LISTENING" "$LC_WARM"\n')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split(), ["unmarked", "marked", str(T + 5), "1"])

    def test_mark_keeps_the_first_sighting_for_every_boundary(self):
        variables = {
            "clone_start": "LC_CLONE_START", "cloned": "LC_CLONED", "ip": "LC_IP",
            "ssh": "LC_SSH", "minted": "LC_MINTED", "listening": "LC_LISTENING",
            "assigned": "LC_ASSIGNED",
        }
        for name, variable in variables.items():
            script = (f'source "{LIB}"\ntartci_lifecycle_reset\n'
                      f'tartci_lifecycle_mark {name} {T + 5}\n'
                      f'tartci_lifecycle_mark {name} {T + 9}\n'
                      f'printf "%s\\n" "${variable}"\n')
            out = subprocess.run(["bash", "-c", script], capture_output=True,
                                 text=True, check=True)
            self.assertEqual(out.stdout.strip(), str(T + 5), name)

    def test_reset_clears_the_previous_vm(self):
        script = (f'source "{LIB}"\nevent(){{ printf "%s\\n" "$@"; }}\n'
                  f'LC_ASSIGNED={T}; LC_CLONED={T}; CLONE_STARTED_AT={T}\n'
                  'tartci_lifecycle_reset\n'
                  'printf "%s %s %s|\\n" "$LC_ASSIGNED" "$LC_CLONED" "$CLONE_STARTED_AT"\n')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "0 0 |")

    def test_timing_rows_parse_as_timing_tsv(self):
        import timing_lib
        assigns = " ".join(f"{k}={v}" for k, v in SERVED.items())
        script = (f'source "{LIB}"\ntartci_lifecycle_reset; {assigns}\n'
                  'printf "phase\\tseconds\\n"\n'
                  f'tartci_lifecycle_tsv_rows {SERVED_TIMES[0]} {SERVED_TIMES[1]} {SERVED_TIMES[2]}\n'
                  'printf "total\\t1540\\n"\n')
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "timing.tsv"
            path.write_text(out.stdout)
            phases = timing_lib.parse_timing(path)
        self.assertEqual((phases["lc_job"], phases["lc_clone"], phases["total"]),
                         (1200.0, 30.0, 1540.0))
        self.assertNotIn("lc_total", phases)


class RunnerWiring(unittest.TestCase):
    """runner.sh stamps each boundary where it happens and emits on the served path."""

    def setUp(self):
        self.src = RUNNER.read_text()

    def after(self, anchor: str, stamp: str, within: int = 300) -> None:
        at = self.src.index(anchor)
        self.assertIn(stamp, self.src[at:at + within], f"{stamp} must follow {anchor!r}")

    def test_each_boundary_is_stamped_at_its_event(self):
        self.assertIn('source "$TARTCI_ROOT/providers/tart-macos/lifecycle.lib.sh"', self.src)
        self.after('  LAST_RUN_LEASE_DENIED=0\n  tartci_lifecycle_reset', "ephemeral_boot_name")
        self.after('mem_mb=${lease_mem:-golden}"', 'tartci_lifecycle_mark cloned\n', 400)
        self.after('mem_mb=${lease_mem:-golden}"',
                   'tartci_lifecycle_mark clone_start "${CLONE_STARTED_AT:-0}"', 300)
        self.after('event boot_ip "ip=$ip', 'tartci_lifecycle_mark ip\n', 300)
        self.after('event boot_failed "no_ssh"', 'tartci_lifecycle_mark ssh\n', 300)
        self.after('event mint_jit "labels=$selected_labels', 'tartci_lifecycle_mark minted\n', 120)
        self.after("grep -q 'Listening for Jobs'", 'tartci_lifecycle_mark listening "$now"', 160)
        self.after('      assigned_at="$now"\n', 'tartci_lifecycle_mark assigned "$now"', 200)
        self.after('vm="$CURRENT_VM"\n      tartci_lifecycle_mark warm', "tartci_boundary_proof_start")

    def test_running_job_without_listening_stamps_registration_and_zero_idle(self):
        branch = self.src[self.src.index('if [ "$assigned" = 0 ] && grep -q \'Running job:'):]
        branch = branch[:branch.index('    fi', branch.index('tartci_lifecycle_mark assigned'))]
        self.assertIn('tartci_lifecycle_mark listening "$now"', branch)
        self.assertLess(branch.index('tartci_lifecycle_mark listening'),
                        branch.index('tartci_lifecycle_mark assigned'))
        stamps = {**SERVED, "LC_LISTENING": T + 300, "LC_ASSIGNED": T + 300}
        got = fields(emit(stamps, SERVED_TIMES))
        self.assertEqual((got["register_s"], got["idle_s"]), (60, 0))

    def test_the_event_is_written_after_teardown_and_before_timing(self):
        emit_at = self.src.index('tartci_lifecycle_emit "$t_start" "$t_runner_done" "$t_done" "$rc"')
        self.assertLess(self.src.index('event teardown "rc=$rc"'), emit_at)
        self.assertLess(self.src.index('  t_done="$(now_epoch)"\n  tartci_lifecycle_emit'), emit_at)
        self.assertLess(emit_at, self.src.index('tartci_lifecycle_tsv_rows "$t_start"'))


class UsageReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def lane(self, extra: tuple = ()) -> None:
        served = fields(emit(SERVED, SERVED_TIMES))
        unserved = fields(emit(UNSERVED, UNSERVED_TIMES, rc=124))
        write_lane(self.root, "a", [
            ev("11:00:00", "clone_start"),
            ev("11:25:40", "vm_lifecycle", vm="v1", detail="served=1", **served),
            ev("11:30:00", "clone_start"),
            ev("11:31:00", "assignment_v2_pre_mint_denied", vm="v2"),   # no runner: no event
            ev("11:45:00", "vm_lifecycle", vm="v3", detail="served=0", **unserved),
            # Outside the window: never counted.
            ev("10:10:00", "vm_lifecycle", vm="v0", detail="served=1", **served),
            *extra,
        ])

    def test_the_overhead_split_is_reported_exactly(self):
        self.lane()
        lc = report(self.root)["hosts"][0]["lifecycle"]
        # VM seconds exclude pre_clone: (1540 - 60) + (900 - 60) = 1480 + 840.
        self.assertEqual((lc["vms"], lc["served"], lc["warm"]), (2, 1, 0))
        self.assertEqual(lc["vm_seconds"], 2320.0)
        self.assertEqual(lc["job_seconds"], 1200.0)
        self.assertEqual(lc["job_share"], round(1200 / 2320, 4))
        self.assertEqual(lc["overhead_per_served_job_s"], 1120.0)
        self.assertEqual(lc["phases"]["idle"], {"vms": 2, "seconds": 640.0, "median_s": 320.0})
        self.assertEqual(lc["phases"]["job"], {"vms": 1, "seconds": 1200.0, "median_s": 1200.0})
        self.assertEqual(lc["phases"]["clone"]["seconds"], 60.0)

    def test_text_output_names_the_split(self):
        self.lane()
        out = io.StringIO()
        with redirect_stdout(out):
            pu.main(["--range", "1h", "--until", UNTIL, "--now", UNTIL,
                     "--events-root", str(self.root), "--slots", "2", "--host-label", "h"])
        text = out.getvalue()
        self.assertIn("lifecycle (2 VM(s) that reached a runner, 1 served, 0 warm): job share "
                      "51.7% of 38.7 VM-min; overhead per served job 18.7 min", text)
        self.assertRegex(text, r"phases: pre_clone 2\.0 min \(median 60s, 2\); clone 1\.0 min")

    def test_a_host_without_the_event_reports_none_not_zero_overhead(self):
        # Negative control: the same lane minus its vm_lifecycle events (an
        # older tartci) must not read as a host with no overhead.
        write_lane(self.root, "a", [ev("11:00:00", "clone_start"),
                                    ev("11:20:00", "teardown", vm="v1", detail="rc=0")])
        host = report(self.root)["hosts"][0]
        lc = host["lifecycle"]
        self.assertEqual((lc["vms"], lc["job_share"], lc["overhead_per_served_job_s"]),
                         (0, None, None))
        self.assertIn("lifecycle: no vm_lifecycle events in the window",
                      "\n".join(pu.render_host(host)))

    def test_the_fleet_total_sums_hosts(self):
        self.lane()
        host = report(self.root)["hosts"][0]
        fleet = pu.fleet_total([host, {**host, "host": "h2"}, {"host": "h3", "error": "x"}])
        lc = fleet["lifecycle"]
        self.assertEqual((lc["vms"], lc["served"], lc["vm_seconds"], lc["job_seconds"]),
                         (4, 2, 4640.0, 2400.0))
        self.assertEqual(lc["overhead_per_served_job_s"], 1120.0)
        self.assertEqual(lc["phase_seconds"]["teardown"], 160.0)


if __name__ == "__main__":
    unittest.main()
