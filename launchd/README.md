# launchd templates — serve the GitHub Actions pool at boot

These are LaunchAgent templates that run the per-job ephemeral runner supervisors
(`providers/tart-linux/runner.sh`, `providers/qemu-windows/runner.sh`, and the
macOS provider as it graduates from the Pulp script) under
`launchd` so a host serves the pool across reboots. They are the persistent
counterpart of `tartci serve <os> --loop`.

## Generic toolkit vs. Pulp's concrete instance

The **runner scripts are project-agnostic** — repo, golden, labels, and the
"is there queued work?" workflow name or names are all env-driven
(`TARTCI_RUNNER_REPO`, `TARTCI_LINUX_GOLDEN` / `TARTCI_MACOS_GOLDEN` /
`TARTCI_WIN_GOLDEN`, `TARTCI_RUNNER_LABELS`,
`TARTCI_RUNNER_WORKFLOW_NAME`, `TARTCI_RUNNER_WORKFLOW_NAMES`).

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
Their `Label`s are `com.danielraffel.pulp.tart-runner-macos-gate`,
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
Only use `/Volumes` for macOS launchd through the opt-in, receipt-bound native
launcher described below. Do not grant broad access to Bash, Node, Python, or
`env`: those interpreter identities are shared by unrelated tools and produce
repeated privacy prompts.
Shipyard fleet probes should point `host_class.<name>.tartci_bin` at that same
wrapper and `host_class.<name>.tart_home` at the same `$HOME/VMs` store; otherwise
capacity and supervisor health will be read from different Tart homes.

### External-volume responsible process (Daniel's M3 profile only)

`profiles/m3-macos-fleet.toml` deliberately keeps its 4 TiB Tart store at
`/Volumes/Workshop/VMs`. Its managed LaunchAgents therefore start the stable
Developer-ID-signed `TartCILauncher.app` at
`~/.local/libexec/TartCILauncher.app`. Its signature seals the exact TartCI
support cohort and five rendered M3 lane environments. The resident launcher
accepts only `--lane <sealed-enum>` or the fixed `--probe-store`, spawns no
caller-selected executable or arguments, owns the child process group, and
bounds TERM-to-KILL cleanup inside launchd's 30-second exit window. It contains
no scheduler, queue, GitHub, listener, or capacity policy.

Build/sign the artifact on a controlled signing surface, never during fleet
installation:

```sh
scripts/build_macos_launcher.sh \
  --output /absolute/staging/TartCILauncher.app \
  --approval-output /absolute/staging/TartCILauncher.sha256 \
  --identity '<Developer ID Application identity>' \
  --support-root /absolute/immutable/tartci-generation \
  --profile profiles/m3-macos-fleet.toml
scripts/macos_launcher_identity.py verify /absolute/staging/TartCILauncher.app \
  --identifier com.danielraffel.tartci.launcher \
  --team-id 95CX6P84C4 --sha256 <profile-pinned-sha256>
```

The M3 profile pins path, identifier, Team ID, exact profile policy, and the
path to an owned mode-0600 approval digest produced by the signing build.
The bundle binds the same exact TartCI source commit as the installed support
cohort. The installer accepts it with `--launch-helper-source`, atomically
publishes it, and binds path, owner, mode, realized bundle digest, designated
requirement, and hardened-runtime status into the fleet receipt. Unsigned, ad-hoc, Apple
Development, wrong-Team, wrong-identifier, symlinked, or changed binaries fail
closed. M1, M5, and public home-backed profiles do not declare this helper and
retain the ordinary launch path.

`tartci pool on` runs a one-shot LaunchAgent probe through this same identity
before loading any fleet supervisor or opening participation. The probe must
write, read back, and delete a temporary file in the declared Tart store. Its
first M3 run may require one explicit Removable Volumes consent; TartCI never
edits TCC databases or invokes `tccutil`. Denial, timeout, signature drift, or
digest drift leaves the pool off. `$HOME/VMs` is the safe rollback while the
external-volume identity is unavailable.

The bare `com.danielraffel.pulp.tart-runner` label is retired. Never load it
beside the replacement: both can resolve to the same runner name and state
file. Run `scripts/migrate_macos_gate_agent.sh` to inspect the exact plan, then
re-run with `--apply --attest-external-gui-label-updated` only after the
external `shipyard-macos-gui` deployment knows the replacement label. The
helper bootouts/removes only that
legacy label and installs the
guarded `com.danielraffel.pulp.tart-runner-macos-gate` replacement. Its
pre-start uniqueness check refuses to serve if any other loaded Tart macOS
agent resolves to the same runner name or state file.

