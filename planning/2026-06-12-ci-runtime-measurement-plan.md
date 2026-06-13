# Optional CI runtime measurement — tartci emitter companion plan

**Date:** 2026-06-12
**Status:** First implementation merged to `main` via PR #13; proof/status
tracking merged via PR #14.
Phases 1-5 have a first implementation:
`scripts/timing_lib.py`, `scripts/runtime_measure.py`, `tartci runtime`,
guarded Linux/Windows/macOS runner emission hooks, optional docs, manifest
comments, backfill, and export. Local validation so far:
`python3 -m unittest scripts/test_runtime_measure.py` and `./scripts/lint.sh`
pass. Companion Shipyard PRs #361 and #362 are also merged; they add
`shipyard metrics import tartci`, `shipyard metrics import github`, agent-facing
metrics summaries/findings, and the GitHub import path fix.

Proof status, 2026-06-13:

- PR #13 merged at `92249b12da814167f52e91dcf9d23dbd81f00438`.
- PR #14 merged at `eb7d3426c2dec10c0afe19fd7908fce1250ef583`.
- PR #361 merged at `659d7bf715d59f0fc5be35c5533144ca1f42e93f`.
- PR #362 merged at `b5734f60007e4b31d3113c5b0023cb8851b92691`.
- Cross-repo import proof succeeded from merged code plus the follow-up
  Shipyard GitHub importer fix: backfilled 2 real local `timing.tsv` records
  from `$HOME/VMs/logs/tartci-linux` and `$HOME/VMs/logs/tartci-win`, exported
  them with `tartci runtime export`, imported them with
  `shipyard metrics import tartci`, imported 6 live Pulp GitHub Actions job
  rows with `shipyard metrics import github`, then queried `summary` and
  `watch` from the same isolated `metrics.db`.
- Shipyard skill guidance now documents the optional metrics integration,
  including non-tartci projects, `metrics import github`, `metrics import
  tartci`, `summary`, `watch`, `advise`, and `compare`.
- Live VM emission proof succeeded against Pulp run
  `27459243068` / job `81169692100`: a one-shot macOS Tart runner with
  `TARTCI_RUNTIME_MEASURE=1` and labels
  `["self-hosted","macOS","ARM64","tartci-runtime-proof"]` emitted a
  runner-sourced `tart-macos` record for `tartci-runtime-proof-01`.
  The record has `status=pass`, `source=runner`, `boot_ms=14000`,
  `run_ms=387000`, `total_ms=404000`, `tags=["runtime-proof","macstudio"]`,
  and `external_id=github:27459243068/81169692100/`.
- The fresh runtime export imported into Shipyard with
  `shipyard metrics import tartci --file ... --json` (`imported: 1`).
  `shipyard metrics summary --project pulp --json` then reported one
  `tart-macos` VM row for `Daniels-Mac-Studio.local`; `shipyard metrics watch
  --project pulp --since 1d --json` reported the expected
  `insufficient_samples` finding.
- The proof workflow's macOS operator job and required `macos` wrapper job both
  completed successfully. The remainder of that manual run was cancelled after
  proof collection because the Linux leg resolved to an unrelated local label.
- Linked Linux live emission proof succeeded against Pulp Docs Consistency run
  `27459504462` / job `81170402485`: a one-shot Tart Linux runner with
  `TARTCI_RUNTIME_MEASURE=1` and labels
  `["self-hosted","Linux","ARM64","tartci-runtime-proof-linux"]` emitted a
  runner-sourced `tart-linux` record for `linux-ephr-77366-1`.
  The record has `status=pass`, `source=runner`, `boot_ms=7000`,
  `run_ms=15000`, `total_ms=24000`, `tags=["runtime-proof","macstudio",
  "linux","linked"]`, and `external_id=github:27459504462/81170402485/`.
