#!/usr/bin/env python3
"""Gate lanes against each host's own gate reserve: report always, refuse only worse.

m3, 2026-10-04 (#373): two 12-core Pulp gate slots against a 14-core reserve;
with agent builds holding the rest, the second slot was lease-denied 8 times
and macos jobs queued. The fit is computed per host from its live
host-profile. The host facts below were read with `tartci host-profile --json`
on each host on 2026-10-05.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_doctor as fd  # noqa: E402
import gate_reserve_fit as grf  # noqa: E402

try:  # the shipped profiles are TOML; /usr/bin/python3 3.9 has no tomllib
    import tomllib
except ModuleNotFoundError:
    tomllib = None  # type: ignore[assignment]
requires_tomllib = unittest.skipIf(tomllib is None, "reads TOML profiles (Python 3.11+)")

ROOT = Path(__file__).resolve().parents[1]
HOSTS = {
    "m3": {"role": "dedicated-builder", "reserved_gate_cores": 14, "vm_pool_cores": 14,
           "reserved_gate_mem_mb": 44110, "per_compile_job_mem_mb": 1536},
    "m5studio": {"role": "dev-overflow", "reserved_gate_cores": 20, "vm_pool_cores": 6,
                 "reserved_gate_mem_mb": 192196, "per_compile_job_mem_mb": 1536},
    "m1": {"role": "light", "reserved_gate_cores": 3, "vm_pool_cores": 3,
           "reserved_gate_mem_mb": 27648, "per_compile_job_mem_mb": 1536},
    "m5": {"role": "dev-overflow", "reserved_gate_cores": 8, "vm_pool_cores": 6,
           "reserved_gate_mem_mb": 67876, "per_compile_job_mem_mb": 1536},
}


def profile(host: str) -> dict:
    return tomllib.loads((ROOT / "profiles" / f"{host}-macos-fleet.toml").read_text())


def pulp_gate(data: dict) -> dict:
    return next(lane for lane in data["lane"] if lane["id"] == "pulp-gate")


SHARE_LINE = 'vm_cores_from = "gate-reserve"\n'


def old_m5_text() -> str:
    """m5's profile as it was before its Pulp lane sized from the gate reserve."""
    text = (ROOT / "profiles" / "m5-macos-fleet.toml").read_text()
    if text.count(SHARE_LINE) != 1:
        raise AssertionError("m5's pulp-gate lane no longer sets vm_cores_from once")
    return text.replace(SHARE_LINE, "")


def old_m5() -> dict:
    return tomllib.loads(old_m5_text())


class FitTests(unittest.TestCase):
    @requires_tomllib
    def test_m3_m5studio_and_m5_fit_and_m1_overcommits_cores(self) -> None:
        expected = {"m3": [], "m5studio": [], "m5": [],
                    "m1": ["gate_reserve_overcommitted lane=pulp-gate axis=cores demand=6 reserve=3"]}
        for host, lines in expected.items():
            with self.subTest(host=host):
                self.assertEqual(grf.finding_lines(grf.fit(profile(host), HOSTS[host])), lines)

    @requires_tomllib
    def test_control_m5_without_the_reserve_share_overcommits_cores(self) -> None:
        self.assertEqual(grf.finding_lines(grf.fit(old_m5(), HOSTS["m5"])),
                         ["gate_reserve_overcommitted lane=pulp-gate axis=cores "
                          "demand=12 reserve=8"])

    @requires_tomllib
    def test_m5_slots_are_four_cores_each(self) -> None:
        rows = [r for r in grf.fit(profile("m5"), HOSTS["m5"]) if r["lane"] == "pulp-gate"]
        self.assertEqual({r["axis"]: (r["demand"], r["reserve"]) for r in rows},
                         {"cores": (8, 8), "memory": (2 * grf.vm_mem_mb(4), 67876)})

    def test_an_explicit_vm_lane_is_not_a_gate_lane(self) -> None:
        data = {"lane": [{"id": "a", "priority": "vm", "supervisors": 2},
                         {"id": "b", "priority": "gate"}, {"id": "c"}]}
        self.assertEqual([lane["id"] for lane in grf.gate_lanes(data)], ["b", "c"])

    def test_vm_memory_matches_the_shell_derivation(self) -> None:
        script = (f"TARTCI_ROOT={ROOT}; . {ROOT}/providers/common/vm-lease.lib.sh; "
                  "tartci_profile_value(){ printf 1536; }; "
                  "for c in 1 3 6 7 8 14; do tartci_vm_lease_derived_mem_mb $c; echo; done")
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             timeout=30).stdout.split()
        self.assertEqual([int(v) for v in out], [grf.vm_mem_mb(c) for c in (1, 3, 6, 7, 8, 14)])


