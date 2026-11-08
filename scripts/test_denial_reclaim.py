#!/usr/bin/env python3
"""A VM lease refused for disk frees regenerable caches once, then retries once.

Pins (m1, 2026-11-08: two Pulp gate slots idle for hours on a disk refusal
nothing reclaimed): the reclaim deletes only OCI cache images nothing
references and old gate ccache entries, never a local VM; it runs at most
once per interval per host; the lane retries admission exactly once and only
when the reclaim ran; and every failure leaves the refusal as it was.

Run:  python3 scripts/test_denial_reclaim.py
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import denial_reclaim as dr  # noqa: E402
import disk_headroom  # noqa: E402
import fleet_doctor  # noqa: E402
import pulp_reapers  # noqa: E402
import testing_support  # noqa: E402

HELPER = ROOT / "providers" / "common" / "vm-lease.lib.sh"
NOW = 2_000_000_000.0
OLD = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 10 * 86400))
RECENT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 3600))
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def oci(name: str, accessed: str = OLD, running: bool = False, source: str = "OCI") -> dict:
    return {"Name": name, "Source": source, "Accessed": accessed, "Running": running,
            "Size": 40, "Disk": 100, "State": "stopped"}


class Plan(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.oci_root = self.tmp / "cache" / "OCIs"

    def link(self, name: str, digest: str) -> None:
        repo, tag, _ = dr.split_oci(name)
        directory = self.oci_root / repo
        directory.mkdir(parents=True, exist_ok=True)
        (directory / digest).mkdir(exist_ok=True)
        os.symlink(str(directory / digest), str(directory / tag))

    def verdicts(self, entries, *, values=(), text="") -> dict:
        rows = dr.plan_oci(entries, values=set(values), text=text, oci_root=self.oci_root,
                           now=NOW, min_idle_hours=24)
        return {row["name"]: row["verdict"] for row in rows}

    def test_only_unreferenced_oci_images_are_deleted(self):
        self.link("ghcr.io/org/golden:latest", DIGEST_A)
        got = self.verdicts([
            oci("ghcr.io/org/golden:latest"),
            oci(f"ghcr.io/org/golden@{DIGEST_A}"),
            oci(f"ghcr.io/org/golden@{DIGEST_B}"),
            oci("ghcr.io/cirruslabs/macos-sonoma-base:latest"),
            oci(f"ghcr.io/cirruslabs/macos-sonoma-base@{DIGEST_B}"),
        ], text='golden = "ghcr.io/org/golden:latest"\n')
        self.assertEqual(got, {
            "ghcr.io/org/golden:latest": "keep",
            f"ghcr.io/org/golden@{DIGEST_A}": "keep",    # what the golden tag points at
            f"ghcr.io/org/golden@{DIGEST_B}": "delete",  # an old digest of it
            "ghcr.io/cirruslabs/macos-sonoma-base:latest": "delete",
            f"ghcr.io/cirruslabs/macos-sonoma-base@{DIGEST_B}": "delete",
        })

    def test_a_profile_value_keeps_its_image(self):
        got = self.verdicts([oci("ghcr.io/org/lane-image:v2")],
                            values={"ghcr.io/org/lane-image:v2"})
        self.assertEqual(got["ghcr.io/org/lane-image:v2"], "keep")

    def test_a_referenced_tag_that_cannot_be_resolved_keeps_every_digest(self):
        got = self.verdicts([oci("ghcr.io/org/golden:latest"),
                             oci(f"ghcr.io/org/golden@{DIGEST_B}")],
                            text="ghcr.io/org/golden:latest")
        self.assertEqual(got[f"ghcr.io/org/golden@{DIGEST_B}"], "keep")

    def test_a_bare_repository_reference_keeps_the_repository(self):
        got = self.verdicts([oci("ghcr.io/org/golden:old")], text="pull ghcr.io/org/golden now")
        self.assertEqual(got["ghcr.io/org/golden:old"], "keep")

    def test_local_vms_running_and_recent_images_are_never_deleted(self):
        got = self.verdicts([
            oci("pulp-build-runner:latest", source="local"),
            oci("pulp-build-runner:latest"),            # an OCI row without a registry
            oci("m1-pulp-gate-01-abc"),
            oci("ghcr.io/org/busy:1", running=True),
            oci("ghcr.io/org/fresh:1", accessed=RECENT),
            oci("ghcr.io/org/unknown:1", accessed=""),
        ])
        self.assertEqual(set(got.values()), {"keep"})

    def test_tags_are_deleted_before_digests(self):
        rows = dr.plan_oci([oci(f"ghcr.io/x/y@{DIGEST_A}"), oci("ghcr.io/x/y:1")],
                           values=set(), text="", oci_root=self.oci_root, now=NOW,
                           min_idle_hours=24)
        self.assertEqual([r["name"] for r in rows], ["ghcr.io/x/y:1", f"ghcr.io/x/y@{DIGEST_A}"])


class FakeHost:
    """Answers ps, tart list/delete and ccache like a disk-full host would."""

    def __init__(self, entries: list[dict], *, tart_running: bool = False,
                 busy_after_ps: int | None = None) -> None:
        self.entries = entries
        self.calls: list[list[str]] = []
        self.tart_running = tart_running
        self.busy_after_ps = busy_after_ps
        self.ps_calls = 0

    def __call__(self, argv, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(argv))
        out = ""
        if argv[0] == "ps":
            self.ps_calls += 1
            busy = self.tart_running or (self.busy_after_ps is not None
                                         and self.ps_calls > self.busy_after_ps)
            out = "/usr/sbin/sshd\n" + ("/opt/homebrew/bin/tart pull ghcr.io/x/y:1\n"
                                        if busy else "")
        elif argv[:2] == ["tart", "list"]:
            out = json.dumps(self.entries)
        return subprocess.CompletedProcess(argv, 0, out, "")

    def deleted(self) -> list[str]:
        return [c[2] for c in self.calls if c[:2] == ["tart", "delete"]]

    def evictions(self) -> list[list[str]]:
        return [c for c in self.calls if "--evict-older-than" in c]


class Run(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = self.tmp / "ci" / "ccache"
        self.cache.mkdir(parents=True)
        self.store = self.tmp / "VMs"
        self.store.mkdir()
        self.profile = self.tmp / "profile.toml"
        self.profile.write_text(
            f'[host]\ntart_home = "{self.store}"\ncache_root = "{self.tmp / "ci"}"\n'
            '[[lane]]\ngolden = "ghcr.io/org/golden:latest"\n')
        self.env = {"HOME": str(self.tmp)}

    def args(self, **over) -> argparse.Namespace:
        values = dict(fix=True, lane="m1-pulp-gate", required_bytes=52 * 2**30,
                      measure_path=str(self.store), tart_home=[str(self.store)],
                      profile=str(self.profile), state_dir=str(self.tmp / "state"),
                      budget_seconds=600.0, oci_min_idle_hours=24.0)
        values.update(over)
        return argparse.Namespace(**values)

    def run_once(self, host: FakeHost, *, now: float = NOW, env: dict | None = None,
                 busy: str | None = None, store_probe=lambda path: None, **over) -> dict:
        return dr.run(self.args(**over), env={**self.env, **(env or {})}, runner=host,
                      now=now, busy_probe=lambda: busy, ccache="/bin/echo",
                      store_probe=store_probe)

    # The profile's cache_root is read with tomllib; under 3.9 the lane's
    # TARTCI_CI_CACHE (rendered into every runner plist) names the cache.
    @testing_support.requires_tomllib
    def test_a_run_deletes_unreferenced_images_and_evicts_old_ccache(self):
        host = FakeHost([oci("ghcr.io/org/golden:latest"), oci("ghcr.io/org/stale:1")])
        report = self.run_once(host)
        self.assertEqual(report["status"], "ran")
        self.assertEqual(host.deleted(), ["ghcr.io/org/stale:1"])
        self.assertEqual(host.evictions(),
                         [["/bin/echo", "-d", str(self.cache), "--evict-older-than", "14d"]])
        self.assertEqual(report["ccache"]["status"], "evicted")
        # Every tart call is scoped to the store it judged.
        events = (self.tmp / "state" / dr.EVENTS_NAME).read_text()
        self.assertIn('"event": "reclaim_on_lease_denial"', events)

    @testing_support.requires_tomllib
    def test_the_ccache_age_is_configurable(self):
        self.profile.write_text(self.profile.read_text()
                                + "[reclaim]\ndenial_reclaim_ccache_max_age_days = 21\n")
        host = FakeHost([])
        self.run_once(host)
        self.assertEqual(host.evictions()[0][-1], "21d")

    def test_a_busy_host_keeps_its_ccache(self):
        host = FakeHost([oci("ghcr.io/org/stale:1")])
        report = self.run_once(host, busy="a Tart VM is running")
        self.assertEqual(report["ccache"]["status"], "skipped")
        self.assertEqual(host.evictions(), [])
        self.assertEqual(host.deleted(), ["ghcr.io/org/stale:1"])

    def test_a_running_pull_keeps_every_oci_image(self):
        host = FakeHost([oci("ghcr.io/org/stale:1")], tart_running=True)
        report = self.run_once(host)
        self.assertEqual(report["oci"]["status"], "skipped")
        self.assertEqual(host.deleted(), [])

    def test_a_pull_that_starts_mid_run_stops_the_deletes(self):
        # ps #1 is the pre-plan check, #2 precedes the first delete; a pull
        # that starts after that must stop the second delete.
        host = FakeHost([oci("ghcr.io/org/stale:1"), oci("ghcr.io/org/stale:2")],
                        busy_after_ps=2)
        report = self.run_once(host)
        self.assertEqual(host.deleted(), ["ghcr.io/org/stale:1"])
        self.assertEqual(report["oci"]["status"], "stopped")
        self.assertIn("tart pull", report["oci"]["stopped"])

    def test_an_unhealthy_store_gets_no_tart_call(self):
        host = FakeHost([oci("ghcr.io/org/stale:1")])
        report = self.run_once(host, store_probe=lambda path: "timeout after 10s")
        self.assertEqual([c for c in host.calls if c[0] == "tart"], [])
        self.assertIn("timeout after 10s", report["oci"]["stores"][0]["reason"])

    def test_the_real_store_gate_refuses_a_store_it_cannot_open(self):
        self.assertIsNone(dr.store_unhealthy(self.store, timeout_s=10))
        self.assertTrue(dr.store_unhealthy(self.tmp / "missing", timeout_s=10))
        failing = self.tmp / "store-health.json"
        failing.write_text(json.dumps({"path": str(self.store), "state": "draining",
                                       "consecutive_failures": 3}))
        # One good probe clears a recorded failure, as the watchdog's state does.
        self.assertIsNone(dr.store_unhealthy(self.store, failing, timeout_s=10))

    def test_a_reference_scan_past_its_budget_deletes_nothing(self):
        host = FakeHost([oci("ghcr.io/org/stale:1")])
        report = self.run_once(host, budget_seconds=1e-6)
        self.assertEqual(report["oci"]["status"], "skipped")
        self.assertIn("reference scan", report["oci"]["reason"])
        self.assertEqual(host.deleted(), [])

    def test_the_default_budget_fits_the_stale_boot_bound(self):
        import tartci_launchd_watchdog as watchdog
        self.assertEqual(dr.DEFAULT_BUDGET_S, 300.0)
        self.assertLess(dr.DEFAULT_BUDGET_S * 2, watchdog.DEFAULT_STALE_BOOT_S)
        self.assertIn("disk-denial-reclaim", watchdog.STALE_BOOT_PHASES)

    def test_at_most_once_per_interval(self):
        first = self.run_once(FakeHost([]))
        self.assertEqual(first["status"], "ran")
        host = FakeHost([oci("ghcr.io/org/stale:1")])
        second = self.run_once(host, now=NOW + 29 * 60)
        self.assertEqual(second["status"], "cooldown")
        self.assertEqual(host.calls, [])
        self.assertEqual(self.run_once(FakeHost([]), now=NOW + 31 * 60)["status"], "ran")

    def test_a_second_lane_does_not_run_it_concurrently(self):
        state = self.tmp / "state"
        with dr.try_lock(state / dr.LOCK_NAME) as held:
            self.assertTrue(held)
            host = FakeHost([oci("ghcr.io/org/stale:1")])
            self.assertEqual(self.run_once(host)["status"], "busy")
            self.assertEqual(host.calls, [])

    def test_off_switches(self):
        host = FakeHost([oci("ghcr.io/org/stale:1")])
        self.assertEqual(self.run_once(host, env={"TARTCI_DISK_DENIAL_RECLAIM": "0"})["status"],
                         "disabled")
        self.profile.write_text(self.profile.read_text() + "[reclaim]\ndenial_reclaim = false\n")
        if dr.tomllib is not None:
            self.assertEqual(self.run_once(host)["status"], "disabled")
        self.assertEqual(host.calls, [])

    def test_a_plan_spends_no_cooldown_and_deletes_nothing(self):
        host = FakeHost([oci("ghcr.io/org/stale:1")])
        report = self.run_once(host, fix=False)
        self.assertEqual(report["status"], "planned")
        self.assertEqual(host.deleted(), [])
        self.assertFalse((self.tmp / "state" / dr.STAMP_NAME).exists())

    def test_profile_keys_validate_through_the_reclaim_table(self):
        self.assertEqual(pulp_reapers.validate_table(
            {"denial_reclaim": True, "denial_reclaim_interval_minutes": 30,
             "denial_reclaim_ccache_max_age_days": 14}), [])
        self.assertTrue(pulp_reapers.validate_table({"denial_reclaim_interval_minutes": 1}))

    def test_tartci_reclaim_on_lease_denial_is_this_path(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "disk_reclaim.py"), "--on-lease-denial",
             "--json", "--state-dir", str(self.tmp / "state"), "--profile", str(self.profile)],
            capture_output=True, text=True, check=False,
            env={**os.environ, "TARTCI_DISK_DENIAL_RECLAIM": "0"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["status"], "disabled")


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


DISK_DENIAL = ('{"ok":false,"reason":"disk_capacity_exceeded","exceeded_axis":'
               '{"cores":false,"memory":false,"disk":true},"disk":{"free_bytes":28991029248,'
               '"required_bytes":56371445760,"requested_bytes":25769803776}}')
MEMORY_DENIAL = ('{"ok":false,"reason":"memory_exceeded","exceeded_axis":'
                 '{"cores":false,"memory":true,"disk":false},"disk":null}')


class LeaseWiring(unittest.TestCase):
    """The shipped helper: exactly one retry, only after a reclaim that ran."""

    def acquire(self, denial: str, reclaim_body: str) -> tuple[int, int, bool, str]:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts").mkdir()
            count = root / "count"
            reclaimed = root / "reclaimed"
            _write_exec(root / "scripts/leases.py", f'''#!/usr/bin/env python3
import pathlib
p=pathlib.Path({str(count)!r}); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n))
if n==1: print({denial!r}); raise SystemExit(75)
print('{{"ok":true,"reason":"acquired","disk":{{"free_bytes":60,"required_bytes":50,"requested_bytes":24,"reserved_bytes":0,"device_id":"1"}}}}')
''')
            _write_exec(root / "scripts/denial_reclaim.py",
                        f"#!/usr/bin/env python3\nimport pathlib,sys\n"
                        f"pathlib.Path({str(reclaimed)!r}).write_text(' '.join(sys.argv[1:]))\n"
                        + reclaim_body)
            store = root / "VMs"
            store.mkdir()
            result = subprocess.run(["bash", "-c", f'''
              source {HELPER}; tartci_start_vm_lease_heartbeat() {{ :; }}
              event() {{ printf '%s %s\\n' "$1" "$2" >> {str(root / "events")!r}; }}
              heartbeat() {{ printf 'heartbeat %s\\n' "$1" >> {str(root / "events")!r}; }}
              export TARTCI_ROOT={root} TARTCI_HOME_VOLUME_FLOOR=0
              rc=0; tartci_acquire_vm_lease unit 1 test-vm gate labels '' {store} provider lane runner || rc=$?
              echo "rc=$rc calls=$(cat {count})"
            '''], capture_output=True, text=True, check=False)
            match = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            rc = int(match.split()[0].split("=")[1]) if match.startswith("rc=") else -1
            calls = int(match.split()[1].split("=")[1]) if match.startswith("rc=") else -1
            events = (root / "events").read_text() if (root / "events").exists() else ""
            return rc, calls, reclaimed.exists(), events + result.stderr

    def test_a_reclaim_that_ran_retries_admission_exactly_once(self):
        rc, calls, reclaimed, log = self.acquire(
            DISK_DENIAL, 'print(\'{"status":"ran","freed_bytes":9663676416}\')\n')
        self.assertEqual((rc, calls, reclaimed), (0, 2, True), log)
        self.assertIn("disk_denial_reclaim lane=lane status=ran freed_gib=9.0", log)
        self.assertIn("disk_denial_reclaim_retry rc=0", log)

    def test_cooldown_or_failure_leaves_the_denial_untouched(self):
        for body in ('print(\'{"status":"cooldown"}\')\n',
                     'raise SystemExit(3)\n',
                     'print("not json")\n'):
            with self.subTest(body=body):
                rc, calls, reclaimed, log = self.acquire(DISK_DENIAL, body)
                self.assertEqual((rc, calls, reclaimed), (75, 1, True), log)

    def test_the_lane_heartbeats_before_it_reclaims(self):
        rc, _, _, log = self.acquire(DISK_DENIAL, 'print(\'{"status":"cooldown"}\')\n')
        self.assertEqual(rc, 75)
        self.assertIn("heartbeat disk-denial-reclaim", log)
        self.assertLess(log.index("heartbeat disk-denial-reclaim"),
                        log.index("disk_denial_reclaim lane=lane status=cooldown"))

    def test_a_reclaiming_lane_is_busy_and_blocked_not_idle_or_unknown(self):
        import gate_supply
        import lane_busy
        self.assertIn("disk-denial-reclaim", lane_busy.BUSY_PHASES)
        self.assertNotIn("disk-denial-reclaim", lane_busy.IDLE_PHASES)
        self.assertIn("disk-denial-reclaim", gate_supply.BLOCKED_PHASES)

    def test_a_non_disk_refusal_never_reclaims(self):
        rc, calls, reclaimed, log = self.acquire(
            MEMORY_DENIAL, 'print(\'{"status":"ran"}\')\n')
        self.assertEqual((rc, calls, reclaimed), (75, 1, False), log)

    def test_the_knob_turns_it_off(self):
        os.environ["TARTCI_DISK_DENIAL_RECLAIM"] = "0"
        try:
            rc, calls, reclaimed, _ = self.acquire(DISK_DENIAL, 'print(\'{"status":"ran"}\')\n')
        finally:
            del os.environ["TARTCI_DISK_DENIAL_RECLAIM"]
        self.assertEqual((rc, calls, reclaimed), (75, 1, False))

    def test_the_floor_precheck_also_reclaims_then_remeasures_once(self):
        for reclaim_rc, retry_rc, expected_calls, expected_rc in (
                (0, 0, 2, 0), (1, 0, 1, 75), (0, 75, 2, 75)):
            with self.subTest(reclaim_rc=reclaim_rc, retry_rc=retry_rc):
                result = subprocess.run(["bash", "-c", f'''
                  source {HELPER}; calls=0
                  tartci_check_disk_floor_observed() {{
                    calls=$((calls+1)); TARTCI_LAST_DISK_ADMISSION_ATTEMPT_JSON='{{"fresh":true}}'
                    [ "$calls" -eq 1 ] && return 75
                    return {retry_rc}
                  }}
                  tartci_try_worktree_cleanup() {{ return 1; }}
                  tartci_try_disk_denial_reclaim() {{ return {reclaim_rc}; }}
                  export TART_HOME=/Volumes/Example/VMs
                  rc=0; tartci_check_macos_disk_floor_with_cleanup_once /Volumes/Example/VMs lane runner || rc=$?
                  test "$calls" -eq {expected_calls}; test "$rc" -eq {expected_rc}
                '''], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_grant_that_leaves_no_room_for_the_next_vm_says_so(self):
        rc, _, _, log = self.acquire(DISK_DENIAL, 'print(\'{"status":"ran"}\')\n')
        self.assertEqual(rc, 0)
        self.assertIn("disk_headroom_low", log)


class Headroom(unittest.TestCase):
    GIB = 2 ** 30

    def test_m1_after_the_manual_reclaim_reads_low(self):
        # 56 GiB free, the next VM needs 49 GiB (25 floor + 24 growth).
        self.assertEqual(disk_headroom.judge(56 * self.GIB, 49 * self.GIB, 24 * self.GIB), "low")
        self.assertEqual(disk_headroom.judge(27 * self.GIB, 49 * self.GIB, 24 * self.GIB),
                         "refusing")
        self.assertEqual(disk_headroom.judge(80 * self.GIB, 49 * self.GIB, 24 * self.GIB), "ok")

    def test_status_judges_the_store_like_a_lease(self):
        with tempfile.TemporaryDirectory() as td:
            agents = Path(td) / "agents"
            agents.mkdir()
            value = disk_headroom.status(agents_dir=agents, tart_home=td,
                                         store_dir=Path(td) / "leases")
            self.assertIn(value["state"], ("ok", "low", "refusing"))
            self.assertEqual(value["floor_gb"], 25)
            self.assertEqual(value["growth_gb"], 24)
            self.assertEqual(value["required_bytes"], 49 * self.GIB)

    def test_doctor_codes(self):
        base = {"tart_home": "/Users/x/VMs", "free_bytes": 56 * self.GIB,
                "required_bytes": 49 * self.GIB, "reserved_bytes": 0, "floor_gb": 25,
                "growth_gb": 24, "reservation_count": 0}
        for state, code, verdict in (("low", "vm_disk_headroom_low", fleet_doctor.PROBLEM),
                                     ("refusing", "vm_disk_headroom_refusing",
                                      fleet_doctor.PROBLEM),
                                     ("ok", "vm_disk_headroom_ok", fleet_doctor.OK),
                                     ("unknown", "vm_disk_headroom_unmeasured",
                                      fleet_doctor.UNKNOWN)):
            finding = fleet_doctor.check_vm_disk_headroom({**base, "state": state})
            self.assertEqual((finding.code, finding.state), (code, verdict))
            self.assertIn(code, fleet_doctor.CODES)
        low = fleet_doctor.check_vm_disk_headroom({**base, "state": "low"})
        self.assertIn("56 GiB free, the next VM needs 49 GiB", low.detail)


if __name__ == "__main__":
    unittest.main()
