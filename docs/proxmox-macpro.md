# The Proxmox host (`macpro`) — x86_64 CI capacity

tartci's own providers are Tart (macOS/Linux guests on Apple Silicon) and QEMU
(Windows-on-ARM). This page documents a **second, non-tartci hypervisor** in the
same fleet: a Proxmox VE host serving the lanes the Macs structurally cannot.

It is written down here because a reader of this repo would otherwise conclude the
fleet is Macs-only and reach for a Tart provider for x86_64 work — which is the
specific mistake this host exists to prevent.

**Status:** operational, serving Pulp's disposable Linux x64 PR lane. The
profile is managed by tartci/Shipyard; the Linux host uses systemd pool
supervisors rather than the macOS Tart launchd provider.

---

## At a glance

| | |
|---|---|
| **Host** | `macpro` 192.168.86.43 — Proxmox VE 8.4, Xeon E5-1650 v2 6c/12t, 31 GB, 338 GB thin pool |
| **Serves** | Pulp `Linux (x64)` (advisory) · Windows nightly (planned) |
| **Model** | golden template → linked clone → one job → destroy |
| **Templates** | `9005` `pulp-linux-golden-warm4` (current) · `9004`, `9003`, `9002`, `9001` (rollback/superseded) |
| **Pool** | `pulp-ephemeral-pool@{1,2}.service`, 2 enabled slots; slot 3 is an operator-gated expansion |
| **Windows VM** | `300` `pulp-win-ci`, Server 2022 Eval x64 — **stopped**; do not treat its free memory as approval to enable slot 3 |
| **Governor** | `/usr/local/sbin/macpro-governor.sh` — mem hard, CPU 1.5x overcommit, 2c/4G host reserve |
| **Credential** | `/root/.config/pulp/secrets/gh-runner-pat` (600, root) — `Administration: read/write` only |
| **Rollback** | unset the routing variable; the lane returns to GitHub-hosted |

## Why it exists

Pulp's CI had a measured problem: **~198 job-minutes of hosted work per PR against
769 job-minutes of hosted queue wait** — a 3.9:1 wait-to-work ratio on
GitHub-hosted runners. Meanwhile the required checks each sat ~16 minutes waiting
for a machine to do under 2 minutes of work.

Two lanes could not move to existing hardware:

| Lane | Hosted cost | Why not a Mac |
|---|---|---|
| `Linux (x64)` | 26m run + **75m wait** | `tart-linux` provisions **arm64** guests. Moving an x64 build there is an architecture change, not a relocation — it would delete the only x64 Linux coverage. |
| `Windows MSVC` / `Windows (x64)` | **61m run** (longest job in the CI) | `qemu-windows` is Windows-on-**ARM**. x64 MSVC needs native x86_64 or slow emulation. |

`macpro` is a Late-2013 Mac Pro — real Apple hardware, but with an **x86_64 Xeon**.
That is the whole point: it is the only x86_64 host in a fleet of Apple Silicon.

---

## The machine

```
host      macpro   192.168.86.43   `ssh macpro`   (reachable from m1, m3, m5)
          Proxmox VE 8.4, kernel 6.8.12-39-pve
cpu       Xeon E5-1650 v2 — 6 cores / 12 threads @ 3.5 GHz (Ivy Bridge-EP)
memory    31 GB DDR3
disk      466 GB Apple PCIe SSD → local-lvm thin pool, 338 GB usable
network   wired, bridged to vmbr0
gpu       2x AMD Tahiti LE (GCN 1.0)
```

Roughly **1.5–2x a GitHub-hosted runner** (4 vCPU / 16 GB), and with no queue.

### Two hardware facts that constrain what it can do

