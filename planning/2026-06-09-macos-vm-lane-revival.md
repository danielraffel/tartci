# Reviving & abstracting the macOS ephemeral-VM CI lane

**Date:** 2026-06-09
**Status:** Planning — pre-implementation. Analyzed via RepoPrompt oracle + cross-checked
(adversarially) with Codex, across two passes (core plan, then a wedge-monitoring hardening pass).
No code changed yet.
**Repos in scope:** `tartci` (abstraction target), `pulp` (first consumer, current home
of the macOS scripts under `tools/ci/`), `shipyard` (CI orchestrator / scheduler).

---

## TL;DR

The disposable-VM macOS CI lane is **already built but dead**. pulp's `tools/ci/tart-runner.sh`
(JIT runner → clone golden → run one job → destroy VM) plus a full golden-image pipeline exist
on the Mac Studio, but its launchd supervisor crash-loops (exit 126 / "Operation not permitted")
and macOS CI silently fell back to **bare-metal** persistent runners. That bare-metal warm-build-dir
reuse is the root cause of the recurring ODR/stale-build-dir corruption we mop up by hand.

tartci is the right home and is ~90% there: Linux (Tart), Windows (QEMU), and x86_64-cross lanes
are wired and host-validated. **macOS is the only unwired lane.** The work is *revive + abstract +
prove + pool + make it self-heal*, not *build from scratch*.