- Linked Windows live emission proof succeeded against Pulp Docs Consistency
  run `27459521777` / job `81170449321`: a one-shot QEMU Windows runner with
  `TARTCI_RUNTIME_MEASURE=1`, the cacheopt golden, and labels
  `["self-hosted","Windows","ARM64","tartci-runtime-proof-windows"]` emitted a
  runner-sourced `qemu-windows` record for
  `win-ephr-daniels-mac-studio-79179-1`. The record has `status=pass`,
  `source=runner`, `boot_ms=23000`, `setup_ms=11000`, `run_ms=50000`,
  `total_ms=86000`, `tags=["runtime-proof","macstudio","windows","linked"]`,
  and `external_id=github:27459521777/81170449321/`.
- Windows idle-timeout classification proof succeeded with no matching queued
  job: a one-shot QEMU Windows runner using labels
  `["self-hosted","Windows","ARM64","tartci-runtime-idle-timeout-proof"]` and
  `TARTCI_RUNNER_IDLE_TIMEOUT_SECS=60` emitted a runner-sourced
  `qemu-windows` record for `win-ephr-daniels-mac-studio-82716-1` with
  `status=fail`, `exit_code=124`, `failure_class=idle_timeout`,
  `boot_ms=24000`, `setup_ms=12000`, `run_ms=60000`, and `total_ms=99000`.
  The stale GitHub runner registration was deleted and no proof runner
  registration remained after cleanup.
- A filtered proof export containing the linked Linux record, linked Windows
  record, macOS record, and Windows idle-timeout record imported into Shipyard
  with `shipyard metrics import tartci --file ... --json`; Shipyard reported
  `imported: 3` because the macOS proof had already been imported. The
  resulting `shipyard metrics summary --project pulp --json` included
  `tart-linux`, `tart-macos`, and `qemu-windows` rows plus a failed
  idle-timeout row.
**Parent plan:** `Shipyard/planning/2026-06-12-ci-runtime-measurement-plan.md`
(canonical at `/Volumes/Workshop/Code/Shipyard`). That plan owns the normalized
store (`metrics.db`), GitHub import, summaries, drift detection, and the
agent-facing `shipyard metrics watch/advise/compare` contract. **This plan is the
tartci side of that boundary:** the optional VM-lane emitter of host-local
runtime truth, in a wire shape Shipyard imports verbatim.
**Scope:** `tartci` only. Shipyard work happens in the Shipyard repo against the
parent plan; Pulp/repos only ever opt in.

---

## Ownership boundary (adopted from the parent plan)

> Shipyard owns `metrics.db`, imports, summaries, drift detection, and stable
> JSON output for agents. tartci optionally emits timing events such as VM boot,
> readiness, setup, cache-restore, cache-save, and shutdown. Repos optionally
> annotate runs. External tools import/export through JSON but are not required.

Consequences for this plan:

- tartci **emits and stores host-local records**; it does **not** grow a
  baseline/drift/advice engine. `shipyard metrics watch/advise` is the analysis
  surface; `shipyard metrics import` (or a tartci-export pull) is the bridge.
- The dependency is optional **in both directions**: tartci serving works
  unchanged with measurement off or Shipyard absent; Shipyard metrics works for
  GitHub-hosted/SSH/local lanes with no tartci installed. Graceful fallback
  everywhere — never a punishment when missing (same contract as the optional
  pulp-CLI integration in README).
- This plan **answers the parent plan's first open question**: yes — tartci
  emits boot/setup/run timing *directly* from the supervisors that observe it,
  rather than Shipyard inferring it from wrapper milestones. Direct fields are
  strictly better: the runner scripts already hold the timestamps.

---

## Why this exists — value to autonomous agents

An autonomous agent that dispatches a CI job today gets nothing back but the
GitHub run URL. It has to guess when to poll, can't tell "slow but normal cold
build" from "wedged," and re-learns every repo's timing each session. The
parent plan's customer definition applies verbatim: *the customer is an agent
deciding whether runners are healthy, whether a lane needs closer monitoring,
and whether a change is worth investigating.* tartci's contribution is the data
only a host-local VM supervisor can see:

1. **Phase truth for VM lanes.** Queue→boot→ssh-ready→setup→run→cleanup splits,
   per job, with wall-clock timestamps — the `boot/setup/run split for Tart VM
   lanes` reporting view in the parent plan is impossible without this.
2. **Cold vs warm, attributably.** Cache warmth (`cache_mode` + how it was
   determined), ccache hit % where observable, and **golden image identity** —
   so Shipyard can partition baselines and answer "did the golden rebake or the
   cache change move the needle?" (the parent plan's example finding —
   *"p90 increased 42% after the latest golden image tag"* — requires the
   golden tag to be **in the record**).
3. **Completion metadata keyed by what the agent already knows.** The agent
   knows `repo` + `run_id`; a compact summary file per completed job gives it
   durations, outcome, warmth, and log paths to attach to its own response when
   a build lands — even on a host where Shipyard isn't installed.
4. **Outcome separation.** Normalized `failure_class` distinguishing runner
   failures (boot_failed, ssh_failed, idle_timeout, timeout) from source
   failures — the parent plan's "acceptable failure rate after separating
   source failures from runner failures" signal depends on the emitter making
   this distinction at the moment it is observable.

**Non-negotiable constraint:** strictly optional, per-repo. A tartci consumer
who never enables it sees zero behavior change, zero new files, zero new
dependencies, zero errors.

---

## Verified current state (tartci repo inspection, 2026-06-12)

1. **Two partial timing systems exist; neither serves agents or Shipyard.**
   - `providers/tart-linux/runner.sh` and `providers/qemu-windows/runner.sh`
     write per-job `timing.tsv` (`phase<TAB>seconds`: `boot_to_ssh`,
     [`preflight`,] `runner_process`, [`post_diag`,] `cleanup`, `total`) under
     `$TARTCI_LINUX_LOGS` / `$TARTCI_WIN_LOGS`. No repo/run_id/job_id linkage,
     no outcome, no wall-clock timestamps, no cache/golden metadata.
   - `providers/tart-macos/runner.sh` — the **production required lane** for
     Pulp — writes **no timing artifact at all**, despite having the richest
     linkage: atomic heartbeat state (`$STATE_DIR/<runner>.state.json`) and
     `events.jsonl` with `repo`, `run_id`, `job_id`, `phase`, `vm`, `vm_ip`.
2. **`tartci timings`** (`scripts/timing_summary.py`) summarizes `timing.tsv`
   (median/p90/min/max) but defaults only to Windows/Linux log roots and knows
   nothing beyond phase seconds.
3. **`metrics.jsonl`** (`metrics/report.py`, `dashboard.py`, `sample.jsonl`
   schema: `ts/os/arch/provider/mode(cold|warm)/…/ccache_hit_pct/tests_*`) is
   **hand-maintained** — `docs/new-repo-agent-guide.md` §3.2 tells onboarders
   to write lines by hand. Right *concepts* (mode, cache hit %), no automation,
   no run linkage.
4. **`planning/2026-06-11-ci-routing-profiles.md` (tartci `main`)** promises a
   future `tartci status --json` including "recent timing summaries by
   provider/OS/arch," and draws the same boundary as the parent plan (tartci =
   host-local provider truth; Shipyard = fleet/repo). This plan supplies that
   data source; no third timing system.
5. Conventions this plan must obey (enforced by `scripts/lint.sh` + CI):
   bash 3.2-compatible shell, zero-dependency stdlib Python, file-based/no
   server on the tartci side, atomic state writes (macOS heartbeat precedent),
   0/1/2 exit-code convention (`doctor --reap` precedent), jitconfig/secret
   redaction precedent (`tartci observe`).

---

## Design

### D1. Opt-in model — env-driven, effectively per-repo, zero default change

The serving supervisors are already configured per-repo per-LaunchAgent via env
(`TARTCI_RUNNER_REPO`, labels, goldens). Measurement opt-in rides the same
mechanism, which is what makes it **per-repo** in practice: enable it in one
repo's LaunchAgent/serve invocation and not another's.