### Managed second macOS gate slot

Hosts whose governed CPU and memory budgets admit two 6-core, 8-GiB guests may
install one additional Pulp gate supervisor with the canonical
`com.danielraffel.pulp.tart-runner-macos-gate-slot2` label. Render and validate
the profile without changing launchd:

```sh
tartci gate-slot2 render --home "$HOME" --tart-home "$TART_HOME" --output /tmp/gate-slot2.plist
tartci gate-slot2 validate /tmp/gate-slot2.plist \
  --sibling "$HOME/Library/LaunchAgents/com.danielraffel.pulp.tart-runner-macos-gate.plist"
tartci gate-slot2 install
```

The install command is dry-run by default. At a terminal pool-drain/off
boundary, `tartci gate-slot2 install --apply` atomically installs the plist but
does not load it. A later `tartci pool on` loads both managed gate supervisors.
The slot has its own launchd label, runner-name prefix, state directory, queue
lane ID, event/job logs, and `--slot 2`, while intentionally sharing the host's
`TART_HOME` and artifact cache. Its event-class-v2 profile serves merge groups
before PR heads and never advertises the legacy `pulp-gate-fast` selector.

The `com.danielraffel.pulp.tart-runner-*` name makes the slot visible to
`tartci pool status/on/off`, the launchd watchdog, and Shipyard's dynamically
discovered CI-serving lanes. Do not hand-launch a duplicate provider process or
copy an older generic slot plist; the renderer and sibling validator are the
ownership boundary.

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
tartci launchd reload com.danielraffel.pulp.tart-runner-macos-gate
tartci launchd status                                     # health of every tartci agent
```

The wrapper treats `bootout` as asynchronous: before mutation it reads the
loaded job's effective `exit timeout` from `launchctl print`, adds a five-second
termination margin, and waits for that job to become absent before
bootstrapping the replacement plist.
Bootstrapping immediately after `bootout` can otherwise fail while teardown is
still in progress and leave the host with no runner. A teardown that exceeds
its own declared allowance fails closed without attempting bootstrap. An
A loaded job with an infinite or unknown timeout is refused before `bootout`.
An already-absent job has no teardown risk and is bootstrapped directly.

### Host and Tart-guest HTTP relay routing

Do not give a host-side controller Tart's bridge address. On a host whose
direct GitHub TLS path is measurably unreliable, render that host's controller
with an explicit loopback HTTP CONNECT proxy and give only disposable guests
the bridge address:

The durable source is the opt-in host file
`~/.config/tartci/network-profile.toml` (copy the commented shape from
`docs/examples/host-network-profile.toml`). `tartci network-profile status`
shows its intended relay and controller drift;
`tartci network-profile reconcile` renders the existing restricted relay
template, proves an authenticated GitHub request through loopback, injects the
fixed host/guest addresses into every installed macOS controller, and applies
changed plists with a full bootout/bootstrap. It refuses controller reloads
while any Tart VM is running and records the exact successfully loaded plist
generation so an interrupted reload is retried. An absent profile is a no-op
until it has owned relay state, so this does not impose a fleet-wide proxy.
Both uppercase and lowercase proxy variables are owned, and `NO_PROXY` plus
`no_proxy` are constrained to loopback; stale controller environment cannot
bypass the measured relay path. The host address remains
`http://127.0.0.1:49125`, while only guests receive
`http://192.168.64.1:49125`.
After first apply, removing or disabling the profile fails pool admission rather
than silently leaving unprobed proxy state. Supported rollback is explicit and
idle: `tartci pool off`, remove/disable the profile, then run
`tartci network-profile rollback`. Rollback refuses running VMs or loaded
controllers, restores pre-profile environment/relay state, and removes the
ownership receipt only after convergence.

