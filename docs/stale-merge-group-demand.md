# A queued run that can never run, and the tier walk it froze

Friction report for the macOS assignment lane. Two defects, one visible
symptom: hosts reporting `SCAN BLIND`, restarting themselves for credentials
they did not need, while real pull-request work sat queued.

## The contradiction

GitHub run `32218602754` is a `merge_group` run for Pulp #7677, a pull request
that merged fourteen days earlier. Every way of asking about it disagrees with
every other way:

| question | answer |
|---|---|
| Is the merge queue holding an entry for it? | No — the queue is empty |
| Does its queue branch exist? | No — deleted |
| Does it have jobs? | `jobs: []` |
| What does the REST API say its status is? | `queued` |
| Cancel it | "the run is already completed" |
| Force-cancel it | "the run is not queued" |
| Delete it, as the App | `403` |
| Delete it, as a maintainer | `403` |

Nothing in the run's own status distinguishes this from a live entry, and no
available operation removes it. It is queued, permanently, by an authority that
will not let anyone say otherwise.

## Defect 1 — demand derived from status alone never drains

The demand scanner counted a run as demand because the API called it `queued`.
For a run in the state above, that count is permanent. Because assignment tiers
are walked highest-priority-first, a permanent count in the top tier does not
merely inflate a number: it outranks every lower tier forever.

**Fix.** `StaleDemandClassifier` in `scripts/assignment_scan.py` refuses to
count a `merge_group` run whose queue branch is confirmed absent, or which is
confirmed to carry no queued job.

Three properties are load-bearing and easy to get wrong:

- **Positive determination only.** An API error, a timeout, or any
  indeterminate answer leaves the run counted. Treating uncertainty as
  staleness would rebuild the demand suppressor this guard exists to prevent,
  and that is the worse failure of the two: a run wrongly counted wastes a
  boot, a run wrongly discarded strands real work.
- **Exclusion and memory use different rules.** A run is excluded when *either*
  signal is confirmed. It is *remembered* — so later passes skip its job fetch
  entirely — only when *both* are, because a remembered verdict outlives the
  observation that produced it.
- **The probe must be able to say 404.** `_gh` deliberately collapses every
  non-200 into a fail-closed error, which is right for demand and wrong here: a
  staleness verdict has to tell a definite absence apart from a timeout. Hence
  the separate tri-state `AssignmentScanner.ref_exists`.

## Defect 2 — an unobservable tier made every lower tier unobservable

`tartci_assignment_v2_select_live` walked tiers highest-first and returned `ERR`
on the **first** tier whose scan failed. A tier's scan failing says nothing
about the tiers beneath it, so one tier's transient error became total
blindness: the supervisor reported `SCAN BLIND`, and on a sustained window
`exit 75`-ed to refresh `gh` credentials — a remedy predicated on stale auth,
which was not the cause. Where the true cause was an HTTP timeout, that is an
unbounded restart loop that never learns a lower tier had work the whole time.

**Fix.** An unobserved tier is skipped, not fatal, and emits
`assignment_tier_unobserved`. Advancing is safe because assignment classes are
exclusive — a VM minted for a lower class cannot claim higher-class work — and
because the existing pre-mint re-check denies the mint outright if the higher
tier turns out to have demand after all. `ERR` is still returned when nothing
was found *and* some tier went unobserved, so only an exhaustive walk may report
an empty queue.

**What is not safe is caching that selection.** A verdict reached without seeing
the top tier is correct to act on once and wrong to remember for the cache TTL.
`select_live` is always called inside a command substitution, so an exported
variable would be set in a subshell and silently lost; the signal travels
through a state file instead.

## How the two defects relate

They are independent, and the second is the one that was actually starving the
fleet. Measured on the live hosts with the ghost present, merge-group demand was
**0** across three stable samples while pull-request demand was **3** — the
ghost does **not** produce a phantom merge-group count, because its zero jobs
match no label.

Its real cost is narrower and still worth removing: it is one of four queued
runs in workflow `Build and Test`, so **every scan pass on every lane fetched
its jobs**, permanently. That added call feeds the very API timeout that is
Defect 2's trigger. Removing it makes the timeout less likely; making the walk
survive a timeout is what stops the starvation.

## Evidence in the log

- `assignment_tier_unobserved tier=… tier_index=… action=continue_to_lower_tier`
- `assignment_stale_demand tier=… detail=…`

The scanner writes its evidence to stderr, because stdout carries the demand
count the caller parses. `tartci_assignment_v2_tier_demand` captures that stderr
to a temp file and deletes it — including on the success path, which is the path
a stale run is detected on. It now lifts `stale-demand:` lines out as typed
events before the file is discarded; without that step the evidence was written
and immediately thrown away.

## Known remaining rough edge

`tartci_assignment_v2_total_demand` keeps the abort-on-first-failure shape: one
tier's failure makes it report `ERR` for the whole fleet. That is left alone
deliberately. It sums across tiers, so a partial answer would be a genuine
undercount, and its only caller is the `--print-queue` diagnostic — its `ERR`
means "I do not know," and is never acted on. It is worth knowing that an
operator asking `--print-queue` during a single-tier hiccup gets `ERR` for
everything, which is the misleading signal that sent the first diagnosis of this
incident in the wrong direction.
