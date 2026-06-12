# CI Routing Profiles Plan

Date: 2026-06-11

## Goal

Make local CI routing easy to understand, inspect, and change across tartci,
Shipyard, and Pulp without editing scattered GitHub variables by hand.

The profile file must answer mechanically:

- where PR, release, coverage, and scheduled jobs run;
- what fallback order applies when a local host is busy or offline;
- which lanes use local ARM64 VMs and which stay on GitHub-hosted x64;
- whether scheduled failures should file or update issues;
- which GitHub variables or workflow inputs a profile would apply.

The file should also be readable by humans and agents. Descriptions are useful,
but structured settings are the source of truth.

## Review Status

Claude review was requested before implementation. The local Claude CLI was not
authenticated during initial drafting:

```text
Not logged in - Please run /login
```

This document is written so a later Claude pass can review the plan without
needing chat history.

## Boundary

tartci owns host-local provider truth:

- Tart/QEMU provider commands;
- host-local runner capacity and residue checks;
- profile parsing and explanation for local capabilities;
- timing summaries and cache/golden prep status;
- JSON status that another orchestrator can consume.

Shipyard owns fleet and repo routing:

- host ordering across Mac Studio, M5/blackbook, and GitHub fallback;
- reading `tartci status --json` from reachable hosts;
- planning and applying repo-level GitHub variables;
- issue filing/updating for scheduled main/nightly failures;
- fleet-wide explanations such as "where will Pulp PR Windows run?".

Pulp owns workflow behavior:

- resolver inputs and repository variables;
- workflow_dispatch overrides;
- scheduled GitHub-hosted Intel Linux/Windows validation;
- coverage/release path distinctions.

Important constraint: GitHub Actions cannot change `runs-on` after a job is
queued. An ordered fallback chain is therefore a Shipyard planning decision made
before dispatch or repository variable updates. Pulp workflows should receive one
concrete selector for a given run, not a fallback list that GitHub interprets.

## Profile Format

Use commented TOML. tartci already uses TOML manifests, and Shipyard already has
profile documentation in TOML. Do not introduce a separate language.

Profiles should be small, explicit, and repo-aware:

```toml
# Profile: normal-local-fast
#
# PR: checks that run for pull requests, workflow_dispatch PR validation, and
#     regular fast feedback.
# Release: conservative release workflows. These may intentionally use fewer
#     local fallbacks than PR checks.
# Coverage: coverage-producing jobs. Use clean ephemeral runners; do not route
#     coverage to warm bare-metal build pools.
# Scheduled: periodic validation on main. This is the right place for slower
#     GitHub-hosted Intel Linux/Windows checks.
# issue_on_failure: for scheduled/main jobs, create or update a deduped issue
#     with run URL, commit, lane, failing step, and error signature.

name = "normal-local-fast"
description = "Fast PRs on local ARM64 VMs with GitHub Intel validation nightly."

[repo."danielraffel/pulp".pr.macos]
strategy = "ordered-fallback"
targets = ["macstudio.macos-arm64-vm", "m5.macos-arm64-vm", "github.macos-arm64"]

[repo."danielraffel/pulp".pr.linux]
strategy = "ordered-fallback"
targets = ["macstudio.linux-arm64-vm", "m5.linux-arm64-vm", "github.linux-x64"]

[repo."danielraffel/pulp".pr.windows]
strategy = "ordered-fallback"
targets = ["macstudio.windows-arm64-qemu", "github.windows-x64"]

[repo."danielraffel/pulp".release.macos]
strategy = "ordered-fallback"
targets = ["macstudio.macos-arm64-release-vm", "github.macos-arm64"]

[repo."danielraffel/pulp".coverage.macos]
strategy = "ordered-fallback"
targets = ["macstudio.macos-arm64-coverage-vm", "github.macos-arm64"]
ephemeral_required = true

[repo."danielraffel/pulp".scheduled.nightly_intel]
enabled = true
branch = "main"
targets = ["github.linux-x64", "github.windows-x64"]
issue_on_failure = true
```

Target IDs are stable profile vocabulary, not GitHub labels. A separate target
catalog maps each ID to capabilities, a `runs_on_json` selector, and optional
GitHub variable names. Example:

```toml
[targets."macstudio.windows-arm64-qemu"]
description = "Mac Studio Windows ARM64 QEMU runner."
provider = "tartci"
host = "macstudio"
os = "windows"
arch = "arm64"
runs_on_json = ["self-hosted", "Windows", "ARM64", "pulp-build-windows"]

[targets."github.windows-x64"]
description = "GitHub-hosted Windows x64 runner."
provider = "github"
os = "windows"
arch = "x64"
runs_on_json = "windows-latest"
authoritative_for = ["windows-x64"]
```

Required strategy vocabulary:

- `ordered-fallback`: try targets in order before falling back.
- `github-only`: always use GitHub-hosted selectors.
- `local-only`: never choose GitHub fallback automatically.
- `disabled`: do not run this lane from the profile.