`tartci pool on` runs that reconciliation before reopening admission. The
launchd watchdog does the same on every heal pass, covering reboot and offline
rejoin. If the relay or authenticated probe is unhealthy, both paths fail
closed instead of loading a scan-blind controller. Pool admission preserves the
network-profile tool's exact refusal cause; it does not replace an unavailable
Tart inventory with the false claim that a VM is running. Tart inventory is
tri-state (`running`, `idle`, or `unavailable`) and resolves
`TARTCI_TART_CLI`, PATH, then the canonical Homebrew locations. It preserves an
explicit `TART_HOME` or derives `[host].tart_home` from the installed fleet
profile; it never treats Tart's unrelated default store as authoritative. The
watchdog still refuses alive-but-frozen recovery when inventory is unavailable,
so this diagnostic distinction does not weaken the long-build guard. Do not put relay hostnames,
GitHub tokens, or proxy variables in shell startup files; the profile records
only non-secret per-host intent, and `ghapp` supplies short-lived App auth.
While pool participation is off, reconciliation loads and proves only the
relay, writes controller intent to disk, and records it as staged; it never
starts a disabled controller. `pool on` then bootstraps those exact files.

```sh
python3 scripts/render_launchd_template.py \
  launchd/com.danielraffel.pulp.tart-runner-macos-release.plist.template \
  --set "TART_HOME=$TART_HOME" --set "HOME=$HOME" \
  --environment "HTTP_PROXY=http://127.0.0.1:49125" \
  --environment "HTTPS_PROXY=http://127.0.0.1:49125" \
  --environment "TARTCI_GUEST_HTTP_PROXY=http://192.168.64.1:49125" \
  > "$HOME/Library/LaunchAgents/com.danielraffel.pulp.tart-runner-macos-release.plist"
```

`providers/tart-macos/runner.sh` writes `TARTCI_GUEST_HTTP_PROXY` into the
ephemeral Actions runner's `.env`; it never copies the host loopback address.
Reload the controller with `bootout` plus `bootstrap`, then confirm its live
`launchctl print` environment. `kickstart` retains the old environment.

The Pulp release supervisor yields its shared Apple VM slot whenever required
`Build and Test` PR-head or merge-group work is queued or running. Its
unassigned JIT timeout is intentionally 60 seconds, bounding the case where a
release job disappears after boot but before assignment. A tagged release that
has already claimed the runner remains non-preemptive.

The optional `com.danielraffel.network.http-connect-ssh-relay` agent runs a
restricted CONNECT listener for loopback and Tart's bridge subnet. Each allowed
client CIDR is paired with the exact local destination address, so a matching
physical LAN cannot enter through the host's LAN interface. Render both
relay hosts so loss of one Mac fails over before accepting a CONNECT request:

```sh
python3 scripts/render_launchd_template.py \
  launchd/com.danielraffel.tartci.http-connect-ssh-relay.plist.template \
  --set "HOME=$HOME" \
  --set "TARTCI_HTTP_RELAY_PRIMARY=macmini" \
  --set "TARTCI_HTTP_RELAY_SECONDARY=m1" \
  > "$HOME/Library/LaunchAgents/com.danielraffel.network.http-connect-ssh-relay.plist"
```

Its non-tartci label intentionally keeps this silent network service outside
the runner stale-log watchdog. The relay opens the requested public endpoint
through SSH and waits for a positive ready marker before acknowledging CONNECT,
then uses a fresh SSH transport per request. Do not add a persistent
ControlMaster: a live but wedged multiplex socket can accept local connections
while preventing every controller and guest from completing TLS. Before
deployment, require repeated bounded `curl` and App-authenticated GitHub calls
through `127.0.0.1:49125`; test `192.168.64.1:49125` from inside a disposable
guest. Keep this opt-in and measured per host rather than exporting proxy
variables globally or applying it fleet-wide.

## LaunchAgent self-heal watchdog

`com.danielraffel.tartci.launchd-watchdog.plist.template` runs
`tartci launchd heal` on a `StartInterval` (default 300s). Because the
exit-126 wedge above logs nothing (the script never runs), no in-agent logging
can catch it — recovery must live outside the wedged agent. The watchdog
(`scripts/tartci_launchd_watchdog.py`) discovers every tartci LaunchAgent, and
for each reads `launchctl print` (`last exit code`, `state`) plus the log mtime.
It heals crash-looping agents when they **exited non-zero AND their log has gone
stale** (the two together distinguish the invisible crash-loop from a healthy
between-jobs idle, whose "waiting" log is always fresh). Healing is the same full
bootout+bootstrap+kickstart, rate-limited (default: max 3 heals per label per
hour) so a genuinely broken plist logs loudly to
`~/Library/Logs/tartci/tartci-launchd-watchdog.log` instead of thrashing. It
never heals itself. Decision logic is covered hermetically by
`scripts/test_tartci_launchd_watchdog.py` (no launchd needed). Install:

The same pass reconciles durable pool intent. When
`~/.config/tartci/native-build-participation` is absent or `1`, every discovered Pulp or
Forge `tart-runner` / `qemu-runner` plist is expected to be loaded; an absent
job is bootstrapped through the normal rate-limited heal path. When the flag is
`false`, unloaded runners remain intentionally offline and are never
resurrected. This closes the gap where a host retained an ON flag while its
runner jobs had disappeared from launchd.

The watchdog also recognizes the supervisor's explicit GitHub-auth refresh
contract. A pool-enabled `serve --loop` process exits `EX_TEMPFAIL` (75) after
sustained queue-scan blindness and expects launchd to respawn it. If the loaded
job remains `not running` or `spawn scheduled` beyond the bounded restart grace
(default 60 seconds), the watchdog reloads it on its next five-minute pass
instead of waiting for the generic 30-minute stale-log threshold. Participation
OFF remains authoritative, and other nonzero exit classes retain the conservative
stale-log rule.

Per-lane launchd enablement is also authoritative. A host may participate while
legacy, release, sanitizer, Linux, or Windows plists remain explicitly disabled.
The watchdog reads `launchctl print-disabled` once per pass and never reloads an
exact disabled label. If that enablement map cannot be read, automatic recovery
fails closed for pool-controlled lanes instead of guessing that every discovered
plist should be active.

```
mkdir -p "$HOME/Library/Logs/tartci"
sed -e "s|\$HOME|$HOME|g" \
  launchd/com.danielraffel.tartci.launchd-watchdog.plist.template \
  > "$HOME/Library/LaunchAgents/com.danielraffel.tartci.launchd-watchdog.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.danielraffel.tartci.launchd-watchdog.plist"
launchctl kickstart -k "gui/$(id -u)/com.danielraffel.tartci.launchd-watchdog"
```

### Persistent Actions runner install missing

The watchdog also audits `actions.runner.*` LaunchAgents. If a plist survives
but its absolute `Program` or first `ProgramArguments` executable is gone,
status is `broken`, not `wedged`. A full reload cannot recreate a deleted runner
tree, so the watchdog deliberately does not thrash `launchctl`.

Recover only after GitHub proves the exact runner is absent or `offline` and
not busy. Reinstall the reviewed Actions runner archive at the exact directory
reported by the watchdog, verify the release SHA-256, and re-register the exact
name and role labels with `config.sh --unattended --replace --disableupdate`.
Then use the runner's supported service lifecycle rather than editing launchd
state by hand:

```sh
cd /absolute/runner/directory/from-the-watchdog
./svc.sh uninstall 2>/dev/null || true
./svc.sh install
./svc.sh start
./svc.sh status
```

Require all three postconditions: `runsvc.sh` exists, `launchctl print
gui/$(id -u)/<exact-label>` is running, and the GitHub runners API reports the
exact name online with the intended role label (for example `pulp-preamble`).
Use the GitHub App wrapper for API and registration-token calls so a stripped
SSH shell or personal-token quota cannot create a false diagnosis. If GitHub
reports busy or returns unknown state, stop; do not replace the registration.

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
`com.danielraffel.pulp.tart-runner-macos-release.plist.template`, which uses
ordered `TARTCI_RUNNER_WORKFLOW_TIERS` entries for `Release CLI`, `Sign and
Release`, and `Release-path PR gate`. Every runner advertises the shared pool:

```text
self-hosted,macOS,ARM64,pulp-build-vm-release
```

Keep `PULP_RELEASE_MACOS_RUNS_ON_JSON` on the existing fallback lane until a
real Release CLI proof claims `pulp-build-vm-release` and completes. After that,
the intended selector is:

```json
["self-hosted","macOS","ARM64","pulp-build-vm-release","pulp-release-tagged"]
```

Route the PR-time gate through its separate selector:

```json
["self-hosted","macOS","ARM64","pulp-build-vm-release","pulp-release-pr-gate"]
```

Do not switch either workflow selector until the tier-capable supervisor is
deployed on every host; otherwise newly queued jobs request labels no runner
advertises.

Each tier line is `class-label|exact workflow display name`. First-seen class
labels define priority; workflows sharing the same label form one
GitHub FIFO class. The Pulp template assigns tagged workflows
`pulp-release-tagged` and the PR gate `pulp-release-pr-gate`. The JIT runner
advertises only the selected class, preventing an older lower-tier job from
claiming tagged-release capacity. It still boots through one supervisor and the
same host-wide VM cap. Existing single-name and plural-name
agents need no migration.