class ShareCoresTests(unittest.TestCase):
    """share_cores: the largest per-slot size whose slots fit the reserve on both axes."""

    def test_each_hosts_share(self) -> None:
        # m3's facts give the 7 that #373 chose by hand; m5studio's reserve is
        # larger than its pool, so the pool stays the size.
        self.assertEqual({host: grf.share_cores(facts, 2) for host, facts in HOSTS.items()},
                         {"m3": 7, "m5studio": 6, "m1": 1, "m5": 4})
        self.assertEqual(grf.share_cores(HOSTS["m5"], 1), 6)

    def test_memory_can_bind_before_cores(self) -> None:
        # 7 cores fit 14 by cores, but 2 x 12288 MB does not fit 20000 MB;
        # 6 cores need 2 x 10240 = 20480, still too much; 5 cores 2 x 8192 fit.
        host = {"reserved_gate_cores": 14, "vm_pool_cores": 14, "reserved_gate_mem_mb": 20000}
        self.assertEqual(grf.share_cores(host, 2), 5)
        lanes = {"lane": [{"id": "g", "supervisors": 2, "vm_cores_from": "gate-reserve"}]}
        self.assertEqual(grf.finding_lines(grf.fit(lanes, host)), [])

    def test_no_reserve_keeps_the_pool_and_a_tiny_reserve_never_goes_below_one(self) -> None:
        self.assertEqual(grf.share_cores({"reserved_gate_cores": 0, "vm_pool_cores": 6}, 2), 6)
        self.assertEqual(grf.share_cores({"reserved_gate_cores": 1, "vm_pool_cores": 6}, 2), 1)

    def test_an_explicit_vm_cores_wins(self) -> None:
        lane = {"vm_cores": 5, "vm_cores_from": "gate-reserve", "supervisors": 2}
        self.assertEqual(grf.lane_vm_cores(lane, HOSTS["m5"]), 5)
        self.assertEqual(grf.lane_vm_cores({"supervisors": 2}, HOSTS["m5"]), 6)

    def test_share_cores_cli_reads_a_host_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            facts = Path(td) / "host.json"
            facts.write_text(json.dumps(HOSTS["m5"]))
            res = subprocess.run([sys.executable, "scripts/gate_reserve_fit.py", "share-cores",
                                  "--slots", "2", "--host-json", str(facts)],
                                 cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "4"), res.stderr)


class MemoryAxisTests(unittest.TestCase):
    """Pure fixtures (no TOML), so these run under the hosts' Python 3.9 too.

    A host with a small gate memory reserve: two 7-core slots need 2 x 12288 MB
    and fit 14 cores, but not 20000 MB.
    """
    HOST = {"reserved_gate_cores": 14, "vm_pool_cores": 14,
            "reserved_gate_mem_mb": 20000, "per_compile_job_mem_mb": 1536}

    def lane(self, cores: int) -> dict:
        return {"lane": [{"id": "pulp-gate", "vm_cores": cores, "supervisors": 2}]}

    def test_memory_overcommits_while_cores_fit(self) -> None:
        self.assertEqual(grf.finding_lines(grf.fit(self.lane(7), self.HOST)),
                         ["gate_reserve_overcommitted lane=pulp-gate axis=memory "
                          "demand=24576 reserve=20000"])

    def test_a_target_that_grows_memory_is_refused_on_the_memory_axis(self) -> None:
        # 8 cores: 2 x 14336 MB = 28672; cores 16 > 14 too. Each axis ratchets
        # on its own, so both refusals are named.
        rows, refusals = grf.ratchet(self.lane(7), self.lane(8), self.HOST)
        self.assertIn("gate_reserve_worse lane=pulp-gate axis=memory installed_over=4576 "
                      "target_over=8672 reserve=20000", refusals)
        _, same = grf.ratchet(self.lane(7), self.lane(7), self.HOST)
        self.assertEqual(same, [])


