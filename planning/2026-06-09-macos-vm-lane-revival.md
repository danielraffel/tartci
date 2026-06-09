# Reviving & abstracting the macOS ephemeral-VM CI lane

**Date:** 2026-06-09
**Status:** Planning — pre-implementation. Analyzed via RepoPrompt oracle + cross-checked
(adversarially) with Codex. No code changed yet.
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
prove + pool*, not *build from scratch*.

This doc reconciles two independent analyses. Where they disagreed, Codex's correction is marked
**[CODEX]**.

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
   `/bin/bash: /Volumes/Workshop/Code/pulp/tools/ci/tart-runner.sh: Operation not permitted` and
   `getcwd: ... Operation not permitted`. The script exists and is `-rwx`. The bare-metal runners
   dodge this because their executable lives under `~/actions-runner-*` (home dir, always
   TCC-accessible); they only touch `/Volumes` *after* launch with a full user token.

3. **tartci is the abstraction and is mostly done.** First-class providers `providers/tart-linux/`
   and `providers/qemu-windows/`; schema-v2 manifests (`manifests/pulp.linux.toml`,
   `pulp.windows.toml`); a `tartci` dispatcher (`up`/`serve`/`doctor`/`bench`/`metrics`);
   host-mounted ccache warm across clones. Linux + Windows + x86_64-cross + pool-serving lanes are
   wired and host-validated. **macOS is unwired:** no `providers/tart-macos/`, no
   `manifests/pulp.macos.toml`; `tartci up/serve/prepare macos` exit 2 pointing back at pulp.

4. **AVF caps macOS at 2 running VMs/host** (XNU `hv_apple_isa_vm_quota`). Linux/Windows guests are
   *not* subject to this. Persistent VMs (`pulp-vm`, `rosetta-probe`) already consume slots, leaving
   ≤1–2 ephemeral macOS slots per Mac.

5. **shipyard has the capacity brain but it isn't wired in.** `src/capacity.rs` models the cap
   (`DEFAULT_CAP=2`, live `tart list`, fail-closed) and `src/reroute.rs` drains cloud-queued macOS
   jobs back to local when slots free — but the job-admission scheduler (`queue_scheduler.rs`) only
   counts generic host-pool leases (`max_concurrency`), with no OS-awareness and no call into
   `capacity.rs`. Dead weight while pulp is on bare metal.

   - **Confirmed latent bug:** `capacity.rs::TartVm` (src/capacity.rs:112) has only `state`/`running`
     — no `OS` field. `parse_tart_running` (line 140) counts *every* running Tart VM. Once Linux
     Tart VMs share the host, they would wrongly consume macOS slots. Ground-truth fix: pulp's
     `tart-runner.sh` already filters `select(.OS == "macOS" and .State == "Running")` — so the field
     is `OS`, value `"macOS"`. Verify against live `tart list --format json` before editing.

---

## Desired end state

- **Reliable isolated ephemeral VMs** via Tart (macOS + Linux) and QEMU (Windows), serving two
  use cases from one primitive:
  - (a) ephemeral CI runners (one throwaway VM per job), and
  - (b) **on-demand pre-configured VMs an agent can boot to test in isolation** (tartci's `bench`
    concept) — explicitly in scope, not CI-only. **[CODEX: the plan must design this, not defer it.]**
- **macOS abstracted into tartci** as a first-class provider (`providers/tart-macos/` + manifest),
  matching the Linux/Windows pattern; pulp scripts become thin compatibility wrappers, then retire.
- **Pooling across Mac Studio + M5**, respecting the per-host 2-VM cap, with intelligent overflow.
- **The required `pulp-build` gate stays safe throughout** — pilot on non-required `pulp-build-vm`,
  graduate only after a clean pilot, one-command rollback.

---

## Open decisions to resolve *empirically first* (before writing code)

These are the riskiest assumptions; each is a short experiment, not a debate.

