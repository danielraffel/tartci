# Reviving & abstracting the macOS ephemeral-VM CI lane

**Date:** 2026-06-09
**Status:** **Approved plan — ready to execute.** Analyzed via RepoPrompt oracle + cross-checked
(adversarially) with Codex over three passes (core plan → wedge-monitoring hardening → executability).
**Repos in scope:** `tartci` (abstraction target), `pulp` (first consumer; macOS scripts in
`tools/ci/`), `shipyard` (CI orchestrator / scheduler). Future consumers: `v8-builder`, `skia-builder`.

---

## For the executing agent — read this first

**You are running ON the primary Mac Studio host.** macOS VM work is **local** — do **not** `ssh`
back into the controller. For the multi-host phase, SSH only to the configured secondary host aliases
needed for setup/verification; establishing/verifying outbound SSH is a Phase-5 prerequisite. Keep
actual host aliases in operator-local config, not reusable repo docs.

**Mission:** take pulp's macOS CI from "VM lane built but dead, silently on bare-metal" to a working
**ephemeral disposable-VM lane**, abstracted into `tartci`, pooled across the controller and secondary
Apple Silicon hosts
(macOS capped at **2 VMs/host**; **Linux/Windows uncapped**), with **warm caches** (pulp, Skia, v8)
and **self-healing wedge handling**, while keeping the required `pulp-build` gate green the whole time.
End state also serves **on-demand pre-configured `bench` VMs** an agent can boot to test in isolation.

**How to execute:** do Phases 0→6 **in order**; each has an explicit **validation gate** — do not
advance until it's green. Phases 1–2 and Open Decisions #1/#2 are short empirical experiments; **run
them first** and let their outcomes finalize Phases 2 and 5. After each phase, append a one-line status
under it (see the Progress checklist at the end) and commit.

**Repos on this host (verify paths before use):**
- `tartci` — `/Users/danielraffel/Code/tartci` (abstraction target; you'll add the macOS provider here).
- `pulp` — `/Volumes/Workshop/Code/pulp` (the launchd plist references this path; first consumer).
- `shipyard` — clone if not present (scheduler / `capacity.rs` / `reroute.rs`).
- Goldens live in `TART_HOME=/Volumes/Workshop/VMs` (export it before any `tart` command).

**Branch / commit / PR rules (obey each repo's CLAUDE.md):**
- **pulp & shipyard: NEVER push to `main`.** Branch → PR → CI → merge. In shipyard use `shipyard pr` /
  `shipyard ship` (it runs the version-bump + skill-sync gates).
- **tartci:** branch → PR → merge (its history is PR-based, `#N`). *(This planning doc is the one
  exception committed directly to `main`.)*
- Commit after each phase; keep changes scoped per repo.

**Safety invariants (never violate):**
1. Keep bare-metal `pulp-build` runners online until Phase 6; never leave the required label with zero
   online runners.
2. Pilot on the **non-required** `pulp-build-vm` label until proven.
3. Reaper `--fix` deletes **only** positively-CI-owned, stale, over-age resources — never goldens,
   `pulp-vm`, `rosetta-probe`, or `bench` VMs.
4. Maintain a one-command rollback (re-enable bare-metal) at all times.

**When in doubt, prefer reading a script's `--help`/usage over trusting a command transcribed here** —
representative commands below may have drifted from the actual flags.

---

## TL;DR

The disposable-VM macOS lane is **already built but dead**. pulp's `tools/ci/tart-runner.sh` (JIT
runner → clone golden → run one job → destroy VM) plus a full golden pipeline exist on the Studio, but
its launchd supervisor crash-loops (exit 126 / "Operation not permitted") and macOS CI silently fell
back to **bare-metal** persistent runners. That warm-build-dir reuse is the root cause of the recurring
ODR/stale-build-dir corruption we mop up by hand.

`tartci` is the right home and is ~90% there: Linux (Tart), Windows (QEMU), x86_64-cross are wired and
host-validated. **macOS is the only unwired lane.** Work = *revive + abstract + warm-cache + prove +
pool + self-heal*, not build-from-scratch. The disposable VM becomes the **recovery boundary**, which
makes wedge handling far simpler than today's bare-metal worker-killing.

---

## Verified current state (live inspection of `macstudio` + the three repos, 2026-06-09)

1. **pulp macOS CI runs bare-metal.** 3 persistent runners (`pulp-studio-01/02/03`) build directly on
   the host into reused warm dirs `/Volumes/Workshop/ci/pulp/work/pulp-studio-0N/pulp/pulp/build-*`.
   Confirmed by live `ninja`/`cmake` with host paths. (Also on this Mac: a `Shipyard-studio-01` runner,
   a `v8builder-01` runner, and interactive agent/dev work — the host is shared.)
2. **The VM lane is fully built but dead.** Goldens in `TART_HOME=/Volumes/Workshop/VMs`:
   `pulp-build-runner:latest`, layered `macos-build-base → pulp-build-base → pulp-build-runner`, plus
   `pulp-linux-build`, `windows-build-wip`, persistent `pulp-vm`. Supervisor
   `com.danielraffel.pulp.tart-runner.plist` exits **126** (`/bin/bash: …/tart-runner.sh: Operation not
   permitted`) — a LaunchAgent can't exec a script on the `/Volumes/Workshop` secondary volume (macOS
   TCC). Bare-metal runners dodge it because everything is under `~/actions-runner-*`.
3. **tartci is the abstraction, mostly done.** First-class `providers/tart-linux/`,
   `providers/qemu-windows/`; schema-v2 manifests; a `tartci` dispatcher (`up`/`serve`/`doctor`/`bench`
   /`metrics`); host-mounted ccache warm across clones. **macOS unwired:** no `providers/tart-macos/`,
   no `manifests/pulp.macos.toml`; `tartci up/serve/prepare macos` exit 2. A `tart-ci` skill exists in
   pulp (`.agents/skills/tart-ci/SKILL.md`) and must be updated to point at tartci.
4. **AVF caps macOS at 2 running VMs/host** (XNU `hv_apple_isa_vm_quota`). **Linux/Windows guests are
   NOT subject to this** — they're bounded only by host CPU/RAM/disk. Persistent VMs (`pulp-vm`,
   `rosetta-probe`) already consume macOS slots.