class NoReserveTests(unittest.TestCase):
    """A host that reserves no gate cores cannot be measured: it reads n/a, never fits.

    A CI runner or a small host: host-profile gives reserved_gate_cores 0. The
    fit has no reserve to put the slots in, so neither "every gate lane fits"
    nor "OVERCOMMITTED" is true.
    """
    HOST = {"reserved_gate_cores": 0, "vm_pool_cores": 1, "reserved_gate_mem_mb": 0}
    LANES = {"lane": [{"id": "pulp-gate", "supervisors": 2, "vm_cores": 7}]}
    PROFILE = '[[lane]]\nid = "pulp-gate"\nsupervisors = 2\nvm_cores = 7\n'

    def test_no_reserve_is_named_not_applicable(self) -> None:
        self.assertEqual(grf.fit(self.LANES, self.HOST), [])
        self.assertEqual(grf.not_applicable_lines(self.LANES, self.HOST),
                         ["gate reserve: n/a (this host reserves no gate cores)"])
        self.assertEqual(grf.ratchet(self.LANES, self.LANES, self.HOST), ([], []))

    def test_no_gate_lanes_has_nothing_to_call_not_applicable(self) -> None:
        self.assertEqual(grf.not_applicable_lines({"lane": []}, self.HOST), [])

    def test_an_unread_memory_reserve_names_the_memory_axis(self) -> None:
        host = {"reserved_gate_cores": 14, "vm_pool_cores": 14, "reserved_gate_mem_mb": 0}
        self.assertEqual(grf.not_applicable_lines(self.LANES, host),
                         ["gate reserve: memory axis n/a (this host reports no gate "
                          "memory reserve)"])
        self.assertEqual([r["axis"] for r in grf.fit(self.LANES, host)], ["cores"])

    def summary(self, host: dict) -> dict:
        import macos_fleet_lanes
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(macos_fleet_lanes, "live_host_profile", return_value=host):
            config = Path(td) / "profile.toml"
            config.write_text(self.PROFILE)
            return macos_fleet_lanes.gate_reserve_summary(config)

    @requires_tomllib
    def test_summary_and_doctor_say_not_applicable_not_fits(self) -> None:
        value = self.summary(self.HOST)
        self.assertEqual(value, {"lines": ["gate reserve: n/a (this host reserves no gate "
                                           "cores)"], "problem": None})
        finding = fd.check_gate_reserve(value, installed_present=True)
        self.assertEqual((finding.state, finding.code),
                         (fd.NOT_APPLICABLE, "gate_reserve_not_applicable"))
        self.assertIn("reserves no gate cores", finding.detail)

    @requires_tomllib
    def test_summary_carries_the_memory_n_a_beside_the_cores_fit(self) -> None:
        value = self.summary({"reserved_gate_cores": 14, "vm_pool_cores": 14,
                              "reserved_gate_mem_mb": 0})
        self.assertEqual(value["lines"], [
            "gate reserve: every gate lane fits",
            "gate reserve: memory axis n/a (this host reports no gate memory reserve)"])
        self.assertEqual(fd.check_gate_reserve(value, installed_present=True).code,
                         "gate_reserve_fits")


class RatchetTests(unittest.TestCase):
    @requires_tomllib
    def test_the_373_sizing_is_refused_against_the_installed_profile(self) -> None:
        installed, target = profile("m3"), profile("m3")
        pulp_gate(target)["vm_cores"] = 12
        rows, refusals = grf.ratchet(installed, target, HOSTS["m3"])
        self.assertEqual(refusals, ["gate_reserve_worse lane=pulp-gate axis=cores "
                                    "installed_over=0 target_over=10 reserve=14"])

    @requires_tomllib
    def test_m1_and_the_old_m5_report_on_every_update_and_never_block(self) -> None:
        for host, data in (("m1", profile("m1")), ("m5", old_m5())):
            with self.subTest(host=host):
                rows, refusals = grf.ratchet(data, data, HOSTS[host])
                self.assertEqual(refusals, [])
                self.assertEqual(len(grf.finding_lines(rows)), 1)

    @requires_tomllib
    def test_m5_moving_to_the_reserve_share_is_accepted_and_fits(self) -> None:
        rows, refusals = grf.ratchet(old_m5(), profile("m5"), HOSTS["m5"])
        self.assertEqual((grf.finding_lines(rows), refusals), ([], []))

    @requires_tomllib
    def test_a_smaller_overcommit_passes_and_reports_smaller(self) -> None:
        target = old_m5()
        pulp_gate(target)["vm_cores"] = 5
        rows, refusals = grf.ratchet(old_m5(), target, HOSTS["m5"])
        self.assertEqual(refusals, [])
        self.assertEqual(grf.finding_lines(rows),
                         ["gate_reserve_overcommitted lane=pulp-gate axis=cores demand=10 reserve=8"])

    @requires_tomllib
    def test_a_first_install_reports_and_refuses_nothing(self) -> None:
        rows, refusals = grf.ratchet(None, profile("m1"), HOSTS["m1"])
        self.assertEqual((len(grf.finding_lines(rows)), refusals), (1, []))