1. **What actually fixes the launchd failure? [CODEX — top risk]**
   The exit-126 is an *exec*-permission denial. Relocating the executable to `$HOME` may clear that
   — but `tart` still has to reach `/Volumes/Workshop/VMs` at *runtime*, and the launchd job's shell
   must reach `/Volumes/Workshop/ci/...`. Both pulp's plist template and `tartci/docs/runbook.md`
   warn that `/Volumes` access under launchd needs Full Disk Access. The bare-metal runners succeed
   because *everything* lives under `$HOME` / `/Users/Shared`, not because of a path trick.
   **Experiment (~10 min):** copy `TART_HOME` to `$HOME/VMs` (or `/Users/Shared/...`), point a
   minimal LaunchAgent at a `$HOME` wrapper, `launchctl start`, and see if a VM boots. Outcome
   decides: *(A) move data + executable under `$HOME`* (cheap) vs *(B) signed FDA helper* (expensive).
   Do **not** declare the launchd fix done until a VM boots from launchd.

2. **Tart JSON OS field name** — confirm `OS` / `"macOS"` against live `tart list --format json` on
   the host before touching `capacity.rs` (Tart has changed this across versions).

3. **Admission architecture: central brain vs. self-coordinating daemons. [CODEX]**
   GitHub binds a queued job to exactly one runner, so the concern is **VM waste, not correctness**.
   - *Central admission* (shipyard probes capacity, SSHes `tartci serve macos --once` onto the
     chosen host): correct end-state for cost, but shipyard's lease store is per-process today, and
     it adds a SPOF + stale-snapshot + SSH-fan-out failure modes. Large implementation gap.
   - *Self-coordinating daemons* (`tartci serve --loop` per host + a shared/host-local lease + an
     **idle-exit timeout** so speculative boots reap cheaply): simpler, safe for the pilot.
   **Decision:** start with per-host daemons + idle-timeout reap; treat central admission as a later
   `VmSlot` **lease-resource** design in shipyard (not SSH fan-out bolted onto `queue_scheduler`).

4. **M5 golden distribution. [CODEX — Phase-5 blocker]** How does M5 get `pulp-build-runner`?
   Local `tart build` (reproducible, slow) vs. `tart push`/`pull` via a registry (fast, needs a
   registry + auth). Decide before multi-host pooling.

---

## Phased plan

Ordering is dependency-driven. Each phase has an explicit validation gate; do not advance until it's green.

### Phase 0 — Safety freeze & inventory
- Keep all bare-metal `pulp-build` runners online; change no workflow labels.
- Inventory goldens + **live** VM slot usage on Studio (and later M5): `TART_HOME=/Volumes/Workshop/VMs tart list --format json`.
- **Gate:** ≥1 free macOS VM slot on Studio; required `pulp-build` still served by bare metal.

### Phase 1 — Prove the macOS primitive interactively (the unblock you chose to start with)
- On `macstudio`, independent of launchd and GitHub: clone `pulp-build-runner:latest` → boot →
  SSH → configure/build → discard, via `pulp/tools/ci/tart-run-job.sh`.
- **[CODEX] Also prove the JIT path** (`tart-runner.sh`, or a `--once` mode) — `tart-run-job.sh`
  alone does not exercise JIT runner registration, which Phase 3 ports.
- Verify: exit 0; no clone left in `tart list`; build ran *inside* the VM (not under
  `/Volumes/Workshop/ci/pulp/work/pulp-studio-*`); host ccache shows activity.
- **Gate:** an interactive throwaway-VM build succeeds and leaves no clone behind.

### Phase 2 — Fix the launchd / TCC failure
- Run **Open Decision #1** experiment first. Recommended default: move `TART_HOME` + working dirs
  under `$HOME`/`/Users/Shared` *and* install the executable under `$HOME/.local/bin`; LaunchAgent
  uses absolute paths only. Fall back to a signed FDA helper only if data-relocation can't satisfy
  runtime `/Volumes` access. Reject plain LaunchDaemon (root + env blast radius).
- **Gate:** LaunchAgent no longer exits 126 **and a VM actually boots from launchd** and reaches the
  runner loop; no VM boots unless pilot work is queued.