5. **shipyard has the capacity brain but it isn't wired in.** `src/capacity.rs` (cap=2, live `tart
   list`, fail-closed) + `src/reroute.rs` (cloud→local drain) exist but the admission scheduler
   (`queue_scheduler.rs`) counts only generic leases, no OS-awareness, no call into `capacity.rs`.
   - **Confirmed latent bug:** `capacity.rs::TartVm` (src/capacity.rs:112) has no `OS` field;
     `parse_tart_running` (line 140) counts *every* running Tart VM — so Linux VMs would wrongly eat
     macOS slots. Fix per pulp's own filter: `select(.OS == "macOS" and .State == "Running")`. Verify
     the field name against live `tart list --format json` first.
6. **shipyard already ships a bare-metal runner watchdog** (`src/runner_watchdog.rs`,
   `src/app/runner_cmd.rs`, `docs/runner-watchdog.md`) — detects `hung_worker` (>`max_job_min`=90),
   `orphaned_busy`, `stale_queued_runs` (>`max_queue_age_hours`=2); `runner kill` does guarded
   SIGTERM→SIGKILL + child reaping + build-dir quarantine. **Model-A machinery** (process table + shared
   build dirs); kept for bare-metal fallback, but Model B changes the recovery primitive (below).

---

## Desired end state

- **Reliable isolated ephemeral VMs** via Tart (macOS+Linux) and QEMU (Windows), one throwaway VM per
  CI job, **and** on-demand pre-configured `bench` VMs an agent can boot to test in isolation.
- **macOS abstracted into tartci** as a first-class provider; pulp scripts become thin wrappers, then retire.
- **Warm caches, best-effort, across pulp / Skia / v8** (see "Warm caches" below): host-mounted
  ccache/sccache warm across clones, Skia baked into the golden, FetchContent/`_deps` mounted, layered
  goldens so dependencies are warm by construction.
- **macOS is the only slot-capped OS (2/host); Linux & Windows lanes are NOT gated by that limit** —
  they scale with host resources and run many concurrently.
- **Pool across the controller + secondary Apple Silicon hosts** with **secondary-host failover** and
  **local queueing**: when no slot is free, a job waits and is picked up the moment a slot opens on any
  configured host (no premature cloud push); cloud-queued macOS jobs are drained back to local when a
  slot frees (`reroute.rs`).
- **Self-healing wedge handling** so a hung job is caught and reaped in minutes without a human.
- **tartci repo kept current** — README/docs/runbook updated, and the `tart-ci` skill updated to point
  at tartci's macOS provider.
- **The required `pulp-build` gate stays safe throughout** — pilot on `pulp-build-vm`, graduate only
  after a clean pilot, one-command rollback.

---

## How Model B changes the wedge problem

The disposable VM is the recovery boundary. We do **not** rebuild the bare-metal `Runner.Worker` model
inside each guest. Recovery is "`tart stop/delete` the VM + reclaim its JIT runner registration," not
"SIGKILL a worker + quarantine its build dir."

| Symptom | Bare-metal (Model A) | Ephemeral-VM (Model B) | Recovery |
|---|---|---|---|
| `hung_worker` (e.g. `ctest --repeat until-pass` hang) | host worker/child stuck | guest job stuck in a disposable VM | per-VM hard wall-clock timeout → kill ssh + `tart run` pid → `tart stop/delete` → reclaim reg |
| `orphaned_busy` | API busy, no local worker | JIT registration lingers after VM death | reclaim by static runner name via GitHub API |
| `stale_queued_runs` | wedged/unavailable runner | no free slots, dead supervisor, bad labels, saturation | shipyard fleet observer + reroute; `max_queue_age_hours=2` |
| stale build-dir corruption | reused dir rots → SEGFAULTs | **disappears** — build dirs are disposable | none needed |
| orphan VM clone | n/a | **new primary failure** — clone survives supervisor death | per-host janitor deletes *owned*, stale, over-age clones |
| stale JIT registration | rare | expected residue after a killed VM | janitor deletes stale offline regs matching *owned* prefixes |
| dead supervisor | runner service offline | `tartci serve` LaunchAgent died while jobs queue | fleet digest reports launchd state + heartbeat age; **host marked unroutable** |
| capacity saturation | generic busy host | per-host 2 macOS slots exhausted (incl. by bench/interactive VMs) | `capacity.rs` + janitor digest; alert as *starvation* |

**[CODEX] Critical:** killing a VM mid-job does **not** reliably re-queue the job — GitHub marks it
failed/lost after heartbeat timeout. **Auto-rerun is a guarded, capped policy** (Open Decision #5).

---

## Wedge detection & self-healing architecture (CLI-level, no bespoke daemon)

Three tiers. Intelligence lives in deterministic CLI exit codes + JSON, so a plain cron or a "really
basic model" loop suffices as a *consumer* — no custom always-on monitor (the only always-on pieces are
the `tartci serve --loop` LaunchAgents that must exist anyway).

**Who runs monitoring?** Not each agent (wedges happen while no agent is active — that's how they slip
through today). Not a bespoke daemon. Self-healing is deterministic CLI (Tiers 1–2, no model); a basic
model or cron is a *consumer/escalator* of Tier-3 JSON, never the authority on "is this VM safe to delete."

### Tier 1 — in-band supervisor self-defense (`tartci serve`) — CORE, lands in Phase 3
Owns the VM it booted. On timeout / exit / INT / TERM / SSH-fail / runner-exit: kill ssh + `tart run`
pid → `tart stop/delete` → reclaim the stale GitHub runner reg **by exact static name + expected
id/state** (not name alone).
- **Per-VM wall-clock timeout** (warn ~90 min, hard-kill ~120 min) + **JIT idle timeout** (~15 min) so
  speculative boots reap. (Thresholds provisional — Open Decision #6.)
- Per-run **state file + heartbeat** (`~/.tartci/state/runners/<name>.json`: provider, host, vm_name,
  runner_name, labels, supervisor_pid + **pid start-time**, started_at, deadline_at, idle_deadline_at,
  phase, slot).
- **Line-oriented JSON lifecycle events** (`vm_clone`, `jit_registered`, `phase`, `timeout`,
  `teardown`). Every `timeout` carries GitHub **run_id/job_id** + reason + a **`rerun_eligible`** flag.
- Keep the VM boundary (no in-guest `ctest` inspection) **but** emit coarse phase / last-output
  timestamps so timeout isn't blind to "slow build vs. truly hung."
- Preserve pulp's macOS behavior the Linux provider lacks: deterministic names, cap gate, stale-name
  reclaim, aggressive cleanup trap, `brew shellenv`.

### Tier 2 — per-host janitor (`tartci doctor --reap --json [--fix]`) — CORE before multi-host
Runs on **each** host via launchd/cron (~5–10 min). No `--fix` = report only; `--fix` = safe cleanup.
Exit `0` healthy/fixed, `1` wedge, `2` unreadable (fail-closed). Emits a per-host JSON digest
(capacity, supervisors[state,last_exit,heartbeat_age], vms[…owned,owner_pid_alive,stale,action],
github_runners[…], problems[], fixed[]).
- Safe `--fix`: delete *owned* stopped clones >15 min; *owned* ownerless-running clones >3 h; stale
  offline regs matching owned prefixes; stale state files (pid gone + VM gone).
- **[CODEX] Positive ownership, not denylist-absence:** destructive cleanup requires a configured CI
  prefix **AND** a state-file/marker pointer, **and** validates owner-PID *start time* (PID reuse). Live
  PID + stale heartbeat = *suspect*, not protected. The protected-name denylist (`*:latest`, goldens,
  `pulp-vm`, `rosetta-probe`, bench names) is necessary but **not sufficient**.
- **[CODEX] Idempotent** (can race Tier 1's teardown of the same VM). Replaces manual `clean-macos-runners.sh`.

### Tier 3 — fleet observer (`shipyard runner fleet-status --json`) — read-only, minimal version before graduation
Aggregates per-host `tartci doctor` digests + `capacity.rs` free slots + GitHub queued-age +
`reroute.rs` state + **per-host supervisor heartbeat / digest freshness**. Exit `0/1/2`. Shipyard
**never** deletes VMs — destructive cleanup is delegated to `tartci doctor --reap --fix` on the host.
- **[CODEX] Never route on capacity alone:** a host is **unroutable** if its supervisor heartbeat /
  digest is stale, *even if `tart list` shows free slots* (else reroute drains into a dead host).
- Alerts: (a) queued-age>threshold **with** free capacity **and** responsive supervisors; (b)
  capacity-free-but-supervisor-dead; (c) **no fresh digest from a host** (who-watches-the-watchers —
  catches all-supervisors-dead even when queue age is low); (d) capacity **starvation** (queued high +
  zero free on a CI host — must not stay silent, especially on mixed-use hosts where bench VMs can
  starve CI).
- Backstop: launchd `KeepAlive` on serve/janitor agents + the poller alerting on missing digests.

### Seam
**tartci owns destructive local VM lifecycle; shipyard owns read-only fleet/cloud aggregation +
reroute.** Do **not** route VM teardown through `shipyard runner kill`. Keep `runner status/watch/kill`
for bare-metal fallback. Reuse the watchdog's *conventions* (exit codes, thresholds, JSON envelope), not
its process inspection / build-dir quarantine / child reaping.

---

## Warm caches (best-effort) — pulp, Skia, v8

A named goal: make repeat builds nearly as fast as the warm bare-metal dirs, **without** the corruption.
- **Host-mounted ccache (Tart/macOS+Linux) / sccache (Windows MSVC)** via virtio-fs, warm across
  ephemeral clones (the Linux manifest already does this: `[mounts] ccache = "${TARTCI_CACHE}/pulp/ccache"`).
  Mount must survive cloud-init fstab revert (systemd `.mount` baked in the golden). Validate hit-rate +
  guard against corruption under concurrent clones.
- **Skia baked into the golden** so configure is offline and Skia never recompiles per job (macOS golden
  bakes `/Users/admin/pulp-skia-build`; Linux bakes `[skia] release="chrome/m149"`). On a Skia bump,
  rebake the golden — don't try to warm Skia per-clone.
- **FetchContent/`_deps` mounted or baked** so the threejs/cmake populate steps don't re-fetch per job.
- **Layered goldens** (`macos-build-base → pulp-build-base → pulp-build-runner`) so toolchain + deps are
  warm by construction; only pulp's own source compiles fresh in each clone.
- **Project-agnostic:** tartci's per-repo `vm-image` manifest means **v8** and **skia-builder** get the
  same treatment via their own manifests + cache mounts later (their runners — `v8builder-*` — already
  exist on the controller + secondary hosts). pulp is the first consumer; v8/skia are fast-follow consumers.

---

## Open decisions to resolve *empirically first*

1. **Launchd fix [CODEX — top risk].** Exit-126 is an *exec* denial; relocating the executable to
   `$HOME` may clear it but `tart` still must reach `/Volumes/Workshop/VMs` at *runtime*. Docs warn
   `/Volumes` under launchd needs Full Disk Access; bare-metal works because *everything* is under
   `$HOME`/`/Users/Shared`. **Experiment (~10 min):** move `TART_HOME` to `$HOME/VMs`, point a minimal
   LaunchAgent at a `$HOME` wrapper, `launchctl start`, see if a VM boots. Decides: relocate data +
   executable (cheap) vs. signed FDA helper (expensive). Not fixed until a VM boots **from launchd**.
   **Status 2026-06-09:** resolved in favor of relocation. The production plist's log shows the real
   exit-126 cause: `getcwd` on `/Volumes/Workshop/Code/pulp` and `/bin/bash` reading the script under
   `/Volumes` both fail with `Operation not permitted`. A proof LaunchAgent with `WorkingDirectory=$HOME`,
   executable under `$HOME/.local/bin`, and `TART_HOME=$HOME/VMs` booted a macOS clone from launchd and
   logged `BOOT_OK vm=launchd-home-proof-20260609-01 ip=192.168.64.37`; launchd reported last exit `0`.
   A sparse-copied proof base remains stopped at `~/VMs/vms/macos-build-base:launchd-proof`.
2. **Tart JSON OS field** — confirm `OS` / `"macOS"` against live `tart list --format json` before
   touching `capacity.rs`.
   **Status 2026-06-09:** `tart list --format json` on this host does **not** include `OS`; keys observed
   were `Accessed`, `Disk`, `Name`, `Running`, `Size`, `Source`, and `State`. `tart get <vm> --format json`
   does include `OS`, with `darwin` for macOS images and `linux` for Linux images. Do not implement
   capacity against `list[].OS == "macOS"` without enrichment/fallback.
3. **Admission architecture [CODEX].** GitHub binds a job to one runner → concern is VM *waste*, not
   correctness. Start with self-coordinating per-host `serve --loop` + idle-timeout reap; central
   SSH-fan-out admission is a later `VmSlot`-lease optimization, not SSH bolted onto `queue_scheduler`.
4. **Secondary-host golden distribution [CODEX — Phase-5 blocker].** Local `tart build` (reproducible, slow) vs.
   `tart push/pull` via a registry (fast, needs registry+auth). Decide before pooling.
5. **[CODEX] Auto-rerun-on-timeout** — timeout = failed/lost run, so define a guarded *capped* rerun
   policy (e.g. rerun once if `rerun_eligible` and not a repeat timeout). Default off until pilot data.
6. **[CODEX] Thresholds provisional.** 90/120-min wall-clock, 15-min idle, 15-min/3-h reap inherit the
   bare-metal shape; VM jobs add boot+SSH-ready+registration+assignment+build+test+upload. Tighten only
   after pilot timing data; idle timeout must not start before GitHub confirms assignment.

---

## Phased plan

Each phase: goal → key changes → validation gate. Representative commands assume `export
TART_HOME=/Volumes/Workshop/VMs` and the pulp checkout at `/Volumes/Workshop/Code/pulp`. **Confirm
script flags via `--help` before running.**

### Phase 0 — Safety freeze & inventory
Keep bare-metal `pulp-build` online; change no labels. Inventory goldens + **live** slot usage:
`tart list --format json`. **Gate:** ≥1 free macOS slot on Studio; required `pulp-build` still on bare metal.

**Status 2026-06-09:** complete. Started from `feat/macos-vm-lane-revival` in tartci with no label changes.
Initial `TART_HOME=/Volumes/Workshop/VMs tart list --format json` had no running VMs. Final safety check
showed four online, idle required bare-metal runners carrying `pulp-build`: `pulp-m5-01`,
`pulp-studio-01`, `pulp-studio-02`, and `pulp-studio-03`.

### Phase 1 — Prove the macOS primitive interactively (START HERE)
Local on macstudio, independent of launchd/GitHub: clone `pulp-build-runner:latest` → boot → SSH →
build → discard, e.g. `tools/ci/tart-run-job.sh --golden pulp-build-runner:latest --src "$PWD" --vm
macos-proof-01 --build-type Release --ctest-args "--output-on-failure -N"` (confirm flags first).
**[CODEX] Also prove the JIT path** (`tart-runner.sh` / a `--once` mode). Verify exit 0; `tart list`
shows no clone; build ran *inside* the VM (not under `/Volumes/Workshop/ci/pulp/work/pulp-studio-*`);
ccache shows activity. **Gate:** an interactive throwaway-VM build succeeds and leaves no clone behind.

**Status 2026-06-09:** interactive primitive is green; JIT infrastructure is green, but the Pulp payload
was red. `tart-run-job.sh --golden pulp-build-runner:latest --vm macos-proof-20260609-01 --build-type
Release --ctest-args "--output-on-failure -N"` built inside the guest (`/Users/admin/build`, not
`/Volumes/Workshop/ci/pulp/work/pulp-studio-*`), listed `Total Tests: 10005`, reported ccache activity
(`1686` cacheable calls, `674` hits), exited `0`, and deleted the clone. The one-shot JIT runner
`macos-jit-proof-20260609-01` registered with `self-hosted,macOS,ARM64,pulp-build-vm`, claimed scratch
workflow run `27230185763`, ran the macOS job inside `/Users/admin/actions-runner/_work/pulp/pulp/build-macos`,
exited `0`, deregistered, and deleted the VM. That job failed in Pulp tests: `9170` tests ran, `9169`
passed and `Screenshot render_to_rgba produces non-black pixels (Skia raster)` failed at
`test/test_screenshot.cpp:132` because `rgba.empty()` was true. Scratch branch
`codex/macos-vm-jit-proof-20260609-191935Z` was deleted; no stale runner registration or VM remained.

### Phase 2 — Fix launchd / TCC
Run Open Decision #1 first. Default: move `TART_HOME` + working dirs under `$HOME`/`/Users/Shared` *and*
install the executable under `$HOME/.local/bin`; absolute paths only. Signed FDA helper only if
data-relocation can't satisfy runtime `/Volumes` access. Reject plain LaunchDaemon.
**Gate:** no exit 126 **and a VM actually boots from launchd** and reaches the runner loop.

**Status 2026-06-09:** TCC/boot gate green. Added the macOS Pulp LaunchAgent replacement shape in
`launchd/com.danielraffel.pulp.tart-runner-macos.plist.template`: production label
`com.danielraffel.pulp.tart-runner`, `$HOME/.local/bin/tartci`, `WorkingDirectory=$HOME`, and
`TART_HOME=$HOME/VMs`. The live proof used the same layout and booted a macOS VM from launchd with exit
`0`; the runner-loop portion depends on Phase 3's `tartci serve macos` provider wiring.

### Phase 3 — Port macOS into tartci as a first-class provider **+ Tier 1 self-defense + warm caches**
New `providers/tart-macos/{run,runner,provision}.sh` from pulp's scripts; generic `TARTCI_*` env with
`PULP_*` fallback; preserve macOS cleanup traps/static names/stale reclaim/cap gate/`brew shellenv`
(**[CODEX]** don't inherit the Linux runner's missing cleanup trap). Add `manifests/pulp.macos.toml`
(schema v2; confirm parser tolerates new sections) with the **cache mounts + baked Skia + FetchContent**
from "Warm caches". Wire dispatcher `up/serve/prepare macos`. **Tier 1 is part of this phase:** add
per-VM wall-clock + idle timeouts, state-file + heartbeat, lifecycle JSON events (timeout events carry
run/job id + `rerun_eligible` + coarse phase timestamps), timeout teardown + reg reclaim.
**Gate:** `tartci up macos` builds+tests in a disposable VM with warm ccache; `tartci serve macos
--once` processes a `pulp-build-vm` job; **a synthetic hung/long-sleep job is torn down at the timeout,
its registration reclaimed, no clone remains, the LaunchAgent keeps serving**; `scripts/lint.sh` passes.

**Status 2026-06-09:** tartci macOS provider/Tier-1 CLI gate green; production LaunchAgent pilot is still
Phase 4. Added `providers/tart-macos/{run,runner,provision}.sh`, `manifests/pulp.macos.toml`, and wired
`tartci prepare/up/serve macos`. `tartci up macos --src /Volumes/Workshop/Code/pulp --golden
pulp-build-runner:latest --vm tartci-macos-up-proof-20260609-01 --build-type Release --ctest-args
"--output-on-failure -N"` built in the guest, listed `Total Tests: 10011`, reported warm ccache activity
(`1491/1687` hits), exited `0`, and deleted the clone. `tartci serve macos --once` assigned scratch run
`27232755147` to `tartci-serve-proof-20260609-01`, configured and built successfully, then failed the same
known Pulp payload test (`Screenshot render_to_rgba produces non-black pixels (Skia raster)`); provider
teardown still removed the VM and runner registration. Timeout validation used a minimal scratch workflow:
run `27236065657` assigned to `tartci-timeout-graceful-20260609-01`, emitted `job_warn`, `job_timeout`
with `run_id=27236065657 job_id=80428279082 rerun_eligible=true`, canceled the run before hard-kill,
let the Actions runner remove `.credentials`/`.runner`, then deleted the VM; no proof VM or proof runner
registration remained. Earlier kill-first timeout attempts proved GitHub can hold an offline runner as
`busy` after VM death; the final implementation cancels first and waits for graceful runner exit before
falling back to kill. `./scripts/lint.sh` passed.

### Phase 4 — Pilot on the non-required label **+ Tier 2 janitor + observability**
`tartci serve macos --loop` on Studio with `…,pulp-build-vm[,pulp-build-studio]`; dispatch via `gh
workflow run build.yml -R danielraffel/pulp -f macos_runner_selector_json='[…,"pulp-build-vm"]'`. Verify
isolation (VM-named runner, in-VM workspace, no `pulp-studio-*` build path, VM deleted, no stale reg).
Implement + run `tartci doctor --reap --json` on Studio; create controlled residues (stopped owned
clone, stale offline reg, stale state file) and verify `--fix` removes **only** owned/stale resources.
Lifecycle/observability events land here. **Gate:** ≥3 pilot jobs pass; janitor fixes only owned residue.

**Status 2026-06-09:** Tier-2 janitor implementation is in place and the Phase-4 gate is green.
Added `tartci doctor --reap --json [--fix]`, `scripts/vm_reap.py`, runner heartbeat ownership fields
(`provider`, host, supervisor PID, PID start time), and the periodic `$HOME`-anchored
`launchd/com.danielraffel.tartci.reap.plist.template`. Live report-only on Studio was clean
(`problems=[]`, `fixed=[]`, `capacity.free=2`, no owned VMs/runners). Controlled residue validation
created a stopped owned clone plus a missing-VM stale state file; report-only proposed only
`delete_stopped_vm` and `delete_stale_state`, and `--fix` deleted the proof VM/state files without
touching protected goldens or unrelated VMs. `plutil -lint` passed for the serve and reap templates, and
`./scripts/lint.sh` passed. Stale offline GitHub-runner deletion is implemented for prefix-matching
offline/non-busy registrations, guarded by the local supervisor heartbeat so a live booting JIT runner is
not deleted. Manufactured JIT-registration proof used `tartci-reap-runner-proof-20260609-live` and
`tartci-reap-runner-proof-20260609-stale`: report-only returned `rc=1`, preserved the live-backed offline
runner with `action=wait_for_live_supervisor`, and proposed `delete_offline_runner` only for the stale
registration; `--fix` returned `rc=0` and deleted only
`github_runner_deleted:tartci-reap-runner-proof-20260609-stale:12824`. Post-clean GitHub runner lookup
found no `tartci-reap-runner-proof-20260609*` registrations, and the live host report was clean again
(`problems=[]`, `fixed=[]`, `capacity.free=2`). The first full pilots hit payload issues described
below, then the direct retarget scratch branch produced the required `pulp-build-vm` green x3.

**Host-side Phase-4 note 2026-06-09:** installed tartci under `$HOME/.local/share/tartci` with a
`$HOME/.local/bin/tartci` shim that executes the copied dispatcher via `/bin/bash`, then rendered and
bootstrapped `~/Library/LaunchAgents/com.danielraffel.tartci.reap.plist`. `launchctl kickstart` ran the
janitor with `TART_HOME=/Users/danielraffel/VMs`; `launchctl print` reported `state = not running`,
`runs = 2`, `last exit code = 0`, and `run interval = 300 seconds`. The log JSON had `fix=true`,
`problems=[]`, `fixed=[]`, `capacity.free=2`, and only the protected stopped proof VM
`macos-build-base:launchd-proof`. Did not start the persistent `pulp-build-vm` serve LaunchAgent yet:
the home Tart store does not have `pulp-build-runner:latest` installed, and the existing old
`com.danielraffel.pulp.tart-runner` LaunchAgent is still the pre-Phase-2 `/Volumes` plist flapping with
exit `126`; replacing that label must stay pilot-only and must not advertise required `pulp-build`.

**Serve-loop pilot note 2026-06-09:** copied `pulp-build-runner:latest` into the launchd-accessible
home Tart store (`TART_HOME=/Users/danielraffel/VMs`; `tart get` reported `OS=darwin`, `State=stopped`,
`Disk=150`, `Size=150.041`). Hardened `providers/tart-macos/runner.sh` so `queued_work` inspects queued
jobs and only counts jobs whose requested labels are a subset of the configured runner labels; added a
bounded `TARTCI_GH_TIMEOUT_SECS` around those `gh api` calls and `--print-queue` as a safe preflight.
Validation: with many unrelated queued `Build and Test` runs, the `pulp-build-vm` `--print-queue`
preflight returned `0`. Replaced the old flapping
`~/Library/LaunchAgents/com.danielraffel.pulp.tart-runner.plist` with the home-anchored pilot plist from
tartci; backed up the old plist as `com.danielraffel.pulp.tart-runner.pre-20260609-phase4.plist`.
`launchctl print` showed the serve agent running with `self-hosted,macOS,ARM64,pulp-build-vm`, and the
log showed repeated `waiting 20s (queued=0 running_macos_vms=0/2)` with no idle VM boot. Phase 4 is still
open until real `pulp-build-vm` jobs pass x3.

**Pilot run 2026-06-09:** dispatched Build and Test run `27238315420` on scratch branch
`codex/tartci-macos-vm-pilot-20260609-215402Z` with
`macos_runner_selector_json=["self-hosted","macOS","ARM64","pulp-build-vm"]`. The pilot LaunchAgent saw
`queued=1`, cloned `pulp-build-runner:latest` to `pulp-vm-01`, booted it at `192.168.64.48`, and the job
`macOS (ARM64) [operator]` ran on the VM. Configure and Build passed; Test failed with exactly one ctest
failure out of `9177`: `4708 - Screenshot render_to_rgba produces non-black pixels (Skia raster)`, with
the failing assertion at `/Users/admin/actions-runner/_work/pulp/pulp/test/test_screenshot.cpp:132`.
The Actions runner removed `.credentials` and `.runner`, exited `0`, and the supervisor discarded
`pulp-vm-01`; `TART_HOME=/Users/danielraffel/VMs tart list` showed only stopped
`macos-build-base:launchd-proof` and `pulp-build-runner:latest`, and GitHub had no `pulp-vm-01`
registration. The scratch run was cancelled after the macOS result to stop unrelated hosted jobs, and
the scratch branch was deleted. This is a valid isolation/lifecycle pilot but **not** a green pilot.

**Pilot payload diagnosis 2026-06-09:** the first real VM pilot used `workflow_dispatch`, and Pulp's
macOS workflow configures non-PR runs with `-DPULP_ENABLE_GPU=OFF`
(`/Volumes/Workshop/Code/pulp/.github/workflows/build.yml:799`). The failing test is compiled on every
Apple build because its gate is `defined(__APPLE__) || defined(PULP_HAS_SKIA)`
(`/Volumes/Workshop/Code/pulp/test/test_screenshot.cpp:105`), but the macOS implementation returns an
empty RGBA buffer whenever `PULP_HAS_SKIA` is not defined
(`/Volumes/Workshop/Code/pulp/core/view/platform/mac/screenshot_mac.mm:243`). So the current
`workflow_dispatch` pilot path cannot produce a green macOS payload until Pulp either keeps the needed
Skia/CPU-raster path enabled for dispatch builds or changes the test/implementation gate for Apple
no-Skia builds. Do not treat this as a tartci lifecycle failure, and do not edit Pulp from this lane
while the parallel Pulp refactor agent is active.

**Janitor no-VM state fix 2026-06-09:** after the pilot, report-only `doctor --reap` found an old
owner-dead state file with no VM (`pulp-daniels-mac-studio-01.state.json`) and proposed only
`delete_stale_state`; `--fix` deleted it, and the next report had `problems=[]`, `fixed=[]`,
`github_runners=[]`, `capacity.free=2`, one live waiting supervisor (`pulp-vm-01`), and no stale VMs.

**Cross-store capacity check 2026-06-09:** the default Tart store has a long-running `rosetta-probe`,
but `tart get rosetta-probe --format json` reports `OS=linux`. That VM does not consume the macOS-only
AVF quota, so the home-store pilot cap should not be reduced for it. A future fleet observer should
still count macOS VMs across all configured Tart stores before routing.

**Local PR-like payload proof 2026-06-09:** after adding `tartci up macos --cmake-args`, ran a clean
Pulp checkout (`91b743b1d`) in disposable VM `tartci-prlike-rgba-proof-20260609223008Z` with
`-DPULP_BUILD_TESTS=ON -DPULP_BUILD_EXAMPLES=OFF` and `ctest -R render_to_rgba`. Configure found
`/Users/admin/pulp-skia-build`, built the Skia-enabled test graph, and ctest passed
`Screenshot render_to_rgba produces non-black pixels (Skia raster)` in `0.04s`. The VM was discarded,
the temporary host checkout was removed, and post-run `doctor --reap` was clean. This strengthens the
payload diagnosis: the screenshot failure is specific to the `workflow_dispatch` no-Skia configure path,
not the Tart VM lifecycle. This is still **not** a green Phase-4 pilot job because it did not exercise
GitHub JIT dispatch or count toward the required `pulp-build-vm` green x3 gate.

**Direct retarget pilot + observability note 2026-06-09:** scratch Pulp branch
`codex/tartci-macos-vm-dispatch-prlike-20260609224714Z` rewired `build-macos.yml` to dispatch directly
to `["self-hosted","macOS","ARM64","pulp-build-vm"]`, use Ninja, and check out exact target SHA
`908dbc3779b6ee07633d1fc77575f1744bc7f703`. Run `27242881525` proved the representative VM mechanics:
the job ran on `pulp-vm-01`, Configure passed, Build passed with `cmake --build build-macos-retarget
--parallel 8` and `/opt/homebrew/bin/ninja -j 8`, and teardown removed the VM/runner with
`doctor --reap --json` clean (`capacity.free=2`, no GitHub runners, no stale VMs). Test failed after
`9931` CTest cases with exactly two failures: `9202 - cmake-ios-auv3-configure` and
`9203 - cmake-ios-hostapp-links`. Both failed compiling `core/runtime/src/model_download.cpp` for
`iphonesimulator`; `external/cpp-httplib/httplib.h` references undeclared
`SecTrustCopyAnchorCertificates`. This is a Pulp iOS-simulator payload/config issue surfaced by the
macOS retarget lane, not a tartci lifecycle failure. While diagnosing, manual SSH showed the visibility
gap: the GitHub UI only said `Test`, while the guest was in `ctest`, then two long CMake/Xcode iOS tests,
then duplicate `three.js` FetchContent clones. Added read-only `tartci observe macos` plus heartbeat
fields (`vm_ip`, `run_id`, `job_id`) so future pilots expose the GitHub step, guest process tree, recent
CTest log, and runner log without ad hoc SSH. The observer now redacts runner `--jitconfig` payloads and
truncates long process command lines by default, keeping the useful process/step signal visible.

**Representative green pilot gate 2026-06-09 / 2026-06-10 UTC:** the same scratch branch then added Pulp
commit `d7f8df437b73ebb82600cc97bddb20b20600bbd1` (`ci: keep httplib macOS cert bridge off iOS`),
which disables the cpp-httplib macOS root-certificate bridge for iOS builds while preserving the macOS
Security/CoreFoundation bridge. Local scratch verification passed both former failing scripts:
`test/cmake/test_ios_auv3_configure.sh` and `test/cmake/test_ios_hostapp_links.sh`. Three sequential
GitHub-dispatched representative jobs then passed on the local Tart VM runner `pulp-vm-01`, all at the
pinned `d7f8df437b73ebb82600cc97bddb20b20600bbd1` SHA:
`27244204561`, `27244825290`, and `27245570264`. Each job ran on
`["self-hosted","macOS","ARM64","pulp-build-vm"]`, completed Configure/Build/Test successfully, and
returned the host to a clean state. Post-run `doctor --reap --json` after the final run reported
`capacity.free=2`, `running_macos_vms=0`, `github_runners=[]`, `problems=[]`, `fixed=[]`, and the
supervisor back in `phase=waiting`; `tart list --format json` showed only stopped protected goldens, and
GitHub had no lingering `pulp-vm-01` runner registration. This satisfies the Phase-4 green x3 pilot and
cleanup gate.

**Cost/visibility lesson 2026-06-09:** full Pulp retarget jobs are useful as the expensive final proof
because they exercise real GitHub JIT registration, label routing, job claim, payload execution, runner
deregistration, and VM cleanup. They should not be the routine tartci health probe. The cheaper ongoing
visibility loop should be layered: local Tart smoke for boot/SSH/teardown, a tiny GitHub sentinel job for
JIT labels/claim/cleanup, and representative Pulp builds only for release gates or lane-changing work.

### Phase 5 — Multi-host pooling + shipyard wiring (controller + secondary hosts)
**Prereq:** establish/verify outbound SSH from the controller to each configured secondary host alias. First fix
`capacity.rs` to count macOS-only VMs (add `os`, filter `OS=="macOS"`, conservative on missing,
fail-closed; tests). Confirm field name (Decision #2). Resolve M5 golden distribution (Decision #4) —
build on each host or pull from a registry. Configure each host (install tartci under `$HOME`, goldens
present, launchd serve + reap agents, `[host_class.*]` for each host). **Make explicit that
only macOS lanes consume a `VmSlot`; Linux/Windows lanes are admitted without the 2-cap.** **[CODEX]
Model the macOS slot as a `VmSlot` lease resource** (per-host ceiling) rather than SSH-fan-out in
`queue_scheduler`; wire `reroute.rs` (probe → free>0 **and supervisor fresh** → candidates →
`decide_reroute` → retarget + launch one VM). **Local-queue + failover:** when no slot is free a job
stays queued and is taken the moment any configured host frees a slot (serve-loop polling + reroute), with no
premature cloud push. **[CODEX] Mixed-use hosts get stricter separation:** CI-specific state root + prefixes +
protected names; `--fix` disabled-or-stricter until ownership markers proven; host-local cap reservations
/ route weights so the dev laptop isn't treated as a dedicated runner (its bench/agent VMs share its Tart
namespace + 2-slot quota). **Gate:** with `pulp-vm` on Studio and a free host idle, two queued jobs → one
to Studio (if free), overflow to the free host; no host exceeds 2 macOS VMs; **a Linux/Windows VM does
NOT reduce macOS free, and Linux/Windows jobs run concurrently beyond 2**; `fleet-status --json` shows
per-host capacity, unreadable hosts, **dead supervisors (host unroutable despite free slots)**, orphan
counts, oldest queued-age; non-zero exit on unreadable host or queued-age-with-capacity.

**Status 2026-06-09:** secondary M-series host reachable via operator-local SSH alias. Non-interactive
SSH did not inherit Homebrew's PATH, so reusable setup docs now require explicit
`tart_bin = "/opt/homebrew/bin/tart"` plus absolute `tart_home = "/Users/<you>/VMs"`. Live Shipyard
capacity proof with `tart_home` support read the controller as `running=0/free=2` and the M-series host
as `running=1/free=1`, total `free=3`, `any_unreadable=false`. A stale global host-class entry failed
closed until overridden locally, confirming unreadable hosts do not advertise free capacity.
`runner reroute-watch --repo danielraffel/pulp --target macos --once --json` now emits the per-host
capacity rows and the full candidate list in observe mode; the live tick saw `free_slots=3`,
`candidate_count=10`, and would have selected PR `#3808` without acting.
Remote M-series inspection showed an existing required-lane LaunchAgent still running the older
`~/Code/pulp-ci/tools/ci/tart-runner.sh` shape with `pulp-build,pulp-build-m5` labels and an active
`pulp-m5-01` disposable VM. Do **not** replace that in place while it is serving required jobs. The safe
cutover is side-by-side: install tartci under `$HOME/.local/share/tartci`, add a non-required pilot
LaunchAgent/label on the M-series host, prove cleanup/observe output there, then graduate labels after
the active required lane drains.
**Status 2026-06-09 side-by-side prep:** installed current tartci to the M-series host's
`$HOME/.local/share/tartci` with `$HOME/.local/bin/tartci` wrapper. Remote
`TART_HOME=$HOME/VMs tartci doctor --reap --json` reported `problems=[]`, `free=1`, and the existing
required disposable VM as unowned/non-stale. Remote
`tartci serve macos --print-queue --labels self-hosted,macOS,ARM64,pulp-build-vm-m5-pilot` returned
`0`, so the unique pilot label would idle. Rendered a valid, distinct, **not loaded** pilot plist at
`$HOME/Library/LaunchAgents/com.danielraffel.pulp.tart-runner-macos-pilot.plist`.
**Status 2026-06-09 pilot load proof:** loaded the side-by-side M-series pilot LaunchAgent. It is
running with labels `self-hosted,macOS,ARM64,pulp-build-vm-m5-pilot`, `TART_HOME=$HOME/VMs`, and
workflow filter `Build and Test (macOS retarget)`. The log shows `queued=0 running_macos_vms=1/2`; Tart
still has only the existing required-lane `pulp-m5-01` VM running. Remote `tartci observe macos --json
--no-guest` reports supervisor `pulp-vm-m5-pilot-01`, phase `waiting`, fresh heartbeat, and
`owner_pid_alive=true`. Rollback remains
`launchctl bootout "gui/$(id -u)/com.danielraffel.pulp.tart-runner-macos-pilot"` on that host.
**Status 2026-06-09 fleet visibility proof:** Shipyard now has
`runner fleet-status`, which aggregates `runner capacity`, host-local
`tartci doctor --reap --json`, supervisor freshness, and queued macOS age. A
live high-threshold check (`--queued-age-threshold-secs 999999 --queue-run-limit 40`)
reported `free_slots=4`, `routable_free_slots=4`, `any_unreadable=false`,
`supervisor_unhealthy=false`, and `problem_hosts=false` across controller +
secondary host. The normal-threshold check exited `1` only because
`queued_age_with_capacity=true` (`queue.count=29`, oldest queued age about
13.2k seconds) while both hosts stayed routable; this is the intended visibility
alert. During validation a transient empty heartbeat state file was caught and
fixed by making the macOS runner heartbeat write a temp file and atomically
rename it into place. Patched tartci was synced to the local and secondary
`$HOME/.local/share/tartci` installs; the local and side-by-side secondary pilot
supervisors were restarted idle and `doctor --reap --json` reported
`problems=[]` with fresh `waiting` heartbeats on both.