The M5 host obtains this lane from `profiles/m5-macos-fleet.toml`, rendered as
`com.danielraffel.tartci.tart-runner-macos-fleet.m5.pulp-release`. The generated
agent preserves the contract above while bringing the controller under the
fleet receipt, readiness, and pool-lifecycle gates. Its replacement list names
only the legacy `com.danielraffel.pulp.tart-runner-macos-release` label; retire
that label through the normal generated installer only after this source change
has merged. Do not manually load either controller during source preparation.

On a host not managed by a generated fleet profile, migrate an installed
release agent by rendering the current template over its plist (preserving that
host's `TART_HOME` substitution), then drain/reload the single
`com.danielraffel.pulp.tart-runner-macos-release` LaunchAgent. Do not load one
release agent per workflow.

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

All unattended macOS, macOS-release, Linux, Windows, and reap agents explicitly
set `TARTCI_GH_CLI=ghapp`. Install the wrapper in the LaunchAgent `PATH` on
every host; no token or secret belongs in a plist. After rendering/loading each
installed agent, verify launchd received the wrapper selection (examples):

```sh
launchctl print "gui/$(id -u)/com.danielraffel.pulp.tart-runner-linux" |
  grep -A1 TARTCI_GH_CLI
launchctl print "gui/$(id -u)/com.danielraffel.tartci.reap" |
  grep -A1 TARTCI_GH_CLI
command -v ghapp
ghapp api repos/Generous-Corp/pulp --jq .full_name
```

A missing wrapper is a deployment failure; do not let the unattended process
fall back to ambient `gh`.

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
Use the installer to keep the authority checkout in a mode-600 canonical
configuration that survives LaunchAgent drift:

```sh
scripts/install_shipyard_queue_tick.sh \
  --repo-root /absolute/path/to/pulp \
  --authority \
  --gh-cli /absolute/path/to/ghapp \
  --mode dry-run
# Re-run with --install only after reviewing the plan.
```

The installer removes any previous health verdict before kickstart and succeeds
only after the newly started tick publishes a fresh healthy verdict. After the
dry-run log and health file are clean, arm the single authority explicitly:

```sh
scripts/install_shipyard_queue_tick.sh \
  --repo-root /absolute/path/to/pulp \
  --authority \
  --gh-cli /absolute/path/to/ghapp \
  --mode live \
  --install
```

Use `--mode reap-only` on a non-authority host that should clean terminal
ship-state without merging. Every mode requires `--gh-cli` pointing to an
executable GitHub App wrapper; unattended operation never falls back to ambient
`gh`. Never hand-edit the installed plist to change mode; re-run the installer
so the rendered mode and fresh health proof stay coupled.

Full-live additionally requires `SHIPYARD_QUEUE_AUTHORITY=1`; set that on
exactly one host whose Shipyard runner tag matches
`[merge_queue].mutation_machine`. Other CI Macs may remain dry-run or reap-only
but cannot become queue writers. Set `SHIPYARD_QUEUE_REPO_ROOT` to the
authority's repository checkout; the tick runs Shipyard from that directory
and requires `authority_matches=true` before full-live operation. An
authority-local `shipyard merge-queue hold` causes the configured authority
tick to exit before any GitHub read; during an incident, run it on that
authority (and propagate it fleet-wide for consistent operator status). This
integration requires Shipyard 0.80.0 or newer; install that release before
deploying the script or plist. Missing authority configuration is a hard
unhealthy exit, never a silent downgrade to reap-only. The last machine verdict
is written to `~/Library/Logs/shipyard-queue-tick.health.json`; inability to
write that verdict is itself loud and nonzero. Unreadable or malformed queue
control and ship-state observations are unhealthy rather than successful
no-ops. A ship-state is
recoverably archived only after three consecutive, explicit GitHub not-found
responses; generic GitHub errors remain fail-closed and do not increment that
counter. Re-bootstrap after changing the installed plist.
Design + adversarial review: pulp
`planning/2026-06-30-ship-queue-resilience-design.md`.

## Retire Orchard on upgrades

Deleting the old templates from a checkout does not stop an already-loaded
KeepAlive LaunchAgent. Every upgraded host must first preview and then apply the
idempotent cleanup:

```sh
scripts/disable_orchard.sh
scripts/disable_orchard.sh --apply
```

The apply step boots out the two retired controller/worker labels, removes only
their exact installed user plists, and fails unless both labels and both plists
are absent. It is safe to repeat and must be run on every former shadow host.

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