- **No AVX2.** Ivy Bridge-EP predates it. This is why the host runs **Linux, not
  macOS**: newer macOS increasingly assumes AVX2, and OpenCore-based macOS VMs on
  pre-Haswell silicon top out around Monterey → Xcode 14 → incomplete C++20, which
  cannot build Pulp (`CMAKE_CXX_STANDARD 20 REQUIRED`). Virtualizing does not
  remove a CPU-generation constraint. Intel *macOS* coverage belongs on a 2018-or-
  later Mac that runs a current Xcode natively.
- **GCN 1.0 GPUs.** `amdgpu` needs `si_support=1` and RADV on that generation is
  uneven. Passthrough *might* give Dawn a Vulkan device and fix the chronic
  headless-Linux GPU test failures, but treat that as an experiment, not a plan.

---

## How it is configured

### Golden template + disposable clone

The same isolation model as `providers/tart-linux`, implemented with Proxmox
primitives:

```
9005  pulp-linux-golden-warm4   template — deps + prebuilt Skia + warm ccache + gh
200+  pulp-ci-ephemeral-N linked clone, one job, destroyed
```

Per job: linked clone (copy-on-write, ~28 s to boot), mint a single-use
registration token, register a `--ephemeral` runner, run exactly one job, destroy
the clone.

Why this rather than a persistent runner with a cleanup hook:

- **Nothing to clean**, so no hook can forget a path and no free-space heuristic
  has to guess what is safe to delete.
- **The warm cache cannot be poisoned.** It lives in the golden, which is
  read-only; a job cannot corrupt what the next job inherits.
- **It closes the reused-build-dir class outright** — the 2026-06-07 random-SEGFAULT
  incident on the macOS runners. Pulp's `build.yml` sets `clean: false` on
  self-hosted, so persistent build dirs across branches are a live hazard.

Two slots run as `pulp-ephemeral-pool@{1,2}.service`; **systemd restarting a slot
is what provisions the next clone** — that loop *is* the pool.

VMIDs `200..202` have stable network identities: `192.168.86.251..253` and
deterministic locally administered MAC addresses. The Actions registration name
is deliberately different: `pulp-ci-ephemeral-<vmid>-<uuid>` is unique for every
invocation so an interrupted runner cannot collide with its replacement.

### Scripts on the host

```
/usr/local/sbin/pulp-ephemeral-runner.sh   one clone → one job → teardown
/usr/local/sbin/macpro-governor.sh         capacity admission (Tier 1)
/etc/systemd/system/pulp-ephemeral-pool@.service
/root/.config/pulp/secrets/gh-runner-pat   fine-grained PAT, mode 600, root
```

The PAT carries only `Administration: read/write` — enough to mint a registration
token per job, nothing else.
It stays on the Proxmox host. The golden includes the uncredentialed `gh`
executable for Actions steps, while each job authenticates it with the
short-lived `GITHUB_TOKEN` injected by Actions. Never bake a
`~/.config/gh/hosts.yml` login into the template.

### Resource governance

Mirrors the tiered model in Pulp's `CLAUDE.md`, because a shared host must hand out
a *share*, never the machine:

- **Tier 0 — hypervisor-enforced, unbypassable.** Per-VM `cores=4 cpulimit=4
  cpuunits=50 balloon=0`. `cpuunits=50` is below the default 100 so build VMs
  *yield* to the host rather than competing evenly; `balloon=0` pins memory so a
  build is never squeezed mid-link.
- **Tier 1 — admission control.** `macpro-governor.sh` reserves 2 threads + 4 GB
  for the hypervisor and refuses anything that would oversubscribe. Every clone
  passes through it, so nothing — Actions, Shipyard, a stray manual run — can take
  more than a share.

**Memory is a hard limit; CPU allows 1.5x overcommit.** The asymmetry is
deliberate and was a corrected mistake: an OOM mid-link yields a truncated object
file that reads like a compiler bug, whereas CPU contention only costs time. An
earlier version refused both equally and left threads idle while work queued.

```
lease-able: 15 vCPU (10 physical x 1.5), 27972M
```

---

## What must be warm in the golden