### Phase 6 — Graduate the required gate + update repo & skill
**[CODEX] Pre-*validate* the JIT path end-to-end** (JIT runners are minted per-VM and discarded — not
"pre-registered"): bring up a VM JIT runner advertising `pulp-build`, confirm it takes a required job
while bare metal is still live. Drain bare-metal one at a time (relabel `…-03`, run a required job, then
`…-02`, keep `…-01` as fallback). **Rollback (one action):** re-enable bare-metal launchd / re-add
`pulp-build`; clean bare-metal workdirs first. **[CODEX] Explicit rollback trigger:** if a required check
misses SLA by > N min, bare metal re-registers automatically. **Update tartci** README/runbook/
new-repo-agent-guide (macOS "wired"; pilot-vs-required labels; 2-VM cap; `$HOME`-executable rule; the
three tiers; warm-cache notes) and **update the `tart-ci` skill** to point at tartci's macOS provider.
**Gate:** for an agreed window — required jobs run in VMs, no `pulp-studio-*` build paths, no host >2
macOS VMs, secondary-host failover works, janitor ran clean, `fleet-status` stayed healthy, docs + skill updated.

---

## File-by-file impact

**tartci**
- `providers/tart-macos/run.sh` — new; from pulp `tart-run-job.sh`.
- `providers/tart-macos/runner.sh` — new; from pulp `tart-runner.sh`. Tier 1:
  `TARTCI_JOB_TIMEOUT_SECS` (7200), `TARTCI_JOB_WARN_SECS` (5400), `TARTCI_RUNNER_IDLE_TIMEOUT_SECS`
  (900), state-file+heartbeat, lifecycle JSON, timeout wrapper + teardown + reg reclaim. Keeps static
  names, cap gate, stale reclaim, cleanup trap, `brew shellenv`.
