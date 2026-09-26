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
classes, with merge-group first unless the slot declares a preference order
(see "Per-slot class preference" below). A host may add only the canonical managed
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
until graduated): only the canary host's profile carries the retarget. The
canary is m1 (`profiles/m1-macos-fleet.toml`, `pulp-gate`, 120 s); m3 and m5
carry none and are unaffected by deploying these bytes. To enable on ONE host, add
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

## Per-slot class preference (opt-in, PR-first canary on m3)

Every slot consults the classes in configured tier order, merge-group first.
Under a continuous merge queue that starves PR-head work, and the idle retarget
above cannot help because the slot is never idle. Measured 2026-09-25: a slot
selects PR-head, merge-group demand appears during the two-to-four-minute boot,
`assignment_v2_pre_mint_denied selected_tier=1` discards the VM, and the slot
re-boots for merge-group. PR-head jobs waited 60-80 minutes.

`assignment_slot_tier_order` on an event-class-v2 lane reorders the class
preference for named supervisor slots:

```toml
assignment_slot_tier_order = { 2 = ["pulp-build-pr-head", "pulp-build-merge-group"] }
```

It renders `TARTCI_ASSIGNMENT_V2_TIER_ORDER=pulp-build-pr-head,pulp-build-merge-group`
into that slot's LaunchAgent only; other slots render nothing and keep today's
behaviour byte for byte. The order must name every tier class exactly once, so a
preference can never become a reservation, and the slot key must be a supervisor
number the lane actually runs. Validation rejects anything else, and the
supervisor refuses the env var outside `event-class-v2`.

The same order drives all three V2 decisions, so the slot never contradicts
itself:

| decision | PR-first slot | default slot |
|---|---|---|
| selection, both classes waiting | PR-head | merge-group |
| selection, only merge-group waiting | merge-group (work-conserving) | merge-group |
| pre-mint of a PR-head boot, merge-group arrived | **admit** | deny |
| pre-mint of a merge-group boot, PR-head arrived | deny, re-select PR-head | admit |
| top-tier receipt (`assignment_top_tier_receipt_max_age_seconds`) | PR-head may use it | merge-group may use it |
| idle retarget (if enabled) | falls back in slot order | falls back in slot order |

Tier numbers keep their configured meaning on every slot (0 is merge-group, 1
is PR-head), so `selected_tier` in events, the per-tier runner group, and the
class-derived lease priority (merge-group `110`, PR-head `100`) are unchanged.
The advertised label set is unchanged too: both classes are still registered in
configured order, so `fleet/advertised-labels.json` does not move. The startup
`LOOP` line prints `tier_order=` so the effective order is visible in the slot
log.

A PR-first slot still yields a merge-group boot to a PR-head arrival: that is
the same pre-mint recheck every slot runs, applied in this slot's order, and
the merge-group job keeps every other slot in the fleet, all of which prefer it.

## Release event classes (declared per lane, m5 only)

An event-class-v2 lane may declare the Pulp release classes after its two gate
tiers. The validator accepts exactly these extras, each with exactly its
workflows, listed contiguously and at most once:

| class | workflows | lease priority |
|---|---|---|
| `pulp-release-tagged` | `Release CLI`, `Sign and Release` | `120` (gate) |
| `pulp-release-pr-gate` | `Release-path PR gate` | `90` (non-gate) |

Gate tiers stay first, so a default slot keeps gate-first order and only takes
release work when no gate work waits. Each class is its own JIT registration
(base labels plus that one class label), so a release runner cannot take a gate
job and a gate runner cannot take a release job. A lane that does not declare a
class never scans for it, so hosts without the declaration never pick a release
job. Tagged releases lease at `120`, above merge-group, so a release boot is
admitted from gate-reserved capacity; the release PR gate stays non-gate at
`90`, below PR-head, as the legacy release lane's `vm` class. The numeric
values apply only to registrations carrying the gate base label
`pulp-build-vm`; the legacy `pulp-release` lane (`pulp-build-vm-release`) keeps
`gate`/`vm`.

Only m5's `pulp-gate` lane declares the classes, and one slot is release-first:

```toml
assignment_slot_tier_order = { 2 = ["pulp-release-tagged", "pulp-build-merge-group", "pulp-build-pr-head", "pulp-release-pr-gate"] }
```