Pulp's expensive dependencies, and where each comes from. Getting this list wrong
means a golden that *looks* warm and is not.

| Dependency | Source | Warm it? |
|---|---|---|
| **three.js** | FetchContent, **full git repo** — fetched only when `PULP_BUILD_TESTS` **and** `PULP_ENABLE_GPU` are ON | **yes — large, and easy to miss** |
| Skia (carries **Dawn**) | prebuilt archive via `fetch_skia_for_release.py linux-x64` | yes — 151 MB |
| perfetto, yoga | FetchContent | yes |
| ccache | per-build | yes — warm by building before templating |
| GitHub CLI (`gh`) | Ubuntu package | yes — preamble/alias jobs call it with their injected `GITHUB_TOKEN` |
| QuickJS | vendored via CHOC, in-tree | no |
| V8 | `FindV8.cmake`, opt-in engine backend | only if enabled |

**Bake the golden with the flags CI actually uses.** Pulp's matrix build leaves
`PULP_ENABLE_GPU` and `PULP_BUILD_TESTS` at their defaults on `pull_request` /
`merge_group` (they are only forced OFF on `workflow_dispatch`). Baking with GPU
off skips three.js entirely, so every ephemeral job then pays for a full
`mrdoob/three.js` clone.

**`PULP_SHARED_FETCHCONTENT_SOURCE_DIR` is load-bearing, not a nicety.** Without
it, FetchContent sources land in the build tree and die with the clone. The
per-platform defaults are:

```
Linux    $XDG_CACHE_HOME/pulp/fetchcontent-src  or  ~/.cache/pulp/fetchcontent-src
macOS    ~/Library/Caches/Pulp/fetchcontent-src
Windows  $LOCALAPPDATA/Pulp/fetchcontent-src
```

Note the Linux default is lowercase `pulp`, while Pulp's `build.yml` caches
`~/.cache/Pulp/...` (capital P). On a case-sensitive filesystem those are different
directories — worth verifying before trusting either.

**`FETCHCONTENT_BASE_DIR` is a DIFFERENT cache and does not substitute.** The
first warm golden (`9002`) carried 3.2 GB of CMake FetchContent *build* trees at
`~/.cache/pulp/fc`, wired via `FETCHCONTENT_BASE_DIR` in the runner `.env`. That
is not what `setup.sh` reads. It consults the *source* cache above, found `0`
entries, and every job re-cloned three.js — ~20 minutes before a line compiled.
The golden looked warm by every measure except the one that mattered.

Two rules follow:

- Verify the golden by the path the **bootstrap** resolves, not the one you set.
  On a clone: `du -sm ~/.cache/pulp/fetchcontent-src` should be >1.5 GB, and the
  `threejs-*` directory alone ~1 GB.
- **The runner's `.env` is the only file that reaches a job.** Job steps are
  non-interactive, so `~/.bashrc` and `~/.zprofile` are never sourced. An export
  that is not in `<runner>/.env` does not exist as far as CI is concerned.

**Proving a golden is warm requires reading the pool log, not the clone.** A clone
from an *old* golden warms its own cache during its job and then looks identical
to one that inherited it. The only honest check is `journalctl -u
'pulp-ephemeral-pool@*' | grep 'linked-cloning golden'` to confirm which template
a VM came from, plus its age — a 70-second-old clone holding 1.8 GB cannot have
downloaded it. A check that cannot distinguish the two will report success on the
wrong evidence.

---

## Windows

**Windows runs nightly, not per merge.** It is billed at 2x on GitHub-hosted
runners, was ~90% of billable Actions spend, and gates nothing — no Windows
context appears in Pulp's required checks, so the merge queue never waits for it.
Running it per merge group consumed the hosted pool the *required* checks queue
behind, doubled by `max_entries_to_build=2`.

Coverage lives in `cross-platform-check.yml`, which builds and tests Windows
nightly and whose `tracking-issues` job find-or-creates a per-platform issue on
failure, reopens a closed one, and auto-closes on recovery. Catch, file, do not
block.

