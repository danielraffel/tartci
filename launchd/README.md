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
watchdog refuses both alive-but-frozen and crash-loop recovery when inventory is
unavailable, so this diagnostic distinction does not weaken the long-build
guard. That refusal is only as good as the rendered `TART_HOME`: a LaunchAgent
inherits no login shell, so an agent installed without it reads Tart's default
store, reports an empty inventory, and reads every long build as an idle host. Do not put relay hostnames,
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
stale AND no VM is running** (the first two together distinguish the invisible
crash-loop from a healthy between-jobs idle, whose "waiting" log is always
fresh; the third separates it from a supervisor that is simply quiet inside a
long build). The VM condition is load-bearing rather than belt-and-braces: a
`serve --loop` exits `EX_TEMPFAIL` by design and launchd reports that non-zero
code for the whole life of the respawned job, so a supervisor that writes
nothing for the 30-minute stale threshold while a required gate job builds is
indistinguishable from a crash-loop on exit code and log age alone. Healing it
boots out the supervisor under that live job and takes its guest with it. An
inventory that cannot be read is reported `unknown` and never healed. Healing is the same full
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

An interval agent is quiet by design between runs, so the shared 30-minute
stale-log threshold would call an hourly agent frozen on every other pass and
boot out an agent that is working. The watchdog reads `StartInterval` from each
agent's own plist and widens that agent's bound to twice its interval. The bound
never shortens: a one-minute agent keeps the 30-minute floor, and only a slower
agent widens past it. Two intervals is the smallest bound that survives one
skipped run. An absent, zero, negative, or non-integer `StartInterval` yields no
bound rather than a zero one, because a zero would collapse the threshold and
make every agent read as wedged.

A reload is also refused outright for an interval agent that is currently
`running`. Such an agent is running its one job, not serving a loop that can be
interrupted anywhere: the reclaimer is mid-`rmtree`, and booting it out there
leaves a half-deleted tree no later pass can classify. The watchdog logs the
refusal and lets the next interval start it cleanly.

Not every nonzero exit is a wedge. Some agents exit with a code that reports an
application condition: the program ran to completion and is naming something a
reload cannot fix. The reclaimer's 2 (unusable scan root), 3 (still below the
free-space floor), and 4 (process table unreadable, so nothing could be proven
idle) are those. Treating them as the crash-loop signature boots out a working
agent every hour and buries the condition it was reporting, so they get their
own verdict, `attention`: it is reported, `--status` exits non-zero on it, and
it is marked `!` rather than a tick, but it is never healed. Everything not
listed stays on the wedge path on purpose. 126 and 127 (not executable, not
found) and signal-derived exits are exactly the no-Full-Disk-Access wedge class
this watchdog exists to recover.

Per-lane launchd enablement is also authoritative. A host may participate while
legacy, release, sanitizer, Linux, or Windows plists remain explicitly disabled.
The watchdog reads `launchctl print-disabled` once per pass and never reloads an
exact disabled label. If that enablement map cannot be read, automatic recovery
fails closed for pool-controlled lanes instead of guessing that every discovered
plist should be active.