```sh
TARTCI_RUNTIME_MEASURE=1                       # master switch; unset/!=1 → no-op
TARTCI_RUNTIME_STORE="$HOME/.tartci/runtime"   # default when enabled
TARTCI_RUNTIME_GH_ENRICH=1                     # best-effort `gh` outcome enrichment
TARTCI_RUNTIME_GOLDEN_HASH=0                   # stat-only golden identity by default
TARTCI_RUNTIME_TAGS="macstudio"                # free-form tags → repo "tweak labels"
```

`TARTCI_RUNTIME_TAGS` doubles as the parent plan's *"repos optionally annotate
runs with profile/lane/tweak labels"* hook: an operator testing a named tweak
(`windows-sccache`, `linux-vm-cpu12`) tags the supervisor, and Shipyard's
before/after `compare` gets its label for free.

Hard rules:
- Every runner hook is wrapped so a measurement failure **logs a warning and
  never changes job outcome or serving-loop behavior** (`|| true` discipline,
  same as cleanup paths).
- No new required manifest keys. `manifests/example.toml` gains a fully
  commented optional `[measurements]` section as **documentation of intent**
  only — runners keep reading env (no manifest parser in the serving hot path).
- With the switch unset: no new directories, no new files, no new process
  spawns beyond a single `[ "$TARTCI_RUNTIME_MEASURE" = 1 ]` test.
- Rollback = unset the env var. The store can be deleted at any time with no
  CI impact.

### D2. Record shape — aligned to Shipyard's schema, not a parallel vocabulary

File-based store, host-local:

```text
$TARTCI_RUNTIME_STORE/
  records/<owner__repo>.jsonl          # append-only, one JSON object per job
  summaries/by-run/<owner__repo>/<run_id>/<job_id|unknown>.summary.json
  inflight/<host>/<runner_name>.json   # start marker (job in progress)
```

**Field names and units copy the parent plan's `jobs`/`steps` tables** so
`shipyard metrics import tartci` is a column-mapping, not a translation layer:

- **Identity/linkage:** `schema_version`, `project`, `repo`, `workflow`,
  `job`, `provider` (`tart-linux|tart-macos|qemu-windows`), `backend="vm"`,
  `host`, `platform`, `arch`, `runner_name`, `vm_name`, `labels`, and
  **`external_id`** = `github:<run_id>/<job_id>/<attempt>` — the parent plan's
  dedup key (`UNIQUE(provider, external_id)`), so a tartci record and a later
  GitHub import of the same job merge instead of double-counting.
- **Timestamps/durations:** `queued_at`/`started_at`/`completed_at` (UTC) and
  integer **`queue_ms`, `boot_ms`, `setup_ms`, `run_ms`, `total_ms`** mapped
  from the lanes' phases (`boot_to_ssh`→`boot_ms`, `preflight`→`setup_ms`,
  `runner_process`→`run_ms`); the full native phase detail rides along as
  `phases_ms{}` (incl. `cleanup`, `post_diag`) and can land in `steps`.
- **Outcome:** `status` (`pass|fail`), `exit_code`, `timed_out`, and
  **`failure_class`** from a closed set separating runner failures from source
  failures: `source_failure runner_timeout idle_timeout boot_failed ssh_failed
  jit_failed runner_nonzero unknown`; best-effort `github_conclusion`.
- **Cache (parent-plan field names):** `cache_mode` (`cold|warm|unknown`) +
  `cache_mode_source` (`probe|env|unknown`), `cache_hit` /`ccache_hit_pct`
  where parseable from existing run output, `cache_restore_ms`/`cache_save_ms`
  when a lane observes them (answers parent open question #2: shared field
  names, emitted by whoever observes the value, both stores converge).
- **Resource/golden hints (cheap only):** CPU count, RAM cap
  (`TARTCI_WIN_CPUS`/`TARTCI_WIN_MEMORY_MB` and Tart equivalents), **golden
  name/tag + stat** (size, mtime); content hash only behind
  `TARTCI_RUNTIME_GOLDEN_HASH=1` (hashing a 100GB+ qcow2 per job is not free).
