# launchd templates — serve the GitHub Actions pool at boot

These are LaunchAgent templates that run the per-job ephemeral runner supervisors
(`providers/tart-linux/runner.sh`, `providers/qemu-windows/runner.sh`, and the
macOS provider as it graduates from the Pulp script) under
`launchd` so a host serves the pool across reboots. They are the persistent
counterpart of `tartci serve <os> --loop`.

## Generic toolkit vs. Pulp's concrete instance

The **runner scripts are project-agnostic** — repo, golden, labels, and the
"is there queued work?" workflow name are all env-driven
(`TARTCI_RUNNER_REPO`, `TARTCI_LINUX_GOLDEN` / `TARTCI_MACOS_GOLDEN` /
`TARTCI_WIN_GOLDEN`, `TARTCI_RUNNER_LABELS`, `TARTCI_RUNNER_WORKFLOW_NAME`).

Runtime measurement is also env-driven and optional. Add these only to hosts
where you want local timing history for agents or Shipyard import:

```text
TARTCI_RUNTIME_MEASURE=1
TARTCI_RUNTIME_STORE=$HOME/.tartci/runtime
TARTCI_RUNTIME_GH_ENRICH=1
TARTCI_RUNTIME_TAGS=macstudio
```

With the switch unset, the supervisors create no runtime store and serving
behavior is unchanged.

The **templates here are Pulp's concrete instance** — the first consumer.
Their `Label`s are `com.danielraffel.pulp.tart-runner`,
`com.danielraffel.pulp.tart-runner-macos-release`,
`com.danielraffel.pulp.tart-runner-linux`, and
`com.danielraffel.pulp.qemu-runner-windows` because the
[shipyard-macos-gui](https://github.com/danielraffel/shipyard-macos-gui) "Serve
CI builds from this Mac" switch hard-codes those labels (its
`CIServingLane.known`) to `launchctl load/unload` them. Keep the known labels
exactly as-is when serving Pulp; add new labels to Shipyard before expecting its
GUI to toggle them.

## macOS launchd rule

The macOS lane must not point launchd at a `/Volumes` checkout or VM store. On
2026-06-09 the Studio proof showed the existing Pulp plist crash-looping with
exit 126 because launchd could not `getcwd` under `/Volumes/Workshop/Code/pulp`
or read `/Volumes/Workshop/Code/pulp/tools/ci/tart-runner.sh`. The green proof
used `WorkingDirectory=$HOME`, a wrapper under `$HOME/.local/bin`, and
`TART_HOME=$HOME/VMs`; launchd booted a macOS clone and exited 0.

Use `com.danielraffel.pulp.tart-runner-macos.plist.template` as the replacement
shape: install tartci into `$HOME/.local/share/tartci`, expose a small
`$HOME/.local/bin/tartci` wrapper, and keep macOS goldens under `$HOME/VMs`.
Only use `/Volumes` for macOS launchd after introducing a signed Full Disk
Access helper.
Shipyard fleet probes should point `host_class.<name>.tartci_bin` at that same
wrapper and `host_class.<name>.tart_home` at the same `$HOME/VMs` store; otherwise
capacity and supervisor health will be read from different Tart homes.

If a host already has a required-lane Pulp LaunchAgent using
`com.danielraffel.pulp.tart-runner`, do not overwrite it during pilot. Install a
side-by-side pilot plist with a distinct `Label`, log path, and non-required
runner labels, then load it only after `shipyard runner capacity` shows a free
slot. Graduate labels later, after the required lane drains and rollback is
ready.

When more than one Mac serves the same pool selector, keep the workflow selector
shared but make each runner name unique. The macOS runner derives its default
name from the last `pulp-build-*` label, so a host may add an extra host-specific
label after the shared pool label. A job requiring
`self-hosted,macOS,ARM64,pulp-build,pulp-build-vm` still matches a runner that
advertises that full set plus one extra label.

### Reload rule: always bootout+bootstrap, never kickstart alone

launchd caches a job's spec in memory. `KeepAlive` respawn and
`launchctl kickstart -k` both re-run the CACHED spec — **neither re-reads the
plist from disk.** So if you edit a plist (a routing / label change) or move the
tartci tree (a reinstall) and then only `kickstart`, launchd keeps running the
STALE spec. When that stale spec resolves to a now-unreadable path (the
`/Volumes` no-Full-Disk-Access case above, or a moved `~/.local` generation),
every respawn exits 126 *before the script runs* — nothing is logged, the log
freezes, and `runs=` climbs into the thousands while `KeepAlive` respawns it
forever. On 2026-07-06 this had silently taken the required macOS gate offline
for ~2 weeks. Only `bootout` (drop the cached spec) + `bootstrap` (re-read the
plist) heals it.

Use the wrapper instead of raw `launchctl` so you can never get this wrong:

```
tartci launchd reload com.danielraffel.pulp.tart-runner   # bootout+bootstrap+kickstart
tartci launchd status                                     # health of every tartci agent
```

## LaunchAgent self-heal watchdog

`com.danielraffel.tartci.launchd-watchdog.plist.template` runs
`tartci launchd heal` on a `StartInterval` (default 300s). Because the
exit-126 wedge above logs nothing (the script never runs), no in-agent logging
can catch it — recovery must live outside the wedged agent. The watchdog
(`scripts/tartci_launchd_watchdog.py`) discovers every tartci LaunchAgent, and
for each reads `launchctl print` (`last exit code`, `state`) plus the log mtime.
It heals an agent only when it **exited non-zero AND its log has gone stale**
(the two together distinguish the invisible crash-loop from a healthy
between-jobs idle, whose "waiting" log is always fresh). Healing is the same full
bootout+bootstrap+kickstart, rate-limited (default: max 3 heals per label per
hour) so a genuinely broken plist logs loudly to
`~/Library/Logs/tartci/tartci-launchd-watchdog.log` instead of thrashing. It
never heals itself. Decision logic is covered hermetically by
`scripts/test_tartci_launchd_watchdog.py` (no launchd needed). Install:

```
mkdir -p "$HOME/Library/Logs/tartci"
sed -e "s|\$HOME|$HOME|g" \
  launchd/com.danielraffel.tartci.launchd-watchdog.plist.template \
  > "$HOME/Library/LaunchAgents/com.danielraffel.tartci.launchd-watchdog.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.danielraffel.tartci.launchd-watchdog.plist"
launchctl kickstart -k "gui/$(id -u)/com.danielraffel.tartci.launchd-watchdog"
```

## GitHub-hosted queue-saturation detector

`com.danielraffel.pulp.queue-saturation.plist.template` runs
`scripts/gh_queue_saturation.py` on a `StartInterval` (default 300s) to catch the
inverse of a wedge: the required self-hosted gate sits **online and idle** while
its GitHub-hosted routing preamble is starved behind a saturated shared pool, so
the required check reads `pending` for reasons that have nothing to do with the
code or the runners. A runner-health check sees green runners and reports "fine";
this detector sees the triad — deep repo-wide queue **and** an idle required-gate
runner **and** a required check pending past a grace window — and says
"GitHub-hosted starvation." It runs here, on the always-on Mac, precisely because
a scheduled workflow on `ubuntu-latest` would queue behind the saturation it is
meant to report. Dry-run by default (`PULP_SAT_APPLY=0`, logs the verdict); set
`PULP_SAT_APPLY=1` to open/update a single tracking issue once the log has baked.
Decision logic is covered hermetically by `scripts/test_gh_queue_saturation.py`
(no network, no `gh`, no clock). Design:
`planning/2026-07-06-ci-queue-saturation-watchdog.md` in the pulp repo. Install:

```
mkdir -p "$HOME/Library/Logs"
sed -e "s|\$HOME|$HOME|g" \
  launchd/com.danielraffel.pulp.queue-saturation.plist.template \
  > "$HOME/Library/LaunchAgents/com.danielraffel.pulp.queue-saturation.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.danielraffel.pulp.queue-saturation.plist"
launchctl kickstart -k "gui/$(id -u)/com.danielraffel.pulp.queue-saturation"
```

## Release CLI macOS launchd rule

`Release CLI` is a different workload from `Build and Test`, so serve it with a
different Tart VM label and LaunchAgent. Use
`com.danielraffel.pulp.tart-runner-macos-release.plist.template`, which filters
on `TARTCI_RUNNER_WORKFLOW_NAME=Release CLI` and advertises the shared release
pool label:

```text
self-hosted,macOS,ARM64,pulp-build-vm-release
```

Keep `PULP_RELEASE_MACOS_RUNS_ON_JSON` on the existing fallback lane until a
real Release CLI proof claims `pulp-build-vm-release` and completes. After that,
the intended selector is:

```json
["self-hosted","macOS","ARM64","pulp-build-vm-release"]
```

The release lane can stay loaded before the variable is flipped; it will idle
because queued Release CLI jobs do not request `pulp-build-vm-release` yet.

## Windows QEMU launchd rule

The Windows lane uses QEMU directly, so every participating Apple Silicon host
needs Homebrew QEMU on the service PATH, the same Windows qcow2 golden in a
local golden store, and the tartci scripts installed under a home-backed path.
Use the qemu template's install recipe, which points launchd at
`$HOME/.local/share/tartci` rather than a mounted workspace.

