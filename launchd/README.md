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

It is safe-by-construction rather than denylist-only. VM deletion requires both
an allowed CI prefix (`pulp-vm-`/`tartci-` by default) and a tartci state-file
ownership marker. Goldens, `pulp-vm`, `rosetta-probe`, and bench names remain
protected. Offline GitHub runner registrations are removed only when they match
an owned CI prefix and no fresh live supervisor heartbeat backs them. Run it
report-only first:

```sh
TART_HOME="$HOME/VMs" "$HOME/.local/bin/tartci" doctor --reap --json
```

Then install the LaunchAgent once the report is clean. Logs land in
`~/Library/Logs/tartci/tartci-reap.log`.

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