### Phase 3 — Port macOS into tartci as a first-class provider
- New `providers/tart-macos/{run,runner,provision}.sh`, ported from pulp's
  `tart-run-job.sh` / `tart-runner.sh` / `tart-provision.sh`. Generic `TARTCI_*` env with `PULP_*`
  fallback. **Preserve** the macOS-specific bits the Linux provider lacks: aggressive SIGTERM/EXIT
  cleanup traps (kill `tart run` host proc, stop+delete clone), static runner-name derivation
  (`--name`/`--name-prefix`/`--slot`), stale-runner/clone reclaim, `brew shellenv`, and the per-host
  macOS-VM cap gate (`running_macos_vms < cap`). **[CODEX: don't copy the Linux runner's missing
  cleanup trap into macOS.]**
- Add `manifests/pulp.macos.toml` (schema v2: `os="macos"`, base `macos-tahoe-base`, Xcode/brew/pip,
  baked Skia, ccache + fetchcontent mounts, runner labels). First confirm the manifest parser
  tolerates any new sections.
- Wire dispatcher: `tartci up/serve/prepare macos` → the new provider scripts (replace the exit-2 stubs).
- **[CODEX] Add an idle-exit timeout to the `serve --once`/`--loop` path** so speculative boots reap.
- **Gate:** `tartci up macos` builds+tests in a disposable VM; `tartci serve macos --once` registers
  a JIT runner and processes a `pulp-build-vm`-labeled job; `scripts/lint.sh` passes.

### Phase 4 — Pilot on the non-required label
- Run `tartci serve macos --loop` on Studio with `self-hosted,macos,arm64,pulp-build-vm[,pulp-build-studio]`.
- Dispatch real pulp work at it: `gh workflow run build.yml -f macos_runner_selector_json='["self-hosted","macos","arm64","pulp-build-vm"]'`.
- Verify isolation: runner name is the VM's, workspace is in-VM, no `/Volumes/.../pulp-studio-*`
  build path, VM deleted after job, no stale runner registration left behind.
- **[CODEX] Add lifecycle observability here** (structured events: clone/boot/assign/teardown/reap/
  capacity-decision) — needed to debug the pilot, not a post-graduation nicety.
- **Gate:** ≥3 pilot jobs pass with no orphan VMs and no stale offline runners.

