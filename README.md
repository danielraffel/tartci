# tartci — local CI VMs on macOS (Tart + QEMU + Shipyard)

[![lint](https://github.com/danielraffel/tartci/actions/workflows/ci.yml/badge.svg)](https://github.com/danielraffel/tartci/actions/workflows/ci.yml)

Stand up **fast, cached, disposable Linux / Windows / macOS build VMs on an Apple
Silicon Mac**, optionally wired to GitHub runners +
[Shipyard](https://github.com/danielraffel/Shipyard), so you can build & test a repo
locally instead of (or alongside) GitHub-hosted runners. Headless CI is the
priority; the same goldens double as GUI **bench** VMs you can open in UTM to test things like
plugins in a DAW.

> Project-agnostic. [Pulp](https://github.com/Generous-Corp/pulp/) is the first consumer, but any repo (e.g. a Pulp-based
> plugin) plugs in with one `vm-image` manifest. This repo holds **scripts +
> configs + docs only — never the large VM images** (those stay local / pulled /
> baked, and are gitignored).

## What you get
- **macOS + Linux** via **[Tart](https://github.com/cirruslabs/tart/)** (Apple Virtualization) — native speed, CoW
  clones, host-mounted ccache warm across clones.
- **Windows** via **standalone QEMU** (hvf) — AVF can't install Windows (no inbox
  virtio-blk driver; black installer display), so Windows is first-class on QEMU
  with an NVMe disk + `ramfb` display.
- Host-mounted caches, SSH access + log collection, and a tiny metrics dashboard.
- **Host resource governance** — a per-host weighted lease store keeps builds and
  VMs from oversubscribing a shared Mac (see below).

### Host resource governance

**Drain a roaming host** with `tartci pool drain` (and bring it back with
`tartci pool on`; reserve `pool off` for an immediate stop;
`tartci pool status [--json]` shows state). This host-level switch writes the
native-build participation flag and a durable admission state. Drain refuses
new native leases and JIT registrations, lets an already assigned exact job
finish, and disables runner restart across reconnect/reboot. The persistent
Actions preamble runner requires a Shipyard routing hold plus authoritative
idle receipt before tartci will boot it out; without that receipt drain remains
pending and exits nonzero. The current supported Shipyard CLI does not produce
this receipt; do not create it by hand. Persistent-runner drains therefore stay
fail-closed until an authoritative producer is deployed. `pool off` remains
immediate and may terminate work.
See the runbook.

On a host whose governed budget supports two full macOS guests, use
`tartci gate-slot2 install` to preview the canonical event-class slot-2 profile.
Apply it only while the pool is drained/off; `tartci pool on` then loads both
managed supervisors. See `launchd/README.md` for the collision, routing, and
rollback contract.

Shared Macs run CI validation, agent builds, and VM runners on the same
hardware. Without a shared budget they oversubscribe — two hosts melted in July
2026, one CPU-bound, one memory-bound/OOM. tartci is the per-host governor:

- **Weighted lease store** (`scripts/leases.py`) — builds and VM runners acquire
  a lease before starting. Priority classes (`background` < `build` < `vm` <
  `runner` < `gate`) order contention, and a reserved gate-core headroom keeps
  the required `macos` gate schedulable even when non-gate work fills the host.
- **Memory as a second admission axis** — leases carry a memory weight
  (`--mem-mb`, capacity via `--capacity-mem-mb`); admission is
  `min(core-budget, memory-budget)`, so a build that would exhaust RAM is refused
  even when CPU is free. Legacy core-only records are estimated as
  `cores × per-job memory` so a mixed store never over-admits.
- **Disk growth as a per-volume admission axis** — VM runners reserve their
  worst-case writable growth in the same locked transaction as cores and
  memory. Admission retains a free-space safety floor after active reservations
  plus the new request, keyed by filesystem device so two supervisors cannot
  independently pass a stale `df` check. The defaults are a 25 GiB floor and a
  configurable 24 GiB VM growth allowance, sized above the roughly 19 GiB
  observed Pulp full-gate growth. Shared ccache and baked CoW dependencies stay
  outside this accounting and are not copied per job.
- **Role profiles** (`scripts/host_profile.py`) — each host derives a role from
  its cores + `hw.model`: **dedicated-builder**, **dev-overflow**, or **light**,
  each with both a core budget and a memory budget. `tartci host-profile` emits
  the derived budget (`PULP_BUILD_JOBS`, `PULP_BUILD_MEM_BUDGET_MB`) that a
  consumer's build wrapper reads.
- **Agent surfaces** — `tartci host-profile` (derived budget), `tartci leases`
  (inspect/acquire/release the store), `tartci status` (provider/capacity/role
  state), and `tartci profile validate` (check lane selectability).
- **One scheduler path** — GitHub Actions distributes label-matched jobs,
  Shipyard supervises queue progress, and Tart CI provides governed local VMs.
  Orchard is not part of the supported or operational fleet path. Upgraded
  shadow hosts must run `scripts/disable_orchard.sh --apply` to remove both
  retired LaunchAgents.

Deep setup — onboarding a host, role derivation, and pool verification — lives
in [`docs/runbook.md`](docs/runbook.md).

> **Project Status:** a working lab toolkit, hardening toward turnkey. Wired +
> proven today: the **Linux Tart lane** (`tartci up linux` — ephemeral clone →
> warm host ccache → full build + ctest → discard, host-validated green), the
> **Windows QEMU lane** (`tartci up windows` — per-job CoW overlay → dynamic SSH
> port → MSVC arm64 build + ctest → discard; host-validated: overlay/SSH
> mechanics + full 735-target compile green, ctest is the golden's proven 7287/7303),
> the **macOS Tart primitive** (`tartci up macos` / `serve macos` now dispatch to
> `providers/tart-macos`; 2026-06-09 validation proved disposable in-VM Release
> build + ctest enumeration, JIT runner assignment/cleanup, and `$HOME` launchd
> boot; one Pulp screenshot payload test remains red in the scratch JIT run),
> the **x86_64 cross/emulation smoke lane** (`tartci up linux --target-arch
> x86_64` — cross-compile + run dynamic x64 binaries under Rosetta-for-Linux;
> `--self-test` proves the toolchain+emulator chain golden-agnostically), the **pool-serving
> lanes** (`tartci serve linux|windows` — ephemeral per-job GitHub Actions
> runners, ported from Pulp's proven `tools/ci` supervisors), the metrics, and
> `tartci doctor/bench/metrics`. **Not yet wired:** the Windows/Prism cross lane
> (Linux/Rosetta is wired; Windows-on-ARM
> x64-via-Prism is a follow-up). Emulated x64 is a SMOKE/debug signal only —
> GitHub-hosted x64 stays the authoritative x64 gate.

## Quick start
```bash
git clone <this-repo> tartci && cd tartci
./tartci doctor           # report host prereqs + golden/bench stores (always safe)
./tartci doctor --reap --json    # report owned stale CI VM/runner residue
./tartci setup            # brew-install tart/qemu/sshpass + create local stores
./tartci bench windows    # clone the Windows golden → open in UTM for GUI/DAW testing
./tartci metrics report   # text build/cache table (or `metrics dashboard` for HTML)
./tartci status --json    # host-local provider/capacity/profile state for agents
./tartci host-profile --json  # derived role budget; read-only
./tartci network-profile status --json # opt-in per-host relay intent/drift
./tartci leases status --json # host-wide core/memory/per-volume disk reservations
./tartci profile plan normal-local-fast --repo Generous-Corp/pulp --json
./tartci timings          # summarize per-job Windows/Linux VM timing.tsv files
./tartci runtime summary --repo owner/repo --run-id 123 --json
./tartci prepare linux     # bake/provision the Linux golden (Rosetta x64 smoke enabled)
./tartci up linux         # ephemeral Linux build+test of a ref (clone→build→ctest→discard)
./tartci up linux --target-arch x86_64   # cross-build x64 + run tests under Rosetta (SMOKE)
./tartci up macos --src /path/to/pulp     # ephemeral macOS build+test clone (host caches mounted)
./tartci up windows       # ephemeral Windows build+test (CoW overlay→build→ctest→discard)
./tartci serve linux      # serve the GitHub Actions pool: ephemeral per-job runner(s)
./tartci serve macos --once --labels self-hosted,macOS,ARM64,pulp-build-vm
./tartci serve windows --loop   # keep serving Windows jobs (throwaway overlay each)
./tartci windows run      # boot the Windows installer/single-operator VM (from-scratch)
./tartci windows optimize # prewarm/validate the booted Windows golden before tagging
./tartci goldens list     # canonical Windows golden + drift / prune candidates
./tartci goldens sync --to m1 --prune       # PUSH the canonical golden to a host over the fastest link (Thunderbolt→LAN→Tailscale), verify, repoint + reload its runner, prune old
./tartci goldens sync --from macstudio      # PULL the canonical golden onto THIS host from a peer (new-machine setup — copy instead of baking)
```
`tartci doctor`, `bench`, `metrics`, `up linux`, `up windows`, and `serve
linux|macos|windows` are wired today. `tartci status --json` and `tartci profile
list|show|explain|plan` are read-only helpers for Shipyard, Codex, Claude, or
other agents to answer where CI should run from machine-readable config.
Pool-serving VM runners acquire host-core leases before booting guests. The
lease size defaults to the host profile's `vm_pool_cores`; override per host
with `TARTCI_MACOS_VM_CORES`, `TARTCI_LINUX_VM_CORES`, or
`TARTCI_WIN_VM_CORES`. Set `TARTCI_VM_LEASES=0` only for operator-controlled
break-glass debugging. Tart-backed macOS/Linux runners apply the lease size with
`tart set --cpu` after cloning and before boot; QEMU Windows passes the leased
size through `-smp`.

macOS admission inventory is bounded by one shared
five-second budget so a wedged `tart list` or `tart get` cannot freeze every
supervisor on a host; override it with `TARTCI_TART_INVENTORY_TIMEOUT_SECS`.
Inventory failure remains fail-closed at the configured macOS hard cap.
`tartci up linux [--ref <git-ref>] [--no-gpu]
[--keep]` clones the `pulp-linux-build` golden, mounts the host ccache, and
builds + ctests in-guest. `tartci up windows [--ref <git-ref>] [--smoke]
[--keep]` makes a per-job CoW overlay off the Windows golden on a dynamic SSH
port (concurrent-safe), builds GPU-off under MSVC arm64, then discards the
overlay (see `providers/`). `tartci up macos --src <checkout>` clones the
macOS runner golden, mounts source read-only plus ccache/FetchContent, builds in
`~/build`, runs ctest, and discards the clone. See
`docs/runbook.md` for the from-scratch, gotcha-by-gotcha guide and
`docs/new-repo-agent-guide.md` to onboard a new repo.

**The fleet is not Macs-only.** A Proxmox host (`macpro`, an x86_64 Xeon Mac
Pro) serves the lanes Apple Silicon structurally cannot — native x64 Linux, and
Windows x64 later. `tart-linux` provisions **arm64** guests and `qemu-windows` is
Windows-on-**ARM**, so routing an x64 build at either is an architecture change,
not a relocation. See [`docs/proxmox-macpro.md`](docs/proxmox-macpro.md); it is not
TartCI-managed. Its Proxmox/systemd runner service is a separate execution
provider that Shipyard coordinates alongside TartCI. Native Intel macOS/Metal
checks similarly run directly on `macmini`, not inside TartCI.

### Serve the GitHub Actions pool
`tartci up` does ONE on-demand build and exits; `tartci serve <os>` is the
**pool-serving** sibling. It mints a Just-In-Time (single-job) runner config,
boots a throwaway clone per queued job, and lets the GitHub **workflow** drive
the build — the host just supplies a clean VM each time. `--loop` keeps serving
(what the LaunchAgents run); the default is one job then exit (pilot-safe).
The Linux Tart and Windows QEMU loops scan queued and in-progress workflow runs for queued jobs,
ignores stale queued jobs older than `TARTCI_RUNNER_MAX_QUEUED_AGE_SECONDS` (six
hours by default), and checks queued job labels by default
(`TARTCI_RUNNER_QUEUE_MATCH_LABELS=1`). That keeps each supervisor safe to leave
loaded while the repo still defaults ordinary Windows jobs to GitHub-hosted
`windows-latest`; a race-loser VM that boots but never claims a job exits after
`TARTCI_RUNNER_IDLE_TIMEOUT_SECS` (15 minutes by default), deletes its stale
GitHub runner registration, and discards its VM/overlay while releasing the
capacity lease. Linux rechecks the exact runner's GitHub `busy` state at the
deadline, bounds API-uncertainty retries, and removes a confirmed-idle JIT
registration during teardown. Linux also exports `CCACHE_DIR=~/.ccache` and requires that
directory to resolve to the writable host
virtio-fs share with the expected filesystem tag before JIT registration; a
missing or incorrectly bound cache
fails closed instead of silently running cold. Linux exports
`CMAKE_BUILD_PARALLEL_LEVEL` from `TARTCI_LINUX_BUILD_PARALLEL_LEVEL` (default
4, capped by the VM lease's core count) so workflow `cmake --build` calls use
the provisioned VM without requiring workflow changes. Ephemeral Windows runner
names include a host-derived prefix by default so multiple Macs can serve the
same label without repo-scoped runner-name collisions.
Repo / golden / labels are env-driven (`TARTCI_RUNNER_REPO`,
`TARTCI_LINUX_GOLDEN` / `TARTCI_MACOS_GOLDEN` / `TARTCI_WIN_GOLDEN`,
`TARTCI_RUNNER_LABELS`); see each `providers/*/runner.sh` header.
The macOS JIT runner also enforces a bounded warm-cache capacity through
`TARTCI_CCACHE_MAX_SIZE` (default `40G`) in the Actions runner environment, so
each disposable VM shares compiler objects without falling back to ccache's
small process default. Set a smaller value explicitly on space-constrained
hosts; depend mode remains disabled regardless of this capacity setting.

Pulp's merge-group/PR-head gate can use the staged event-class assignment V2
mode. It removes the legacy `pulp-gate-fast` selector from JIT advertisements,
requires the queued job's class token, exhaustively and fail-closed scans all
pages, and freshly revalidates higher plus selected demand before minting. The
shipped LaunchAgent remains in legacy mode; bounded observation, promotion, and rollback are documented in
[`docs/assignment-v2-rollout.md`](docs/assignment-v2-rollout.md).

**Shipyard admission guard (coordinated rollout).** Provider supervisors can
require a final repository cleanup verdict after the VM is reachable and
immediately before JIT registration:

```sh
TARTCI_ADMISSION_CLEAN_MODE=required
TARTCI_SHIPYARD_CLI=shipyard
TARTCI_ADMISSION_CLEAN_BASE=main
```

The default is `disabled` so TartCI can be installed before the matching
Shipyard command is deployed. Do not set `required` until that host has a
Shipyard build exposing `runner admission-clean`. Once enabled, only its typed
`admit` verdict permits `generate-jitconfig`; `defer`, cancellation still in
flight, auth/API/schema errors, and partial observations all discard the
unregistered VM and back off for the normal provider poll interval. TartCI does
not inspect or cancel runs. Authority hosts let Shipyard clean and rescan;
non-authority hosts admit an already-clean queue or defer until the authority
tick finishes. This guard is the correctness boundary; a periodic queue-tick
health marker may avoid a wasteful boot, but never authorizes registration.

**Routing provider API calls off a personal PAT (`TARTCI_GH_CLI`).** Every
provider polls GitHub each `VM_POLL` seconds on every host (queue check, plus
JIT mint / runner reclaim / job + run polling). On a shared personal PAT that
polling is the dominant secondary-rate-limit ("token invalid") source, and it
multiplies with each host added. Set `TARTCI_GH_CLI` to a CLI that authenticates
as a **GitHub App** (e.g. a `ghapp` wrapper that runs `gh` with an App token) to
move all of it onto the App's separate rate-limit bucket. Default `gh` — generic
behavior is unchanged; opt in per host via the LaunchAgent env.
The same setting also governs `tartci doctor --reap` runner reads and cleanup,
so the fleet health surface cannot silently fall back to an exhausted ambient
token while the providers themselves use the App.

**JIT credential exception and denial fuse (`TARTCI_JIT_GH_CLI`).** Keep
`TARTCI_GH_CLI=ghapp` as the default for queue scans, reads, cleanup, and all
ordinary control-plane calls. A host that has a separately governed classic
credential solely for the GitHub runner-registration edge may set
`TARTCI_JIT_GH_CLI` to its secret-free wrapper command; only
`generate-jitconfig` uses that override. Never put a token in a plist and never
silently fall back from `ghapp` to ambient `gh`.

The dynamic macOS fleet profile supports this as the optional per-lane
`jit_github_cli` field. It accepts only an executable name (not a shell command
or token); the renderer emits it as `TARTCI_JIT_GH_CLI` for that lane alone.
The M1 Forge gate uses `tartci-m1-stackbench-jit-gh`, whose deployed wrapper
reads the host's mode-0600 stackbench credential at runtime. Keep normal
`TARTCI_GH_CLI=ghapp` unchanged and deploy the wrapper before activating a
profile that names it.

On M1, deploy the versioned wrapper as
`~/.local/bin/tartci-m1-stackbench-jit-gh` with executable mode. The wrapper is
`scripts/tartci-m1-stackbench-jit-gh`; it refuses a missing, empty, or
non-0600 credential and never prints its contents.

If GitHub rejects JIT registration with 401, 403, or 404, the macOS provider
records a keyed admission-denial receipt under its state directory. The key
binds repo, runner group, labels, and selected JIT CLI. The loop then refuses
to boot another VM for that same contract, so a bad credential cannot consume
the fleet in a retry loop. Repair the named auth route (or intentionally change
the route, which changes the key) before clearing the receipt and dispatching
another gate.

If a Shipyard deployment also runs an external organization runner-group policy
verifier, the App installation needs **Self-hosted runners: Read-only** at the
organization level. Repository `Actions` access does not grant this. The Mac
Pro's host-side verifier reads the group's repository,
workflow, and runner membership boundaries before trusting a host. Grant write
only when the controller must configure groups or remove registrations. After
changing an App permission, approve the pending installation update and mint a
fresh installation token; a cached token keeps the old permissions and returns
`403 Resource not accessible by integration`. See
[`docs/runbook.md`](docs/runbook.md#github-app-runner-group-access) for setup and
the security rationale.

**Scan-blindness self-heal (why a rate-limited poll can't silently wedge a lane).**
Each `VM_POLL` the serve loop asks GitHub "are there queued jobs my labels can
serve?" That scan has two outcomes, and the loop keeps them distinct:

- **Normal / "not blind":** the bounded scan succeeds and returns a **count** —
  `0` means no matching job was found in the current scan window, while `>0`
  means boot a VM. A successful `0` proves the scanner and credentials worked;
  it does **not** prove the repository's complete queue is empty. The scanner
  deliberately rotates a bounded set of recent runs to control API cost, so a
  busy repository can expose an older queued job on a later poll.
- **Blind:** the `gh` scan *fails* (secondary rate-limit / API timeout / a degraded
  or expired token / a network blip). The loop must NOT read that as "no work" —
  doing so is how a supervisor can sit idle for hours (`waiting queued=0`) while the
  required gate backs up, looking perfectly healthy the whole time. So a failed scan
  returns the sentinel **`ERR`** (never a bogus `0`); `--print-queue` prints `ERR`.

On a blind scan the loop logs `SCAN BLIND …`, counts consecutive blind polls, and
after ~3 minutes of *continuous* blindness **self-restarts** (`exit 75` → launchd
`KeepAlive` respawns a fresh process with fresh `gh`/App-token auth — the exact
manual `launchctl kickstart` recovery, automated). A single blip costs nothing: any
successful scan resets the counter. Tune the window with `TARTCI_SCAN_BLIND_MAX`
(polls). A *hung* (not erroring) `gh` is also covered — every poll has a
`TARTCI_GH_TIMEOUT_SECS` timeout, so a stalled call raises rather than blocking the
loop forever. This is why moving polling onto the App token (above) matters most: it
keeps scans *succeeding* so the self-heal rarely has to fire. Quick host check:
`runner.sh --print-queue` should print a number; if it prints `ERR`, that host's
`gh`/App auth is degraded — fix the token, not the runner.

Use Shipyard's queue-front observation (`shipyard runner fleet-status`) when
the operational question is whether a required merge gate is making progress.
Tart CI owns disposable VM capacity and bounded job discovery; Shipyard owns
queue ordering, required-context classification, enrollment repair, and
superseded-run policy. Do not add Orchard as a second scheduler.

The Shipyard queue janitor is a separate control-plane role from Tart CI runner
capacity. Never enable its full-live mode fleet-wide. Exactly one host may set
`SHIPYARD_QUEUE_AUTHORITY=1`, and its stored Shipyard runner tag must match the
repo's `[merge_queue].mutation_machine`. This integration requires Shipyard
0.80.0 or newer and must be deployed only after that binary is installed. A
Shipyard hold on the configured authority stops its tick before GitHub reads;
non-authority hosts must explicitly use dry-run or
`SHIPYARD_TICK_REAP_ONLY=1`; an old plist that requests full-live without
authority now exits unhealthy rather than silently changing modes. Full-live also requires
`SHIPYARD_QUEUE_REPO_ROOT` to name the authoritative checkout; the tick runs
Shipyard there and verifies its runner tag matches that repo's
`mutation_machine`.
Treat a full-live tick missing either `SHIPYARD_QUEUE_REPO_ROOT` or
`SHIPYARD_QUEUE_AUTHORITY=1` as a failed control plane, not a healthy reap-only
tick. Alert on the nonzero exit and
`~/Library/Logs/shipyard-queue-tick.health.json`.
Each Shipyard reconcile and auto-merge subprocess is bounded to 45 seconds so
a wedged GitHub child cannot stop the five-minute cadence. Override that bound
only with `SHIPYARD_QUEUE_COMMAND_TIMEOUT_SECS=1..300`; a timeout fails closed
and publishes degraded health without attempting the mutation.

To serve across reboots, install a LaunchAgent
from `launchd/` (the Shipyard macOS GUI's "Serve CI builds from this Mac" switch
toggles those agents). Emulated x86_64 stays on the on-demand `up` lane (smoke /
debug); pool jobs build whatever arch the workflow targets. The Linux, macOS,
and Windows serve loops only boot when a queued job's requested labels are
satisfiable by the configured runner labels, so a pilot agent does not boot for
unrelated queued `Build and Test` work.
For additional Apple Silicon pool members, use the generic host checklist in
`docs/runbook.md`: stable SSH alias, absolute `/opt/homebrew/bin/tart`,
absolute `$HOME/.local/bin/tartci`, home-backed `TART_HOME`, and matching
Shipyard `host_class` capacity config. If multiple hosts can serve the same
platform, do not let them race the same workflow selector. Give each host one
extra host-specific label after the shared `pulp-build-*` pool label, then let
the workflow resolver pick a selector before the job is queued. Pulp's default
policy is Mac Studio primary (`pulp-host-macstudio`), M5/blackbook overflow
(`pulp-host-m5`), then GitHub-hosted fallback when each local selector is already
at its configured in-progress capacity. These Linux/Windows runners are JIT
ephemeral, so the resolver checks in-progress jobs for each host label rather
than looking for idle registered GitHub runners. That avoids duplicate VM boots
and avoids queued self-hosted jobs that cannot later spill to GitHub-hosted.

Tart's maintained upstream and Homebrew channel are now `openai/tart` and
`openai/tools/tart`. New hosts use `brew install openai/tools/tart`; do not add
the retired `cirruslabs/cli` tap. Existing hosts require a one-at-a-time,
idle-boundary tap migration because Homebrew refuses same-named formulae from
both taps. Preserve fleet capacity by proving scheduler and VM idleness before
taking only one host out of participation, prefetched uninstall/install with
rollback, then a real ephemeral VM canary before moving to the next host. See
the runbook for the exact sequence. Remote health probes use
`/opt/homebrew/bin/tart` (or the host profile's explicit `tart_bin`) rather than
ambient `command -v`: a stripped SSH shell commonly omits Homebrew and must be
reported as launch-environment drift, not as an absent Tart installation.

An exception-only recovery pool may deliberately let several hosts satisfy one
shared queued-job label so an offline host cannot strand the job. Preserve a
practical host preference with the opt-in
`TARTCI_RUNNER_MIN_QUEUED_AGE_SECONDS`: use `0` on the preferred host, a bounded
delay on the first fallback, and a longer delay on the second fallback. The
scanner ignores the job until its GitHub queue timestamp reaches that age; it
does not sleep a supervisor or reserve capacity. GitHub still assigns the job
once, and any later JIT registration that loses the claim follows normal
bounded idle teardown. Do not use this for required checks that already have a
pre-queue host resolver and host-specific labels.

The fast gate supervisors (Pulp: M3 and M5) should advertise
`TARTCI_VM_LEASE_PRIORITY=gate`; advisory supervisors must yield to that class.
Pulp's exclusive `pulp-release-tagged` class also receives a gate-priority
lease automatically, so a queued tagged release can use the reserved capacity
while an advisory VM holds the non-gate budget. The lower-priority
`pulp-release-pr-gate` class remains ordinary VM work.
If both mutually exclusive class labels appear, the PR-gate classification
wins and the lease fails down to ordinary VM priority.
This reserves host-core leases, but it does not choose a specific GitHub job
after a runner boots. Host placement needs a label: Pulp's fast supervisors and
required selector carry `pulp-gate-fast`, while the slower M1 keeps only the
generic `pulp-build-vm` label for rollback/non-required use. Required and
advisory jobs likewise must not share an indistinguishable label set: use
separate class labels for coverage, snapshot, GPU-proof, and example-validation
jobs. Shipyard may coalesce or cancel provably redundant workflow runs before
boot, but Tart CI must never guess queue priority by PR number or mutate the
merge queue.

### Priority-aware idle gate (secondary macOS lanes)

A *secondary* macOS lane (an advisory coverage or sanitizer VM) shares a host
with the *required* build-gate lane, and macOS allows only **two running macOS
guests per host**. If the secondary lane grabs and holds a slot, it can starve
the required gate — which is exactly what forced a coverage lane to be backed
out of one downstream project. The idle gate prevents that.

Set two env vars on the secondary lane's supervisor (default unset = OFF, so
the primary gate runner and existing lanes are byte-for-byte unchanged):

- `TARTCI_YIELD_TO_WORKFLOW_NAME` — the priority workflow to defer to
  (e.g. `Build and Test`).
- `TARTCI_YIELD_TO_LABELS` — the priority lane's labels
  (e.g. `self-hosted,macOS,ARM64,pulp-build,pulp-build-vm`).

When set, the loop boots only when (1) this lane has queued work, (2) a VM slot
is free, and (3) the priority lane has **no** queued or in-progress work whose
requested labels are a subset of `TARTCI_YIELD_TO_LABELS`. Keep the secondary
lane on the **same `TART_HOME`** as the gate so `running_macos_vms` stays a true
host-wide 2-guest semaphore (a separate store would hide the secondary VM from
the gate's count and let total guests exceed Apple's cap). Preview the current
yield count with `serve macos --print-priority-demand` (returns 0 when OFF). install the same golden qcow2 and the same
tartci checkout/home copy on every participating Apple Silicon host, keep
Homebrew's `/opt/homebrew/bin` in the LaunchAgent `PATH`, and leave
`PULP_LOCAL_WINDOWS_RUNS_ON_JSON` unset until a Windows-native workflow has
proved the local label. The golden must contain the hosted-runner assumptions
the workflow uses: Git Bash on `PATH`, Chocolatey, `ccache` when the workflow
calls it, Visual Studio Build Tools/MSVC for C++ workflows, and `C:\tmp`. The
supervisor imports `vcvarsall` before launching the Actions runner so Bash steps
can still see `cl.exe`; override the default `arm64` target with
`TARTCI_WIN_VCVARS_ARCH` if a repo needs a different MSVC environment. Windows
preflight is fast by default (`TARTCI_WIN_PREFLIGHT_MODE=fast`); use
`TARTCI_WIN_PREFLIGHT_MODE=full` only when debugging a golden/toolchain/network
issue. QEMU sizing is tunable per host with `TARTCI_WIN_CPUS` and
`TARTCI_WIN_MEMORY_MB`; keep the template conservative and override on larger
hosts such as Mac Studio. When VM leases are enabled, `TARTCI_WIN_VM_CORES`
or the host profile's VM pool size becomes the effective QEMU `-smp` count
instead of the ungated `TARTCI_WIN_CPUS` default.
Supervisor diagnostics and rough benchmark timings are kept per job under
`TARTCI_WIN_LOGS` as `preflight.log`, `runner-output.log`, `runner-diag.log`,
`qemu.log`, and `timing.tsv`; see `docs/runbook.md` for the full setup and
proof recipe.

### Host-health auto-yield (optional)

A tartci host that also carries an interactive/RAM-heavy workload can saturate
(memory-pressure critical → jetsam → unclean reboot) and take the required gate
down mid-job. When the host publishes a shared `host_vitals` green/warn/critical
signal, the serve loop can **stop booting new VMs while the host is saturated**
and resume automatically once it recovers — automating the manual "pause the
pool during a heavy session".

Off by default (no `host_vitals` call, no behavior change). To enable on a host
that has a `host_vitals.sh` on `PATH` (exit `0`=green / `10`=warn / `20`=critical):

- `TARTCI_HOST_VITALS_YIELD=1` — turn the gate on (yields on **critical**).
- `TARTCI_HOST_VITALS_YIELD_ON_WARN=1` — also drain on **warn** (more cautious).
- `TARTCI_HOST_VITALS_BIN` — override the probe path (default `host_vitals.sh`).

Unlike the priority gate, this gate **fails open**: a missing or erroring probe
prints `0` (boot), so a broken `host_vitals` can never wedge the required
runner — host-health yield is crash-avoidance, not a correctness gate. Preview
the decision with `serve macos --print-host-health` (`0` boot / `1` yield;
returns `0` when OFF). `host_vitals.sh` is deliberately **not** shipped by
tartci — bring your own (any script matching the exit-code contract); Pulp's
lives at `tools/scripts/host_vitals.sh`.

### Runtime measurements (optional)

VM serving can emit host-local runtime records for agents and Shipyard without
changing runner behavior. Set `TARTCI_RUNTIME_MEASURE=1` on a serving
LaunchAgent or one-shot `tartci serve` invocation. When unset, no runtime store
is created and the runners behave as before.

Records live under `TARTCI_RUNTIME_STORE` (default `~/.tartci/runtime`) as
append-only JSONL plus per-run summaries. They include VM boot/setup/run/cleanup
durations, runner identity, repo/run/job linkage when available, cache/golden
hints, outcome, and a runner-vs-source `failure_class`. The store is local; it
does not publish to GitHub.

Useful agent-facing queries:

```sh
tartci runtime summary --repo owner/repo --run-id 123 --json
tartci runtime recent --repo owner/repo --limit 20 --json
tartci runtime export --repo owner/repo --since-days 14
tartci runtime backfill --repo owner/repo --timing "$HOME/VMs/logs/tartci-linux"
```

Shipyard owns baselines, drift detection, and advice. tartci only emits the VM
lane truth that Shipyard can import later.

For Pulp-style required macOS gates, keep setup two-step. First serve the
non-required pilot label (`self-hosted,macOS,ARM64,pulp-build-vm`) and prove a
job can drain locally. After that, graduate by serving both labels from the VM
supervisor (`self-hosted,macOS,ARM64,pulp-build,pulp-build-vm`) and route the
workflow selector to the full label set. Keep the bare-metal `pulp-build`
runners online as rollback fallback, but excluded from the default VM route by
the extra `pulp-build-vm` label.

Keep release workflows on their own macOS VM lane. Serve
`self-hosted,macOS,ARM64,pulp-build-vm-release` from
`launchd/com.danielraffel.pulp.tart-runner-macos-release.plist.template`, then
move `PULP_RELEASE_MACOS_RUNS_ON_JSON` to that selector only after a real
release proof completes. The template's ordered
`TARTCI_RUNNER_WORKFLOW_TIERS` keeps one capped supervisor while registering
tagged `Release CLI` / `Sign and Release` runners with
`pulp-release-tagged`, ahead of `Release-path PR gate` runners registered with
`pulp-release-pr-gate`. The class labels are mutually exclusive because GitHub,
not Tart CI, assigns compatible queued jobs after registration. Do not create a
separate supervisor per workflow or share the Build and Test `pulp-build-vm`
label with release jobs.

### Reap stale CI residue
`tartci doctor --reap --json` is the report-only Tier-2 janitor for tartci VM
CI hosts. It emits capacity, supervisor heartbeat, VM/overlay, GitHub runner,
problem, and fixed-action fields with exit codes suitable for launchd/cron (`0`
healthy/fixed, `1` stale or wedged, `2` unreadable). Add `--fix` only after a
clean report: VM or overlay deletion requires an allowed CI prefix plus a tartci
state-file ownership marker, and goldens/bench names/`pulp-vm`/`rosetta-probe`
stay protected. Offline GitHub runner registrations are reaped only when their
names match an owned CI prefix and no fresh live supervisor heartbeat backs
them. See `launchd/com.danielraffel.tartci.reap.plist.template` for the
periodic host-local LaunchAgent shape. macOS/Linux Tart and Windows QEMU runner
heartbeats replace their state files atomically so `doctor`, `observe`, and
fleet-level probes never need to infer health from a partially written JSON
file. `KEEP_FAILED=1` Windows inspection VMs are left alone for the configured
keep-failed window before becoming reap candidates.

For a repository-scoped ephemeral lane, do not treat GitHub's `busy` bit as
proof that a worker still exists. The digest distinguishes
`offline_busy_live_local_owner`, `offline_busy_unconfirmed_local_state`, and
`offline_busy_orphaned_no_local_owner`. The last state means that no matching
TartCI state, lease, supervisor, or VM evidence was found; it is an actionable
reconciliation condition, not ordinary capacity. Agents must capture two
bounded snapshots, inspect the associated in-progress job, and preserve the
protected queue while the state is ambiguous. Only an exact documented
recovery may cancel that stale job and remove its runner registration;
`--fix` must never bulk-cancel busy runners or reset a shared runner namespace.

For Vellum, use the repository-scoped prefixes and groups from
`profiles/normal-local-fast.toml` (for example
`vellum-macos-ephemeral-`/`vellum-macos-build` or
`vellum-pr-safe-ephemeral-`/`vellum-pr-safe-build`). A fresh worktree reuses
those live identities and reconciliation rules; it does not register a new
runner merely because the checkout directory changed. After recovery, require
one real job assignment and explicit VM/lease/runner teardown before declaring
the lane healthy or switching the repository selector from its hosted
fallback.

The disposable guest egress contract must include the Actions runner control
plane, not only the repository website: `github.com`, the resolved
`pipelines*.actions.githubusercontent.com` runner endpoint, and
`broker.actions.githubusercontent.com` must be reachable over HTTPS. A guest
can successfully `curl https://github.com` while the Runner.Listener remains
offline if the broker endpoint is blocked or reset. Record both probes in a
Vellum dispatch proof; fail closed and keep the hosted fallback when the
broker probe fails.

### Observe a live macOS runner
`tartci observe macos` is the read-only operator view for macOS VM jobs. It
combines the janitor digest, matching GitHub run/job/step state, guest process
snapshots, recent CTest output, and the runner log tail. Guest process snapshots
redact GitHub runner `--jitconfig` payloads and truncate long command lines by
default; use `--process-line-width N` if a wider diagnostic view is needed. Use
`--no-guest` when the VM is already gone or SSH is not useful, `--runner NAME`
to narrow a host with multiple supervisors, and `--json` for scripts.

### x86_64 cross / emulation (smoke, not a gate)
The guest is ARM64 (Apple Virtualization has no x86). `tartci up linux
--target-arch x86_64` cross-compiles for x64 (gcc/g++-x86-64-linux-gnu) and runs
the test subset under **Rosetta-for-Linux** (binfmt) — a first line of defense for
atomics / SIMD / ISA-divergent behavior, but **not** a gate: GitHub-hosted x64
stays authoritative (sanitizers, SIMD/Highway dispatch, and RT timing are
unreliable emulated). It defaults **GPU OFF** because the prebuilt Skia maps both
Linux arches to the same `libskia.a` path (`docs/gotchas.md`); `--gpu` requires
an explicit x64 Skia tree via `--skia-dir`, else it fails loud. `--self-test`
proves just the dynamic x64 toolchain+emulator chain with a trivial binary
(golden-agnostic). The provision step installs Rosetta systemd mount/binfmt
units plus an amd64 userland so the setup survives reboot. The manifest declares
this with `target_arch`/`cross` + an `[emulation]` table — see
`manifests/example.x64.toml`.

## Per-project use
A repo drops a `.shipyard/vm-image.toml` (see `manifests/`) declaring its
`os`/`arch`/`toolchain`/`packages`/`caches`/`mounts`. tartci bakes or clones a
golden from it — zero hand-provisioning. Non-generic needs (extra SDKs, a special
toolchain) go in that manifest.

CI routing policy can live beside the consumer repo or in tartci `profiles/` as
commented TOML. tartci treats profiles as a read-only contract: they explain
what each repo wants for `pr`, `release`, `coverage`, `scheduled`, and
`issue_on_failure`, and they map stable target IDs to concrete GitHub
`runs-on` selectors. Shipyard can consume that contract as the router, while
tartci remains the VM/provider layer. `profiles/normal-local-fast.toml` keeps
two Mac Pro Linux capabilities deliberately separate:

- same-repository, unprivileged PR and debug work uses the PR-safe label
  `pulp-pr-safe-linux-x64`, runner group `pulp-pr-safe-build`, and a short
  PR-specific health lease;
- protected merge-group/main work uses `pulp-auto-linux-x64`, runner group
  `pulp-trusted-build`, and its own merge-group lease.

Both selectors include
`self-hosted,Linux,X64,pulp-build-linux-x64,pulp-host-macpro`, but the distinct
capability label is mandatory. A PR-safe runner group is restricted to the
main-owned reusable workflow; a feature-branch workflow does not gain direct
pool access. Each lane falls back to GitHub before assignment when its lease is
missing or expired. Native Intel validation prefers the old Intel Mac mini and
falls back to hosted Intel macOS. Use
`tartci profile explain <name> --repo OWNER/REPO --json` when an agent needs
descriptions and settings from the same parseable source of truth. See
[Shipyard profiles](https://github.com/danielraffel/Shipyard/blob/main/docs/profiles.md)
for the orchestration side.

The profile also defines the fleet naming posture and workflow-class defaults.
PR/debug work may use the PR-safe lane. Release builds, signing, deployment,
privileged, fork, untrusted, secret-bearing, and unsupported-architecture work
remain hosted-only unless they receive a separately reviewed dedicated trust
contract. The Intel Mac mini is a native `macos` / `x64` compatibility lane,
not a Tart VM and not a replacement for the authoritative hosted Intel check.
A missing repository stanza remains hosted-only.

Stable target IDs and `host/lane/slot` identities must not be confused with
static GitHub runner names. A disposable runner keeps the former for health and
audit, but registers with a unique per-boot GitHub name and reclaims only an
offline registration belonging to its own slot. This avoids zombie collisions
without losing fleet observability.

### Queue admission is per pull request

Routing readiness must never be implemented by globally holding the merge queue
or disabling auto-merge across the repository. A routing migration may mark
only its own PRs with the Shipyard opt-out label (normally
`shipyard:no-auto-merge`) until the restricted runner-group and merge-group
proofs are complete. Shipyard then leaves those PRs out of admission while
unrelated eligible PRs continue to enter and drain the protected queue.

The local-first resolver is fail-closed: it selects a local target only when
the profile's exact label set, live online idle capacity, runner-group scope,
and health lease all agree. Otherwise it resolves to the hosted target before
GitHub assigns `runs-on`; GitHub cannot retarget an already queued job. Do not
use a global queue hold, cancel unrelated merge groups, or disable
repository-wide auto-merge as a routing safety interlock.

## Optional pulp-CLI integration
Soft dependency: if installed, `pulp doctor` reports "local CI VMs: available",
and `pulp vm up <os>` delegates here. If absent, Pulp is unaffected. Maximally
useful when present; never a punishment when missing.

## Layout
```
providers/   tart-linux · tart-macos · qemu-windows  (per-OS provision + run + runner)
launchd/     LaunchAgent templates to serve the pool at boot (Pulp's concrete instance + how to genericize)
manifests/   example vm-image.toml per project profile
metrics/     dashboard.py + report.py (file-based; no server) + sample.jsonl
bench/        helper to clone a golden → open in UTM for GUI testing
scripts/     lint.sh — repo hygiene gate (shellcheck + bash -n + py_compile + TOML)
docs/        runbook.md (human from-scratch) · new-repo-agent-guide.md (agent onboarding) · gotchas.md
             proxmox-macpro.md (the x86_64 Proxmox host — NOT tartci-managed yet)
             macmini-metal.md (native Intel macOS/Metal — NOT a Tart VM)
```

## Contributing checks
Run the same gate CI runs before you push — it's one script, portable to macOS's
stock bash 3.2:
```bash
./scripts/lint.sh    # shellcheck -S warning + bash -n on every shell script
                     # (incl. the extensionless `tartci` dispatcher), py_compile
                     # on metrics/*.py, and a parse check on manifests/*.toml
```
`.github/workflows/ci.yml` runs this on every push + PR. It guards the class of
bug that only bites at run time on a CI host — e.g. a help line that executes as
a command before `set -e`.

## Metrics (file-based, no server)
Per-run build/configure/ctest times + cache-hit% land in a `metrics.jsonl` (one
JSON object per line). Two zero-dependency Python scripts read it:
```bash
python3 metrics/report.py    metrics/sample.jsonl                 # text table
python3 metrics/dashboard.py metrics/sample.jsonl index.html      # self-contained HTML
```
`sample.jsonl` carries the real 2026-06 bring-up numbers so the dashboard renders
out-of-box (a "Last run" hero + per-OS configure/build/ctest + cache-warmth
trends). Your own `metrics.jsonl` is gitignored.

## Status
Bring-up proven on a Mac Studio (2026-06): Linux build+test+cache green; Windows
24H2-ARM build green + tests pass. See `docs/runbook.md` for the recipe and the
hard-won gotchas (ISO 512-byte alignment, ramfb vs virtio-gpu, all-USB install
media, MSVC partial-install nuke, BOOTAA64 auto-boot, …).