Leave `TARTCI_RUNNER_QUEUE_MATCH_LABELS=1` unless you are debugging the queue
poller. With that default, the supervisor only boots QEMU when a fresh queued
job's requested labels can be satisfied by the configured runner labels, for example
`self-hosted,Windows,ARM64,pulp-build-windows`. That makes it safe to keep the
LaunchAgent loaded while a repo still defaults ordinary Windows jobs to
GitHub-hosted `windows-latest`.

Per-job diagnostics are separate from the disposable overlay. The template
writes them under `TARTCI_WIN_LOGS`. `preflight.log` records `vcvarsall`
discovery and `cl.exe` visibility, and `runner-output.log` records the same
MSVC environment import immediately before the Actions agent starts. Override
the default `arm64` `vcvarsall` target with `TARTCI_WIN_VCVARS_ARCH` when a repo
needs a different Visual Studio environment:

```sh
tartci timings "$HOME/VMs/logs/tartci-win"
tail -F "$HOME/Library/Logs/tartci/qemu-runner-windows.log"
```

Use a Windows-native workflow for proof runs before setting a repo-level
Windows `runs-on` variable. A Unix shell smoke can prove assignment, but it will
fail on Windows if the step assumes tools like `chmod`.

## Janitor

`com.danielraffel.tartci.reap.plist.template` runs the Phase-4 Tier-2 janitor:

```sh
tartci doctor --reap --json --fix
```

It is safe-by-construction rather than denylist-only. VM or overlay deletion
requires both an allowed CI prefix (`pulp-`, `linux-ephr-`, `win-ephr-`, and
`tartci-` by default) and a tartci state-file ownership marker. Goldens,
`pulp-vm`, `rosetta-probe`, and bench names remain protected. Offline GitHub
runner registrations are removed only when they match an owned CI prefix and no
fresh live supervisor heartbeat backs them. Windows `KEEP_FAILED=1` inspection
VMs are held for the configured keep-failed window before they become reap
candidates. Run it report-only first:

```sh
TART_HOME="$HOME/VMs" "$HOME/.local/bin/tartci" doctor --reap --json
```

Then install the LaunchAgent once the report is clean. Logs land in
`~/Library/Logs/tartci/tartci-reap.log`.

## Shipyard queue janitor

`com.danielraffel.shipyard.queue-tick.plist.template` runs
`scripts/shipyard_queue_tick.sh` every 5 min to make the Shipyard ship-queue
progress **independent of any interactive session** — so a cmux restart or a
Claude session running out of quota can no longer strand a validated PR or leak
ship-state. Per active ship-state whose worker is not live, it: reaps records
whose PR GitHub reports merged/closed (`shipyard ship-state discard`), drives
open green PRs to merge via shipyard's own fail-closed `auto-merge` (no-op
unless all targets green and the live head matches the validated SHA), and
surfaces (does not auto-rebase) behind/DIRTY PRs.

Safe-by-construction: acts only on PRs that already have a ship-state record,
never reimplements merge logic, never edits state files, fails closed on any
GitHub read error, and skips live/fresh workers. It defaults to **DRY-RUN**
(`SHIPYARD_TICK_APPLY=0`) — deploy observe-only first, watch
`~/Library/Logs/shipyard-queue-tick.log`, then flip `SHIPYARD_TICK_APPLY=1`.
Full-live additionally requires `SHIPYARD_QUEUE_AUTHORITY=1`; set that on
exactly one host whose Shipyard runner tag matches
`[merge_queue].mutation_machine`. Other CI Macs may remain dry-run or reap-only
but cannot become queue writers. Set `SHIPYARD_QUEUE_REPO_ROOT` to the
authority's repository checkout; the tick runs Shipyard from that directory
and requires `authority_matches=true` before full-live operation. An
authority-local `shipyard merge-queue hold` causes the configured authority
tick to exit before any GitHub read; during an incident, run it on that
authority (and propagate it fleet-wide for consistent operator status). This
integration requires Shipyard 0.79.0 or newer; install that release before
deploying the script or plist. Re-bootstrap after changing the installed
plist. See the template's header comment for the exact `sed` install recipe.
Design + adversarial review: pulp
`planning/2026-06-30-ship-queue-resilience-design.md`.

## Serving a different repo

1. Copy a template to `com.<you>.<repo>.<provider>.plist.template`.
2. Change the `<Label>` and the `--labels` argument to your repo's runner labels.
3. Point `TARTCI_RUNNER_REPO` (and golden/labels) at your repo via the plist's
   `EnvironmentVariables` or the `runner.sh` env defaults.
4. If you drive it from the Shipyard macOS GUI, add a matching row to that app's
   `CIServingLane.known` with your new label.

## Install (Pulp)

See the header comment in each `.plist.template` for the exact `sed` install
recipe (launchd does **not** expand `$HOME`/`$TARTCI_REPO` — the install `sed`
must write absolute paths). Logs land in `~/Library/Logs/tartci/`.