Because the order names every class, it is a preference: with no release
queued the slot selects exactly what a default slot selects. Pulp opts in
separately (`PULP_RELEASE_CLASS_TOKENS=1` appends the class label to the
release selectors); until both sides are live, release jobs keep riding idle
gate runners. Rollback is unsetting the Pulp variable; host-side, delete the
extra tiers and slot order, re-render, and reload slot 2 at an idle boundary.

Canary, per decisions contract row 1: only m3 (`profiles/m3-macos-fleet.toml`,
`pulp-gate` slot 2) carries the key. m3 slot 1, m1 and m5 are unaffected by
deploying these bytes. Enable through the ordinary path: validate and render
with `tartci fleet-macos`, then reload the slot-2 supervisor at an idle boundary
(a host self-update does this). Watch for at least a week:

- PR-head queue age should fall; `mint_jit tier=1` on the m3 slot-2 log should
  follow PR-head demand even while merge-group work waits.
- `assignment_v2_pre_mint_denied selected_tier=1` should disappear from m3 slot
  2 whenever PR-head work is still queued; any that remain should be genuine
  cancellations or another host's claim.
- Merge-group latency should not regress beyond the one slot now preferring
  PR-head: the fleet still has five merge-group-first slots.
- `assignment_v2_pre_mint_denied selected_tier=0` on m3 slot 2 counts the
  merge-group boots it yielded to PR-head; a high rate says the fallback boots
  are being wasted and the order should be revisited.

Rollback is deleting the key, re-rendering, and reloading slot 2; nothing on disk
outlives it.

## Fallback lanes without a timer (opt-in, not enabled)

m1's `pulp-gate` lane is the fleet's fallback: `min_queued_age_seconds = 600`
leaves every job younger than ten minutes to m3 and m5. That rule is blind. The
queue-wait breakdown of 2026-09-26 (151 required `macos` jobs, planning
`2026-09-23-build-speed-plan.md`) found an m1 slot free for 4.2 min on average
(PR 5.1, p90 10.0) during the first ten minutes of a job's wait, while the
preferred hosts could not take it: m5's second lane can never lease a 12-core
VM beside its first in a 14-core universe, m3 denies at 24/26 cores, and both
were at the Apple 2-VM cap for long stretches. The fallback policy replaces
"wait ten minutes" with "wait while a preferred host can actually take the
job".

### Designs compared

| design | how it decides | why not / why |
|---|---|---|
| Shorter timer (0-120 s) | config only | Still blind in both directions: when m3/m5 are free, m1 races them and adds to the 72 pre-mint denials already measured; when they are full, m1 still waits the interval. |
| GitHub-only inference | boot when no idle runner of the class is registered elsewhere | On an ephemeral JIT fleet a free host has no registration until it mints, so "no idle runner" cannot tell a free host from a full one. |
| Peers push state to a shared store | peers publish free slots; m1 reads | Needs a new shared write location and credentials, and has the same freshness problem as reading. |
| **Peers' live supply, read over SSH (chosen)** | m1 asks each preferred host `tartci pool supply` only while young demand exists | Reuses the SSH path fleet self-update already relies on, reads the same heartbeats, lease store and VM inventory the peer admits with, and keeps the age rule as the upper bound. |

### What the lane does

Nothing changes for demand the age rule already admits. For a class with no
demand old enough, a fallback lane:

1. counts that class's queued jobs at any age (one exhaustive scan);
2. reads every preferred host's `tartci pool supply --repo R --class C --json`
   in parallel (`scripts/gate_supply.py report`), and this host's own sibling
   lanes;
3. boots now only when demand exceeds what is already covered.

| preferred hosts' report | verdict | lane does |
|---|---|---|
| every host `ok`, demand > free + in flight (peers) + covering siblings | `grant` | boots now for that class; event `fallback_grant` |
| every host `ok`, demand covered | `hold` | waits; event `fallback_hold` |
| any host unreachable, unreadable, `unknown`, or a report older than `fallback_peer_max_age_seconds` | `unknown` | keeps the age rule; event `fallback_unknown` |

