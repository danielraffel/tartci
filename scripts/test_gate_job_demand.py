"""Gate demand over answers captured from Generous-Corp/pulp at 2026-10-13
16:48 UTC (scripts/fixtures/gate_job_demand/), with this repository's
fleet/advertised-labels.json at that time.

- `pulp-runs-queued.json` and `pulp-runs-in_progress.json`: the two run
  listings. `Build and Test` 38433042640 (queued, its macOS job queued and
  its Linux job running), the 2026-08-19 merge_group zombie 32218602754
  (queued, no jobs), and two in-progress `Build and Test` runs whose macOS
  and hosted jobs are all running.
- `pulp-jobs-<run>.json`: each of those runs' latest jobs; the
  `Release-path PR gate` run's (38433042658, hosted jobs only) was read a few
  minutes later.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import gate_job_demand as demand

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gate_job_demand"
NOW = demand.epoch("2026-10-13T16:48:10Z")
PULP = "Generous-Corp/pulp"
QUEUED_MACOS = 115420876725


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


ADVERTISED = load("advertised-labels.json")


class Recorder:
    """A fetch that answers from the captures and records each path."""

    def __init__(self, overrides: dict | None = None, fail: str | None = None):
        self.paths: list[str] = []
        self.overrides = overrides or {}
        self.fail = fail

    def __call__(self, path: str) -> dict:
        self.paths.append(path)
        if self.fail and self.fail in path:
            raise RuntimeError("HTTP 502")
        if path in self.overrides:
            return self.overrides[path]
        if path.startswith(f"repos/{PULP}/actions/runs?status="):
            status = path.split("status=", 1)[1].split("&", 1)[0]
            return load(f"pulp-runs-{status}.json")
        if path.startswith(f"repos/{PULP}/actions/runs/"):
            run = path.split("/actions/runs/", 1)[1].split("/", 1)[0]
            return load(f"pulp-jobs-{run}.json")
        raise RuntimeError(f"not captured: {path}")


def pulp_registrations() -> list[dict]:
    return [reg for reg in demand.registrations(ADVERTISED) if reg.get("repo") == PULP]


class GateJobDemandTests(unittest.TestCase):
    def test_a_queued_macos_job_in_a_running_run_is_demand(self) -> None:
        fetch = Recorder()
        jobs, errors = demand.queued_jobs(fetch, pulp_registrations(), NOW)
        self.assertEqual(errors, [])
        self.assertEqual([job["id"] for job in jobs], [QUEUED_MACOS])
        job = jobs[0]
        self.assertEqual((job["run_id"], job["workflow"]), (38433042640, "Build and Test"))
        self.assertEqual(job["hosts"], ["m1", "m5", "m5studio", "studio"])

    def test_a_queued_macos_job_in_an_in_progress_run_is_demand(self) -> None:
        # The common shape: Build and Test is in_progress because its hosted
        # jobs started, and its macOS job is still queued. Only the
        # in_progress listing finds it.
        path = f"repos/{PULP}/actions/runs/38432684644/jobs?filter=latest&per_page=100"
        answer = copy.deepcopy(load("pulp-jobs-38432684644.json"))
        macos = [job for job in answer["jobs"] if job["id"] == 115418896156]
        self.assertEqual(macos[0]["labels"][:2], ["self-hosted", "macOS"])
        macos[0]["status"] = "queued"
        fetch = Recorder({path: answer})
        jobs, errors = demand.queued_jobs(fetch, pulp_registrations(), NOW)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(job["id"] for job in jobs), sorted([QUEUED_MACOS, 115418896156]))
        self.assertTrue(any("status=in_progress" in p for p in fetch.paths))

    def test_the_zombie_run_is_never_read(self) -> None:
        fetch = Recorder()
        demand.queued_jobs(fetch, pulp_registrations(), NOW)
        self.assertFalse(any("/runs/32218602754/" in path for path in fetch.paths), fetch.paths)
        # Unrelated workflows (CodeQL, Nightly Intel) are not read either.
        self.assertFalse(any("/runs/38420540013/" in path for path in fetch.paths))

    def test_a_queued_hosted_job_is_not_demand(self) -> None:
        # The captured in-progress run's Linux job, as if it were still queued.
        path = f"repos/{PULP}/actions/runs/38432684644/jobs?filter=latest&per_page=100"
        answer = copy.deepcopy(load("pulp-jobs-38432684644.json"))
        linux = [job for job in answer["jobs"] if "ubuntu-latest" in job["labels"]]
        self.assertTrue(linux)
        for job in linux:
            job["status"] = "queued"
        jobs, _ = demand.queued_jobs(Recorder({path: answer}), pulp_registrations(), NOW)
        self.assertEqual([job["id"] for job in jobs], [QUEUED_MACOS])

    def test_old_demand_is_dropped(self) -> None:
        later = NOW + demand.MAX_JOB_AGE_SECONDS + 60
        jobs, _ = demand.queued_jobs(Recorder(), pulp_registrations(), later)
        self.assertEqual(jobs, [])
        young, _ = demand.queued_jobs(Recorder(), pulp_registrations(), NOW)
        self.assertEqual(demand.jobs_for(young, "m1", NOW)[0]["id"], QUEUED_MACOS)
        self.assertEqual(demand.jobs_for(young, "m1", later), [])

    def test_a_host_only_counts_jobs_it_serves(self) -> None:
        jobs, _ = demand.queued_jobs(Recorder(), pulp_registrations(), NOW)
        self.assertEqual(len(demand.jobs_for(jobs, "studio", NOW)), 1)
        self.assertEqual(demand.jobs_for(jobs, "m9", NOW), [])
        # Only m1's registrations: m1 still serves it, and only m1 is named.
        m1_jobs, _ = demand.queued_jobs(Recorder(), [r for r in pulp_registrations()
                                                     if r["host_id"] == "m1"], NOW)
        self.assertEqual(m1_jobs[0]["hosts"], ["m1"])

    def test_machine_nicknames_resolve_to_the_advertised_host_id(self) -> None:
        # m3 is registered as studio and m5s as m5studio; a reader given the
        # nickname must see that host's registrations and jobs, not nothing.
        self.assertEqual(demand.canonical_host("m3"), "studio")
        self.assertEqual(demand.canonical_host("M5S"), "m5studio")
        self.assertEqual(demand.canonical_host("m5"), "m5")
        self.assertEqual(demand.canonical_host("studio"), "studio")
        for nickname, host_id in (("m3", "studio"), ("m5s", "m5studio")):
            self.assertTrue(demand.registrations(ADVERTISED, host_id))
            self.assertEqual(demand.registrations(ADVERTISED, nickname),
                             demand.registrations(ADVERTISED, host_id))
        jobs, _ = demand.queued_jobs(Recorder(), pulp_registrations(), NOW)
        self.assertEqual(demand.jobs_for(jobs, "m3", NOW), demand.jobs_for(jobs, "studio", NOW))
        self.assertTrue(demand.jobs_for(jobs, "m3", NOW))

    def test_a_failed_read_is_an_error_not_no_demand(self) -> None:
        jobs, errors = demand.queued_jobs(Recorder(fail="status=queued"),
                                          pulp_registrations(), NOW)
        self.assertEqual(jobs, [])
        self.assertTrue(errors and "HTTP 502" in errors[0], errors)
        # Other repositories' registrations are read too; an uncaptured read
        # is reported, never dropped.
        _, errors = demand.queued_jobs(Recorder(), demand.registrations(ADVERTISED), NOW)
        self.assertTrue(any("not captured" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
