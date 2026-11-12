# Event-class JIT assignment V2 rollout

## Serving fewer gate classes

M1 omitted `pulp-build-merge-group` after #439, rejoined it on 2026-10-11 when
merge-group capacity was scarce, and on 2026-10-16 left it again. Over 546
native macOS gate jobs (2026-10-12..16) m1's success median was 32.7 minutes
against 17.9 (m3), 18.7 (m5studio) and 21.9 (m5), and pairing two m1 jobs did
not slow either, so the gap is the host. Each merge-group batch m1 took held the
queue about 13 minutes longer, and a replay of the same arrivals without m1
left median and p90 total time unchanged with merge-group p90 slightly better.
M1 is now overflow-only: both slots serve PR-head work only once it has queued
600 s (no `fallback_preferred_hosts`), plus release classes, and its spare
capacity goes to non-latency-critical work.

An event-class-v2 lane may serve fewer classes only by naming the classes it keeps with `v2_gate_classes`. M1 declares PR-head only:

```toml
v2_gate_classes = ["pulp-build-pr-head"]
```

The list must be a non-empty subset of those two classes, and the lane's leading tiers must match it exactly. Dropping a tier without the key, or keeping a tier the key omits, fails validation. M1's published supply therefore carries no merge-group registration. Rollback is to restore both classes, the merge-group tier and slot orders, and regenerate the published labels.

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

## Pre-clone demand check (on for the m1, m3 and m5studio pulp-gate lanes)

A V2 lane acquires its VM lease before it clones (`boot_vm_to_ssh` in
`providers/tart-macos/runner.sh`), and lease acquisition never waits, so no VM
is cloned without a lease. What the clone does not hold is current *demand*:
the class was selected from a cache up to the selection TTL old (120 s), and
the Shipyard admission precheck between selection and clone takes tens of
seconds more. Demand that another lane or host takes in that window is only
noticed at the pre-mint check, after the clone and boot, and the booted VM is
discarded with `assignment_v2_pre_mint_denied`.

Measured from the hosts' events logs, 2026-09-27 to 2026-10-01 (all hosts):
262 VMs were discarded at pre-mint, 208 with `blocker_reason=own_class_empty`
and 40 with `higher_class_demand`. 175 of the own-class denials had selected a
class with exactly one queued job, and for 164 another lane minted a runner for
the same class inside the window (136 of them on another host). Each such VM
cost 91-185 s (per-host median across m1, m3, m5, m5s) of clone, boot and
preflight before the discard, plus the delete. For 126 of the
own-class denials the competing lane had minted at least 15 s *before* this
lane started its clone, so the job was already gone when the clone began.

