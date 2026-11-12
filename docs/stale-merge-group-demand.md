# A queued run that can never run

A `merge_group` workflow run can report `queued` permanently after its merge
queue entry is gone. Nothing in the run's own status distinguishes it from a
live entry, and no available operation removes it, so a demand scanner that
trusts `status` alone counts it forever.

## The contradiction

GitHub run `32218602754` is a `merge_group` run for Pulp #7677, a pull request
that merged weeks earlier. Every way of asking about it disagrees with every
other way:

| question | answer |
|---|---|
| Is the merge queue holding an entry for it? | No — the queue is empty |
| Does its queue branch exist? | No — deleted (`404`) |
| Does it have jobs? | `jobs: []`, `total_count: 0` |
| What does the REST API say its status is? | `queued` |
| Cancel it | "the run is already completed" |
| Force-cancel it | "the run is not queued" |
| Delete it, as the App | `403` |
| Delete it, as a maintainer | `403` |

It is queued, permanently, by an authority that will not let anyone say
otherwise.

## What this costs, stated precisely

**It does not inflate a merge-group demand count.** Its zero jobs match no
class label, so it never counts as demand for any tier. Claims that it starves
the PR-head tier are wrong, and were measured to be wrong: with the ghost
present, merge-group demand read `0` across three stable samples while
pull-request demand read `3`.

Its real cost is narrower. It is one of the queued runs in workflow
`Build and Test`, so **every scan pass on every lane fetches its jobs**,
permanently. Each assignment scan already fails closed if any single request
exceeds `TARTCI_GH_TIMEOUT_SECS`, so a permanent extra call raises the
probability of a scan failing — and a failed scan is a lane that mints nothing.
Removing the fetch makes a blind scan less likely. It does not, by itself, make
a blind scan survivable.

## The guard

`StaleDemandClassifier` in `scripts/assignment_scan.py` refuses to count a
`merge_group` run whose queue branch is confirmed absent **and** which is
confirmed to carry no queued job, and remembers that verdict so later passes
skip its job fetch entirely.

Three properties are load-bearing and easy to get wrong:

- **Positive determination only.** An API error, a timeout, or any
  indeterminate answer leaves the run counted. Treating uncertainty as
  staleness would rebuild the demand suppressor this guard exists to prevent,
  and that is the worse failure of the two: a run wrongly counted wastes a
  boot, a run wrongly discarded strands real work — and merge-group work
  stranded is a merge that never lands.
- **Exclusion requires BOTH halves.** A branch confirmed absent beside a run
  that still carries a queued job is the one combination that could remove real
  demand, and no observed stale run has that shape. A live branch with no
  queued job already contributes zero, so nothing is gained by excluding it
  either. Both exclusion and memory therefore use the same conjunction — a
  remembered verdict outlives the observation that produced it, so the narrower
  rule governs both.
- **The probe must be able to say 404.** `_gh` deliberately collapses every
  non-200 into a fail-closed error, which is right for demand and wrong here: a
  staleness verdict has to tell a definite absence apart from a timeout. Hence
  the separate tri-state `AssignmentScanner.ref_exists`.

## Evidence in the log

`assignment_stale_demand tier=… detail=…`

The scanner writes its evidence to stderr, because stdout carries the demand
count the caller parses. `tartci_assignment_v2_tier_demand` captures that stderr
to a temp file and deletes it — including on the success path, which is the path
a stale run is detected on. It now lifts `stale-demand:` lines out as typed
events before the file is discarded; without that step the evidence was written
and immediately thrown away.

## What this deliberately does NOT fix

- **A scan that goes blind.** `tartci_assignment_v2_select_live` still walks
  tiers highest-first and returns `ERR` on the first tier whose scan fails, so
  one tier's transient error still makes lower tiers unobservable for that poll.
  Skipping an unobserved tier was prototyped and rejected on measurement: the
  pre-mint re-check that makes advancing safe runs *after* `tart clone` and
  `tart run`, so a denial discards a booted VM and restarts the supervisor —
  far more expensive than the single poll it saves, and worse on the hosts where
  scans fail most.
- **Merge-group VMs minted against demand that evaporates.** Those are a
  cross-host herd: several supervisors independently observe the same one or two
  queued jobs and each mint for them. The phantom share rises with the size of
  the mint cluster, which is the signature of a herd, not of a miscount. There
  is no cross-host mint coordination today.
- **Repeated reads of the same pages.** Both assignment classes watch the same
  workflow, so one selection plus its pre-mint re-check reads the same run and
  job pages several times over.
