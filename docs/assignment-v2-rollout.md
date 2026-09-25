# Event-class JIT assignment V2 rollout

V2 partitions Pulp's required macOS JIT capacity at GitHub's assignment
boundary. A runner advertises the shared platform/base labels plus exactly one
of:

- `pulp-build-merge-group`
- `pulp-build-pr-head`

It does **not** advertise `pulp-gate-fast`. An older job that requests only the
legacy generic selector therefore matches neither V2 runner class.

| queued job | merge-group runner | PR-head runner |
|---|---:|---:|
| base + `pulp-build-merge-group` | yes | no |
| base + `pulp-build-pr-head` | no | yes |
| base + `pulp-gate-fast` only | no | no |

The supervisor requires the selected class token to be present on the queued
job. Its V2 scan consumes every run and job page. API errors, malformed payloads,
timeouts, or a pagination-cap hit are uncertainty and deny boot/mint. Immediately
before JIT minting it freshly rescans every higher class and the selected class.
If higher demand arrived or selected demand was cancelled, the unregistered VM
is discarded and its lease is released.

Registration authority is class-specific as well as label-specific. Both Pulp
event classes use repository group `1`; the exclusive class label still keeps
merge-group and PR-head jobs separate:

```text
TARTCI_RUNNER_WORKFLOW_TIER_GROUPS=pulp-build-merge-group|1
                                     pulp-build-pr-head|1
```

The actual value is newline-delimited. The provider rejects a missing,
misordered, or malformed mapping. GitHub evaluates a merge-group workflow under
its `gh-readonly-queue/...` ref, so an organization group restricted to
`build.yml@refs/heads/main` leaves an exact-label runner online and idle while
the job remains queued. Repository JIT avoids that false capacity for both
classes. Required Shipyard admission-clean runs at this same final JIT boundary.

## Modes and staged migration

`TARTCI_RUNNER_ASSIGNMENT_MODE` is reversible:

- `legacy` (code default): current tier behavior; no V2 observation or routing.
- `observe`: current assignment behavior, plus a rate-limited
  `legacy=... v2=...` parity event and log sample (15 minutes by default).
- `event-class-v2`: V2 labels and fail-closed exhaustive assignment admission.

Normal V2 boot selection is cached locally for two minutes to bound fleet API
traffic; the safety-critical pre-mint check always bypasses that cache. Observe
samples are limited to once per 15 minutes. An individual exhaustive scan has a
1,200-call hard ceiling and a 60-second wall-clock deadline; reaching either is
uncertainty and fails closed rather than silently truncating a busy queue.

The template also declares:

```text
TARTCI_ASSIGNMENT_V2_OMIT_LABELS=pulp-gate-fast
TARTCI_ASSIGNMENT_V2_CLASS_LABELS=pulp-build-merge-group,pulp-build-pr-head
```

The shipped Pulp template remains `legacy`. Deploy those bytes first, then
enable `observe` on one drained host at a time. Keep one dynamic macOS gate
supervisor per governed slot; each supervisor's ordered tiers serve both
classes, with merge-group first. A host may add only the canonical managed
slot-2 profile when its governor can admit two complete guests. Do not create a
supervisor per event class or an ad-hoc duplicate process. Confirm from the rendered
LaunchAgent environment (or set the same env explicitly):

```bash
TARTCI_RUNNER_ASSIGNMENT_MODE=observe \
TARTCI_RUNNER_WORKFLOW_TIERS=$'pulp-build-merge-group|Build and Test\npulp-build-pr-head|Build and Test' \
TARTCI_RUNNER_WORKFLOW_TIER_GROUPS=$'pulp-build-merge-group|1\npulp-build-pr-head|1' \
tartci serve macos --print-assignment-parity
```

Expected parity is semantic, not necessarily equal counts: legacy may see an
older generic-only job that V2 correctly reports as ineligible. Cross-check each
V2 class count against the queued jobs' actual labels. Do not promote while any
event-class job lacks its class token, any expected event-class job is absent,
or any scan reports `ERR`.

After the workflow-side event selectors are live, drain one fast host at an
idle boundary, set `event-class-v2`, reload its existing supervisor, and run a
real PR-head canary followed by a merge-queue canary. Prove the runner heartbeat
advertises exactly one class and omits `pulp-gate-fast`; prove VM, JIT runner,
and lease teardown after each job. Then advance one drained host at a time.

Two hosts can observe and mint against the same still-queued job. GitHub assigns
it once; runner names remain per-boot ephemeral and the losing JIT runner follows
the existing bounded idle timeout, registration cleanup, VM discard, and lease
release path. V2 does not introduce persistent runner identity or a second
scheduler.

Merge-group leases use numeric priority `110`; PR-head leases use gate priority
`100`. Both retain the governor's reserved gate capacity, while merge-group
demand sorts first. A runner carrying both class labels is invalid and falls
down to ordinary `vm` priority. Managed Pulp V2 profiles must omit
`TARTCI_VM_LEASE_PRIORITY`; fleet validation rejects an explicit lane priority
so a host-level default cannot flatten the class ordering or keep M1 out of the
reserved gate budget. The runtime override remains available to non-V2 lanes.

## Work-conserving idle retarget (opt-in, canary path)