`workflow_dispatch` still runs it on demand for a Windows-touching change.

### The Windows VM on this host

VM `300` (`pulp-win-ci`) — Windows Server 2022 Eval, x86_64 native, 4c/10G/80G,
q35 + OVMF, virtio disk and NIC. Installed unattended via an `autounattend.xml`
ISO modelled on `providers/qemu-windows/make-autounattend.sh`, adapted from
Win11-ARM64 to Server-2022-x64: virtio drivers injected in the `windowsPE` pass so
Setup can see the disk, OpenSSH Server enabled at first logon, and the operator's
public key written to `administrators_authorized_keys`.

Unlicensed by design — Server 2022 Eval is 180 days and needs no key. A CI builder
does not need activation.

Its intended job is the **nightly** Windows run, moving that off 2x-billed hosted
minutes. Nightly is the right latency class for self-hosted: if the host is down,
a nightly slips, whereas a required check would strand merges.

## Operating it

```sh
ssh macpro
qm list                                        # 9005 golden, 200+ ephemeral clones
systemctl status 'pulp-ephemeral-pool@*'
systemctl is-enabled pulp-ephemeral-pool@1 pulp-ephemeral-pool@2
systemctl show pulp-ephemeral-pool@1 pulp-ephemeral-pool@2 -p ExecMainStartTimestamp -p ActiveState -p SubState
journalctl -u 'pulp-ephemeral-pool@1' -f       # per-job lifecycle
/usr/local/sbin/macpro-governor.sh status      # capacity and current commitment
```

`active (running)` proves only the current process. Both `is-enabled` results
must also be `enabled`, which proves the two slots rejoin the pool after a host
restart. After a reboot, compare `uptime -s` with each unit's
`ExecMainStartTimestamp` and confirm both ephemeral runner registrations return
online before routing work back to the host.

Add a slot: `systemctl enable --now pulp-ephemeral-pool@3` — but check the governor
first, and leave headroom if a Windows VM is planned.

Drain without killing active jobs: disabling a unit is not sufficient because
the already-running service still has `Restart=always`. Runtime-mask each active
slot, then stop only slots whose runner is still idle. An in-flight slot finishes
its job, destroys its clone, and cannot restart through the mask:

```sh
systemctl mask --runtime pulp-ephemeral-pool@1 pulp-ephemeral-pool@2
systemctl stop pulp-ephemeral-pool@2  # only after proving slot 2 is idle
# After maintenance:
systemctl unmask --runtime pulp-ephemeral-pool@1 pulp-ephemeral-pool@2
systemctl enable --now pulp-ephemeral-pool@1 pulp-ephemeral-pool@2
```

**Rollback:** unset Pulp's `PULP_LOCAL_LINUX_RUNS_ON_JSON` and the lane returns to
GitHub-hosted. `runs-on` has **no automatic fallback**, so if this host is down or
its pool is stopped, jobs routed here queue indefinitely rather than erroring.
Unset the variable rather than waiting.

Pulp's `runner_topology.json` declares the lane and `runner-topology-check.yml`
reconciles it hourly against the live repo variable — so a variable change and its
contract edit must land together.

---

## Gotchas this host paid for

- **Random clone MACs exhaust the LAN DHCP pool.** A destroyed VM does not release
  its lease immediately. One random MAC per short CI job accumulated hundreds of
  live leases on 2026-08-02, leaving healthy clones with only IPv6 and holding the
  merge queue. Keep the VMID-to-IP/MAC mapping deterministic and the Actions
  runner name unique; network identity and runner identity solve different
  incident classes.
- **Clone before clearing `machine-id` and two VMs share a DHCP lease.** Identical
  `machine-id` → identical DHCP identity → the same IP handed to both. Clear
  `/etc/machine-id`, `/var/lib/dbus/machine-id`, and the SSH host keys *before*
  templating.