`assignment_pre_clone_demand_check = true` on an event-class-v2 lane (env
`TARTCI_ASSIGNMENT_V2_PRE_CLONE_CHECK=1`; absent = off) asks the pre-mint
question once more, live, immediately before the clone (after the admission
precheck; a parked warm VM's hand-off has no clone and is not checked):

| live observation | verdict |
|---|---|
| selected class has demand, every preferred class empty | clone |
| selected class empty (`own_class_empty`) | **skip**: `assignment_v2_pre_clone_denied`, drop the cached selection, back off one poll |
| a preferred class has demand (`higher_class_demand`) | **skip**: as above; the next pass re-selects that class |
| scan uncertain or failed | clone (`assignment_v2_pre_clone_uncertain`), exactly as without the check |

The top-tier receipt shortcut (`assignment_top_tier_receipt_max_age_seconds`)
is disabled for this one call: that receipt is the selection being re-checked.
Lease acquisition, ranked waiters, the gate reserve, the agent floor and class
preference are unchanged; the check only removes a clone the pre-mint check
would refuse on the same observation, and costs one exhaustive scan per clone
attempt. A skip counts as an idle pass, not a blocked one (like a contended job
claim), and withdraws the lane's ranked lease waiter. `--print-pre-clone-selection
<tier>` reports the decision as a safe preflight.

Canary result, m3 both slots, 2026-10-01T09:19Z to 10-04T17:18Z against the
preceding 80 h: 68 clones skipped (`assignment_v2_pre_clone_denied`), 10
fail-open clones (`_uncertain`), served jobs up from 156 to 229, no refused
work. The per-job fall in pre-mint `own_class_empty` matched the no-knob control
host, so the measured win is the skipped clones, not a lower discard rate. On
that evidence the key is set on the pulp-gate lane of the m1, m3 and m5studio
profiles. m5 joined after its ranked-lease canary read (2026-10-03 18:34Z to
10-10 18:38Z, 168 h: 846 leases, 0 real inversions), which the check would
otherwise have confounded, so every pulp-gate lane now runs it.

What it does not fix: two hosts that both clone for the same single job inside
the same few seconds. Neither can see the other's boot until one mints, so the
loser still discards at pre-mint. Removing that needs a fleet-wide boot claim
(today `job_claim.py` sees only this host's boots and the fleet's minted idle
runners).

**Retarget instead of discard.** When the pre-mint check denies a booted VM's
class, the lane re-runs admission for every class in its preference order that
has queued demand, live, and mints the VM with the first class that admits
(`assignment_v2_pre_mint_retarget from_tier=… to_tier=…`). The VM is discarded
(`assignment_v2_pre_mint_discard reason=no_class_waiting|runner_group_differs|jit_admission_denied`)
only when no class with demand admits, the new class lives in another runner
group, or its JIT admission is refused. The lease is kept, not re-acquired:
every gate class is at or above the gate priority threshold, so the lease store
treats them alike, and releasing it would let another lane take the slot between
release and re-acquire. `--print-pre-mint-retarget <tier>` prints `keep`, the
retarget tier, or `discard` as a safe preflight.

**Hold before discarding.** When no class is waiting at all
(`no_class_waiting`), the booted VM is held for one more scan first
(`assignment_v2_pre_mint_hold hold_seconds=N`). The lane releases the
host-global mint lock, waits `TARTCI_ASSIGNMENT_V2_PRE_MINT_HOLD_SECS`
(default 25, range 0..60; 0 disables), takes the lock again and repeats the
same live retarget scan. A class that now admits is served
(`assignment_v2_pre_mint_retarget … held=1`, or `assignment_v2_pre_mint_kept`
when it is the VM's own class); otherwise the VM is discarded as before with
`held=1`. After a hold the lane's job claim moves to the class it will serve,
and a class another lane already covers (it booted or minted for the job during
the hold) is discarded with `demand_covered` instead of minting a second runner.
A held lane heartbeats `pre-mint-hold`, which `tartci pool supply` counts in
flight for every class, since the VM may serve any of them. The hold runs at
most once per boot and never for the other discard reasons. On m3 slot 1 on 2026-10-25 five booted VMs were discarded for
`no_class_waiting`.

`tartci pre-mint-outcomes [--days N] [--json]` counts retargets and discards per
lane per day from each lane's `events.jsonl`. Every pre-mint denial ends in one
of the two, so discards are denials minus retargets, which keeps logs written
before retargeting existed comparable. A VM the hold kept for its own class
counts with the retargets.

**Canary: m3 `pulp-gate` (both slots).** m3 has the highest share of
catchable denials (42 of 74 pre-mint discards in the baseline) and the most
served jobs per day, so the sample accrues fastest; m5, with more discards,
also carries the ranked-waiter canary, and this check changes which lanes wait
for a lease, so measuring either there would confound the other.

Proxy, before vs after on the canary host, from `tartci pool status --usage
--range ...` and the events log: discards per served job and clone-seconds per
served job, with served jobs per day as the control. Expected: pre-mint
`own_class_empty` discards fall by roughly half, `assignment_v2_pre_clone_denied`
appears in their place, and served jobs per day stay flat. A rise in median
queue wait for the pulp classes, or `assignment_v2_pre_clone_denied` with no
matching fall in pre-mint denials, means the check is refusing real work: roll
back.

Rollback: delete `assignment_pre_clone_demand_check` from
`profiles/m3-macos-fleet.toml`, re-render, and reload the pulp-gate slots at an
idle boundary.

## Fleet-wide boot claims (opt-in, not enabled)

A lane claims a queued job of its class before it clones (`scripts/job_claim.py`),
but until now a claim was visible only on its own host and, fleet-wide, only
once a runner had minted. A lane on another host that is cloning or booting was
invisible, so every free lane on every host could boot for one queued job, and
all but the first discarded at the pre-mint check. Measured 2026-10-01T09:19Z to
10-04T17:18Z: pre-mint `own_class_empty` per served job was 0.27 (m1), 0.26
(m3), 0.65 (m5) and 0.20 (m5s), and for 23/26, 61/63, 77/93 and 34/52 of those
another host minted the same class inside the discarded VM's claim-to-denial
window (control, the same windows shifted 1-2 h: 19/52, 20/126, 38/190, 18/104).

Every host publishes its live claims, read-only:
`tartci job-claim status --publish` (key, VM, age; the host's id; and the age
it declares for its claims, `host.job_claim_max_age_seconds`, at most the
1800 s claim TTL). A lane with `assignment_fleet_claim_peers = true` (env
`TARTCI_JOB_CLAIM_FLEET_PEERS=1`) reads every other host in the published
supply over SSH before it claims (`job_claim.py gather-peers`: parallel,
`ConnectTimeout=2`, a hard `TARTCI_JOB_CLAIM_FLEET_READ_SECS` budget, default 5,
after which stragglers are killed with their process group). A peer's claim for
the same class stands like a local one while its age is within the age that host
declares, or 900 s when it declares none; a claim whose VM already shows as an
idle runner counts once (the runner name is the VM name).

Fail open: an unreachable, slow, failing or unparsable peer counts no claims,
and the lane boots exactly as without peers. The per-attempt count rides on
every `job_claim` / `job_claim_contended` event (`fleet_booting=`,
`peers_unread=`); `job_claim_peer_unread` is logged once per peer per hour. A
host writes its claim only after reading its peers, so two lanes can never both
refuse one job; two hosts that both read before either writes can still both
boot (a window of seconds), and the pre-mint check catches the loser.

m1 declares 1800 s: its lanes wait for a VM lease after claiming, so its claims
live about 26 min on average against 4-5 min elsewhere.

A claim is keyed by the repository and the lane's selected labels, so a lane
whose labels pin it to one host (`vellum-host-<id>`) shares no key with its
peers, and their claims never count. Such a lane names the labels its jobs
request in `job_claim_labels` (env `TARTCI_JOB_CLAIM_LABELS`); the validator
requires them to be labels the lane advertises, and refuses the key on an
event-class-v2 lane, whose class labels already match across hosts. On a lane
with workflow tiers, `job_claim_labels` names only the labels every class
shares, and the selected tier's class label is appended to the key, so a
merge-group claim and a PR-head claim never cover each other. The labels the
runner registers with, host pin included, are unchanged.

The `vellum-gate` lane runs with both keys on every host: in the ~67 h before
the change, 31 of its 65 boots got no job, because each free host booted for
the same queued proof. A host's claim counts for its declared age, so a peer
whose boot hangs holds the job back until its claim expires or its VM is torn
down (`guest_heartbeat_stale_seconds`); m1's 600 s queue age keeps it last.
Every other lane keeps the key off. The Pulp gate canary remains: m1, once it
has had the pre-clone check for 72 h, with m3, m5 and m5studio as same-window
controls; m5 joins after its ranked-lease canary read.

## Per-slot class preference (merge-group first)

Every slot consults the classes in configured tier order. Slots that advertise
both Pulp gate classes put `pulp-build-merge-group` before
`pulp-build-pr-head`, because a queued merge group blocks the PRs behind it.
The m3 and m5studio slot-2 profiles now use that merge-group-first order. The
PR-first order introduced by d278155 on 2026-09-25 rested on the premise that
the merge-group job kept every other slot in the fleet. That premise expired
when #439 made m1 PR-only. PR-head fairness now comes from m1's two dedicated
slots, while m3 and m5studio preserve merge-group progress.

`assignment_slot_tier_order` on an event-class-v2 lane reorders the class
preference for named supervisor slots:

```toml
assignment_slot_tier_order = { 2 = ["pulp-build-merge-group", "pulp-build-pr-head"] }
```

It renders `TARTCI_ASSIGNMENT_V2_TIER_ORDER=pulp-build-merge-group,pulp-build-pr-head`
into that slot's LaunchAgent only; other slots render nothing and keep today's
behaviour byte for byte. The order must name every tier class exactly once, so a
preference can never become a reservation, and the slot key must be a supervisor
number the lane actually runs. Validation rejects anything else, and the
supervisor refuses the env var outside `event-class-v2`.

The same order drives all three V2 decisions, so the slot never contradicts
itself:

| decision | merge-group-first slot | default slot |
|---|---|---|
| selection, both classes waiting | merge-group | merge-group |
| selection, only merge-group waiting | merge-group (work-conserving) | merge-group |
| pre-mint of a PR-head boot, merge-group arrived | deny, re-select merge-group | deny |
| pre-mint of a merge-group boot, PR-head arrived | admit | admit |
| top-tier receipt (`assignment_top_tier_receipt_max_age_seconds`) | merge-group may use it | merge-group may use it |
| idle retarget (if enabled) | falls back in slot order | falls back in slot order |

Tier numbers keep their configured meaning on every slot (0 is merge-group, 1
is PR-head), so `selected_tier` in events, the per-tier runner group, and the
class-derived lease priority (merge-group `110`, PR-head `100`) are unchanged.
The advertised label set is unchanged too: both classes are still registered in
configured order, so `fleet/advertised-labels.json` does not move. The startup
`LOOP` line prints `tier_order=` so the effective order is visible in the slot
log.

The m1 profile is an explicit exception: it declares only
`pulp-build-pr-head` for its two dedicated slots and does not advertise
merge-group. This keeps the slower 3-core m1 guest out of merge batches while
the merge-capable m3, m5 and m5studio slots protect merge-group demand.

## Release event classes (declared on every host's pulp-gate lane)

An event-class-v2 lane may declare the Pulp release classes after its two gate
tiers. The validator accepts exactly these extras, each with exactly its
workflows, listed contiguously and at most once:

| class | workflows | lease priority |
|---|---|---|
| `pulp-release-tagged` | `Release CLI`, `Sign and Release` | `120` (gate) |
| `pulp-release-pr-gate` | `Release-path PR gate` | `115` (gate) |

The release tiers are declared after the gate tiers, so a slot with no
`assignment_slot_tier_order` keeps gate-first order and takes release work only
when no gate work waits; a slot's order can put them first (below). Each class is its own JIT registration
(base labels plus that one class label), so a release runner cannot take a gate
job and a gate runner cannot take a release job. A lane that does not declare a
class never scans for it, so hosts without the declaration never pick a release
job. Tagged releases lease at `120`, above merge-group, so a release boot is
admitted from gate-reserved capacity; the release PR gate leases at `115`,
gate class like PR-head, so a slot that boots it holds what a gate guest on
that slot would and an ordinary build holding the host's non-gate budget cannot
lock it out. Where ranked VM lease waiters are on, 115 puts a ready release PR
gate ahead of merge-group, PR-head and the other lanes' `gate` class (forge,
spectr, vellum), and behind a tagged release. Each supervisor slot holds at most one lease, so neither release
class can take a second slot's reserve. The numeric
values apply only to registrations carrying the gate base label
`pulp-build-vm`; the legacy `pulp-release` lane (`pulp-build-vm-release`) keeps
`gate`/`vm`.

Every host's `pulp-gate` lane declares the classes, and each host has one
release-first slot that puts both release classes before the gate classes:

```toml
assignment_slot_tier_order = { 2 = ["pulp-release-tagged", "pulp-release-pr-gate", "pulp-build-merge-group", "pulp-build-pr-head"] }
```

That is m5's slot 2 and m1's slot 2; on m3 and m5studio it is slot 1. On m1,
m3 and m5 the other slot orders both gate classes ahead of both release
classes. On m5studio, the release-first canary ("Slot priority", "Release
first"), the other slot also consults `pulp-release-tagged` first and then
keeps merge-group ahead of PR-head and the release PR gate last.

The pre-mint check admits a class only while every class the slot prefers
over it is empty. On 2026-09-26 the release PR gate was ordered last on both
of m5's slots; with gate work queued continuously from 18Z it was selected
once in six hours, that boot was denied at pre-mint when merge-group work
reappeared, and three `Release-path PR gate` jobs waited over three hours
while tagged releases (first on slot 2) were served. A class that no slot
prefers ahead of steady gate demand is a class that host never serves.

Because the order names every class, it is a preference: with no release
queued the slot selects exactly what a default slot selects. Pulp opts in
separately (`PULP_RELEASE_CLASS_TOKENS=1` appends the class label to the
release selectors); until both sides are live, release jobs keep riding idle
gate runners. Rollback is unsetting the Pulp variable; host-side, delete the
extra tiers and slot order, re-render, and reload the slots at an idle
boundary.

## Ranked VM lease waiters (opt-in, m5 canary)

Every VM lane on a host (both Pulp gate slots, the release lane, forge, spectr,
vellum) takes its lease from the same host lease store, and each acquires only
after its own Shipyard admission precheck (about 40 s). The store used to grant
the first caller, whatever its priority. On m5 on 2026-09-27 a governed agent
build (priority 40) held its 6-core non-gate share, which by design is never
taken, leaving room for exactly one 6-core VM:

- 05:42:05Z slot 2 claimed a `pulp-release-pr-gate` job (queued=1); 05:42:13Z
  forge's `clone_start` took the cores; 05:42:42Z slot 2 `lease_denied axis=cores`;
- 06:10:37Z slot 2 claimed again; 06:10:46Z slot 1's merge-group clone took the
  cores; 06:11:20Z slot 2 denied.

`[leases] rank_vm_waiters = true` in the fleet profile (env
`TARTCI_RANK_VM_WAITERS=0|1` for one shell) turns on ranking:

- A lane registers as a **waiter** (`leases.py wait`: priority, cores, memory,
  timestamp, owner pid) right after it claims a job and before its admission
  precheck, keeps it while its acquire is denied and it waits for capacity
  (bounded by `TARTCI_VM_WAITER_HOLD_SECS`, default 900), and withdraws it
  when it stops wanting a VM. A grant withdraws it atomically.
- A VM acquire (or a parked warm VM's `resize` to a core lease) is **deferred**
  (`reason=deferred_to_waiter`, rc 75, event `lease_deferred_to_waiter` naming
  `waiter_lane` and `waiter_priority`) while a strictly higher-priority waiter,
  refreshed within `waiter_fresh_secs` (default 90) and whose owner process is
  still alive, fits in the host now and would not fit once this lease is
  granted.
- Priorities are the ones lanes already lease at; nothing new is ranked:
  release tagged 120 > release PR gate 115 > merge-group 110 > PR-head 100 =
  the `gate` class (100: forge, spectr, vellum) > `vm` (60).
- **Ties stay first-come**: equal priority is not ranked, so the first acquire
  wins exactly as before.
- **Work-conserving**: a waiter that cannot fit anyway (it needs more than is
  free) blocks nobody, and a grant that leaves room for the waiter is never
  deferred. Disk is not ranked; it is one volume-wide axis both leases are
  already admitted against.
- **Agent and other non-VM builds are untouched**: they cannot register, are
  never deferred, and keep their whole non-gate share.

Off (the default, every host but m5), the store never reads or writes
`waiters.json` and the supervisor never registers; admission is byte-for-byte
today's. `tartci leases status --json` lists live waiters when the knob is on.

What it changes on m5: both races above involved the release PR gate. It
leases at 115, above forge's `gate` class (100) and slot 1's merge-group (110),
so with the knob on both races flip: the forge and merge-group acquires are
deferred while the release PR gate waits and fits. More generally the knob stops
a higher class (a tagged release at 120, the release PR gate at 115,
merge-group at 110) losing the one free slot to any lower VM lane whose acquire
happens to land first. With the knob off (every other host) 115 admits exactly
as 100 did: any priority at or above the gate class only lifts the non-gate
budget.

### Canary proxy

- **Mechanism**: a higher-priority VM lane loses a core race to a lower one.
- **Count**: per hour of contention, the `lease_denied` events with
  `fields.axis == "cores"` for a lane at priority P where a `lease_acquired` (or,
  before this change, a `clone_start`) by another lane at priority below P
  occurred within the 90 s before it. Hours with no `lease_denied` at all are
  not contention and are excluded from the denominator.
- **Source**: every lane's `events.jsonl` on m5 (under each supervisor's state
  directory), merged and sorted by `ts`. `lease_denied` carries `priority`;
  `lease_acquired` carries `priority`, `kind` and `lease_cores`.
- **Control**: the total `lease_acquired` count over the same window must be
  non-zero; a zero means the logs were not read or the lanes were idle, and the
  race count proves nothing.

```bash
cat ~/.tartci/state/macos-fleet/*/events.jsonl |
python3 -c '
import json, sys, datetime as dt
CLASS = {"background": 10, "build": 40, "vm": 60, "runner": 80, "gate": 100}
def prio(v):
    v = str(v)
    return int(v) if v.isdigit() else CLASS.get(v)
t = lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
def parse(line):
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) and "ts" in row else None
rows = sorted(filter(None, map(parse, sys.stdin)), key=lambda r: r["ts"])
acq = [r for r in rows if r["event"] == "lease_acquired"]
denied = [r for r in rows if r["event"] == "lease_denied"
          and (r.get("fields") or {}).get("axis") == "cores"]
def inverted(d):
    p = prio(d["fields"].get("priority"))
    return p is not None and any(
        a["runner"] != d["runner"]
        and dt.timedelta(0) <= t(d["ts"]) - t(a["ts"]) <= dt.timedelta(seconds=90)
        and (prio(a["fields"].get("priority")) or 0) < p for a in acq)
lost = sum(map(inverted, denied))
hours = len({d["ts"][:13] for d in denied})
print(f"control lease_acquired={len(acq)} cores_denials={len(denied)} "
      f"contended_hours={hours} inversions={lost} per_hour={lost / max(1, hours):.2f}")'
```

`lease_acquired` is emitted by the same change (knob on or off), so the
before/after needs either a day of these bytes with the knob off or the hand
count above as the baseline; `clone_start` carries no priority. Expect the
inversion count to fall to zero on m5 while the control stays comparable, and
`lease_deferred_to_waiter` to appear roughly where inversions used to. Rollback: delete the `[leases]` table,
re-render, restart the supervisors at an idle boundary; stale waiters expire on
their own within `waiter_fresh_secs`.

## Slot priority (opt-in, off on every host)

Every lane on a host claims a free macOS VM slot first-come (the two-guest cap,
`macos-vm-cap.lib.sh`). A lane that has just torn its VM down loops straight
back to a claim, so whichever lanes are already running keep winning. Ranked VM
lease waiters (above) cannot help: they rank lanes that already hold a slot
reservation, and the slot itself is taken before any lease is asked for. On
2026-11-09 the Pulp gate lanes held most slots fleet-wide while Vellum's
merge-group jobs waited: GitHub's own record for the day puts Vellum
merge-group job queue wait at p50 634 s and p90 2896 s, against Pulp's p90
823 s (pulp-planning ledger, "merge-group job queue wait").

`[leases] slot_priority` (`off` | `shadow` | `on`, env `TARTCI_SLOT_PRIORITY`;
default `off`) ranks the lanes on one host that want the next free slot
(`scripts/slot_priority.py`, `providers/tart-macos/slot-priority.lib.sh`):

- **Order**: a tagged release (with `slot_priority_release_first`, below),
  then a `merge_group` job of any repository, then other events (push,
  release, schedule, dispatch), then `pull_request`. A merge-group job can land
  up to five PRs; a PR-head job only reports on one.
- **Across repositories, oldest first**: within a class the longest wait ranks
  first, whatever its repository and whichever lane asked first, so one
  repository cannot starve another. The wait is the older of the job's GitHub
  `created_at` and the lane's `waiting_since` (when it began wanting a slot
  without a break), so a lane refused for a long time keeps ageing even when
  its scan only sees a newer job.
- **A lane is ranked by the job it will likely get**: GitHub, not tartci,
  picks which matching job a new runner takes, likeliest the oldest. A lane
  whose labels match several classes (vellum-gate) ranks as the class of its
  OLDEST counted job, so a fresh merge-group job cannot win the slot for an
  old PR job.
- **No class starves either**: a lower-class job queued
  `slot_priority_promote_after_secs` (default 2700, 300-14400) ranks as
  merge-group.
- **Ties**: when the lane began waiting, then the lane id (a strict order).
- **Only lanes that can boot now count**: an intent ahead of this lane holds a
  slot back only if its VM lease would be admitted on the store's current core
  and memory budget, counting the intents ahead of it as granted, and (with
  ranked VM lease waiters on) only if that acquire would not be deferred to a
  higher-priority live waiter. A lane that defers withdraws its own lease
  waiter, so it never blocks the lane it yielded to.
- **Intent lifecycle**: refreshed at the claim point, and while the lane waits
  for lease capacity (lease-fit `not_now`), because that is exactly when the
  next freed slot must find it ranked; withdrawn when it claims, when its lease
  can never fit, and on every path that stops wanting a slot (yields, storage,
  pool drain, JIT denial, claim-delay backoff, blind scan, unknown inventory).
  Dead owners and intents not refreshed within 240 s count for nothing. The
  store lock is taken with a 10 s bound; a timeout claims first-come. A lease
  wait only keeps an intent the lane already has; it never creates one, and it
  does not rescan, so after 300 s its class reads `unknown` and it ranks only
  through its `waiting_since`. Withdrawing the lease waiter on a deferral also
  gives up the lane's place in the lease-waiter order.
- **Non-preemptive and within the budgets**: it decides only who takes a FREE
  slot. It never reads or writes a lease, a reservation or a VM, never stops
  running work, and changes neither the cap nor any core or memory budget.
- **Bounded**: in mode `on` an outranked lane does not claim this poll
  (`slot_priority_deferred`, and a `demand_waiting` sample with
  `reason=slot_priority`). After `TARTCI_SLOT_PRIORITY_MAX_DEFER_SECS` (default
  300) of consecutive deferrals it claims anyway (`slot_priority_defer_expired`):
  the lane it waits for is not taking the slot, and a free slot must not idle.
- **No extra GitHub call**: the event class and age come from the scan the lane
  already made. With the knob on, `queue_scan.py` and `assignment_scan.py` get
  `--detail-file` and write the matched jobs' run `event` and oldest
  `created_at` beside their count (`<state>/<runner>.slot-demand.<class>.json`).
  The cross-lane meeting point is the existing lease store
  (`slot-intents.json` under its lock).
- **Shadow** records intents and, whenever ranking would have given the slot
  to another lane, logs `slot_priority_shadow` "would have given slot to
  `<lane>` (`<repo>` `<class>`)" and claims first-come exactly as with the knob
  off.

Known limits, so a reading is not over-interpreted:

- A single-label lane (vellum-gate) ranks by the best job it counted, but
  GitHub, not tartci, picks which matching job its runner gets. A Vellum lane
  that won the slot for its merge-group job may be handed its PR job instead.
- An event-class-v2 scan stops at its first witness, so its age is a lower
  bound on the class's oldest job (`witness_only` in the detail file; the
  intent records `queued_at_lower_bound`). A single-label scan counts every
  match, so its age is exact. The `waiting_since` floor bounds the difference.
- Unknown or stale detail (older than 300 s) ranks with `pull_request`: a lane
  moves ahead only on evidence.
- It does not lift Vellum's lease-fit denials. On m5, Vellum leases at `vm` and
  is held to the non-gate budget ("cores=6 budget=6 used=10"); that is the
  budget, which this does not change. Fair share (below) does.

### Release first

`[leases] slot_priority_release_first` (boolean, default `false`; env
`TARTCI_SLOT_PRIORITY_RELEASE_FIRST=0|1`; on only in the m5studio canary) ranks a
lane about to serve a tagged release ahead of every other class, including a
promoted lower class, for the next free slot on its host.

On 2026-11-10 Pulp's release run for v1.99.1 (the first since v1.80.0 able to
publish) had every build job green and then queued "Smoke darwin-x64", a 40 s
job, from 11:34Z with no runner. Every host was at its two-VM cap with ~40 min
PR and merge-group gates. Two things kept it waiting: only one slot per host
preferred the release class, so when the other slot freed first it took gate
work; and a release-tier lane ranked as `other` (its run's event is a tag
`push`), below any merge-group intent on the host.

- **Evidence is the class label**: a release run's event is `push`, so the
  scan cannot tell it from any other push. The lane's selected registration
  can: a registration carrying `pulp-release-tagged` (the event-class-v2
  release class, `scripts/macos_fleet_lanes.py` `V2_EXTRA_CLASS_WORKFLOWS`)
  can only be handed a tagged-release job. Such a lane ranks as `release`
  (rank -1, below merge-group's 0, so the starvation promotion never lifts
  anything past it).
- **Only on evidence**: a release lane with no fresh scan detail stays
  `unknown`, as before. The release PR gate (`pulp-release-pr-gate`) is a PR
  check and keeps its ordinary class.
- **Unchanged without a release**: no lane carries the label unless a tagged
  release is what it selected, so with none queued the order is exactly the
  one above.
- **Every canary slot asks for releases first**: on m5studio each `pulp-gate`
  slot's `assignment_slot_tier_order` starts with `pulp-release-tagged` and
  then keeps its previous order. A slot whose first class has no queued job
  selects what it did before.
- **It covers the whole release run**: `pulp-release-tagged` carries the
  Release CLI build legs and Sign and Release as well as the smokes, so a
  release run takes each newly freed canary slot until its matrix drains.
  Gate work waits at most that long plus `TARTCI_SLOT_PRIORITY_MAX_DEFER_SECS`.
- **Bounded, non-preemptive**: nothing running is stopped; the gate a release
  displaces waits for the next free slot. A lane deferring to a release still
  claims after `TARTCI_SLOT_PRIORITY_MAX_DEFER_SECS` (default 300) if the
  release lane does not take the slot. No slot is reserved for releases.

**Why a canary, and what it costs.** Selection scans classes in slot order
and stops at the first with demand, and the pre-mint check rescans the
classes preferred over the selected one. A slot that consults the release
class first therefore adds one two-workflow scan (`Release CLI`,
`Sign and Release`) to every live selection (cache 120 s) and every pre-mint
whenever no release waits, all on the shared GitHub App REST budget. That
budget is the fleet's single point of failure (a 15k/h drain stalled every
lane on 2026-11-09), and a scan error in the first class blinds the slot
(fail-closed) where before it blinded only the host's release-first slot. Hence
one host first: m5studio adds the scan to one slot (its slot 2).

**When to widen** to m3, m5 and m1 (key plus the slot order), compare at least
a week of canary against the week before, all of:

- Tagged-release job wait from GitHub (`started_at - created_at` of
  `pulp-release-tagged` jobs, p50 and max), and the share of them that started
  on m5studio. A falling wait with m5studio taking release jobs is the benefit.
- App REST rate per hour on the scan App (`X-RateLimit-Used` deltas, or the
  per-host scan call ledger), m5studio against the hour before. Widening to
  three more hosts adds about three more slots' worth of that delta; it must
  leave the fleet well under the 15k/h budget at peak.
- `scan_blind` / `assignment_v2_*` ERR events on m5studio that name the
  release tier: any rise is the fail-closed cost above.
- Merge-group and PR-head job wait on m5studio: they must not regress beyond
  the release runs' own length.

Rollback: delete the key and restore m5studio slot 2's previous order
(merge-group, PR-head, then the release classes), re-render, restart the lanes
at an idle boundary.

### Measuring it

Turn it on in `shadow` on one host first. Count per hour, from the merged lane
event logs, `slot_priority_shadow` events by `winner_class`/`winner_repo`
against the control `job_claim` count over the same window (a zero control
means the logs were not read). Before `on`, compare the metric in the ledger,
merge-group job queue wait p50/p90 per repository from GitHub's
`started_at - created_at` for self-hosted macOS jobs of `merge_group` runs, and
the lane-event slot wait (first `demand_waiting` to the next `job_claim`).
Rollback: set `slot_priority = "off"` (or delete the key), re-render, restart
the supervisors at an idle boundary; stale intents expire on their own.

## Fair share between repositories (on every macOS fleet host)

Every VM lane leases at its repository's priority: Pulp's gate derives 100 (PR
head) or 110 (merge group), Vellum leases at `vm` (60). Admission and ranked
waiters both follow that priority, so while Pulp has a steady queue every VM a
host frees goes back to a Pulp lane. A Vellum lane is held to the non-gate
budget, so lease_fit answers `not_now` and it never reaches an acquire, never
registers a waiter, and slot priority (above) cannot rank it. On 2026-11-10 13
Vellum "Locked macOS GPU proof" runs waited up to four hours this way: m1 and
m5 refused the lease ("cores=3 budget=3 used=6", "cores=6 budget=6 used=14")
and m5s had both VM slots held by Pulp.

`[leases] fair_share_after_secs` (0 = off, the default; else 300-14400; env
`TARTCI_FAIR_SHARE_AFTER_SECS`) makes a lane *starved* once it has had work it
could not place for that long (`scripts/fair_share.py`):

- **Promotion**: a starved VM lease is admitted at gate priority, against the
  host budget instead of the non-gate share, but only while another
  repository holds or waits for a VM on the host. A lane alone on a host is
  admitted exactly as before.
- **Yield**: a lane of another repository whose lease would take the cores or
  memory a starved lane needs, or the last free VM slot it needs
  (`--vm-slot-cap`), yields. lease_fit answers `yield` (exit 5, event
  `fair_share_yield`, heartbeat `lease-wait`) so the yielding lane neither scans
  nor asks Shipyard; acquire defers it (`deferred_to_waiter`) as a backstop.
- **Order**: among starved lanes of different repositories, the one starved
  longest goes first. A starved lane is not ranked below another repository's
  waiter on priority alone.
- **Last-slot yield**: a lane with `last_slot_yield` (m5's Vellum lane, which
  yields the host's last guest slot while any Pulp gate job is queued) stops
  yielding to that other repository while it is starved: its priority scan
  answers 0 without asking GitHub (`yield_demand` in
  `providers/tart-macos/runner.sh`, event `fair_share_yield_lifted` once per
  starved stretch), in the loop and in the post-claim recheck. Without this
  the starved lane would still yield every time Pulp had queued work, which is
  the steady-queue case fair share exists for. A `yield_to_workflow` lane
  yields to its own repository and is unaffected.
- **At most half the slots**: while another repository has demand, a lane
  admitted through fair share holds at most half the host's VM slots (rounded
  down, at least 1; 1 of 2 on every current host). A repository already at its
  share is neither promoted nor yielded to; it waits like any other lane, so
  starvation does not move onto Pulp's required gate.
- **Within one repository nothing changes**: merge-group above PR-head, the
  priorities lanes lease at and ranked waiters all stay as they are.
- **The clock**: per lane, in the supervisor. It starts at the older of the
  first scan that counts queued work and the oldest matching queued job's
  `created_at` (from the scan's slot-demand detail file, written while slot
  priority is on; no extra GitHub call). It survives polls that cannot scan
  (lease_fit `not_now`, blind scans). The lane's own part resets when it is
  granted a VM (`fair_share_granted`) or a scan finds its queue empty, but the
  job age carries across a grant, so a lane whose backlog is still older than
  `fair_share_after_secs` is starved again at its next poll. A starved lane
  registers or refreshes its lease waiter (with `repo` and `demand_since`) on
  every poll it cannot place work, which is how other lanes see it. A lane
  that lease_fit holds at `not_now` from its first poll never scans, so with
  its clock unstarted it scans once per `TARTCI_FAIR_SHARE_PROBE_SECS`
  (default 300) to start it (`fair_share_demand_probe`).
- **Work-conserving**: a starved lane that would not fit even at gate priority
  blocks nobody. A stale claim corrects itself: whenever a starved waiter holds
  another lane back, the capacity it needs is free, so its own lease_fit passes
  and its next scan either places the job or finds the queue empty and drops
  the waiter.

Known limits: the cost of fairness is paid by the gate. A Pulp job may wait one
Vellum job's length for a VM after Vellum has waited `fair_share_after_secs`,
and while a promoted Vellum VM runs it counts as gate use. Without slot priority's detail file the clock counts only the lane's
unplaced demand, so a lane granted a VM starts again at its next unplaced job. A parked warm VM upgraded in place (`leases resize`)
is not ranked by fair share; it already holds its slot. While a starved lane's
last-slot yield is lifted, that lane's fail-closed last-slot guard is off for
its boot by design, so a Pulp gate job may wait one Vellum job length for m5's
last slot.

### Measuring it

Per host and per repository, from the lane event logs: `fair_share_granted`
(and its `waited=`), `fair_share_yield`, and the slot wait from the first
`demand_waiting` or `lease_unfit_now` to the next `job_claim`, against the
`job_claim` count as the control. From GitHub: Vellum "Locked macOS GPU proof"
queue wait p50/p90 (`started_at - created_at`) before and after, and the same
for Pulp's `macos` merge-group jobs, which must not regress beyond one Vellum
job length. Rollback: delete the key, re-render, restart the lanes at an idle
boundary; waiters expire on their own.

### Rollout

| host | `fair_share_after_secs` | status |
|---|---|---|
| m5 | 1800 | canary, on |
| m5studio | 1800 | on (this step) |
| m3 | off | next, gated on measured Vellum wait |
| m1 | off | after m3, same gate |

m5studio's own Vellum lane carries no `last_slot_yield`, so only promotion and
yield apply there.

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
or an unreadable lease store makes the whole report `unknown`, never zero.
The supervisor keeps its current phase fresh while its queue scan runs
(`providers/tart-macos/heartbeat-keepalive.lib.sh`, every
`TARTCI_HEARTBEAT_KEEPALIVE_SECS`, default 30, 0 = off): a 90-200 s scan used to
age an idle lane's heartbeat past 120 s, and on m3 7 of 20 supply samples read
`unknown` for that reason alone. The refresh stops at the supervisor's next
heartbeat and exits within one interval of the supervisor dying, so a dead
lane still goes stale. A
Tart inventory that cannot be read (after the same retry the supervisor uses)
blocks new clones on that host. Its supply report has `"inventory":
"unavailable"` and `free: 0`, so fallback hosts do not count it as capacity.

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

### Enabling it (m1 `pulp-gate`, preferring m3 only)

m1's `pulp-gate` lane no longer sets the key (2026-10-16): as an overflow-only
host it takes only work queued 600 s or more. Before that it set
`fallback_preferred_hosts = ["studio"]`. m5 was left
out on purpose: its `pool supply` report counts lease fit, not host load, so a
starved m5 still reports free slots, and deferring to it keeps young work on the
starved host. m5 is also unreachable over SSH at times while its runners still
serve, and an unreadable peer turns every decision into a hold. Add `"m5"` back
once m5 is reachable and healthy.

The general recipe follows.

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