The ephemeral-VM model also **changes the wedge problem in our favor**: the disposable VM becomes the
recovery boundary (kill the VM, don't surgically kill a worker on a shared host), so most of the
existing bare-metal watchdog machinery becomes unnecessary and recovery gets much simpler.

This doc reconciles independent analyses. Where they disagreed, the correction is marked **[CODEX]**.

---

## Verified current state (live inspection of `macstudio` + the three repos, 2026-06-09)

1. **pulp macOS CI runs bare-metal.** 3 persistent GitHub Actions runners (`pulp-studio-01/02/03`)
   build directly on the host into reused warm dirs `/Volumes/Workshop/ci/pulp/work/pulp-studio-0N/
   pulp/pulp/build-*`. Confirmed by live `ninja -j 9` / `cmake` processes with host paths, not VM
   guests. The only running Tart VM is `rosetta-probe` (a probe, not a build VM).

2. **The VM lane is fully built but dead.** Goldens present in `TART_HOME=/Volumes/Workshop/VMs`:
   `pulp-build-runner:latest`, layered `macos-build-base → pulp-build-base → pulp-build-runner`,
   plus `pulp-linux-build`, `windows-build-wip`, and a persistent `pulp-vm`. The launchd supervisor
   `com.danielraffel.pulp.tart-runner.plist` shows `launchctl` exit code **126**; its log repeats
   `/bin/bash: /Volumes/Workshop/Code/pulp/tools/ci/tart-runner.sh: Operation not permitted`. The
   bare-metal runners dodge this because their executable lives under `~/actions-runner-*` (home dir,
   always TCC-accessible); they only touch `/Volumes` *after* launch with a full user token.

3. **tartci is the abstraction and is mostly done.** First-class providers `providers/tart-linux/`
   and `providers/qemu-windows/`; schema-v2 manifests; a `tartci` dispatcher
   (`up`/`serve`/`doctor`/`bench`/`metrics`); host-mounted ccache warm across clones. Linux + Windows
   + x86_64-cross + pool-serving lanes are wired and host-validated. **macOS is unwired:** no
   `providers/tart-macos/`, no `manifests/pulp.macos.toml`; `tartci up/serve/prepare macos` exit 2.

4. **AVF caps macOS at 2 running VMs/host** (XNU `hv_apple_isa_vm_quota`). Linux/Windows guests are
   *not* subject to this. Persistent VMs (`pulp-vm`, `rosetta-probe`) already consume slots, leaving
   ≤1–2 ephemeral macOS slots per Mac.

5. **shipyard has the capacity brain but it isn't wired in.** `src/capacity.rs` models the cap
   (`DEFAULT_CAP=2`, live `tart list`, fail-closed) and `src/reroute.rs` drains cloud-queued macOS
   jobs back to local when slots free — but the job-admission scheduler (`queue_scheduler.rs`) only
   counts generic host-pool leases, with no OS-awareness and no call into `capacity.rs`.
   - **Confirmed latent bug:** `capacity.rs::TartVm` (src/capacity.rs:112) has only `state`/`running`
     — no `OS` field. `parse_tart_running` (line 140) counts *every* running Tart VM, so once Linux
     Tart VMs share the host they'd wrongly consume macOS slots. Ground-truth fix: pulp's
     `tart-runner.sh` already filters `select(.OS == "macOS" and .State == "Running")` — field is
     `OS`, value `"macOS"`. Verify against live `tart list --format json` before editing.

6. **shipyard already ships a bare-metal runner watchdog** (`src/runner_watchdog.rs`,
   `src/app/runner_cmd.rs`, `docs/runner-watchdog.md`), born from a real 2026-05-12 incident
   (a runner sat stuck on a closed-branch job >75 min while 17 queued runs piled up, blocking a PR
   for hours). It detects `hung_worker` (>`max_job_min`=90), `orphaned_busy`, and `stale_queued_runs`
   (>`max_queue_age_hours`=2); `runner kill` does a guarded SIGTERM→SIGKILL, reaps cmake/ninja/ctest
   children, and quarantines partial `build*` dirs. **This is Model-A (bare-metal) machinery** — it
   inspects the host process table and owns shared build dirs. Model B changes the recovery primitive
   (see next section), so most of it becomes unnecessary for VM jobs but stays valid for any
   bare-metal fallback.

---

## Desired end state

- **Reliable isolated ephemeral VMs** via Tart (macOS + Linux) and QEMU (Windows), serving two
  use cases from one primitive:
  - (a) ephemeral CI runners (one throwaway VM per job), and
  - (b) **on-demand pre-configured VMs an agent can boot to test in isolation** (tartci's `bench`
    concept) — explicitly in scope, not CI-only. **[CODEX: design this, don't defer it.]**
- **macOS abstracted into tartci** as a first-class provider; pulp scripts become thin wrappers, then retire.
- **Pooling across the fleet** — Mac Studio + **BlackBook (M1)** + incoming **M5** — respecting the
  per-host 2-VM cap, with intelligent overflow. (BlackBook is also a dev/agent laptop — see the
  separation rules below.)
- **Self-healing wedge handling** so a hung job is caught and reaped in minutes without a human.
- **The required `pulp-build` gate stays safe throughout** — pilot on non-required `pulp-build-vm`,
  graduate only after a clean pilot, one-command rollback.

---

## How Model B changes the wedge problem

The disposable VM is the recovery boundary. We do **not** rebuild the bare-metal `Runner.Worker`
process model inside each guest. Recovery is "`tart stop/delete` the VM + reclaim its JIT runner
registration," not "SIGKILL a worker and quarantine its build dir."

| Symptom | Bare-metal (Model A) | Ephemeral-VM (Model B) | Recovery |
|---|---|---|---|
| `hung_worker` (e.g. `ctest --repeat until-pass` hang) | host worker/child stuck | guest job stuck inside a disposable VM | per-VM hard wall-clock timeout → kill ssh + `tart run` pid → `tart stop/delete` → reclaim runner reg |
| `orphaned_busy` | API busy but no local worker | JIT registration lingers after VM death | reclaim by static runner name via GitHub API; no process inspection |
| `stale_queued_runs` | wedged/unavailable runner | no free slots, dead supervisor, bad labels, or saturation | Shipyard fleet observer + reroute; `max_queue_age_hours=2` still useful |
| stale build dir corruption | reused host dir rots → SEGFAULTs | **disappears** — build dirs are disposable | none needed |
| orphan VM clone | n/a | **new primary failure mode** — clone survives supervisor death | per-host janitor deletes *owned*, stale, over-age clones |
| stale JIT registration | rare | expected residue after a killed VM | janitor deletes stale offline regs matching *owned* prefixes |
| dead supervisor | runner service offline | `tartci serve` LaunchAgent died while jobs queue | fleet digest reports launchd state + heartbeat age; **host marked unroutable** |
| capacity saturation | generic busy host | per-host 2 macOS slots exhausted (incl. by interactive/bench VMs) | `capacity.rs` + janitor digest; alert as *starvation*, not wedge |

**[CODEX] Critical correction:** killing a VM mid-job does **not** reliably re-queue the job — GitHub
typically marks it failed/lost after the heartbeat times out. **Auto-rerun is a guarded policy
decision, not assumed behavior** (see Open Decision #5).

---

## Wedge detection & self-healing architecture (CLI-level, no bespoke daemon)

Three tiers of responsibility. The intelligence lives in deterministic CLI exit codes + JSON, so a
plain cron or a "really basic model" loop suffices as a *consumer* — none of the tiers requires a
custom always-on monitor process (the only always-on pieces are the `tartci serve --loop`
LaunchAgents that already have to exist).

**Answer to "who runs monitoring?"** Not each agent — wedges happen while no agent is active, which is
exactly how they slip through today. Not a bespoke daemon. The reliable self-healing path is
deterministic CLI (Tiers 1–2, no model); a basic model or cron is a *consumer/escalator* of Tier 3
JSON, never the source of truth for "is this VM safe to delete."

### Tier 1 — in-band supervisor self-defense (`tartci serve`) — CORE, lands in Phase 3
Owns the VM it booted. On timeout / exit / INT / TERM / SSH-failure / runner-exit it kills ssh + the
`tart run` pid, `tart stop/delete`s the VM, and reclaims the stale GitHub runner registration **by
exact static name + expected runner id/state** (not name alone).
- **Per-VM wall-clock timeout** (warn 90 min ≈ `DEFAULT_MAX_JOB_MIN`, hard-kill 120 min) and a
  **JIT idle timeout** (~15 min) so speculative boots reap.
- Writes a per-run **state file** with a heartbeat (`~/.tartci/state/runners/<name>.json`: provider,
  host, vm_name, runner_name, labels, supervisor_pid + **pid start-time**, started_at, deadline_at,
  idle_deadline_at, phase, slot).
- Emits **line-oriented JSON lifecycle events** (`vm_clone`, `jit_registered`, `phase`, `timeout`,
  `teardown`) so cron/model consumers need no parser.
- **[CODEX]** Every `timeout` event carries the GitHub **run_id/job_id**, timeout reason, vm/runner
  name, and a **`rerun_eligible`** flag — timeout produces a failed/lost run, so this is what makes a
  guarded auto-rerun possible.
- **[CODEX]** Keep the VM boundary (no in-guest `ctest` inspection), **but** include coarse
  phase / last-output timestamps from the runner wrapper in events, so the timeout decision isn't
  blind to "slow build vs. truly hung."
- Preserve the macOS-specific behavior pulp's runner already has (and the Linux provider lacks):
  deterministic names, cap gate, stale-name reclaim, aggressive cleanup trap, `brew shellenv`.

### Tier 2 — per-host janitor (`tartci doctor --reap --json [--fix]`) — CORE before multi-host
Runs on **each** host via launchd/cron (~5–10 min). Without `--fix`: report only. With `--fix`: safe
cleanup only. Exit `0` healthy/fixed, `1` wedge found, `2` unreadable (fail-closed). Emits a per-host
JSON digest (capacity{cap,running,free,unreadable}, supervisors[state,last_exit,heartbeat_age], vms[
name,state,age,owned,owner_pid_alive,stale,action], github_runners[…], problems[], fixed[]).
- Safe `--fix`: delete *owned* stopped clones >15 min; *owned* ownerless-running clones >3 h; stale
  offline runner regs matching owned prefixes; stale state files whose pid is gone and VM is gone.
- **[CODEX] Positive ownership, not denylist-absence.** Destructive cleanup requires **both** a
  configured CI prefix **and** a state-file/marker pointer, **and** validates owner-PID *start time*
  (not just PID existence — PID reuse). A live PID with a stale heartbeat is *suspect*, not
  permanently protected. The protected-name denylist (`*:latest`, goldens, `pulp-vm`,
  `rosetta-probe`) is necessary but **not sufficient** — it won't protect future bench names.
- **[CODEX] Idempotent** so it can't double-act when racing Tier 1's teardown of the same VM.
- Replaces the manual `clean-macos-runners.sh` toil.

### Tier 3 — fleet observer (`shipyard runner fleet-status --json`) — read-only, minimal version before graduation
Aggregates per-host `tartci doctor` digests + `capacity.rs` free slots + GitHub queued-age +
`reroute.rs` state + **per-host supervisor heartbeat / digest freshness**. Exit `0/1/2`. Shipyard
**never** deletes VMs — destructive cleanup is delegated to `tartci doctor --reap --fix` on the host.
- **[CODEX] Never route on capacity alone.** A host is **unroutable** if its supervisor heartbeat /
  doctor digest is stale, *even if `tart list` shows free slots* — otherwise reroute drains jobs into
  a host whose `serve` loop is dead. Mark "capacity free but supervisor dead" as actionable.
- Alert conditions: (a) queued-age > threshold **with** free capacity **and** responsive supervisors
  = actionable wedge; (b) capacity-free-but-supervisor-dead; (c) **no fresh digest from a host**
  (who-watches-the-watchers — surfaces an all-supervisors-dead state even when queue age is low);
  (d) **[CODEX]** capacity *starvation* (queued high + zero free on a CI host) — must NOT stay silent
  just because there's no free capacity (esp. on BlackBook where interactive VMs can starve CI).
- Backstop for "everything dead, including doctor": launchd `KeepAlive` on the serve/janitor agents +
  the poller alerting on missing digests.

### Reuse vs. extend the existing watchdog
Clean seam: **tartci owns destructive local VM lifecycle; shipyard owns read-only fleet/cloud
aggregation + reroute.** Do **not** route VM teardown through `shipyard runner kill`. Keep
`runner status/watch/kill` for any bare-metal fallback. Reuse the watchdog's *conventions* (exit-code
semantics, threshold defaults, JSON envelope style) in the new VM/fleet code; do **not** reuse its
`Runner.Worker` process inspection, build-dir quarantine, or child-reaping for VM jobs.

---

## Open decisions to resolve *empirically first* (before writing code)

1. **What actually fixes the launchd failure? [CODEX — top risk of the core plan]** Exit-126 is an
   *exec* denial; relocating the executable to `$HOME` may clear it but `tart` still must reach
   `/Volumes/Workshop/VMs` at *runtime*, and the launchd shell must reach the working dirs. Docs warn
   `/Volumes` under launchd needs Full Disk Access; bare-metal runners work because *everything* is
   under `$HOME`/`/Users/Shared`. **Experiment (~10 min):** move `TART_HOME` to `$HOME/VMs`, point a
   minimal LaunchAgent at a `$HOME` wrapper, `launchctl start`, see if a VM boots. Decides: relocate
   data + executable under `$HOME` (cheap) vs. signed FDA helper (expensive). Don't declare it fixed
   until a VM boots **from launchd**.
2. **Tart JSON OS field** — confirm `OS` / `"macOS"` against live `tart list --format json` before
   touching `capacity.rs`.
3. **Admission architecture. [CODEX]** GitHub binds a job to one runner → the concern is VM *waste*,
   not correctness. Start with self-coordinating per-host `serve --loop` daemons + idle-timeout reap
   (simple, safe for pilot); central SSH-fan-out admission is a later `VmSlot`-lease optimization in
   shipyard, **not** SSH bolted onto `queue_scheduler`. Shipyard's lease store is per-process today.
4. **M5 golden distribution. [CODEX — Phase-5 blocker]** Local `tart build` (reproducible, slow) vs.
   `tart push`/`pull` via a registry (fast, needs registry + auth). Decide before pooling.
5. **[CODEX] Auto-rerun-on-timeout policy** — since a timeout = failed/lost GitHub run, decide the
   guarded, *capped* auto-rerun policy (e.g. rerun once if `rerun_eligible` and not a repeat
   timeout). Default off until pilot timing data exists.
6. **[CODEX] Thresholds are provisional.** 90/120-min wall-clock, 15-min idle, 15-min/3-h reap ages
   inherit the bare-metal shape but VM jobs add boot + SSH-ready + registration + assignment +
   build + test + upload. Tighten **only** after pilot timing data; idle timeout must not start
   before GitHub confirms assignment (assignment lag > idle timeout → premature reap).

---

## Phased plan

Ordering is dependency-driven. Each phase has a validation gate; do not advance until it's green.

### Phase 0 — Safety freeze & inventory
Keep bare-metal `pulp-build` online; change no labels. Inventory goldens + **live** slot usage on
Studio (and later BlackBook/M5): `TART_HOME=/Volumes/Workshop/VMs tart list --format json`.
**Gate:** ≥1 free macOS slot on Studio; required `pulp-build` still served by bare metal.

### Phase 1 — Prove the macOS primitive interactively (chosen starting point)
On `macstudio`, independent of launchd/GitHub: clone `pulp-build-runner:latest` → boot → SSH →
build → discard, via `pulp/tools/ci/tart-run-job.sh`. **[CODEX] Also prove the JIT path**
(`tart-runner.sh` / a `--once` mode). Verify exit 0; no clone left; build ran *inside* the VM; ccache
shows activity. **Gate:** an interactive throwaway-VM build succeeds and leaves no clone behind.

### Phase 2 — Fix launchd / TCC
Run Open Decision #1 first. Default: move `TART_HOME` + working dirs under `$HOME`/`/Users/Shared`
*and* install the executable under `$HOME/.local/bin`; absolute paths only. Signed FDA helper only if
data-relocation can't satisfy runtime `/Volumes` access. Reject plain LaunchDaemon.
**Gate:** no exit 126 **and a VM actually boots from launchd** and reaches the runner loop.

### Phase 3 — Port macOS into tartci as a first-class provider **+ Tier 1 self-defense**
New `providers/tart-macos/{run,runner,provision}.sh` ported from pulp's scripts; generic `TARTCI_*`
env with `PULP_*` fallback; preserve the macOS cleanup traps, static names, stale reclaim, cap gate,
`brew shellenv` (**[CODEX]** don't inherit the Linux runner's missing cleanup trap). Add
`manifests/pulp.macos.toml` (confirm parser tolerates new sections). Wire dispatcher `up/serve/prepare
macos`. **Tier 1 is part of this phase, not a follow-up:** `runner.sh` must add the per-VM wall-clock
+ idle timeouts, state-file + heartbeat, lifecycle JSON events (incl. timeout events carrying GitHub
run/job id + `rerun_eligible` + coarse phase timestamps), and timeout-triggered teardown + runner-reg
reclaim. **Gate:** `tartci up macos` builds+tests in a disposable VM; `tartci serve macos --once`
processes a `pulp-build-vm` job; **a synthetic hung/long-sleep job is torn down at the timeout, its
runner registration reclaimed, no clone remains, and the LaunchAgent keeps serving**; `scripts/lint.sh` passes.

### Phase 4 — Pilot on the non-required label **+ Tier 2 janitor + observability**
Run `tartci serve macos --loop` on Studio with `…,pulp-build-vm[,pulp-build-studio]`; dispatch real
work via `gh workflow run build.yml -f macos_runner_selector_json='[…,"pulp-build-vm"]'`. Verify
isolation (VM-named runner, in-VM workspace, no `pulp-studio-*` build path, VM deleted, no stale reg).
Implement + run `tartci doctor --reap --json` on Studio; create controlled residues (stopped owned
clone, stale offline reg, stale state file) and verify `--fix` removes **only** owned/stale resources.
Lifecycle/observability events land here, not post-graduation. **Gate:** ≥3 pilot jobs pass; janitor
classifies + fixes only owned residue; no orphan VMs / stale regs.

### Phase 5 — Multi-host pooling + shipyard wiring (Studio + BlackBook + M5)
First fix `capacity.rs` to count macOS-only VMs (add `os`, filter `OS=="macOS"`, conservative on
missing, fail-closed; tests). Confirm field name (Decision #2). Resolve M5 golden distribution
(Decision #4). Add `[host_class.*]` for studio/blackbook/m5. **[CODEX] Model the macOS slot as a
`VmSlot` lease resource** (per-host ceiling) rather than SSH-fan-out in `queue_scheduler`; wire
`reroute.rs` in the impure layer (probe → free>0 **and supervisor fresh** → candidates →
`decide_reroute` → retarget + launch one VM). Each host runs its own `tartci doctor --reap`; shipyard
aggregates digests + capacity + queue-age + supervisor freshness into `fleet-status`. **[CODEX]
BlackBook gets stricter separation:** CI-specific state root + prefixes + protected names, and `--fix`
disabled-or-stricter until ownership markers are proven, because agent bench VMs share its Tart
namespace and 2-slot quota; add host-local cap reservations / route weights so the dev laptop isn't
treated as a dedicated runner. **Gate:** with `pulp-vm` on Studio and BlackBook/M5 idle, two queued
jobs → one to Studio (if free), overflow to a free host; no host exceeds 2 macOS VMs; no Linux VM
reduces macOS free; `fleet-status --json` shows per-host capacity, unreadable hosts, **dead
supervisors (host unroutable despite free slots)**, orphan counts, oldest queued-age; exit non-zero on
unreadable host or queued-age-exceeds-threshold-with-capacity.

### Phase 6 — Graduate the required gate
**[CODEX] Pre-*validate* the JIT path end-to-end** (JIT runners are minted per-VM and discarded — not
"pre-registered"): bring up a VM JIT runner advertising `pulp-build`, confirm it takes a required job
while bare metal is still live. Drain bare-metal one at a time (relabel `…-03`, run a required job,
then `…-02`, keep `…-01` as fallback). **Rollback (one action):** re-enable bare-metal launchd /
re-add `pulp-build`; ensure bare-metal workdirs are clean first. **[CODEX] Explicit rollback trigger:**
if a required check misses its SLA by > N min, bare metal re-registers automatically. **Gate:** for an
agreed window — required jobs run in VMs, no `pulp-studio-*` build paths, no host >2 VMs, no recurring
stale-build crashes, M5 overflow works, the janitor ran clean, and `fleet-status` stayed healthy
(no VM job exceeded the warn threshold without teardown).

---

## File-by-file impact

**tartci**
- `providers/tart-macos/run.sh` — new; from pulp `tart-run-job.sh`.
- `providers/tart-macos/runner.sh` — new; from pulp `tart-runner.sh`. Adds Tier 1:
  `TARTCI_JOB_TIMEOUT_SECS` (7200), `TARTCI_JOB_WARN_SECS` (5400), `TARTCI_RUNNER_IDLE_TIMEOUT_SECS`
  (900), state-file + heartbeat, lifecycle JSON events, timeout wrapper around the blocking SSH
  runner command, timeout teardown (kill ssh + `tart run` pid → stop/delete → reclaim reg). Keeps
  static names, cap gate, stale reclaim, cleanup trap, `brew shellenv`.
- `providers/tart-macos/provision.sh` — new; from pulp `tart-provision.sh`.
- `manifests/pulp.macos.toml` — new schema-v2 manifest.
- `tartci` — replace macOS exit-2 stubs with provider dispatch; extend `doctor` with `--reap [--json]
  [--fix]` (alias `tartci reap` optional).
- `scripts/vm_reap.*` — new per-host janitor: parse `tart list --format json` + state files + GitHub
  runners; classify owned/stale; print JSON digest; safe fixes only with `--fix`.
- `launchd/` — macOS `serve` LaunchAgent template (executable under `$HOME`, absolute paths) **and**
  a periodic `com.danielraffel.tartci.reap.plist` (every 5–10 min, `doctor --reap --json --fix`).
- `providers/tart-linux/runner.sh`, `providers/qemu-windows/runner.sh` — fast-follow parity (cleanup
  trap, hard timeout, state/events, overlay/workdir cleanup on timeout) after macOS is proven.
- `bench/` — design the on-demand pre-configured macOS bench VM (agent test use case); ensure bench
  VM names/state are *outside* CI reap prefixes so the janitor never touches them.
- `README.md`, `docs/runbook.md`, `docs/new-repo-agent-guide.md` — macOS no longer "not wired";
  pilot vs. required labels; the 2-VM cap; the `$HOME`-executable rule; the three monitoring tiers.

**pulp**
- `tools/ci/tart-runner.sh`, `tart-run-job.sh` — keep as source of truth through Phase 4; then thin
  wrappers calling tartci; retire only after graduation.
- `com.danielraffel.pulp.tart-runner.plist` — point at the `$HOME` tartci executable; pilot labels.
- GitHub workflow — no required-label change during pilot; `macos_runner_selector_json` → `pulp-build-vm`.

**shipyard**
- `src/capacity.rs` — macOS-only VM counting (+ tests). Lands before fleet/scheduler use it.
- `src/reroute.rs` — no core change; caller uses macOS-only `free` **and** refuses hosts with stale
  supervisor heartbeat.
- host-pool lease store / `queue_scheduler.rs` — add `VmSlot` lease resource (per-host cap); observe-only first.
- `src/app/runner_cmd.rs` — add `runner fleet-status --json` (or `capacity --json --include-health`);
  do **not** route VM teardown through `runner kill`.
- `src/runner_watchdog.rs` — no invasive refactor; keep thresholds/exit-code conventions aligned.
- `docs/runner-watchdog.md` — add an "Ephemeral VM runners" section: `runner kill` is not the VM
  recovery primitive; `tartci serve` owns timeout/teardown, `tartci doctor --reap` owns local orphan
  cleanup, shipyard aggregates fleet health.

---

## Cross-cutting concerns (don't let these fall through the cracks) — mostly **[CODEX]**

- **BlackBook is a dev/agent laptop AND a CI host** — CI VMs and interactive/agent bench VMs share one
  Tart namespace + the 2-slot quota. Use a CI-specific state root + prefixes + protected names; gate
  `--fix` stricter there; add host-local cap reservations / route weights; alert on interactive VMs
  *starving* CI (queued + zero free). Positive CI ownership markers are mandatory before unattended
  `--fix`.
- **Timeout ≠ requeue** — timeouts create failed/lost GitHub runs; auto-rerun is a guarded, capped
  policy (Decision #5), and timeout events must carry run/job id + `rerun_eligible`.
- **Tier 1 ↔ Tier 2 idempotency** — both can act on the same dying VM; cleanup must be idempotent and
  ownership state transactional.
- **Who-watches-the-watchers** — all-supervisors-dead must surface via stale digests / missing
  heartbeats, backed by launchd `KeepAlive`.
- **Golden rebuild cadence** — weekly / on-toolchain-bump rebake as a first-class acceptance criterion.
- **ccache across ephemeral clones** — validate hit rate + guard against corruption under concurrent clones.
- **M5 golden distribution** — Decision #4 (Phase-5 blocker).
- **On-demand agent/bench VM** — first-class design, kept out of CI reap scope.

---

## Immediate next actions (post-discussion)

Three independent checks, runnable in one session on `macstudio`, that finalize Phases 2/3/5:
1. **Phase 1:** interactively prove the throwaway-VM build (`tart-run-job.sh` + the JIT path).
2. **Decision #1 experiment:** the 10-minute launchd-from-`$HOME` / `TART_HOME`-relocation test.
3. **Decision #2:** confirm the Tart JSON `OS` field on the live host.
