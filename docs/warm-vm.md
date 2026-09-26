# Warm pre-booted gate VM (opt-in, off by default)

A cold gate VM spends about a minute between "the lane decided to serve a job"
and "VM up": the lease, the CoW clone, the boot and the SSH wait (0.59 + 0.45
min mean in the 2026-09-26 queue-wait breakdown, planning
`2026-09-23-build-speed-plan.md`). A warm VM pays that while the lane is idle:
one VM is booted to "VM up" and parked. When a job arrives the parked VM becomes
the job's VM, and everything after boot runs exactly as for a cold VM: the
runner-version check, the Aqua and Chrome preflights, the Shipyard admission
check at the JIT boundary, the V2 pre-mint recheck and the single-use JIT mint.

Nothing is registered with GitHub while parked. A JIT config is single use,
fixes its class label (merge-group or PR-head) at mint, and can expire, so the
class is chosen at hand-off, not at park. The admission check (~1.1 min) and
runner registration (~0.5 min) therefore stay on the critical path; the saving
is the ~1 min of lease + clone + boot.

No host enables it today. It is meant for a future host with RAM and cores to
spare; see "Trying it on a new host" below.

## How it behaves

| rule | mechanism |
|---|---|
| at most one parked VM per host | only supervisor slot 1 of the one lane with `warm_vm = true` parks (validation refuses a second warm lane), plus a host-wide claim under `~/.tartci/state/warm-vm/` |
| parked holds memory, not cores | a memory-only lease (`leases.py acquire --memory-only --cores 0 --mem-mb N`): the guest memory and the disk-growth reservation are charged, cores are not |
| hand-off takes the cores | `leases.py resize` upgrades the SAME lease under one store lock: admission is decided against every other lease and the record is rewritten in place, so there is no released window, and the tart guardian and disk reservation are untouched. A denial leaves the lease and the VM exactly as they were: the parked VM waits for cores exactly as a cold boot would |
| Apple's 2-VM limit | the parked VM claims one macOS VM slot through the ordinary reservation every lane's slot claim counts; the lane's own hand-off reuses that reservation |
| does not hide demand | the per-poll lease-fit gate (`lease_fit.py`) does not count a memory-only lease, so neither the parking lane nor another lane skips the poll that would upgrade the parked VM or ask it to yield |
| yields to other demand | a lane on the host that has queued work and cannot get a VM slot, or is denied a lease on the MEMORY axis, while a warm VM is parked, leaves a demand marker; the parked VM is torn down within ~5 s (its sleep polls in 5 s slices). A core-axis denial does not ask it to yield: a parked VM holds no cores |
| expires | after `warm_vm_max_park_seconds` (default 1800), when its guest dies, when the pool is off or draining or the pool transition lock is held (fleet self-update drains first), and on any supervisor exit (launchd bootout, reload, `pool off --now`) |
| one job, one VM | a sibling supervisor of the same repository that sees demand while the VM is parked defers that poll and asks the parked supervisor to hand off (it invalidates its V2 selection cache and wakes), so one job does not get both a warm hand-off and a cold boot |
| cooldown | after a teardown the lane waits `TARTCI_WARM_VM_COOLDOWN_SECS` (default 120) before parking again |
| unproved teardown | a parked VM whose deletion cannot be proved becomes the lane's ordinary pending-delete VM, keeping its lease and its slot reservation until the loop proves it gone (or restarts fail-closed), exactly like a job VM |