- `providers/tart-macos/provision.sh` — new; from pulp `tart-provision.sh`.
- `manifests/pulp.macos.toml` — new schema-v2 manifest incl. cache mounts + baked Skia + FetchContent.
- `tartci` — replace macOS exit-2 stubs with provider dispatch; extend `doctor` with `--reap [--json]
  [--fix]`.
- `scripts/vm_reap.*` — new per-host janitor (parse `tart list --format json` + state files + GitHub
  runners; classify owned/stale; JSON digest; safe `--fix` only).
- `launchd/` — macOS `serve` LaunchAgent template (`$HOME` executable, absolute paths) **and** periodic
  `com.danielraffel.tartci.reap.plist` (5–10 min, `doctor --reap --json --fix`).
- `providers/tart-linux/runner.sh`, `providers/qemu-windows/runner.sh` — fast-follow parity (cleanup
  trap, hard timeout, state/events, overlay/workdir cleanup on timeout).
- `bench/` — design on-demand pre-configured macOS bench VM; keep bench names/state **outside** CI reap prefixes.
- `README.md`, `docs/runbook.md`, `docs/new-repo-agent-guide.md` — macOS wired; labels; cap; tiers; warm caches.

**pulp**
- `tools/ci/tart-runner.sh`, `tart-run-job.sh` — keep as source of truth through Phase 4; then thin
  wrappers calling tartci; retire only after graduation.