- **Provenance:** `source` (`runner` vs `*.backfill`), `tags`.

Write discipline: `fcntl` lock for JSONL appends; temp-file + `os.replace()`
for summaries (the macOS heartbeat already proved why atomic replace matters).
Readers tolerate partial last lines, malformed rows, unknown schema versions.
**Redaction (parent plan: "do not store secrets, full logs, or unbounded
command output"):** records never contain jitconfig payloads, tokens, or
command lines — identity + numbers + log *paths* only; logs stay host-local
and the record points at them.

### D3. Runner emission — additive hooks in all three serve lanes

One new zero-dependency helper, `scripts/runtime_measure.py`, does all JSON
construction (no bash JSON). Shared `timing.tsv` parsing/percentile code is
extracted to `scripts/timing_lib.py` and reused by `timing_summary.py`
(refactor must leave `tartci timings` output byte-identical for existing logs).

- **Linux (`tart-linux/runner.sh`):** keep `timing.tsv` exactly as is; after it
  is written, guarded call to `runtime_measure.py complete` with vm/golden/
  cache-root/labels/rc/timing path. Early-failure branches emit `boot_failed` /
  `ssh_failed` partial records when enabled.
- **Windows (`qemu-windows/runner.sh`):** same pattern after its `timing.tsv`;
  passes the existing diag log paths; classifies idle-timeout and
  session-failure branches it already distinguishes. `cache_mode` stays
  `unknown` unless explicitly set via env — do not invent Windows cache warmth
  that isn't measured.
- **macOS (`tart-macos/runner.sh`) — the biggest gap and the production
  lane:** derive `t_start → t_booted(ssh) → t_runner_done → t_done` from its
  existing flow, write a `timing_summary.py`-compatible `timing.tsv` under a
  new `TARTCI_MACOS_LOGS` root (created **only when measuring**), and emit the
  completion record reusing the `CURRENT_RUN_ID`/`CURRENT_JOB_ID` it already
  tracks. Heartbeat/`events.jsonl` schemas are unchanged — `vm_reap.py` and
  `macos_observe.py` must keep working untouched.
- **Run linkage where it's missing (Linux/Windows):** best-effort `gh`
  enrichment at completion keyed by the unique JIT runner name. No `gh`,
  rate-limited, or offline → record lands with empty `external_id` GitHub
  parts and the local inflight summary still resolves by host/runner; never
  blocks teardown (bounded by the existing `TARTCI_GH_TIMEOUT_SECS` pattern).
  Shipyard's own GitHub import can later fill queue time and conclusions —
  dedup via `external_id` makes the merge safe.
- **`queue_ms`:** the supervisor can observe queue age for the job it claims
  (it already inspects queued jobs' `started_at` when matching labels); emit
  when cheap, omit when not — GitHub import is the authoritative queue-time
  source per the parent plan.
- **`tartci up` one-shot lanes:** out of scope for emission in this pass
  (serve lanes are what agents dispatch to); revisit once the serve schema is
  stable.

### D4. Query/export surface — thin, host-local; analysis lives in Shipyard

New dispatcher command `tartci runtime <sub>` → `scripts/runtime_measure.py`.
Read-only, `--json` everywhere, exit codes `0` (answered, even "no data"),
`1` (only with `--strict`), `2` (store unreadable). Default exit 0 so agent
scripts never trip over an empty store.

- **`summary`** `--repo R --run-id N [--job-id J]` — the completion lookup by
  what the agent already knows: reads the atomic summary file(s);
  `found:false` + exit 0 when absent (the run may have gone to a GitHub-hosted
  runner — normal, not an error). Works with zero Shipyard involvement.
- **`export`** `[--repo R] [--since 14d]` — streams records as JSONL/JSON for
  `shipyard metrics import tartci` (or any external tool — the parent plan's
  Hyperfine/Bencher bridge applies). This, plus the store layout above, **is
  the integration contract** with the parent plan.
- **`recent`** `--repo R [--limit 20]` — the parent plan's "last 20 runs for
  one lane" view, host-local (human table + JSON).
- **`backfill`** — imports existing `timing.tsv` trees, `metrics.jsonl` rows,
  and macOS `events.jsonl` as clearly-marked partial records
  (`source:"timing.tsv.backfill"`, `status`/linkage `unknown` where truly
  unknown, nothing fabricated) so months of existing history reach
  Shipyard's baselines on day one.
- **`prune`** `--keep-days N | --keep N` — retention bound for append-only
  files on a CI host.

**Deliberately not in tartci** (they live in `shipyard metrics` per the parent
plan): baselines, p50/p90 prediction, drift detection (`watch`), advice
(`advise`), before/after comparison (`compare`), poll-interval guidance.
`tartci timings` remains the host-local phase-stats convenience and gains the
macOS root — nothing more.

### D5. Requirements this plan feeds INTO the Shipyard implementation

Hardening from the tartci-side analysis that must survive into the parent
plan's Phase 1/3 (recorded here so it isn't lost at the repo boundary; the
emitter records the fields that make each one possible):

1. **Partition baselines by `cache_mode`.** Cold and warm runs are different
   populations (tartci's `metrics/sample.jsonl` shows 273s cold vs 20.8s warm
   on the same Linux lane). A cold run mixed into a warm baseline poisons the
   exact p50/p90 an agent acts on. `unknown`-mode records get their own bucket
   and never dilute warm/cold stats. (This is why the emitter must record
   `cache_mode_source`, not just the mode.)
2. **Partition / annotate by golden generation.** A golden rebake legitimately
   shifts timings; `watch` should annotate "shift coincides with golden
   change" (the parent plan's own example finding) rather than paging on it —
   possible only because records carry golden identity.
3. **In-flight verdicts, not just post-hoc drift.** The highest-value agent
   query is *"I dispatched this 700s ago — normal?"* `watch`/`advise` (or a
   small `shipyard metrics expect --lane L --elapsed-s N`) should return
   `normal | approaching | overdue` against the matching baseline, alongside
   the already-planned `suggested_poll_interval_secs`.
4. **Insufficient-data honesty.** Every payload that falls back must say so
   (`fallback:true`, sample counts, notes) — an agent must distinguish "p90 is
   620s" from "guessing 900s because there's no history." Matches the parent
   plan's suppress-on-low-samples threshold; the no-history default should
   still return usable conservative guidance, never an error.
5. **Don't double-flag.** Failed/timeout jobs are reported by
   `failure_class`, not also as duration anomalies.

### D6. Convergence with the routing-profiles plan (tartci `main`)

The 2026-06-11 plan's `tartci status --json` "recent timing summaries by
provider/OS/arch" field MUST read from this runtime store when present and
**omit the field** (not error) when measurement is disabled. Shipyard's
fleet/status surfaces likewise read `tartci runtime export`/`status` rather
than re-parsing raw `timing.tsv`. One emitter, one store per host, two
consumers (status + metrics.db).

---

## What this is NOT

- **Not the analysis engine.** Baselines, drift, advice, GitHub import, and
  the agent JSON contract are Shipyard's (parent plan Phases 1–3).
- **Not a server, daemon, or database on the tartci side.** Flat files under
  one directory, matching `metrics/`'s no-server contract. (SQLite is the
  right call where the product is querying trends — and that's Shipyard.)
- **Not a replacement for `metrics.jsonl`**, `timing.tsv`, heartbeat state, or
  the dashboard. All existing artifacts keep their exact formats and readers;
  `backfill` bridges history forward. (Fast-follow, separate decision: teach
  `metrics/dashboard.py` to also read runtime records, and update agent-guide
  §3.2 to point at automated capture instead of hand-written lines.)
- **Not a GitHub publisher.** No PR comments, no uploads — agents and Shipyard
  query locally and decide what to surface.
- **Not mandatory for any lane, any repo, any host** — including Pulp's
  required macOS gate. Pulp opts in on the operator's hosts because the data
  is valuable there; a stranger cloning tartci for their own repo never
  notices the feature exists unless they want it.

---

## Phased execution (each phase has a validation gate; do not advance on red)

Sequencing vs the parent plan: tartci Phases 1–4 are independent and can land
before any Shipyard work; Phase 5's import-contract gate needs the parent
plan's Phase-1 `shipyard metrics` skeleton (or validates `export` shape
against the schema in the parent plan if Shipyard hasn't landed yet).

### Phase 1 — Shared library extraction (pure refactor)
**Implementation status:** Done in this branch.

Create `scripts/timing_lib.py` (parse/percentile/provider-inference); refactor
`scripts/timing_summary.py` onto it.
**Gate:** `tartci timings` output byte-identical on existing Windows/Linux
logs; `./scripts/lint.sh` green.

**Evidence:** `scripts/timing_summary.py` imports `TimingRecord`, `collect`,
and `percentile` from `scripts/timing_lib.py`. `./scripts/lint.sh` passes.

### Phase 2 — Store + emit/query engine against synthetic data
**Implementation status:** Done in this branch.

Create `scripts/runtime_measure.py`: store layout, locked appends, atomic
summaries, `complete/summary/export/recent/backfill/prune`, exit-code
contract, field mapping to the parent-plan schema. Wire `tartci runtime` in
the dispatcher. Stdlib unit tests: empty store, malformed rows skipped,
`external_id` construction, phase→ms mapping, failure_class closed set,
prune, summary-not-found.
**Gate:** all queries correct on synthetic fixtures; empty store returns
`found:false`/empty export with exit 0; lint green.

**Evidence:** `tartci runtime` dispatches to `scripts/runtime_measure.py`.
`scripts/test_runtime_measure.py` covers empty summary, complete-summary-export
round trip, and timing backfill without GitHub identity. The focused unittest
passes.

### Phase 3 — Linux + Windows emission (lower-risk lanes)
**Implementation status:** Done as guarded hooks; fresh Linux, Windows, and
Windows idle-timeout proofs completed.

Guarded hooks in `tart-linux/runner.sh`, then `qemu-windows/runner.sh`
(incl. idle-timeout/session-failure classification).
**Gate (both halves required, per lane):** (a) with env **unset**, a served
job produces zero new files/dirs and an unchanged serving transcript; (b) with
env set, one live job → record + summary with correct linkage and timing
matching `timing.tsv`; a forced Windows idle-timeout boot records
`idle_timeout` with `status=fail`, `failure_class` set.

**Evidence:** both runners call `runtime_measure.py complete` only when
`TARTCI_RUNTIME_MEASURE=1`; calls are warning-only on failure and happen after
existing `timing.tsv` writes. `bash -n` and shellcheck via `./scripts/lint.sh`
pass. Historical timing backfill/import proof succeeded. Fresh env-on proofs
also succeeded:
Linux Docs Consistency run `27459504462` / job `81170402485` emitted a linked
`tart-linux` pass record (`total_ms=24000`), Windows Docs Consistency run
`27459521777` / job `81170449321` emitted a linked `qemu-windows` pass record
(`total_ms=86000`), and a no-match Windows run emitted
`failure_class=idle_timeout` with `exit_code=124`.

### Phase 4 — macOS timing + emission (production lane — extra care)
**Implementation status:** Done as a guarded hook; pilot measured job completed.

`TARTCI_MACOS_LOGS` + compatible `timing.tsv` + completion records reusing
heartbeat run/job state. Add the macOS root to `timing_summary.py` defaults
(only when the dir exists).
**Gate:** env-unset half on the live production supervisor first; then one
measured job on the **pilot** label before enabling on the production
LaunchAgent; `vm_reap.py` + `macos_observe.py` outputs unchanged;
`tartci timings` now shows a macos section.

**Evidence:** `providers/tart-macos/runner.sh` creates `TARTCI_MACOS_LOGS`
only when `TARTCI_RUNTIME_MEASURE=1`, writes a compatible `timing.tsv`, and
passes `CURRENT_RUN_ID`/`CURRENT_JOB_ID` into the runtime record. `tartci
timings` now includes `$HOME/VMs/logs/tartci-macos` when present. The pilot
measurement ran on Pulp run `27459243068` / job `81169692100` with label
`tartci-runtime-proof` and emitted a linked `tart-macos` pass record
(`total_ms=404000`).

### Phase 5 — Export contract + backfill + docs
**Implementation status:** Done for tartci-local export/backfill/docs; Shipyard
import proof succeeded for backfilled timing data and fresh live VM export.

Prove `tartci runtime export` against the parent plan's import (live
`shipyard metrics import tartci` if landed, else schema-validation against the
parent plan's `jobs`/`steps` shape). `backfill` for
`timing.tsv`/`metrics.jsonl`/macOS events. Docs: README "Runtime measurements
(optional)" section, runbook operator recipe (LaunchAgent env example in
`launchd/README.md`), `new-repo-agent-guide.md` agent playbook (dispatch →
`shipyard metrics watch` → sleep → `tartci runtime summary` on completion),
commented `[measurements]` block in `manifests/example.toml`.
**Gate:** a backfilled + live-emitted store round-trips into Shipyard (or
validates against schema) with `external_id` dedup proven on one job recorded
by both paths; docs lint; fresh-clone walkthrough works on one lane.

