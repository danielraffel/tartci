# Soft CPU admission: share idle cores, keep memory strict

Status: design, 2026-09-30. Opt-in per host (`[host] cpu_admission = "soft"`); `"strict"` (today's behaviour) stays the default.

## Problem (measured on m5studio, 2026-09-30)

- Admission treats CPU like memory: every lease subtracts its full core count from a budget, and non-gate
  leases are capped at `total - reserved_gate_cores` **even when no gate VM is running**.
- Two 12-core gate VMs leased 24 of 28 leasable cores; a host build asking for ~12 was refused on the
  **host-total** axis (24 + 12 > 28; the non-gate class cap was not the binding limit) and fell to the agent floor: 6 cores at `taskpolicy -b` (background QoS: lowest CPU priority **and** throttled disk I/O).
  Single files took 6–8 minutes; a 101-step rebuild took 40 minutes.
- Over one minute in the same window, the two VMs holding those 24 cores used **2.5–4.2 cores** between them
  (`ps` %CPU of `com.apple.Virtualization.VirtualMachine`). That sample caught a quiet phase of the gate job
  (compile phases use far more), so it shows the reservation was not always used, not that VMs need ~2 cores.
- Unrelated load inflated the picture: another session on the host ran ~24 busy-loop shells, pushing load to 54–87.
- Memory, by contrast, is a hard limit: the July 2026 host failures were memory exhaustion from unbudgeted builds.

CPU over-commit degrades gracefully (time-sharing); memory over-commit does not (swap, jetsam). The governor
should treat them differently.

## Design

### 1. Memory: unchanged and strict
Every lease (VM and build) still charges memory against `total_mem_mb`, with the gate memory reserve intact.
This is what prevents the July failure class and is not relaxed.

### 2. CPU: admission by slots, VM cores accounted by measurement
In soft mode the core axis no longer refuses non-gate builds on reserved-but-idle VM cores. Instead:
- **Gate VMs** are admitted by the existing macOS VM cap (2) and memory. VM cores stay **accounted**, but by
  measured busy CPU (a two-sample delta of the VM processes' CPU time over a few seconds), not by `vm_cores`.
- **Non-gate builds** (agent / interactive / governed builds) are admitted by a **slot count**,
  `agent_build_slots` (e.g. 3 on m5studio). Up to that many run at once. **There is no waiting:** a build beyond
  the slots, or arriving when measured headroom is low, gets a smaller grant at lower QoS (the floor) at once.
  This is the guarantee asked for: "N agents with worktrees + 2 VMs, always".
- `lease_fit` (the pre-check) and `admits` (the decision) must share the soft branch, or they will disagree.
- Each granted build gets a **job count** computed at grant time (§4), capped by `agent_build_max_jobs` (the
  measured point of diminishing returns, §5). This is how builds scale up when the machine is idle and stay
  modest when it is busy.

### 3. Priority decides contention
- Gate VMs: default QoS (unchanged).
- Granted non-gate builds: `agent_build_qos` (default `utility`, not `background`): below CI, so CI wins when
  both want a core, but without background's disk-I/O throttling. Applied via `taskpolicy -c <qos>` by the
  caller that already applies `-b` for the floor (`governed-build.sh`).
- Floor grants: `agent_floor_qos`, now configurable (today hard-coded `background`); default stays
  `background` for strict mode, `utility` recommended for soft-mode hosts.

### 4. Dynamic job count (scale with real headroom)
At grant time: `jobs = clamp(agent_build_min_jobs, free_cpu_estimate, agent_build_max_jobs)`
where `free_cpu_estimate = total_cores - busy VM cores (measured, §2) - the jobs already granted to active builds`
(their `-j` is known, so subtract it rather than dividing by a build count), and it never exceeds the memory
budget `(free_mem_for_builds // per_job_mem_mb)`. If host-vitals is stale (> 2 min),
fall back to `agent_build_max_jobs // 2`. Jobs are fixed for the life of the lease (make/ninja `-j` cannot change
mid-build); the next build re-reads headroom.

### 5. Measure the knee, don't guess it
Two benchmarks per host class. Preflight each run: pool drained, no busy shells (`ps` load check), and record
the load. Run the `-j` values in **randomised order**, at least three repeats each.
- **Host build scaling:** the same incremental Pulp rebuild (a fixed set of touched files) with
  `CCACHE_DISABLE=1` (a warm cache measures the cache, not the compiler) at `-j` ∈ {4, 6, 8, 10, 12, 16, 20}.
  Record wall time **and** total CPU-seconds per run; CPU-seconds rising while wall time stays flat is the knee's
  signature and a control that the extra jobs were actually used. Pick `agent_build_max_jobs` at the knee: the
  smallest `-j` within 10% of the best time.
- **Gate VM scaling:** the same gate job (pinned commit) via `tartci up macos` at `vm_cores` ∈ {6, 8, 12}.
  Pick `vm_cores` at the knee the same way.
Load-independent proxies stay primary (memory pressure, refused/floor grants, queue wait per job); wall time is
reported with the load it was measured under.

## Configuration (host profile `[host]`)
| Key | Strict default | m5studio (soft) proposal | Notes |
| --- | --- | --- | --- |
| `cpu_admission` | `"strict"` | `"soft"` | |
| `agent_build_slots` | n/a | 3 | concurrent non-gate builds |
| `agent_build_max_jobs` | n/a | 12 to start; §5 sets it | per-build `-j` ceiling |
| `agent_build_min_jobs` | n/a | 4 | |
| `agent_build_qos` | n/a | `"utility"` | |
| `agent_floor_qos` | `"background"` | `"utility"` | configurable (slice 1, shipped) |

Starting values for the other hosts, before their own §5 runs: M3 slots 2, max 10; m5 (laptop) slots 2, max 6,
strict first; m1 stays strict.
## Status / interfaces that change
- `leases.py`: `core_and_memory_verdict` gains a soft branch (core axis advisory: reports would-exceed but does not
  refuse; slot axis enforced for non-gate); `acquire` returns `jobs` and `qos` in the grant; `status` shows
  slots used/available and the job estimate.
- `host_profile.py`: new keys, validation, and `TARTCI_AGENT_BUILD_*` / `TARTCI_AGENT_FLOOR_QOS` exports;
  `agent_floor_qos` stops being hard-coded.
- Pulp `tools/ci/governed-build.sh`: honour `qos` from the grant (`taskpolicy -c <qos>`) and size `-j` from the
  grant's `lease_size_cores`; today it only knows `-b` for the floor.
- Pulp CLI (`tools/cli/cmd_build.cpp`, `tartci_lease.cpp`): today a denied lease fails the build and the CLI only
  knows background QoS. It must request the floor (`--allow-floor`) and apply the granted `qos` and size.

## Slices
1. **Shipped first (this change):** `agent_floor_qos` configurable (`[host]`, `TARTCI_AGENT_FLOOR_QOS`,
   `--agent-floor-qos`), carried in every floor grant and record; m5studio set to `utility` with the floor raised
   6 → 10 (its memory clamp). No admission rule changes.
2. Pulp: governed-build.sh and the CLI honour `qos` and `lease_size_cores`; the CLI takes the floor instead of failing.
3. Shadow the soft verdict (rollout step 1 below).
4. Enforce, after the §5 benchmarks.

## Rollout
1. **Shadow on m5studio:** compute the soft verdict next to the strict one and log every disagreement
   (`soft_would_admit`, jobs) for a day; no behaviour change.
2. **Enforce on m5studio**; compare for a week: build wait + floor-grant rate, per-file compile time, gate job
   durations, memory pressure (`memory_pressure`, swap), host-vitals level.
3. **M3, m5, m1** with per-host values from their own §5 measurements (laptops get fewer slots and lower ceilings;
   m1 likely stays strict).
4. Revert per host by setting `cpu_admission = "strict"`.

## Risks
- CPU over-commit makes CI jobs slower when agents are busy. Mitigation: utility vs default QoS; watch gate job
  durations; slots cap the worst case.
- Dynamic jobs read a sample of load; a burst after grant can over-commit for a while (bounded by slots × max_jobs).
- Memory remains the only hard stop; an estimate error there is the dangerous one, so the memory rule is unchanged.