- `com.danielraffel.pulp.tart-runner.plist` — point at the `$HOME` tartci executable; pilot labels.
- `.agents/skills/tart-ci/SKILL.md` — update to point at tartci's macOS provider + the three tiers.
- GitHub workflow — no required-label change during pilot; `macos_runner_selector_json` → `pulp-build-vm`.

**shipyard**
- `src/capacity.rs` — macOS-only VM counting (+ tests). Lands before fleet/scheduler use it.
- `src/reroute.rs` — caller uses macOS-only `free` **and** refuses hosts with stale supervisor heartbeat.
- host-pool lease store / `queue_scheduler.rs` — add `VmSlot` lease (per-host cap, macOS-only);
  Linux/Windows admitted without the cap; observe-only first.
- `src/app/runner_cmd.rs` — add `runner fleet-status --json`; don't route VM teardown through `runner kill`.
- `src/runner_watchdog.rs` — no invasive refactor; keep thresholds/exit-code conventions aligned.
- `docs/runner-watchdog.md` — add "Ephemeral VM runners" section (VM recovery primitive is `tartci serve`/`doctor`).

---

## Cross-cutting concerns — mostly **[CODEX]**

- **A secondary host may be a dev/agent laptop AND a CI host** — CI + bench VMs share one Tart namespace + the
  2-slot quota. CI-specific state root + prefixes + protected names; gate `--fix` stricter; host-local
  cap reservations / route weights; alert on bench VMs *starving* CI. Positive CI ownership markers
  mandatory before unattended `--fix`.
