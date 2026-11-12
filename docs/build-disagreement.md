# Cross-host build disagreement alarm

`tartci doctor build-disagreement` flags a gate host that fails to compile or
link code that another gate host builds green. That pattern is the visible
symptom of a poisoned shared compiler cache: the cache serves a wrong object
for a translation unit, and every build on that host fails the same way while
the source is fine.

It is read-only against GitHub, never changes scheduling, and is **off by
default**. It is one layer of several against shared-ccache poisoning; it
detects, it does not prevent or repair.

## Why it exists

On 2026-09-26 from about 08:23Z, every Pulp `macos` gate job on M3
(`studio-*` runners) failed to link `test/pulp-osc-render-wav` with an
undefined `write_scenario_wav`, because poisoned zero-include ccache manifests
served `wav_bridge.cpp.o` as `audio_doctor.cpp.o`. M5 built the same main green
throughout. It took about 17 hours to attribute, because each red, read on its
own, looked like "that PR is broken". Only the comparison across hosts says
"that host is broken".

## Rules

Inputs are completed jobs named `macos` in `Generous-Corp/pulp`'s
`build.yml`, listed through `actions/workflows/build.yml/runs` (the
`workflow_id` query parameter on `actions/runs` is ignored by GitHub) and
`actions/runs/<id>/jobs?filter=all`, so re-run attempts count. Runner names map
to hosts: `studio-*` and `m3-*` are M3, `m5-*` is M5, `m1-*` is M1.
GitHub-hosted jobs are out of scope. A job's outcome is its `Build` step: a
job whose Build step succeeded and whose tests failed still counts as a green
build.

- **same_build** — the same built identity has a green Build on host A and a
  Build-step failure on host B, B never built that identity green, and B's log
  carries a compile/link signature. The identity is the head SHA (also matched
  by tree) for events that build their head commit (`push`, `merge_group`,
  `workflow_dispatch`), and the (head, base) pair for `pull_request`, whose
  built commit is the synthetic merge ref.
- **streak** — host B's last K (default 3) Build outcomes are all failures
  with the same error fingerprint across at least two distinct identities,
  while another host completed a green Build during the streak.

Compile/link signatures: `Undefined symbols for architecture`, `undefined
reference to`, `ld: symbol(s) not found`, `ld: <error>` (never `ld: warning`,
which every green build prints), `linker command failed`, and
`<file>.<c|cpp|mm|...>:<line>: error:`. A log that also shows a killed
compiler, a full disk or OOM is a resource failure and does not count. The
fingerprint is the set of undefined symbols or error lines with paths and line
numbers removed.

A finding names the host, commit, failing job and its URL, the green
counterpart job, the error fingerprint and the remedy. A log that cannot be
read yields `unknown` (`disagreement_log_unreadable`), never `problem`.

## Detection floor

- An identity built on only one host cannot be compared. Merge-queue commits
  are usually built once, and pushes to main reuse the merge-queue receipt, so
  **same_build** mostly sees re-runs and repeated PR builds. **streak** is the
  rule that catches a host that fails everything.
- A streak shorter than K stays silent.
- If only one host is serving the gate, a poisoned host and a broken main are
  indistinguishable. The check says so rather than guessing.
- Log reads are capped (`max_log_fetches`, default 12) and API calls are capped
  (`max_api_calls`, default 200). A capped read reports `unknown`.

## Enabling

Per invocation:

```sh
tartci doctor build-disagreement --enable            # text
tartci doctor build-disagreement --enable --json     # exit 1 on a problem finding
```

Per host, in the fleet profile (no checked-in profile enables it). A macOS
fleet profile carries the table only once `scripts/macos_fleet_lanes.py` lists
`build_disagreement` among its accepted top-level keys; the installer rejects
unknown tables:

```toml
[build_disagreement]
enabled = true
# Optional tuning; these are the defaults.
repo = "Generous-Corp/pulp"
workflow = "build.yml"
hours = 6
streak = 3
max_api_calls = 200
max_log_fetches = 12
```

Then `tartci doctor build-disagreement --profile <name>`.

GitHub reads go through `TARTCI_GH_CLI` (default `gh`; `ghapp` works, and must
run from inside a checkout of the repo). Job logs need a CLI that accepts
`--allow-escape-sequences`, since current `gh` refuses to print the colour
codes Actions logs carry; set `TARTCI_GH_LOG_CLI` (or `--log-cli`) to a plain
`gh` binary if the main CLI is a wrapper that rejects that flag.

`--from-jobs <file> --logs-dir <dir> --now <time>` replays a recorded job list
offline; `--dump-jobs <file>` records one.

## Periodic watch

The launchd watchdog (`com.danielraffel.tartci.launchd-watchdog`, `tartci
launchd heal`, `StartInterval` 300 s) runs the check on a host whose installed
fleet profile (`~/.config/tartci/macos-fleet-profile.toml`) has
`[build_disagreement] enabled = true`. No new daemon: it runs after the heal
pass through `scripts/build_disagreement_watch.py` and writes to the watchdog
log (`~/Library/Logs/tartci/tartci-launchd-watchdog.log`). It is report only:
it never resets a cache and never changes scheduling.

- **Cadence:** at most once every `interval_minutes` (default and floor 15).
  `tartci launchd status` and `heal --dry-run` never run it.
- **Budgets:** `max_api_calls` and `max_log_fetches` are passed through, capped
  at the detector defaults (200 and 12); the run is killed after
  `timeout_seconds` (default 240) and reported `unknown`.
- **Outcome:** exit 1 (a problem) and 3 (GitHub unreadable) are findings, not
  watchdog failures. A timeout, a crash or unparseable output is `unknown`.
- **Dedup:** one `ALARM` per (flagged host, error fingerprint). A pair still
  being reported is not re-sent; it alarms again only after it has been absent
  for `realert_hours` (default 24). State lives in
  `$TARTCI_HOME/state/build-disagreement-watch.json`.
- **Log CLI:** `TARTCI_GH_LOG_CLI` when it answers `--version` as gh, otherwise
  `/opt/homebrew/bin/gh`, `/usr/local/bin/gh` and each `gh` on PATH, skipping
  wrappers that do not answer as gh. None found is `unknown
  (log_cli_unavailable)` and the detector is not run.

Every due cycle logs one `build-disagreement: ran state=...` line (the count of
runs), plus `ALARM` and `remedy:` lines for a new pair. One host is enough: the
detector reads every gate host's jobs from GitHub, so the canary is the host
that already runs fleet-wide views (M3, `studio`).

`python3 scripts/build_disagreement_watch.py status` prints the last cycle;
`cycle --force --replay-jobs <file> --replay-logs <dir> --replay-now <time>`
replays a recorded window through the same path.

## Remedy

On the flagged host run `tartci ccache reset`: it quarantines zero-include
manifests, `--reset` also moves the whole shared cache aside, `--plan` shows
what it would do, and it refuses while a VM runs or holds a lease unless
`--force`. On a host whose installed tartci predates that command, drain its
gate lanes, move its shared ccache directory aside and resume. Then re-run the failing job named in the finding and confirm
it goes green on that host.