**Evidence:** `runtime export`, `recent`, `summary`, `backfill`, and `prune`
are implemented; `backfill` imports `timing.tsv` and `metrics.jsonl` history.
README, `launchd/README.md`, `docs/new-repo-agent-guide.md`, and
`manifests/example.toml` document the optional integration. The companion
Shipyard main now exposes `shipyard metrics import tartci`. Backfilled real
Linux/Windows timing records imported successfully into Shipyard's SQLite store
alongside live Pulp GitHub job rows. Fresh measured VM exports then imported
successfully into Shipyard: first the macOS proof (`imported: 1`), then a
filtered all-lane proof export (`imported: 3`, adding linked Linux, linked
Windows, and Windows idle-timeout records). `shipyard metrics summary/watch
--project pulp --json` returned the expected agent-readable rows/findings.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Measurement failure breaks a serving job | Every hook `\|\| true`-guarded; classification independent of runner return path; phase gates explicitly test the env-unset half |
| Two stores drift apart (tartci JSONL vs Shipyard SQLite) | Field names/units copied from the parent schema; `external_id` dedup key; Phase-5 round-trip gate; schema_version on every record |
| Polluted baselines → agents act on bad predictions | Emitter records `cache_mode(+source)` + golden identity so Shipyard can partition (D5 #1–2); requirements recorded in both plans |
| `gh` enrichment hangs teardown | Bounded by existing timeout pattern; enrichment optional; GitHub import backfills linkage later via `external_id` |
| Secret leakage into records | Identity+numbers+paths only; jitconfig/command-line redaction rule; reviewed at phase-gate time |
| Store growth on long-lived hosts | `prune`, windowed export defaults, JSONL compactness |
| Concurrent writers (multi-supervisor hosts) | `fcntl` append locks + atomic summary replace; host-local store is the default and the documented recommendation |
| Scope creep into analysis on the tartci side | "Deliberately not in tartci" list in D4; anything baseline-shaped goes to the parent plan |

## Open decisions (resolve during execution, none blocking Phases 1–2)

1. **ccache hit % capture point** — parse from existing in-guest output where
   lanes already print it vs. a dedicated `ccache -s` probe. Default: parse
   what exists; never add guest round-trips just for metrics.
2. **Warmth probe definition** — `cache root nonempty before boot` is necessary
   but crude; decide during Phase 3 whether a ccache stats delta is cheap
   enough to be the `probe`-grade `cache_mode_source`.
3. **Pull vs push into Shipyard** — `shipyard metrics import tartci` pulling
   `runtime export` per host (current bias: simplest, read-only) vs. tartci
   pushing on completion (adds a network dependency to the serving path —
   disfavored). Decide at Phase 5 with the parent plan's Phase-1 shape final.