Events (in the lane's `events.jsonl`):

| event | detail |
|---|---|
| `warm_parked` | `vm reserved_cores=0 reserved_mem_mb vm_cores boot_seconds lease` |
| `warm_handoff` | `parked_seconds labels cores mem_mb`; then the ordinary `admission_check`, `mint_jit`, `boot_ok` |
| `warm_handoff_denied` | the core upgrade was refused; the VM stays parked |
| `warm_expired` | `reason=max_park_age\|pool_closed\|yield_demand\|vm_died\|vm_unreachable\|supervisor_exit parked_seconds` |
| `warm_park_failed` | the park boot failed (`lease_denied=1` when the lease store refused it) |
| `warm_yield_requested` | written by the OTHER lane that asked the parked VM to yield (`reason=slot_full\|memory_denied`) |
| `warm_sibling_defer` | a sibling deferred to the parked VM (once per episode) |

State: `tartci pool status` prints a `warm vm:` line (`none parked`, `parked: <vm> ... reserved cores=0 mem_mb=...`, `STALE`, `OVERDUE`) and `--json` carries a `warm_vm` object. `tartci doctor` reports the `warm_vm` finding (`warm_vm_none`, `warm_vm_parked`, `warm_vm_stale`, `warm_vm_overdue`, `warm_vm_unreadable`). The heartbeat phase is `warm-parked`; `pool off` treats a lane holding only a parked VM as idle (stopping it discards the VM and loses nothing).

## Trying it on a new high-RAM host (for example an M6)

### 1. Prerequisites

The rule of thumb from the queue-wait analysis:

- RAM >= agent peak working set + 2 x gate VM memory (the parked VM plus the
  running one) + 8 GiB headroom, AND
- lease universe >= 2 x gate VM cores + the agent core floor.

With 16 GiB / 12-core gate VMs and ~64 GiB of agent peak, that is about
>= 128 GiB RAM and >= 32 leasable cores. Below that core count the warm VM
mostly sits parked while the job waits for cores (`warm_handoff_denied`), which
is today's dominant failure mode and buys nothing. Check the host's figures:

```bash
tartci host-profile --json \
  | python3 -c 'import json,sys; p=json.load(sys.stdin); print(p["lease_capacity_cores"], "cores,", p["lease_capacity_mem_mb"], "MB leasable,", p["agent_floor_cores"], "agent floor")'
tartci leases status
```

The host
must already be onboarded and serving cold gate jobs, on a tartci generation
that contains this feature (`tartci pool status` prints a `warm vm:` line).

### 2. Measure the idle cost before enabling

A parked VM costs its guest RAM (explicitly leased) and whatever CPU an idle
macOS guest burns (a post-boot guest can index before it settles). Measure on
the new host with the pool drained, so the probe does not take a live slot:

```bash
tartci pool drain
tart clone pulp-build-runner:latest warm-probe
tart set warm-probe --cpu 12 --memory 16384
tart run --no-graphics warm-probe >/tmp/warm-probe.log 2>&1 &
for minutes in 5 15 30; do
  sleep $(( minutes == 5 ? 300 : 600 ))
  echo "t+${minutes}m"; ps -axo rss=,%cpu=,command= | grep '[c]om.apple.Virtualization.VirtualMachine'
done
tart stop warm-probe; tart delete warm-probe
tartci pool on
```

RSS is in KB. Record the 30-minute RSS and %CPU: the RSS is what the host gives
up while parked (compare with the leased `--memory`), and a %CPU that has not
settled by 30 minutes is a cost agents on this host will feel.

### 3. Baseline the minutes to beat

For a week before enabling, measure the provisioning segment the warm VM
removes, per served job, from the lane's own events (cold boots: `clone_start`
to `boot_ok`):

```bash
python3 - ~/.tartci/state/macos-fleet/pulp-gate/events.jsonl <<'PY'
import datetime as dt, json, statistics, sys
def ts(v): return dt.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").timestamp()
start, cold, warm = None, [], []
for line in open(sys.argv[1]):
    try: e = json.loads(line)
    except ValueError: continue
    if e["event"] in ("clone_start", "warm_handoff"):
        start = (e["event"], ts(e["ts"]))
    elif e["event"] == "boot_ok" and start:
        (warm if start[0] == "warm_handoff" else cold).append(ts(e["ts"]) - start[1])
        start = None
for name, xs in (("cold clone->boot_ok", cold), ("warm handoff->boot_ok", warm)):
    if xs: print(f"{name}: n={len(xs)} p50={statistics.median(xs)/60:.2f} min")
PY
```

Add the pre-clone part (`admission_precheck` to `clone_start`, the lease and
disk checks) from the same file if it matters on the host. For the job's whole
wait, use the queue-wait method of the plan (GitHub `created_at` to
`started_at`, joined to the per-VM events by runner name).

### 4. Enable it (exact profile lines)

In the new host's profile, on its `pulp-gate` lane:

```toml
warm_vm = true
# optional: tear the parked VM down after this long (300-14400 s, default 1800)
warm_vm_max_park_seconds = 1800
```

Only supervisor slot 1 renders `TARTCI_WARM_VM=1` (plus `TARTCI_WARM_VM_DIR`
and, when set, `TARTCI_WARM_VM_MAX_PARK_SECS`). Validate and render through the
normal `tartci fleet-macos` path and reload the lane at an idle boundary (a host
self-update does this). Within one idle poll plus the cooldown, `tartci pool
status` should show `warm vm: parked: ...` and the lane log a `warm_parked`
event.

### 5. Measure the benefit

After a week, rerun step 3 on the same file. Minutes saved per day is
`handoffs/day x (cold p50 - warm p50)`; the plan expects ~1 min per served job.
Also count, from `events.jsonl`:

- `warm_handoff` vs `warm_parked`: most parks should end in a hand-off. Many
  `warm_expired reason=max_park_age` means the host is idle enough that the VM
  mostly costs RAM; lengthen nothing, consider turning it off.
- `warm_handoff_denied`: the parked VM waited for cores. Frequent denials mean
  the core rule of thumb is not met on this host.
- `warm_expired reason=yield_demand`: other lanes needed the slot or memory.
  Frequent yields mean the warm VM is competing with forge/spectr/vellum work.
- Idle cost: the parked VM is itself the sample; `ps` it as in step 2 at a few
  points in its park.

### 6. Turn it off

Delete the two profile keys, re-render and reload the lane: the supervisor's
exit discards the parked VM and releases its lease. For an immediate stop
without a reload, `tartci pool drain` tears it down within seconds (the pool
state is read every 5 s while parked); `tartci pool on` afterwards re-opens
the pool, and the VM is parked again only if the key is still set. Confirm with
`tartci pool status` (`warm vm: none parked`) and `tart list`.