- **VMID selection is a race.** Two pool slots read "200 is free" in the same
  instant; one clones it and the other's cleanup destroys 200 out from under the
  winner, whose `qm start` then finds no config file. Observed, not theoretical.
  Hold a `flock` across *claim and clone*, and guard teardown so a slot only
  destroys a VM it actually created.
- **An interrupted job leaks a ghost runner.** A completed `--ephemeral` run
  deregisters itself, but a kill/reboot/abort does not — GitHub then schedules to a
  VM that no longer exists. Deregister *before* destroying, in the exit trap.
- **A teardown guard that is never armed silently disables teardown.** The fix for
  the VMID race above introduced a `CLONED` flag the exit trap reads before
  destroying anything — and nothing ever *set* it. Every job logged `nothing to
  clean (no clone created)` about a clone it had just finished using, so no clone
  was ever destroyed. The pool was persistent runners wearing an ephemeral name:
  the reused-build-dir class it exists to close stayed wide open, and VMIDs drained
  toward "no free clone id" with nothing saying why. Set the flag where the clone
  is committed to disk, before releasing the lock. The tell in the log is exact —
  a healthy teardown says `destroying clone <id>`.
- **`systemctl disable` does not drain an active restart loop.** It prevents a
  unit from starting at boot, but an already-running pool slot still obeys
  `Restart=always` and provisions another clone 30 seconds after teardown. Do
  not use `disable --now` on a busy slot: `--now` kills its job. Runtime-mask
  the unit so its current job can finish and its restart is blocked; stop only a
  separately verified idle slot. Unmask and enable after maintenance.
- **Reclaiming a leaked VM by hand: make the guard `return`, not print.** A
  reclaim script that prints `WORKER ACTIVE — DO NOT TOUCH` and then deletes anyway
  is worse than none. Every refusal must exit before the destroy, and an
  *unreachable* guest is a refusal too — a guest that cannot answer cannot testify
  that it is idle.
- **Capacity is memory, not cores.** 31 GB with two 8 GB clones and a 10 GB Windows
  VM leaves nothing; the governor correctly refuses a third clone with
  `EX_TEMPFAIL`. The Windows VM had also been running for hours serving **no
  registered runner** — stopping it (`qm shutdown 300`, reversible with `qm start`)
  freed exactly enough for slot 3. Audit what is *running* against what is
  *registered* before buying capacity.
- **Two slots cannot serve a merge queue.** `runs-on` has no fallback, so when both
  slots are busy a `merge_group` job queues indefinitely and the entry times out and
  rebuilds forever. Either give the host enough slots to absorb peak, or scope the
  routing to `pull_request` only and leave `merge_group` on hosted runners. The
  latter is strictly safer: it captures the capacity without ever putting this host
  in the merge critical path.
- **A local `timeout` around a remote `apt-get` proves nothing.** The remote
  process survives the disconnect. Poll for the process; do not hold a session.
- **`iU` packages mid-`dist-upgrade` are normal**, not damage — apt unpacks
  everything, then configures.
- **A fresh Proxmox install has apt silently broken**: it points at the enterprise
  repo with no subscription. Switch to `pve-no-subscription` before expecting
  updates.

---

## Relationship to tartci

Today this host is configured with plain Proxmox tooling (`qm`, systemd) and its
own governor, deliberately: it was stood up to prove the lane before investing in
abstraction.

The natural convergence is for tartci to grow a **`providers/proxmox-linux`** (and
later `proxmox-windows`) alongside `tart-linux` and `qemu-windows`, so one front
door covers the whole fleet and the lease store governs all hosts uniformly rather
than this host carrying a parallel governor. The pieces already line up: golden +
ephemeral clone matches `tart-linux`'s model, and `macpro-governor.sh` is a subset
of what `tartci leases` already does for Macs.

Until then, treat this page as the authority for `macpro`, and remember the fleet
is **not** Macs-only.