### Phase 5 — Multi-host pooling + shipyard wiring
- **First:** fix `capacity.rs` to count macOS-only VMs (add `os: Option<String>`, filter on `OS=="macOS"`;
  conservative on missing field; keep fail-closed). Add tests (macOS counted / Linux ignored /
  unreadable→0 / garbage errors). Confirm field name (Open Decision #2) first.
- Add `[host_class.m5]` (and confirm `[host_class.studio]`) config; resolve M5 golden distribution
  (Open Decision #4).
- **[CODEX] Model the macOS slot as a `VmSlot` lease resource** in shipyard's host-pool lease store
  (per-host ceiling), rather than bolting SSH fan-out onto `queue_scheduler`. Wire `reroute.rs` in
  the impure layer (probe → if free>0 → list cloud candidates → `decide_reroute` → retarget + launch
  one VM). Keep one-reroute-per-tick, flap-guard, fail-closed, apply-repo guard.
- Start with self-coordinating per-host daemons + idle reap (Open Decision #3); central admission is
  the later optimization.
- **Gate:** with `pulp-vm` running on Studio and M5 idle, two queued pilot jobs → one admitted to
  Studio (if a slot is free), overflow to M5; no host exceeds 2 macOS VMs; no Linux VM reduces macOS
  free capacity.

### Phase 6 — Graduate the required gate
- **[CODEX] Pre-*validate* the JIT path end-to-end** (not "pre-register" — JIT runners are minted
  per-VM and discarded). Bring up a VM JIT runner advertising `pulp-build` and confirm it takes a
  required job while bare metal is still live.
- Drain bare-metal runners one at a time (relabel/disable `…-03`, run a required job, then `…-02`,
  keep `…-01` as emergency fallback until VM-required jobs are stable).
- **Rollback (one action):** re-enable bare-metal launchd services / re-add the `pulp-build` label;
  ensure bare-metal workdirs are clean first so rollback doesn't reintroduce stale-build corruption.
  **[CODEX] Make the rollback trigger explicit:** if a required check misses its SLA by > N min,
  bare metal re-registers automatically.
- **Gate:** for an agreed window, required `pulp-build` macOS jobs run in VMs, no `pulp-studio-*`
  build paths, no host over 2 VMs, no recurring stale-build crashes, M5 overflow works.

---

## File-by-file impact

**tartci**
- `providers/tart-macos/run.sh` — new; from pulp `tart-run-job.sh`.
- `providers/tart-macos/runner.sh` — new; from pulp `tart-runner.sh`; keep cleanup traps + cap gate + idle timeout.
- `providers/tart-macos/provision.sh` — new; from pulp `tart-provision.sh` (`verify/base/apple-xcode/pulp/runner/tag/resize/list/manifest`).
- `manifests/pulp.macos.toml` — new schema-v2 manifest.
- `tartci` — replace macOS exit-2 stubs with provider dispatch.
- `launchd/` — add a macOS LaunchAgent template; `ProgramArguments[0]` under `$HOME`, absolute paths only.
- `README.md`, `docs/runbook.md`, `docs/new-repo-agent-guide.md` — macOS no longer "not wired"; document pilot vs. required labels, the 2-VM cap, and the `$HOME`-executable rule.
- `bench/` — design the on-demand pre-configured macOS bench VM (agent test use case).

**pulp**
- `tools/ci/tart-runner.sh`, `tart-run-job.sh` — keep as source of truth through Phase 4; then thin
  wrappers calling `tartci serve/up macos`; retire only after graduation.
- `com.danielraffel.pulp.tart-runner.plist` — point `ProgramArguments` at the `$HOME` tartci
  executable; keep pilot labels until graduation.
- GitHub workflow — no required-label change during pilot; confirm `macos_runner_selector_json`
  targets `pulp-build-vm`.

**shipyard**
- `src/capacity.rs` — macOS-only VM counting (+ tests). Lands before scheduler wiring.
- `src/reroute.rs` — no core change; ensure caller uses the macOS-only `free`.
- host-pool lease store / `queue_scheduler.rs` — add a `VmSlot` lease resource with per-host cap;
  observe-only first, then active. Exact module path to confirm during implementation.
- config docs — `[host_class.studio]` / `[host_class.m5]`, `cap=2` default, unreadable-host behavior.

---

## Cross-cutting concerns (don't let these fall through the cracks) — mostly **[CODEX]**

- **Golden rebuild cadence** — make weekly / on-toolchain-bump rebake a first-class acceptance
  criterion, not a footnote. Stale golden = stale toolchain in "pristine" VMs.
- **ccache across ephemeral clones** — validate hit rate and guard against corruption when multiple
  clones mount the same host cache concurrently.
- **Idle-runner reaping & stale-registration cleanup** — define what happens to a JIT runner that
  boots but never gets a job, and to a GitHub runner registration that outlives its VM.
- **M5 golden distribution** — see Open Decision #4 (blocker for Phase 5).
- **On-demand agent/bench VM** — first-class design, not CI-only.
- **Observability** — structured lifecycle events from Phase 4 onward.

---

## Immediate next actions (post-discussion)

1. **Phase 1:** interactively prove the throwaway-VM build on `macstudio` (`tart-run-job.sh` + the JIT
   path). *(This is the starting point already chosen.)*
2. **Open Decision #1 experiment:** the 10-minute launchd-from-`$HOME` / `TART_HOME`-relocation test
   — it decides whether Phase 2 is cheap or needs a signed helper.
3. **Open Decision #2:** confirm the Tart JSON `OS` field on the live host.

These three are independent and can run in one session on `macstudio`; their outcomes finalize
Phases 2 and 5.