A V2 runner advertises exactly one class and can serve nothing else. Between
observing a job and registering for it a host boots a VM (two to four
minutes), and two hosts routinely observe the same job; the loser registers a
runner GitHub will never assign. On m1 the tier-zero receipt widens that window
to 180 s by design. That runner then holds a governed slot for the full idle
timeout (900 s) while the OTHER class queues behind it. Measured 2026-09-24
03:07-03:22Z on m1 slot 2: a merge-group runner minted on a receipt idled to
`idle_timeout elapsed=903s` while ten PR-head jobs waited, the oldest 1 h 38 m.
Fleet-wide since 2026-09-10 the events logs show 69 such windows (m1 15, m3 24,
m5 30), about 17 h of gate-slot time, split evenly across both classes.

`assignment_idle_retarget_seconds = N` on an event-class-v2 lane (env
`TARTCI_ASSIGNMENT_V2_IDLE_RETARGET_SECS`; `0`/absent = off, else 60-3600)
makes a runner that has sat unassigned for N seconds re-observe both classes,
and again every N seconds while still idle:

| own class (age-agnostic) | other class (lane minimum age) | verdict |
|---|---|---|
| any queued job | any | **hold** (`reason=own_class_demand`) |
| none | admissible demand | **retarget**: discard, return to selection |
| none | none | hold (`reason=no_other_demand`) |
| blind | any | hold (`reason=own_class_uncertain`) |
| none | blind | hold (`reason=other_class_uncertain`) |

The own-class probe ignores the lane's minimum queued age on purpose: that age
is a boot-delay policy (m1 leaves work under 600 s to faster hosts), not an
assignment restriction, and GitHub hands a young job of the runner's class to an
already-registered runner in seconds. Discarding it to boot for the other class
would strand the preferred class behind a rule it was never subject to.

Class preference is unchanged. The retarget only discards a runner that is
provably serving nobody; the supervisor then runs its ordinary ordered
selection, where merge-group still wins whenever both classes wait. A job
assigned while the observation ran is kept (`assignment_v2_idle_retarget_overtaken`).
Uncertainty in either probe holds, so the bounded idle timeout remains the
backstop and a blind scan can never discard a runner. The cost while idle is
one witness-bounded scan per class per interval under the host-global
observation lock; a `retarget` exits `run_runner_until_done` with rc 125,
records `teardown rc=125` and `runtime_measure` class `idle_retarget`, and
invalidates the cached selection so the next pass observes live.

`tartci serve macos --print-idle-retarget <tier>` prints the verdict (`1`
retarget / `0` hold) for a hypothetical idle runner of that zero-based tier
without booting anything; with the knob off it prints `0` and makes no GitHub
call.

Canary, per decisions contract row 1 (a staged-rollout gate stays conservative
until graduated): the shipped profiles carry no retarget, so every host is
unaffected by deploying these bytes. To enable on ONE host, add
`assignment_idle_retarget_seconds = 120` to its `pulp-gate` lane in that host's
profile, validate and render through the normal `tartci fleet-macos` path, and
reload its existing supervisors at an idle boundary. Then watch, over at least
a week of both classes:

- `assignment_v2_idle_retarget` events carry `selected_tier`, `to_tier`,
  `elapsed`; each should be followed within one boot by a `mint_jit` of the
  target class and a `job_assigned`, never by a matching `assignment_v2_pre_mint_denied`
  loop (a thrash signature that would say the interval is too short).
- `assignment_v2_idle_hold` events with `reason=own_class_demand` prove the
  guard against discarding a runner that is about to be assigned.
- `idle_timeout` events on that host should fall toward zero; the difference is
  the reclaimed slot time.
- `tartci status` `serving:` must not read BLOCKED for the lane: a retarget
  counts as a work entry that served nothing, which is the honest reading, and
  a served job resets the streak.

Rollback is removing the key (or setting `0`) and reloading; no state on disk
outlives it. Graduate to the remaining hosts one at a time, as with V2 itself.

Two related levers were evaluated and deliberately left alone. m1's
`min_queued_age_seconds = 600` is the documented delayed-fallback stagger
(runbook, "multiple Mac supervisors may watch one shared label"); the 03:20Z
hold was inside an idle window on work already 1 h 38 m old, so the age rule
was not the cause, and m1 served 312 jobs in the same fortnight, so it is not
idling on the rule either. Reserving one PR-head slot per host would cap
merge-group at one runner per host even with two merge groups waiting, which
inverts the stated preference on the class that lands code, and the observed
starvation was an idle hold, not merge-group work consuming both slots. Both
stay open until a measurement shows the retarget leaves either problem behind.

## Rollback and offline rejoin

Rollback the fleet side first: drain one host, restore `observe`, reload the same
supervisor, and verify its parity output and generic labels. Because observe
still advertises each tier class plus the legacy base, event-class jobs remain
serviceable while workflow selectors are rolled back. Only then stop emitting
event-class job labels. `legacy` is the final code-level rollback if V2
observation itself must be disabled.

Do not bypass pool drain, the VM governor, ephemeral identity, assignment
timeout, or reaping during migration. An offline host rejoins through the normal
`tartci pool status` / `tartci pool on` path; launchd `KeepAlive` restarts the one
supervisor, which rechecks pool admission before boot and again before mint.
Run `tartci doctor --reap --json` only under its normal ownership rules to clear
confirmed stale per-boot registrations/VMs. Never reuse a static runner name or
manually edit the lease store.

For a two-slot host, install slot 2 only through `tartci gate-slot2 install`.
The profile fixes the lane at 6 cores and 8192 MiB, shares `TART_HOME`, and
separates launchd identity, runner identity, queue lane, state, and logs. Its
raw labels already omit `pulp-gate-fast`; validation fails closed if the legacy
selector is reintroduced or either supervisor resolves to the same runner/state
identity.