```
mkdir -p "$HOME/Library/Logs/tartci"
: "${TART_HOME:?declare this host's Tart VM store}"
python3 scripts/render_launchd_template.py \
  launchd/com.danielraffel.tartci.launchd-watchdog.plist.template \
  --set "TART_HOME=$TART_HOME" --set "HOME=$HOME" \
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

### Disk reclaimer

`com.danielraffel.tartci.reclaim.plist.template` runs the second janitor:

```sh
tartci reclaim --json --fix
```

The reap agent above frees the VM store; this one frees the volume underneath
it. They are separate because they protect different things: reap keys off a
tartci ownership marker on a VM, and no such marker exists on a developer's
build directory.

It exists because the disk axis of a lease is fatal rather than degrading. A
host whose data volume fills denies EVERY lease `disk_capacity_exceeded` and
stops serving, while its share of the load moves silently to whatever host is
left. m5 reached 14 GiB free on a 3.6 TiB volume, with 488 build directories
totalling 1.32 TB and no cleanup agent of any kind, and refused 276 leases
before anyone noticed the gate was dead rather than slow.

Deletion is safe-by-construction in the same shape as reap. A directory is
removed only when every positive check passes: its basename is a build-directory
name (`build`, `build-<key>`, `build-cov*`, `build-coverage*`), it carries a
generated-tree marker (`CMakeCache.txt`, `build.ninja`, `CMakeFiles/`), it
carries no source marker (`.git`, `CMakeLists.txt`, `Cargo.toml`,
`package.json`) of its own, no live build process names its path, and nothing
inside it changed inside the age gate. A directory that merely has the name is
skipped, and the report says which check rejected it.

Be precise about what the liveness check is worth, because it is the one gate
that sounds stronger than it is. It compares each candidate's path against the
command lines of live build processes, both as spelled and resolved through
realpath, so a build invoked with a relative path from inside its own tree can
name a directory the scan cannot match. It is a cheap first filter, not a proof
of idleness. The gate that actually carries the weight is the age re-check: the
mtime is read a second time immediately before `shutil.rmtree`, so a tree that
was touched between the scan and the delete is kept and reported as
`touched_during_pass`. The window that matters is the one between deciding and
deleting, and that is the window the re-check closes.

A candidate whose age cannot be measured is never deleted. `EACCES`, `EPERM`,
`EIO`, and `ELOOP` while walking a tree mean the janitor could not see it, and
it is kept with the reason `unmeasured`. A vanished entry (`ENOENT`) is
deliberately not in that set: it is a normal, constant event on a live build
tree, and it is positive evidence the tree is busy rather than a failure to
measure.

A `--fix` pass writes a bounded heartbeat to stderr, which the plist points at
the same log file as stdout, so the JSON document on stdout stays parseable. It
announces the pass start and the candidate count, then at most one line every
five minutes as it works, and one line naming each tree immediately before it
is removed. That last line is never rate limited. Two reasons: the watchdog reads this agent's
liveness from its log mtime, so a long working pass must not look frozen, and if
the process is killed mid-unlink that last line is the only record of which tree
was left half deleted.

Two age tiers, so an idle host keeps recent build dirs warm and a full host
reclaims harder: `TARTCI_RECLAIM_MIN_AGE_DAYS` (30) always applies, and the
shorter `TARTCI_RECLAIM_PRESSURE_MIN_AGE_DAYS` (7) applies as well once free
space drops below `TARTCI_RECLAIM_PRESSURE_FREE_GB` (200).

`TARTCI_RECLAIM_FAIL_BELOW_GB` (60) closes the escalation half. A host still
below the floor after a pass exits 3 with a named reason on stderr, so launchd
records a failing janitor and a supervisor sees a full disk rather than only
seeing refused leases. That gap is why the m5 outage ran for days.

Exit 4 is the separate case, and the distinction matters to whoever reads the
recorded status: 3 means the pass measured the host and it is genuinely still
full, while 4 means a measurement the decision depends on could not be taken at
all (the process table, or free space with a floor set), so nothing was deleted
and nothing was certified. Both are failures, but 3 asks for disk and 4 asks why
the janitor cannot see. Exit 2 means no scan root resolved, and 0 means the pass
ran and the host is above its floor.

This agent prevents a slow fill; it does not rescue a host that filled today.
The age gates are the binding constraint, by design: measured against m5's real
tree on 2026-09-10, right after the outage was cleared by hand, a 30-day pass
found 128 candidates and would have deleted none, and even the 7-day pressure
tier reclaimed only 1.6 GiB, because nearly every surviving build directory had
been written in the preceding week. The same scan at a 12-hour gate would have
deleted 98 directories totalling 616 GiB, which is the measure of how much the
gate is holding back rather than failing to see. A host that fills with work
genuinely younger than the pressure gate is meant to exit 3 and escalate to a
human, never to delete a build somebody is still using.

Run it report-only first. Without `--fix` the pass is a dry run and prints what
it would remove:

```sh
"$HOME/.local/bin/tartci" reclaim
```

A dry run reports the bytes a `--fix` pass would free, both in the summary line
("would reclaim N GiB") and in the JSON `reclaimed_bytes`, so the report-only
step tells you what the rollout is actually worth on that host. Under `--fix`
the same figure is what was freed.

Scan roots are discovered rather than declared: the janitor keeps whichever of
`~/Code` and `/Volumes/Workshop/Code` the host actually has, and measures free
space on every volume those roots span. The `TARTCI_RECLAIM_FAIL_BELOW_GB`
floor is judged against the tightest of those volumes, so a healthy disk cannot
certify a full one. Pressure is scoped per volume instead: only candidates on a
volume under `TARTCI_RECLAIM_PRESSURE_FREE_GB` take the shorter
`TARTCI_RECLAIM_PRESSURE_MIN_AGE_DAYS` gate, because failing more is safe and
deleting more is not.
`TARTCI_RECLAIM_ROOTS` still overrides with a colon-separated list, but reach
for it only for a one-off run. A declared root that exists on the wrong volume
is the one fault nothing downstream can catch: the pass reports a clean exit 0
forever while the volume it was installed to protect fills up, which is what
`$HOME/Code` did on a host that keeps its code on Workshop.

The scan depth is 5, raised from an earlier 3. Depth 3 reached
`Code/<repo>/build` and `Code/agent-worktrees/<worktree>/build-cov`, but it
could not see `<root>/<repo>/.claude/worktrees/<worktree>/build`, a nest that
sits five levels below the scan root and now holds the largest single
reclaimable tree on the fleet. On m3 that blind spot was 14.86 GiB of the
1020.17 GiB reclaimable under Workshop, and 14.64 GiB of it was one directory.
Depth is the right knob rather than a per-host list of nests, because one
number names the same pattern on m3, m5 and m1, while a nest list rots the next
time a worktree root moves.

Scanning deeper does not weaken the guards, since the marker, source-marker,
live-process and age tests are all depth independent. Measured on m3's Workshop
root, depth 5 exposes 94 build-named directories that depth 3 never saw, and 88
of them are refused for carrying no generated-tree marker: that is precisely
what `external/skia-build/build`, cargo `target/debug/build`, and
`node_modules/*/build` are. The 6 that pass the marker gates are regenerable
CMake trees, and they still face the live-process and age gates before anything
is removed.

Stop at 5. Deeper scans surface no further reclaimable bytes and only cost walk
time, so 5 is the smallest depth that misses nothing. Depth is also not what
decides whether a host reclaims anything: on a host whose build trees are
rebuilt daily, the age gate is the binding constraint, and lowering that gate to
chase the bytes would delete trees that are still live.

Logs land in
`~/Library/Logs/tartci/tartci-reclaim.log`. The agent runs hourly rather than
the reap agent's five minutes: a pass walks the scan roots and sizes
candidates, and a disk fills over days.

That log is bounded, because it lives on the volume the janitor exists to
protect and launchd appends every pass to it forever. Nothing used to truncate
it: m3 was carrying 368 MiB of tartci logs when the bound was written. Each
pass renames the log aside at startup once it reaches
`TARTCI_RECLAIM_LOG_MAX_BYTES` (8 MiB) and keeps `TARTCI_RECLAIM_LOG_GENERATIONS`
(5) of it. `TARTCI_RECLAIM_LOG` must name the same path as `StandardOutPath`,
which is what the template does; leaving it unset disables rotation entirely.

It renames rather than truncates, because launchd opens `StandardOutPath` fresh
on every spawn of a `StartInterval` job. That was measured on a throwaway job
rather than assumed, and it has a visible consequence: the descriptor a pass
inherited still points at the inode it just renamed, so the pass that triggers a
rotation writes into generation 1 and the new file starts collecting at the next
spawn. The worst case on disk is therefore generations x (max bytes + one
pass's output), roughly 40 MiB, not generations x max bytes exactly.

`tartci status` reports whether this host has the agent, whether launchd holds
it, when it last wrote its log, and the free space on each volume the janitor
scans. Ask it before assuming a host is protected: the failure it exists to
catch is silent, because a host that never got the agent looks exactly like a
host whose passes are all finding nothing.

It reports both janitors on their own lines, the disk reclaimer above and the
VM reaper from the Janitor section, because a host can carry either, both, or
neither: m3 has the VM reaper and no disk reclaimer, while m1 and m5 have no VM
reaper at all. A single collapsed line would have read as healthy on exactly
the host that is half covered.

It reads the roots from `disk_reclaim` itself rather than keeping its own list,
so status cannot disagree with the janitor about which volumes are scanned, and
it prints `unknown` rather than a figure when a volume cannot be read. An
unreadable volume is not a healthy one.

Pulp ships its own `tools/scripts/clean_build_cov.sh`, which covers only
`build-cov*` inside one checkout. That stays: it is the repo-local convenience
for an external cloner who has no tartci. This agent is the fleet-wide job, and
it covers the ordinary `build/` and `build-<key>/` directories that were the
bulk of m5's 1.32 TB.

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
