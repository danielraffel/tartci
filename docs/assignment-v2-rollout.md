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
down to ordinary `vm` priority. The existing explicit
`TARTCI_VM_LEASE_PRIORITY` operator override retains precedence for a valid
single-class runner.

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