@requires_tomllib
class ValidateCliTests(unittest.TestCase):
    def run_validate(self, host: str, target_text: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target.toml"
            target.write_text(target_text)
            facts = Path(td) / "host.json"
            facts.write_text(json.dumps(HOSTS[host]))
            return subprocess.run(
                [sys.executable, "scripts/macos_fleet_lanes.py", "validate", str(target),
                 "--check-reserve", "--installed", str(ROOT / "profiles" / f"{host}-macos-fleet.toml"),
                 "--host-profile-json", str(facts)],
                cwd=ROOT, capture_output=True, text=True, timeout=60)

    def test_validate_refuses_the_worse_m3_profile(self) -> None:
        text = (ROOT / "profiles" / "m3-macos-fleet.toml").read_text()
        self.assertIn("vm_cores = 7", text)
        res = self.run_validate("m3", text.replace("vm_cores = 7", "vm_cores = 12", 1))
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("gate_reserve_worse lane=pulp-gate axis=cores", res.stderr)

    def test_validate_reports_m1_and_passes(self) -> None:
        res = self.run_validate("m1", (ROOT / "profiles" / "m1-macos-fleet.toml").read_text())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("gate_reserve_overcommitted lane=pulp-gate axis=cores demand=6 reserve=3",
                      res.stdout)

    def test_validate_accepts_m5_moving_from_the_old_sizing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            installed = Path(td) / "installed.toml"
            installed.write_text(old_m5_text())
            target = ROOT / "profiles" / "m5-macos-fleet.toml"
            facts = Path(td) / "host.json"
            facts.write_text(json.dumps(HOSTS["m5"]))
            res = subprocess.run(
                [sys.executable, "scripts/macos_fleet_lanes.py", "validate", str(target),
                 "--check-reserve", "--installed", str(installed),
                 "--host-profile-json", str(facts)],
                cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertNotIn("gate_reserve", res.stdout + res.stderr)

    def test_plain_validate_is_unchanged(self) -> None:
        res = subprocess.run([sys.executable, "scripts/macos_fleet_lanes.py", "validate",
                              str(ROOT / "profiles" / "m1-macos-fleet.toml")],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("gate_reserve", res.stdout)


@requires_tomllib
class M5DoctorTests(unittest.TestCase):
    """The doctor's gate_reserve check on m5, through the summary it reads."""

    def finding(self, text: str):
        import macos_fleet_lanes
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(macos_fleet_lanes, "live_host_profile",
                                  return_value=HOSTS["m5"]):
            config = Path(td) / "profile.toml"
            config.write_text(text)
            value = macos_fleet_lanes.gate_reserve_summary(config)
        return fd.check_gate_reserve(value, installed_present=True)

    def test_old_sizing_is_a_problem_and_the_reserve_share_fits(self) -> None:
        before = self.finding(old_m5_text())
        self.assertEqual((before.state, before.code), (fd.PROBLEM, "gate_reserve_overcommitted"))
        self.assertIn("lane=pulp-gate axis=cores demand=12 reserve=8", before.detail)
        after = self.finding((ROOT / "profiles" / "m5-macos-fleet.toml").read_text())
        self.assertEqual((after.state, after.code), (fd.OK, "gate_reserve_fits"))


class DoctorTests(unittest.TestCase):
    def test_overcommitted_fits_unknown_and_not_applicable(self) -> None:
        bad = fd.check_gate_reserve({"lines": ["gate reserve: OVERCOMMITTED lane=pulp-gate ..."],
                                     "problem": "lane=pulp-gate axis=cores demand=6 reserve=3"},
                                    installed_present=True)
        self.assertEqual((bad.state, bad.code), (fd.PROBLEM, "gate_reserve_overcommitted"))
        self.assertIn("must not take agent cores", bad.detail)
        good = fd.check_gate_reserve({"lines": ["gate reserve: every gate lane fits"],
                                      "problem": None}, installed_present=True)
        self.assertEqual(good.code, "gate_reserve_fits")
        self.assertEqual(fd.check_gate_reserve({"lines": ["gate reserve: UNKNOWN (x)"]},
                                               installed_present=True).code,
                         "gate_reserve_unknown")
        self.assertEqual(fd.check_gate_reserve(None, installed_present=False).state,
                         fd.NOT_APPLICABLE)


if __name__ == "__main__":
    unittest.main()