A preferred host's `free` is the number of its lanes that serve the class and
whose fresh heartbeat is idle (`waiting`, `loop`, `backoff`), capped by its free
macOS VM slots (GUI cap, Apple's 2-guest limit, live reservations) and by how
many VM leases of that lane's size its lease store admits right now. m5's
second lane beside a running 12-core VM, and m3 at 24/26 cores, therefore
report `free=0`. `in_flight` counts lanes already between admission and
assignment. A heartbeat older than max(120 s, 6 polls), an unrecognised phase
or an unreadable lease store makes the whole report `unknown`, never zero. A
Tart inventory that cannot be read (after the same retry the supervisor uses)
is treated as the host's own slot claim treats it: the reservation files are
the occupancy (`"inventory": "reservations"` in the report).

The fail-safe is structural: `unknown` and `hold` both fall through to the age
rule, so the fallback lane never boots later than it does today, and never
boots early on a guess. A peer that reports free but does not take the job
(an admission deferral, a lease race) is re-read at the next live selection
(the V2 selection cache is 120 s), when it no longer reads as free; the age rule
is the backstop behind that. Both hosts cannot sit idle behind each other.

Stampede control:

- A sibling lane on the fallback host that is in flight covers one job, and a
  free sibling with a LOWER slot number covers one job before this lane may
  take it, so m1's two slots never both boot for one young job.
- Preferred hosts' in-flight lanes count as coverage.
- The pre-mint recheck of the granted class runs at age 0 (the grant is
  recorded in `$STATE_DIR/<runner>.fallback-grant`, valid 900 s, cleared by the
  next live selection), so a booted VM is not discarded because the job that
  justified it is still younger than 600 s. The peers are not re-read at
  pre-mint: a VM already booted is kept while its job is queued.

Cost while young demand exists: one extra exhaustive scan and one SSH per
preferred host per live selection (at most every 120 s). No GitHub or SSH call
is made when the knob is off or when there is no young demand.

### Enabling it (not enabled on any host)

Prerequisites, checked from the fallback host as the lane's user:

```bash
# every preferred host runs a tartci generation with `pool supply`
ssh -o BatchMode=yes m3 'cd ~ && ~/.local/bin/tartci pool supply --repo Generous-Corp/pulp --class pulp-build-merge-group --json'
ssh -o BatchMode=yes m5 'cd ~ && ~/.local/bin/tartci pool supply --repo Generous-Corp/pulp --class pulp-build-merge-group --json'
```

Both must print a `tartci.gate-supply/v1` report with `"verdict": "ok"`.

The exact profile lines, added to m1's `pulp-gate` lane in
`profiles/m1-macos-fleet.toml` (the lane keeps `min_queued_age_seconds = 600`,
which becomes the upper bound):

```toml
fallback_preferred_hosts = ["studio", "m5"]
# optional; how old a peer report may be before it counts as unknown (30-300, default 60)
fallback_peer_max_age_seconds = 60
```

Host ids resolve to SSH targets from the peers' own profiles (`host.ssh`: m3 is
`studio` reached as `m3`), else the `tartci-<host_id>` convention; the rendered
LaunchAgent carries `TARTCI_FALLBACK_PEERS=studio=m3,m5=m5`. Validation refuses
the key outside `event-class-v2`, with `min_queued_age_seconds = 0`, or naming
the lane's own host. Render and reload through the normal `tartci fleet-macos`
path at an idle boundary (a host self-update does this).

Preflight without booting anything, with the lane's environment:

```bash
tartci serve macos --print-fallback-decision 0   # merge-group; 1 = PR-head
# off | none no young demand | grant <detail> | hold <detail> | unknown <detail>
```

### Measuring it

Use the queue-wait method of the plan (join `created_at`/`started_at` to the
per-VM events). Compare a week before and after for:

- the "policy" component (2b) and m1's free-slot minutes during the first ten
  minutes of a job's wait: both should fall toward zero;
- `fallback_grant` events on m1 each followed by `mint_jit` and `job_assigned`,
  not by `assignment_v2_pre_mint_denied` (a thrash signature);
- `fallback_unknown` rate: a steady stream names a peer that cannot be read
  (`detail` says which) and means the lane is running on the age rule;
- m3/m5 job counts: m3 (19.4 min gate p50) should not lose jobs it would have
  won while free, because a free m3 slot holds the grant.

Rollback: delete the two keys, re-render, reload. Nothing on disk outlives it
(the grant file is ignored once the knob is gone).

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