- **Timeout ≠ requeue** — timeouts create failed/lost runs; auto-rerun is guarded+capped (Decision #5);
  timeout events carry run/job id + `rerun_eligible`.
- **Tier 1 ↔ Tier 2 idempotency** — both can act on the same dying VM; cleanup idempotent, ownership transactional.
- **Who-watches-the-watchers** — all-supervisors-dead surfaces via stale digests / missing heartbeats +
  launchd `KeepAlive`.
- **Golden rebuild cadence** — weekly / on-toolchain-or-Skia-bump rebake as a first-class acceptance criterion.
- **Secondary-host golden distribution** — Decision #4 (Phase-5 blocker).
- **On-demand agent/bench VM** — first-class design, kept out of CI reap scope.

---

## Immediate next actions (in one session on macstudio)

Three independent checks that finalize Phases 2/3/5:
1. **Phase 1:** interactively prove the throwaway-VM build (`tart-run-job.sh` + the JIT path). **Status
   2026-06-09:** interactive build green; JIT runner lifecycle green; JIT payload failed one Pulp screenshot
   test, so do not treat this as a green pilot payload yet.
2. **Decision #1 experiment:** the 10-minute launchd-from-`$HOME` / `TART_HOME`-relocation test.
   **Status 2026-06-09:** green; `$HOME` wrapper + `$HOME/VMs` booted a macOS VM from launchd with exit `0`.
3. **Decision #2:** confirm the Tart JSON `OS` field on the live host. **Status 2026-06-09:** resolved;
   use `tart get --format json` for `OS`, not `tart list`.

---

## Progress checklist (executing agent: tick + datestamp as you complete each)

- [x] Phase 0 — safety freeze & inventory (2026-06-09)
- [x] Phase 1 — macOS primitive proven (interactive + JIT lifecycle) (2026-06-09; JIT payload failed one Pulp screenshot test)
- [x] Phase 2 — launchd boots a VM (no exit 126) (2026-06-09; runner loop completed by Phase 3 provider)
- [x] Phase 3 — tartci `providers/tart-macos` + manifest + Tier 1 + warm caches; synthetic-wedge teardown verified (2026-06-09; production LaunchAgent pilot remains Phase 4)
- [x] Phase 4 — pilot on `pulp-build-vm` green x3; Tier 2 janitor proven; `tartci observe macos` added and used for live process/CTest visibility (2026-06-09; green runs `27244204561`, `27244825290`, `27245570264`)
- [ ] Phase 5 — controller+secondary hosts pooled; capacity.rs macOS-only; VmSlot lease; failover + local queue; Linux/Windows ungated; fleet-status
- [ ] Phase 6 — required `pulp-build` graduated to VMs; bare-metal fallback retained; tartci docs + `tart-ci` skill updated