Required contexts:

- `pr`
- `release`
- `coverage`
- `scheduled`

## CLI Surface

Add tartci commands first:

```bash
tartci profile list
tartci profile show normal-local-fast
tartci profile explain normal-local-fast --repo danielraffel/pulp
tartci profile explain normal-local-fast --repo danielraffel/pulp --json
tartci profile plan normal-local-fast --repo danielraffel/pulp
tartci status --json
```

`explain` is read-only and answers in plain English and JSON.

`plan` is read-only and shows:

- selected lanes and ordered fallback chain;
- the single concrete selector that would be used now, when live host status is
  available;
- GitHub variables that would be set or cleared;
- workflow schedules that are expected to exist;
- warnings for unproven local x64 emulation, coverage without ephemeral labels,
  and missing host status.

Do not add `apply` until `plan` is useful enough to review safely.

Shipyard should then consume the same concepts:

```bash
shipyard ci profile plan normal-local-fast --repo danielraffel/pulp
shipyard ci profile apply normal-local-fast --repo danielraffel/pulp
shipyard ci status --repo danielraffel/pulp
```

Shipyard may wrap tartci status on each host rather than reimplement provider
facts.

Minimum `tartci status --json` fields:

- host identity and clock;
- supported targets and capabilities;
- runner capacity by provider/OS/arch;
- active VMs and active GitHub runner jobs;
- stale residue problems from `doctor --reap`;
- cache/golden freshness where known;
- recent timing summaries by provider/OS/arch.

## Pulp Mapping

The first Pulp profile should map to existing variables rather than requiring a
workflow rewrite:

- `PULP_LOCAL_MACOS_RUNS_ON_JSON`
- `PULP_LOCAL_LINUX_RUNS_ON_JSON`
- `PULP_LOCAL_WINDOWS_RUNS_ON_JSON`
- `PULP_OVERFLOW_BUILD_MACOS_RUNS_ON_JSON`
- `PULP_RELEASE_MACOS_RUNS_ON_JSON`
- `PULP_COVERAGE_MACOS_RUNS_ON_JSON`
- `PULP_COVERAGE_WINDOWS_RUNS_ON_JSON`

These variables hold concrete `runs-on` selectors, not ordered fallback lists.
Shipyard can set them for repo defaults, or pass workflow_dispatch inputs for a
single run. If no live capacity signal is available, profile planning must show
the configured fallback chain but avoid claiming a concrete local selector is
available.

Windows and Linux Intel scheduled validation should stay GitHub-hosted until
local x64 emulation is explicitly proven useful:

- local Windows QEMU is Windows ARM64;
- `vcvarsall x64` inside Windows ARM64 is an emulated x64 smoke, not an
  equivalent replacement for GitHub-hosted `windows-latest`;
- local Linux ARM64 plus Rosetta/qemu-user x64 is useful for smoke/debug, not
  yet the authoritative x64 gate.

## Issue Filing Policy

Issue filing belongs to Shipyard or a dedicated Pulp scheduled workflow, not to
tartci host scripts.

Only file/update issues for scheduled main/nightly failures by default. Deduping
key should include:

- repo;
- workflow;
- lane/platform/arch;
- failure step;
- normalized error signature.

PR failures should stay on the PR unless explicitly configured otherwise.

Before adding a new scheduled Pulp workflow, audit existing scheduled workflows
such as `cross-platform-check.yml` and `nightly-full-build.yml` so the profile
maps to an existing job where possible instead of duplicating nightly coverage.

## Rollout

1. Commit this plan on tartci `main`.
2. Add commented example profiles under `profiles/`.
3. Add tartci read-only profile parser and `list/show/explain/plan`.
4. Add `tartci status --json` with host-local provider/capacity/cache/timing
   facts.
5. Teach Shipyard to read tartci profile/status output and produce fleet-level
   plans.
6. Add Pulp profile mapping docs and a checked-in profile for
   `normal-local-fast`.
7. Add or confirm nightly GitHub-hosted Intel Linux/Windows scheduled validation.
8. Add issue filing only after dedupe behavior is implemented and tested.
9. Add `apply` only after plans are stable and reviewable.

## Acceptance Criteria

- A human can open one profile file and understand PR/release/coverage/scheduled
  routing without external docs.
- `tartci profile explain ... --json` returns structured data that a Claude
  plugin, Codex agent, or Shipyard can parse.
- `tartci profile plan ...` is read-only and shows exact variable changes.
- Shipyard can answer fleet-level routing with host fallback order.
- Pulp can keep fast local ARM64 PR lanes while running nightly GitHub Intel
  Linux/Windows validation.
- Coverage profiles refuse or warn on warm non-ephemeral runner labels.
- Local x64 emulation is clearly labeled smoke/debug until proven otherwise.
