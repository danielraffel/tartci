# tartci — local CI VMs on macOS (Tart + QEMU + Shipyard)

[![lint](https://github.com/danielraffel/tartci/actions/workflows/ci.yml/badge.svg)](https://github.com/danielraffel/tartci/actions/workflows/ci.yml)

Stand up **fast, cached, disposable Linux / Windows / macOS build VMs on an Apple
Silicon Mac**, optionally wired to GitHub runners +
[Shipyard](https://github.com/danielraffel/Shipyard), so you can build & test a repo
locally instead of (or alongside) GitHub-hosted runners. Headless CI is the
priority; the same goldens double as GUI **bench** VMs you can open in UTM to test things like
plugins in a DAW.

> Project-agnostic. [Pulp](https://github.com/danielraffel/pulp/) is the first consumer, but any repo (e.g. a Pulp-based
> plugin) plugs in with one `vm-image` manifest. This repo holds **scripts +
> configs + docs only — never the large VM images** (those stay local / pulled /
> baked, and are gitignored).

## What you get
- **macOS + Linux** via **[Tart](https://github.com/cirruslabs/tart/)** (Apple Virtualization) — native speed, CoW
  clones, host-mounted ccache warm across clones.
- **Windows** via **standalone QEMU** (hvf) — AVF can't install Windows (no inbox
  virtio-blk driver; black installer display), so Windows is first-class on QEMU
  with an NVMe disk + `ramfb` display.
- Host-mounted caches, SSH access + log collection, and a tiny metrics dashboard.

> **Project Status:** a working lab toolkit, hardening toward turnkey. Wired +
> proven today: the **Linux Tart lane** (`tartci up linux` — ephemeral clone →
> warm host ccache → full build + ctest → discard, host-validated green), the
> **Windows QEMU lane** (`tartci up windows` — per-job CoW overlay → dynamic SSH
> port → MSVC arm64 build + ctest → discard; host-validated: overlay/SSH
> mechanics + full 735-target compile green, ctest is the golden's proven 7287/7303),
> the **macOS Tart primitive** (`tartci up macos` / `serve macos` now dispatch to
> `providers/tart-macos`; 2026-06-09 validation proved disposable in-VM Release
> build + ctest enumeration, JIT runner assignment/cleanup, and `$HOME` launchd
> boot; one Pulp screenshot payload test remains red in the scratch JIT run),
> the **x86_64 cross/emulation smoke lane** (`tartci up linux --target-arch
> x86_64` — cross-compile + run dynamic x64 binaries under Rosetta-for-Linux;
> `--self-test` proves the toolchain+emulator chain golden-agnostically), the **pool-serving
> lanes** (`tartci serve linux|windows` — ephemeral per-job GitHub Actions
> runners, ported from Pulp's proven `tools/ci` supervisors), the metrics, and
> `tartci doctor/bench/metrics`. **Not yet wired:** the Windows/Prism cross lane
> (Linux/Rosetta is wired; Windows-on-ARM
> x64-via-Prism is a follow-up). Emulated x64 is a SMOKE/debug signal only —
> GitHub-hosted x64 stays the authoritative x64 gate.

## Quick start
```bash
git clone <this-repo> tartci && cd tartci
./tartci doctor           # report host prereqs + golden/bench stores (always safe)
./tartci doctor --reap --json    # report owned stale CI VM/runner residue
./tartci setup            # brew-install tart/qemu/sshpass + create local stores
./tartci bench windows    # clone the Windows golden → open in UTM for GUI/DAW testing
./tartci metrics report   # text build/cache table (or `metrics dashboard` for HTML)
./tartci status --json    # host-local provider/capacity/profile state for agents
./tartci profile plan normal-local-fast --repo danielraffel/pulp --json
./tartci prepare linux     # bake/provision the Linux golden (Rosetta x64 smoke enabled)
./tartci up linux         # ephemeral Linux build+test of a ref (clone→build→ctest→discard)
./tartci up linux --target-arch x86_64   # cross-build x64 + run tests under Rosetta (SMOKE)
./tartci up macos --src /path/to/pulp     # ephemeral macOS build+test clone (host caches mounted)
./tartci up windows       # ephemeral Windows build+test (CoW overlay→build→ctest→discard)
./tartci serve linux      # serve the GitHub Actions pool: ephemeral per-job runner(s)
./tartci serve macos --once --labels self-hosted,macOS,ARM64,pulp-build-vm
./tartci serve windows --loop   # keep serving Windows jobs (throwaway overlay each)
./tartci windows run      # boot the Windows installer/single-operator VM (from-scratch)
```
`tartci doctor`, `bench`, `metrics`, `up linux`, `up windows`, and `serve
linux|macos|windows` are wired today. `tartci status --json` and `tartci profile
list|show|explain|plan` are read-only helpers for Shipyard, Codex, Claude, or
other agents to answer where CI should run from machine-readable config.
`tartci up linux [--ref <git-ref>] [--no-gpu]
[--keep]` clones the `pulp-linux-build` golden, mounts the host ccache, and
builds + ctests in-guest. `tartci up windows [--ref <git-ref>] [--smoke]
[--keep]` makes a per-job CoW overlay off the Windows golden on a dynamic SSH
port (concurrent-safe), builds GPU-off under MSVC arm64, then discards the
overlay (see `providers/`). `tartci up macos --src <checkout>` clones the
macOS runner golden, mounts source read-only plus ccache/FetchContent, builds in
`~/build`, runs ctest, and discards the clone. See
`docs/runbook.md` for the from-scratch, gotcha-by-gotcha guide and
`docs/new-repo-agent-guide.md` to onboard a new repo.

### Serve the GitHub Actions pool
`tartci up` does ONE on-demand build and exits; `tartci serve <os>` is the
**pool-serving** sibling. It mints a Just-In-Time (single-job) runner config,
boots a throwaway clone per queued job, and lets the GitHub **workflow** drive
the build — the host just supplies a clean VM each time. `--loop` keeps serving
(what the LaunchAgents run); the default is one job then exit (pilot-safe).
The Windows QEMU loop scans queued and in-progress workflow runs for queued jobs,
ignores stale queued jobs older than `TARTCI_RUNNER_MAX_QUEUED_AGE_SECONDS` (six
hours by default), and checks queued job labels by default
(`TARTCI_RUNNER_QUEUE_MATCH_LABELS=1`). That keeps the supervisor safe to leave
loaded while the repo still defaults ordinary Windows jobs to GitHub-hosted
`windows-latest`; a race-loser VM that boots but never claims a job exits after
`TARTCI_RUNNER_IDLE_TIMEOUT_SECS` (15 minutes by default), deletes its stale
GitHub runner registration, and discards the overlay. Ephemeral Windows runner
names include a host-derived prefix by default so multiple Macs can serve the
same label without repo-scoped runner-name collisions.
Repo / golden / labels are env-driven (`TARTCI_RUNNER_REPO`,
`TARTCI_LINUX_GOLDEN` / `TARTCI_MACOS_GOLDEN` / `TARTCI_WIN_GOLDEN`,
`TARTCI_RUNNER_LABELS`); see each `providers/*/runner.sh` header. To serve across reboots, install a LaunchAgent
from `launchd/` (the Shipyard macOS GUI's "Serve CI builds from this Mac" switch
toggles those agents). Emulated x86_64 stays on the on-demand `up` lane (smoke /
debug); pool jobs build whatever arch the workflow targets. The Linux, macOS,
and Windows serve loops only boot when a queued job's requested labels are
satisfiable by the configured runner labels, so a pilot agent does not boot for
unrelated queued `Build and Test` work.
For additional Apple Silicon pool members, use the generic host checklist in
`docs/runbook.md`: stable SSH alias, absolute `/opt/homebrew/bin/tart`,
absolute `$HOME/.local/bin/tartci`, home-backed `TART_HOME`, and matching
Shipyard `host_class` capacity config. If multiple macOS hosts advertise the
same workflow selector, give each host a unique runner name by appending one
extra host-specific label after the shared `pulp-build-*` pool label or by
setting a unique `--name-prefix` in the installed LaunchAgent.

For Windows QEMU pool members, install the same golden qcow2 and the same
tartci checkout/home copy on every participating Apple Silicon host, keep
Homebrew's `/opt/homebrew/bin` in the LaunchAgent `PATH`, and leave
`PULP_LOCAL_WINDOWS_RUNS_ON_JSON` unset until a Windows-native workflow has
proved the local label. The golden must contain the hosted-runner assumptions
the workflow uses: Git Bash on `PATH`, Chocolatey, `ccache` when the workflow
calls it, Visual Studio Build Tools/MSVC for C++ workflows, and `C:\tmp`. The
supervisor imports `vcvarsall` before launching the Actions runner so Bash steps
can still see `cl.exe`; override the default `arm64` target with
`TARTCI_WIN_VCVARS_ARCH` if a repo needs a different MSVC environment. Windows
preflight is fast by default (`TARTCI_WIN_PREFLIGHT_MODE=fast`); use
`TARTCI_WIN_PREFLIGHT_MODE=full` only when debugging a golden/toolchain/network
issue.
Supervisor diagnostics and rough benchmark timings are kept per job under
`TARTCI_WIN_LOGS` as `preflight.log`, `runner-output.log`, `runner-diag.log`,
`qemu.log`, and `timing.tsv`; see `docs/runbook.md` for the full setup and
proof recipe.

For Pulp-style required macOS gates, keep setup two-step. First serve the
non-required pilot label (`self-hosted,macOS,ARM64,pulp-build-vm`) and prove a
job can drain locally. After that, graduate by serving both labels from the VM
supervisor (`self-hosted,macOS,ARM64,pulp-build,pulp-build-vm`) and route the
workflow selector to the full label set. Keep the bare-metal `pulp-build`
runners online as rollback fallback, but excluded from the default VM route by
the extra `pulp-build-vm` label.

Keep `Release CLI` on its own macOS VM lane. Serve
`self-hosted,macOS,ARM64,pulp-build-vm-release` from
`launchd/com.danielraffel.pulp.tart-runner-macos-release.plist.template`, then
move `PULP_RELEASE_MACOS_RUNS_ON_JSON` to that selector only after a real
Release CLI proof completes. Do not share the Build and Test `pulp-build-vm`
label with release jobs.

### Reap stale CI residue
`tartci doctor --reap --json` is the report-only Tier-2 janitor for macOS Tart
CI hosts. It emits capacity, supervisor heartbeat, VM, GitHub runner, problem,
and fixed-action fields with exit codes suitable for launchd/cron (`0`
healthy/fixed, `1` stale or wedged, `2` unreadable). Add `--fix` only after a
clean report: VM deletion requires an allowed CI prefix plus a tartci state-file
ownership marker, and goldens/bench names/`pulp-vm`/`rosetta-probe` stay
protected. Offline GitHub runner registrations are reaped only when their names
match an owned CI prefix and no fresh live supervisor heartbeat backs them. See
`launchd/com.danielraffel.tartci.reap.plist.template` for the periodic
host-local LaunchAgent shape. macOS runner heartbeats replace their state files
atomically so `doctor`, `observe`, and fleet-level probes never need to infer
health from a partially written JSON file.

### Observe a live macOS runner
`tartci observe macos` is the read-only operator view for macOS VM jobs. It
combines the janitor digest, matching GitHub run/job/step state, guest process
snapshots, recent CTest output, and the runner log tail. Guest process snapshots
redact GitHub runner `--jitconfig` payloads and truncate long command lines by
default; use `--process-line-width N` if a wider diagnostic view is needed. Use
`--no-guest` when the VM is already gone or SSH is not useful, `--runner NAME`
to narrow a host with multiple supervisors, and `--json` for scripts.

### x86_64 cross / emulation (smoke, not a gate)
The guest is ARM64 (Apple Virtualization has no x86). `tartci up linux
--target-arch x86_64` cross-compiles for x64 (gcc/g++-x86-64-linux-gnu) and runs
the test subset under **Rosetta-for-Linux** (binfmt) — a first line of defense for
atomics / SIMD / ISA-divergent behavior, but **not** a gate: GitHub-hosted x64
stays authoritative (sanitizers, SIMD/Highway dispatch, and RT timing are
unreliable emulated). It defaults **GPU OFF** because the prebuilt Skia maps both
Linux arches to the same `libskia.a` path (`docs/gotchas.md`); `--gpu` requires
an explicit x64 Skia tree via `--skia-dir`, else it fails loud. `--self-test`
proves just the dynamic x64 toolchain+emulator chain with a trivial binary
(golden-agnostic). The provision step installs Rosetta systemd mount/binfmt
units plus an amd64 userland so the setup survives reboot. The manifest declares
this with `target_arch`/`cross` + an `[emulation]` table — see
`manifests/example.x64.toml`.

## Per-project use
A repo drops a `.shipyard/vm-image.toml` (see `manifests/`) declaring its
`os`/`arch`/`toolchain`/`packages`/`caches`/`mounts`. tartci bakes or clones a
golden from it — zero hand-provisioning. Non-generic needs (extra SDKs, a special
toolchain) go in that manifest.

CI routing policy can live beside the consumer repo or in tartci `profiles/` as
commented TOML. tartci treats profiles as a read-only contract: they explain
what each repo wants for `pr`, `release`, `coverage`, `scheduled`, and
`issue_on_failure`, and they map stable target IDs to concrete GitHub
`runs-on` selectors. Shipyard can consume that contract as the router, while
tartci remains the VM/provider layer. `profiles/normal-local-fast.toml` is the
current Pulp shape: PR macOS/Linux/Windows prefer local ARM64 VM runners where
enabled, overflow to GitHub where configured, and scheduled Intel
Linux/Windows checks remain GitHub-hosted x64. Use
`tartci profile explain <name> --repo OWNER/REPO --json` when an agent needs
descriptions and settings from the same parseable source of truth. See
[Shipyard profiles](https://github.com/danielraffel/Shipyard/blob/main/docs/profiles.md)
for the orchestration side.

## Optional pulp-CLI integration
Soft dependency: if installed, `pulp doctor` reports "local CI VMs: available",
and `pulp vm up <os>` delegates here. If absent, Pulp is unaffected. Maximally
useful when present; never a punishment when missing.

## Layout
```
providers/   tart-linux · tart-macos · qemu-windows  (per-OS provision + run + runner)
launchd/     LaunchAgent templates to serve the pool at boot (Pulp's concrete instance + how to genericize)
manifests/   example vm-image.toml per project profile
metrics/     dashboard.py + report.py (file-based; no server) + sample.jsonl
bench/        helper to clone a golden → open in UTM for GUI testing
scripts/     lint.sh — repo hygiene gate (shellcheck + bash -n + py_compile + TOML)
docs/        runbook.md (human from-scratch) · new-repo-agent-guide.md (agent onboarding) · gotchas.md
```

## Contributing checks
Run the same gate CI runs before you push — it's one script, portable to macOS's
stock bash 3.2:
```bash
./scripts/lint.sh    # shellcheck -S warning + bash -n on every shell script
                     # (incl. the extensionless `tartci` dispatcher), py_compile
                     # on metrics/*.py, and a parse check on manifests/*.toml
```
`.github/workflows/ci.yml` runs this on every push + PR. It guards the class of
bug that only bites at run time on a CI host — e.g. a help line that executes as
a command before `set -e`.

## Metrics (file-based, no server)
Per-run build/configure/ctest times + cache-hit% land in a `metrics.jsonl` (one
JSON object per line). Two zero-dependency Python scripts read it:
```bash
python3 metrics/report.py    metrics/sample.jsonl                 # text table
python3 metrics/dashboard.py metrics/sample.jsonl index.html      # self-contained HTML
```
`sample.jsonl` carries the real 2026-06 bring-up numbers so the dashboard renders
out-of-box (a "Last run" hero + per-OS configure/build/ctest + cache-warmth
trends). Your own `metrics.jsonl` is gitignored.

## Status
Bring-up proven on a Mac Studio (2026-06): Linux build+test+cache green; Windows
24H2-ARM build green + tests pass. See `docs/runbook.md` for the recipe and the
hard-won gotchas (ISO 512-byte alignment, ramfb vs virtio-gpu, all-USB install
media, MSVC partial-install nuke, BOOTAA64 auto-boot, …).
