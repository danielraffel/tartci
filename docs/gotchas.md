# tartci gotchas — symptom → cause → fix

## `tartci` looks missing over SSH after it was installed

**Symptom:** `ssh host 'command -v tartci'` returns nothing, while the TartCI
runner agents are healthy.

**Cause:** non-login SSH shells often omit `~/.local/bin`. The supported
installation is the absolute home-backed wrapper at `~/.local/bin/tartci`;
launchd runner plists must invoke that path explicitly and include
`~/.local/bin` in their service `PATH`.

**Fix:** verify `test -x "$HOME/.local/bin/tartci"` and inspect the relevant
LaunchAgent, rather than diagnosing from `command -v` alone. Fleet preflight
reports `daemon-can-reach-*` and `tartci-installed` separately so this PATH
difference cannot masquerade as missing TartCI.

Hard-won, one bullet each. Grouped by lane. If a build/install behaves
inexplicably on a fresh Apple Silicon host, the answer is almost certainly here.

## Cross-cutting (AVF / QEMU media)

- **"Invalid disk image. The disk image format is not recognized."**
  → *Cause:* the disk/ISO byte size isn't a multiple of 512; AVF and QEMU both
  reject it. → *Fix:* **512-byte-pad** the file up to the next boundary.
  hdiutil-produced ISOs are already aligned; UUP/Microsoft ones often are not.

- **Tart only boots arm64 guests.**
  → *Cause:* Apple Virtualization.framework has no x86 virtualization/emulation.
  → *Fix:* design every VM as arm64; reach x64 via cross-compile + emulation
  (Rosetta on Linux, Prism on Windows) as a *signal only* — GitHub-hosted x64
  stays the authoritative gate.

- **Tart cannot run on an Intel Mac at all** — not "arm64 guests only", but no
  Tart. Virtualization.framework supports macOS *guests* only on Apple Silicon; on
  Intel it offers Linux guests only. So an Intel Mac joining the fleet cannot use
  the tart provider for anything, and needs a different plan:
  → *macOS VMs on Intel* require a third-party hypervisor — VMware Fusion (free
  for personal use, `vmrun` snapshot/revert), Parallels (`prlctl`), or Anka. Apple's
  EULA allows 2 extra macOS VMs on Apple hardware.
  → *Or run on metal*, which is usually the better trade on older Intel hardware:
  fixed VM RAM strands capacity a 6-core box cannot spare, and macOS images are
  60–80 GB. Register the runner `--ephemeral` and wipe the workspace before **and**
  after each job (before matters — a run killed mid-job never reached its trap),
  keeping ccache and the FetchContent source cache *outside* the wiped path. That
  buys a clean workspace, which is the failure mode reused build dirs actually
  cause; it does **not** buy clean OS state, and saying so plainly is better than
  implying a VM-grade guarantee.

- **Python on a fresh macOS is 3.9, and `tomllib` arrived in 3.11.** Any tool that
  reads a TOML config with `tomllib` will silently fall back to its defaults — a
  config file that appears installed and does nothing. Seen with Pulp's `daw-smoke`
  opt-in, which reported `enabled: False` no matter what the file said. Install a
  modern Python (`uv python install 3.12` needs no sudo) and put it ahead of
  `/usr/bin` on the runner's PATH. Verify by *parsing the config*, not by checking
  the file exists.

- **`ssh <host> 'tart list'` says `command not found`, but Tart is installed.**
  → *Cause:* non-interactive SSH sessions often do not load Homebrew's PATH.
  → *Fix:* configure fleet tools with the absolute Tart path, usually
  `/opt/homebrew/bin/tart`, and pass the intended store explicitly as
  `TART_HOME=/Users/<you>/VMs` (or the host's absolute Tart store path). Treat
  `installed but unreachable from this launch environment` as a distinct
  status from `absent`; never infer absence from ambient `command -v` alone.

- **`tartci setup` still finds `cirruslabs/cli/tart`.**
  → *Cause:* Tart moved to `openai/tart`, and Homebrew will not install the
  same-named `openai/tools/tart` formula while the old tap's keg is installed.
  → *Fix:* take the host out of the pool, wait for zero running Tart VMs,
  prefetch the new formulae, uninstall the old Tart/Softnet kegs, install
  `openai/tools/tart`, and canary one host at a time using the runbook. Do not
  perform the tap migration underneath an active VM.

## Linux (Tart)

- **Injected SSH keys vanish after reboot.**
  → *Cause:* cirruslabs cloud-init re-applies the default
  `~/.ssh/authorized_keys` every boot, wiping your additions. → *Fix:* write keys
  to an **unmanaged** `~/.ssh/authorized_keys_ci` and add an sshd drop-in:
  `AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys_ci`. Never bake
  private keys.

- **Host-ccache mount disappears after reboot.**
  → *Cause:* cloud-init **reverts `/etc/fstab`** on every boot. → *Fix:* use a
  **systemd `.mount` unit** (or mount at job runtime), not fstab.

- **virtio-fs share not visible / share root is permission-denied.**
  → *Cause:* the share root listing is perm-restricted; the named subdir is the
  rw surface. → *Fix:* `mount -t virtiofs com.apple.virtio-fs.automount <mnt>`;
  each `tart run --dir="NAME:host"` appears as `<mnt>/NAME` (use the named
  subdir, not the root).

- **ccache hit rate near zero on the warm build (e.g. 10.69% instead of ~99%).**
  → *Cause:* ccache hashing config (`CCACHE_BASEDIR` / `CCACHE_NOHASHDIR`)
  differs between the cache-populating build and the warm build, so keys don't
  match. → *Fix:* set the hashing config **identically** in both. Matched config
  yields ~99.93%.

- **Link error: undefined `icu_74::Locale::...` on Ubuntu.**
  → *Cause:* Pulp opts into direct `icu::Locale`/BreakIterator calls when
  `PULP_HAS_SKIA` + ICU public headers are present (true with `libicu-dev`), but
  the canvas CMake never links system ICU — libskia exports SkUnicode, not ICU's
  own symbols. → *Fix:* `find_package(ICU COMPONENTS uc i18n data)` + link on
  `UNIX AND NOT APPLE AND NOT ANDROID` (and install `libicu-dev`).

- **`setup.sh` reports "Missing Linux desktop dependencies: drm" even though it's
  installed.**
  → *Cause:* the check runs `pkg-config --exists drm`, but the module is named
  `libdrm`. → *Fix:* correct the module name to `libdrm`.

- **Linux x64 cross link grabs the wrong `libskia.a`.**
  → *Cause:* the fetch script maps both `linux-arm64` and `linux-x64` to the same
  `linux-gpu/lib/Release/libskia.a` (`arch_subdir=""`), and Skia is selected by
  OS, not target arch — you can't bake both arches into one tree. → *Fix:* use
  separate `SKIA_DIR` roots per target arch, or add a Linux arch-subdir to the
  fetch script + teach `FindSkia.cmake` to select it. The x64 link also needs a
  matching x64 glibc/libstdc++ sysroot.

- **Dynamic x86_64 binary says `/lib64/ld-linux-x86-64.so.2` is missing.**
  → *Cause:* Rosetta translates the CPU instructions, but dynamic x64 binaries
  still need an amd64 userspace. → *Fix:* `dpkg --add-architecture amd64`, pin
  existing Ubuntu ports sources to `arm64`, add `archive.ubuntu.com` /
  `security.ubuntu.com` deb822 sources with `Architectures: amd64`, then install
  `libc6:amd64 libstdc++6:amd64 libgcc-s1:amd64 zlib1g:amd64 libtinfo6:amd64
  libxml2:amd64`.

- **x86_64 binaries stop running after a reboot.**
  → *Cause:* Tart's Rosetta virtiofs mount and binfmt registration are runtime
  state. → *Fix:* bake the `mnt-rosetta.mount` and
  `tartci-rosetta-binfmt.service` units from `providers/tart-linux/provision.sh`
  into the golden, and boot Tart x64-smoke clones with `--rosetta=rosetta`.

- **After registering Rosetta, normal arm64 commands fail with `Too many levels
  of symbolic links`.**
  → *Cause:* the binfmt register string was written with decoded NUL bytes
  (`printf '%b'`) instead of literal `\xHH` escapes, so the kernel only kept the
  short ELF prefix and matched arm64 binaries too. → *Fix:* write the canonical
  register string with `printf '%s'`; `binfmt_misc` decodes the escapes itself.

- **`mount -t virtiofs rosetta /mnt/rosetta` fails.**
  → *Cause:* the VM was not booted with a Rosetta share, or host Rosetta is not
  installed. → *Fix:* run `softwareupdate --install-rosetta --agree-to-license`
  on the Mac and boot with `tart run --rosetta=rosetta <vm>`.

## Queue and pool control

- **Nothing merges for hours; adding runners does not help.**
  → *Cause:* the merge queue is building more entries in parallel than the runner
  pool can serve, so no entry finishes inside `check_response_timeout_minutes` —
  each times out, requeues and rebuilds, forever. Measured on Pulp 2026-07-30 with
  `max_entries_to_build: 5`: five merge groups × a full matrix ≈ 50 jobs against a
  pool serving ~3 at a time gave **6 jobs running, 75 queued, and zero merges in
  three hours**. Under saturation, parallelism *reduces* throughput — the classic
  queueing result, and it looks exactly like "we need more machines."
  → *Fix:* set `max_entries_to_build: 1` so the head entry gets the whole pool, and
  raise `check_response_timeout_minutes` (60 → 120) so an entry survives a backlog
  instead of dying mid-flight. On Pulp the first merge landed **39 minutes** later
  (it had a backlog to clear) and the queue then settled at **~19 minutes between
  merges**. Quote the steady-state number, not the recovery one — the first merge
  out of a jam is not representative, and estimating from it overstates what any
  further change will buy.
  Adding self-hosted capacity does not fix this, because the starved jobs are on
  *hosted* labels the new machines do not carry.
  → *Diagnose before tuning:* count running-vs-queued jobs across active runs. Many
  running + many queued is real saturation; **few running + many queued is the
  thrash**. Two plausible causes to rule out first, both cheap: org billing (an
  exhausted spending limit blocks hosted runs — check that usage nets to $0) and
  `githubstatus.com` (an Actions incident looks identical from inside).

- **Which host serves the required gate is worth ~2x, and nothing chooses it.**
  With a serial merge queue (`max_entries_to_build: 1`) every merge waits on exactly
  one macOS gate build, so that job's runtime *is* the throughput floor. Measured on
  Pulp 2026-07-31, same job, same commit range, n=15:

  | host | n | median | range |
  |---|---|---|---|
  | Mac Studio VM (`pulp-studio-01-*`) | 5 | **9.5m** | 8.8-11.0 |
  | `pulp-vm-01-*` | 5 | 11.4m | 9.0-11.8 |
  | m1 box (`pulp-vm-m1-01-*`) | 5 | **18.0m** | 15.8-18.8 |

  The ranges do not overlap. Both hosts carry `pulp-build-vm`, so placement is
  whichever runner grabs the job first — a coin flip worth ~8 minutes on every merge
  that loses it, and it reads as random queue variance rather than a host property.
  → *Fix deployed 2026-07-31:* the M3/M5 gate supervisors carry
  `pulp-gate-fast`, and Pulp's required selector includes it. The M1 supervisor
  keeps the generic `pulp-build-vm` label, so it remains available for rollback
  and non-required use but cannot win the serial required gate. The fast
  supervisors also set `TARTCI_VM_LEASE_PRIORITY=gate`; that reserves host-core
  leases but does **not** influence GitHub placement by itself.
  The managed event-class-V2 successor deliberately omits that fixed priority:
  merge-group derives `110`, PR-head derives `100`, and M1 yields through its
  queue-age delay instead of being forced into the non-gate budget.
  → *Before acting, re-measure:* group gate runtimes by host with the ephemeral
  suffix stripped (`pulp-vm-m1-01-67089-59` -> `pulp-vm-m1-01`). Per-runner-instance
  numbers look like n=1 noise and hide the pattern entirely.

- **A lane that gates nothing can still block everything.** On the same incident,
  three Windows jobs were *running* while the required `macos` job sat queued.
  Windows appears in no required check, so it gated nothing while consuming the
  slots the gate needed. Audit which lanes run per-merge against which are actually
  required; move the rest to a schedule.

- **All three Macs look healthy, but the required front job is still queued.**
  → *Cause:* runner process health is not useful-progress health. A bounded
  scanner can legitimately report `queued=0` for its current window, or GitHub
  can assign an optional job that shares the required job's labels.
  → *Fix:* inspect `shipyard runner fleet-status --repo OWNER/REPO --json`,
  confirm managed Pulp V2 supervisors omit a fixed lease priority and publish
  exactly one derived event class, and separate required-gate labels from
  advisory labels. A fixed `gate` priority remains valid for non-V2 required lanes.

- **macOS runners sit `busy=false` while merge-group jobs stay `queued` for
  hours.** Observed on `Generous-Corp/pulp` 2026-07-28: nine merge-group runs
  queued on the head entry, `pulp-studio-02`, `pulp-studio-03` and
  `pulp-preamble-m5` all idle, nothing merging for ~2h.
  → *Cause:* idle is not the same as eligible. Pulp's required `macos` gate
  routes to `["self-hosted","macOS","ARM64","pulp-build","pulp-build-vm"]`, so a
  runner carrying `pulp-build` + `pulp-build-studio` but **not** `pulp-build-vm`
  can never take gate work no matter how idle it looks. Only two runners carried
  `pulp-build-vm`, and both were busy — so effective gate concurrency was 2
  while four macOS registrations idled.
  → *Diagnose* (count eligible runners, not idle ones):

  ```sh
  ghapp api repos/OWNER/REPO/actions/variables \
    --jq '.variables[] | select(.name=="PULP_LOCAL_MACOS_RUNS_ON_JSON") | .value'
  ghapp api repos/OWNER/REPO/actions/runners \
    --jq '[.runners[] | select([.labels[].name]|index("pulp-build-vm"))]
          | map("\(.name) busy=\(.busy)")'
  ```

  → *Fix:* add gate-eligible capacity, or accept the concurrency. Do **not**
  raise the merge queue's `max_entries_to_build` to compensate: extra entries
  contend for the same eligible runners and the wait simply moves from GitHub's
  queue into the host lease store.

- **A host's role says `dedicated-builder` but it serves no gate work.**
  Same incident: the 28-core Mac Studio (`TARTCI_AGENT_BUILD_CAP_CORES=12`,
  `TARTCI_GATE_RESERVED_CORES=14`) was absent from the gate lane while a 10-core
  `light` MacBook and an 18-core `dev-overflow` box served it — the gate was
  running on the two weakest machines.
  → *Cause:* role and budget describe *capacity*, not *participation*. Gate
  participation is the presence of the `tart-runner-macos-gate` LaunchAgent. The
  Studio still had only the legacy `com.danielraffel.pulp.tart-runner` label and
  had never been migrated.
  → *Root cause of the drift:* its `~/Code/tartci` checkout was parked on a
  merged feature branch, 13 commits behind `main` — and
  `scripts/migrate_macos_gate_agent.sh` did not exist at that commit, so the
  migration was silently unavailable on exactly the host that needed it.
  → *Diagnose:*

  ```sh
  tartci pool status                     # look for tart-runner-macos-gate
  ls ~/Library/LaunchAgents | grep macos-gate
  git -C ~/Code/tartci rev-list --count HEAD..origin/main   # 0 == current
  ```

  → *Fix:* bring the checkout to `main` first, then
  `scripts/migrate_macos_gate_agent.sh --apply
  --attest-external-gui-label-updated`. The attestation flag is a human gate —
  the external `shipyard-macos-gui` deployment must already know the new label —
  so do not self-attest it from automation. Audit **every** pool host for both
  checkout freshness and the gate agent; a stale checkout hides the very script
  that repairs it.

- **GitHub has not been able to reach a host for weeks and nothing said so.**
  A webhook receiver that is unreachable looks exactly like one with nothing to
  deliver. On 2026-07-28 a Tailscale node had re-registered with a `-1` suffix;
  the hook still pointed at the old name, whose node had been **offline for 41
  days**, and every delivery returned `502`. Nobody noticed because nothing
  observes delivery history.
  → *Detect:* `scripts/fleet_preflight.py --repo OWNER/NAME` (read-only) checks
  every invariant that rotted, on the host it runs on. Pair it with a
  scheduled repo-side check — a host-local script cannot report that its own
  host is unreachable, so GitHub must be the vantage point for that half.
  → *Repair:* never hand-edit a hook URL. Shipyard's registrar owns hook
  lifecycle and re-patches on restart:

  ```sh
  shipyard daemon refresh --repo OWNER/NAME --repo OWNER/OTHER
  ```

  Four traps around that command, all observed the same day:

  1. **`gh` is not on the daemon's PATH.** It lives in `/opt/homebrew/bin`,
     which a **non-interactive** shell omits — so `ssh host 'shipyard daemon
     refresh …'` starts a daemon that logs `gh CLI not found on PATH` forever
     and registers nothing. Export a PATH containing `/opt/homebrew/bin` before
     refreshing, and note the daemon inherits whatever you gave it.
  2. **Registration needs a verified tunnel.** `refresh` restarts the tunnel, so
     status immediately after reports `tunnel=inactive · repos=—`. That is
     normal. Re-running `refresh` to "fix" it restarts the tunnel again and
     resets the clock — wait, do not retry.
  3. **A renamed repo fails permanently.** A stale owner (`old/repo` after an
     org move) makes GitHub answer `301/307 Moved Permanently`, and the
     registrar's PATCH/POST do not follow redirects. The daemon retries forever.
     Re-register with the current `OWNER/NAME`.
  4. **The registrar only manages hooks it created** (tracked in
     `daemon/registrations.json`). A hook from a renamed node is an untracked
     **orphan** that keeps failing and duplicates a working one. After a repair,
     list `repos/OWNER/NAME/hooks` and delete any URL that is not some daemon's
     current tunnel URL.

  `shipyard daemon status` prints the live tunnel URL and `repos=…`. A daemon
  registered for a *different* repo answers the request and ignores the events,
  which looks healthy from outside.

- **Before configuring Tailscale Funnel, check whether you need it at all.**
  Funnel exists to accept **public internet ingress**, which is what GitHub
  webhook delivery requires. Shipyard validates the payload HMAC, and Funnel
  exposes one path rather than the host — but it is still a remotely reachable
  service on a machine holding source and credentials.
  Weigh that against what push delivery actually buys: tartci's own demand
  detection (`queue_scan.py`, `providers/*/runner.sh`) is **pure polling with no
  webhook dependency**, and a fleet ran for 41 days with one receiver dead and
  another host with Funnel never configured at all, with no observed
  consequence. If nothing consumes the events (`shipyard daemon status` showing
  `subscribers=0` is the tell), poll-only is both simpler and strictly safer.
  When push latency is genuinely needed, prefer a hosted relay — a small public
  endpoint that verifies the HMAC and queues events for hosts to **pull** —
  over exposing a workstation.

- **Three idle macOS runners, and the merge queue still lands nothing for hours.**
  Observed 2026-07-28: fleet gate concurrency was **1** while two hosts' runners
  sat `busy=false`, and no PR merged for five hours.
  → *Cause:* GitHub assigns a job only to a runner advertising **every** label
  the job requests. Pulp's required gate asks for `…,pulp-build,pulp-build-vm`;
  two hosts' gate supervisors advertised `…,pulp-build,pulp-build-studio`. Those
  supervisors are then *structurally blind* — `queue_scan.py --match-labels 1`
  correctly finds nothing they can serve, so they log `queued=0` forever and
  boot no VM. The scanner is honest; the labels are wrong.
  → *Diagnose:* count **eligible** runners, never idle ones, and compare the
  supervisor's advertised set against the required set:

  ```sh
  ghapp api repos/OWNER/REPO/actions/variables/PULP_LOCAL_MACOS_RUNS_ON_JSON --jq .value
  grep -o 'self-hosted,macOS[a-zA-Z0-9,.-]*' \
    ~/Library/LaunchAgents/com.danielraffel.pulp.tart-runner-macos-gate.plist
  ```

  `scripts/fleet_preflight.py` does this as `gate-labels-match-required`.
  → *Fix:* correct **both** label sites in the plist (`ProgramArguments --labels`
  *and* `TARTCI_RUNNER_LABELS`) to exactly the required set. `runner.sh` passes
  one `$LABELS` to both the queue scan and the JIT registration, so visibility
  and assignability are fixed atomically — that identity (scan set == registered
  set == the workflow's requested set) is what the design assumes.
  → *Do not* add advisory labels (`*-secondary`) to a gate registration: GitHub
  may then hand the VM an optional job, and a JIT runner cannot be retargeted
  after registering. And do not "fix" it from the repo side by pointing the
  required check at `pulp-build-studio` — that routes the gate to persistent
  bare-metal runners with warm build dirs (the ODR class).

- **An organization runner is online/idle, but the repository runner list is
  empty and a PR-head job never assigns.**
  → *Cause:* organization-level JIT creation proves only that GitHub accepted
  the runner group ID. It does not prove the selected repository can see that
  group. Treating the org runner list as capacity produced exactly this phantom
  on M5: group 3 was online/idle while Pulp's repository runner endpoint could
  not see or assign it.
  → *Fix:* both Pulp event classes register through repository group 1 with the
  exact `pulp-build-pr-head` or `pulp-build-merge-group` class. Organization
  group 3's `build.yml@refs/heads/main` restriction cannot admit a merge-group
  workflow evaluated under `gh-readonly-queue/...`. Before every remaining
  organization-scoped JIT mint, TartCI freshly checks group
  visibility and the complete selected-repository list. Unknown/inaccessible
  policy records a contract-keyed denial and boots no further VM for that class.
  The final order is required Shipyard admission-clean → repository-access proof
  → pool lock and assignment/admission rechecks → JIT mint. Do not use an online org row or
  `busy=false` as repository capacity evidence.

- **Each queue query works alone, but several healthy lanes become scan-blind together.**
  Observed 2026-09-01 on M1: an isolated serialized assignment scan completed,
  while concurrent supervisors repeatedly timed out individually valid `ghapp`
  calls. Per-namespace discovery locks did not help because Pulp, Forge, release,
  and sanitizer lanes use distinct repository/workflow namespaces.
  → *Invariant:* all TartCI providers on one host share the host-global queue
  observation lock (`~/.tartci/state/queue-observation.lock` by default).
  Assignment lifecycle discovery for a running JIT VM uses that same lock and
  permits only one API worker. Otherwise a single current-job scan can fan out
  across every in-progress run and starve the admission scanners it is meant
  to complement. Lock contention and deadline exhaustion remain typed,
  fail-closed observations; they never become proof that the queue is empty or
  that a job is terminal.
  Namespace locks still coalesce identical scans; the host lock serializes only
  cache-miss GitHub observation bursts across different namespaces.
  → *Failure behavior:* lock acquisition is bounded by
  `TARTCI_QUEUE_OBSERVATION_LOCK_TIMEOUT_SECS` (120 seconds by default). The
  exhaustive assignment scanner's total deadline is 180 seconds by default,
  leaving a serialized waiter time to perform its own scan after the lock opens.
  Timeout
  is scan-blind/fail-closed: do not report zero demand, publish partial cache
  state, or start a lower-priority VM. The supervisor retries normally.
  → *Do not* fix this by independently increasing every lane's worker count or
  API timeout. The profile-owned exception is a measured event-class lane:
  after the host lock proves there is only one scan owner, it may set
  `assignment_scan_max_workers` from 1 through 4 so a large live queue remains
  exhaustive inside the total deadline. This bounds the whole host burst, not
  each overlapping supervisor independently. Current-job lifecycle discovery
  remains one worker.
  A host whose production traces prove individual GitHub App calls can exceed
  the generic 15-second subprocess limit may declare the bounded
  `host.github_api_timeout_seconds` value (5 through 60). The renderer applies
  it uniformly to every managed lane as `TARTCI_GH_TIMEOUT_SECS`; do not patch
  individual live plists. M1 retains 30 seconds because its pre-immutable live
  configuration and concurrent-supervisor measurements require that margin.
  Override `TARTCI_QUEUE_OBSERVATION_LOCK_FILE` only when every provider on the
  host is explicitly pointed at the same replacement path.

- **`migrate_macos_gate_agent.sh` can leave a host with NO gate agent at all.**
  A run that ends `legacy label remains loaded; refusing replacement startup` →
  `migration failed; restoring prior LaunchAgent configuration` →
  `ROLLBACK FAILED: Legacy agent … was not restored` leaves the legacy agent
  **unloaded and unreplaced**. The host then contributes zero gate capacity.
  → *Why it hides:* `tartci pool status` still lists the agent (as `stopped`),
  so a before/after comparison of that output looks identical. Verify
  *capability*, not presence: `launchctl print gui/$(id -u)/com.danielraffel.pulp.tart-runner`
  and whether any runner with `pulp-build-vm` exists.
  → *Recover:* `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.danielraffel.pulp.tart-runner.plist`.

- **Older `tartci launchd reload <label>` could report FAILED and leave the
  agent down.** `bootout` may return before a LaunchAgent's `ExitTimeOut`
  teardown completes, so an immediate bootstrap races the still-loaded job.
  Current TartCI reads the loaded job's effective timeout from `launchctl
  print`, adds a termination margin, proves absence before bootstrap, and
  fails closed if teardown exceeds that allowance. If operating an older
  deployment, wait for absence explicitly before bootstrapping the plist and
  always confirm the final service is loaded.

- **`reserved_gate_cores` is a floor, not a ceiling.** Easy to misread and get
  backwards. In `leases.py`, a gate-priority lease is limited by `cfg["total"]`;
  only *non-gate* leases are limited by `total - reserved_gate_cores`. So on a
  28-core host with `reserved_gate=14`, two 12-core gate VMs (24 ≤ 26) are both
  admitted — the 14 exists to stop non-gate work from crowding the gate out, not
  to cap the gate at 14. Note also that the release lane advertises
  `pulp-build-vm-release*`, which are *different label strings* from
  `pulp-build-vm`: an idle release VM cannot absorb gate work.

- **The queue tick is running every five minutes but arms or merges nothing.**
  → *Cause:* full-live Shipyard execution was launched without
  `SHIPYARD_QUEUE_REPO_ROOT` or `SHIPYARD_QUEUE_AUTHORITY=1`, so the control
  plane exits unhealthy and takes no GitHub action.
  → *Fix:* repair the single authority's environment and alert on that
  configuration error. Do not treat repeated `merged=0` as proof the queue is
  healthy, and do not add Orchard as a fallback scheduler.

- **A newly booted VM runs an optional job instead of the required gate.**
  → *Cause:* GitHub chooses among all queued jobs matching the runner's labels;
  Tart CI cannot retarget a JIT runner after registration.
  → *Fix:* use distinct required/advisory class labels. Let Shipyard safely
  coalesce superseded runs before capacity is offered; never dequeue/requeue a
  PR merely to change its position.

- **An old Linux provider process remains after its recorded owner is gone.**
  → *Cause:* current doctor output does not yet classify every legacy
  host-process generation with enough ownership evidence for safe deletion.
  → *Fix:* treat this as a rollout follow-up. Do not kill by age, command name,
  or a stale heartbeat alone; an active long job can have a fresh lease/PID
  while its supervisor heartbeat looks stale. A future owner-aware classifier
  may remove an orphan only when all of these are proven together: stale state
  heartbeat, VM absent from Tart, GitHub runner online but idle, recorded owner
  PID alive but not the currently loaded LaunchAgent supervisor/service owner,
  and exact post-cleanup verification. Until that classifier exists, inspect
  the two old-process candidates manually and take no automated action.

- **Doctor warns about a stale heartbeat while the same runner is busy.**
  → *Cause:* the early state-row check can warn before the later GitHub and
  lease observations prove that a long job still has a live owner, fresh lease,
  and busy runner.
  → *Fix:* do not clean it. Suppressing this false positive is a rollout
  follow-up: the final classification must clear the warning only when the
  same runner identity is busy and its current lease/PID ownership is fresh.

## Windows (QEMU)

- **Install media won't boot — BCD `0xc000000d` (\EFI\Microsoft\Boot\BCD).**
  → *Cause:* Win11 **25H2** ARM install media fails under edk2 across every
  AVF/QEMU permutation — a media/version incompatibility, not config. → *Fix:*
  use the **24H2** ARM ISO.

- **Microsoft download page blocks the ISO download (anti-VPN).**
  → *Cause:* downloading via a Tailscale/VPN IP is blocked (code 715-…). → *Fix:*
  build the 24H2 ISO with **UUP dump**'s macOS converter (pulls from the Windows
  Update CDN). `chntpw` won't build on Apple Silicon → **stub it (no-op)**; the
  autounattend handles the registry bypass it would have done.

- **Boot splash hangs / black display during WinPE.**
  → *Cause:* a virtio-gpu display has no WinPE driver. → *Fix:* use **`-device
  ramfb`** for the display, not virtio-gpu.

- **Setup never finds autounattend.xml (0 bytes written / install stalls).**
  → *Cause:* install media on virtio-scsi — WinPE can't read it. → *Fix:* put
  **ALL install media on `-device usb-storage`**, not virtio-scsi. (Also: NVMe
  system disk, since Win-ARM has no inbox virtio-blk driver.)

- **Reboots drop into the UEFI shell instead of Windows.**
  → *Cause:* no boot entry in the firmware's fallback path. → *Fix:* after image
  apply, `mountvol S: /s` then copy `bootmgfw.efi` → `\EFI\Boot\BOOTAA64.EFI` so
  the ESP self-boots (verified SSH-back in ~15 s).

- **MSVC Build Tools installer exits 0 but installs nothing (no `cl.exe`).**
  → *Cause:* a partial/previous VS install makes the installer silently no-op
  (resolves the workload to an empty set, 0-byte error log). → *Fix:* fully nuke
  `BuildTools` + `Packages` + `Setup`, **reboot**, then clean-install. **Verify
  `cl.exe`** under `VC\Tools\MSVC\<ver>\bin\Hostarm64\arm64` — never trust the
  exit code. Prefer an offline VS layout / host-side cache for reproducibility.

- **Provisioning batch files mis-execute when scp'd over.**
  → *Cause:* `.cmd` files run over OpenSSH's `cmd.exe` default shell execute
  unreliably. → *Fix:* run commands **directly** as `ssh pulp-win '<cmd>'`.

- **Complex PowerShell over SSH gets quoting / `%` / `>` mangled.**
  → *Cause:* the cmd.exe default shell mangles special chars. → *Fix:* use
  `powershell -EncodedCommand <base64-utf16le>` (encode the script UTF-16LE +
  base64). Also note: vncdotool mistypes some shifted chars like `>` — fine for
  `:` and `\`.

- **Tests fail trying to use `/tmp/...` paths.**
  → *Cause:* tests use POSIX `/tmp/...`, which resolves to `C:\tmp` on Windows,
  which doesn't exist by default. → *Fix:* create `C:\tmp` before running ctest.

- **`cl` hangs on a translation unit mid-build.**
  → *Cause:* arm→x64 emulation can transiently stall a TU. → *Fix:* kill it and
  resume the build — Ninja is incremental and picks up where it left off.

- **ctest run over SSH behaves oddly (harness quirk).**
  → *Cause:* the test harness assumes interactive/POSIX shell behavior the
  cmd.exe-over-SSH session doesn't provide. → *Fix:* drive ctest via the direct
  `ssh pulp-win '<cmd>'` path (not scp'd scripts), apply the CI label exclude set,
  and ensure `C:\tmp` exists.

## QEMU firmware (edk2)

- **VM drops to the UEFI Shell instead of booting the firmware menu.**
  → *Cause:* edk2 needs the **vars TEMPLATE** (not a zeroed vars file). → *Fix:*
  use the populated `edk2-arm-vars.fd` template.

- **pflash rejected / firmware won't load.**
  → *Cause:* QEMU pflash vars must be **64 MiB**; UTM's `edk2-arm-vars.fd` is
  ~329 KB. → *Fix:* pad the vars file to 64 MiB.

## Compile-time portability (when building the product on Windows)

- **`unistd.h` not found / POSIX shim missing on Windows.**
  → *Fix:* provide a `unistd.h` shim for the MSVC build.

- **Macro collisions near `windows.h` (e.g. `min`/`max`, other macros).**
  → *Cause:* `windows.h` defines macros that clobber identifiers. → *Fix:*
  `#undef` the offending macro right after the `windows.h` include (or define
  `NOMINMAX` where applicable).

## GitHub runner ownership versus local TartCI state

- **GitHub reports an ephemeral runner `busy`, but TartCI reports no VM or
  lease.** → *Cause:* a lost supervisor can leave a GitHub registration and
  in-progress job row behind. → *Fix:* run `tartci doctor --reap --json` twice
  across a bounded interval, capture the runner/job IDs and local evidence, and
  classify the row as live-owner, unconfirmed, or
  `offline_busy_orphaned_no_local_owner`. Preserve protected-queue work while
  an owner is possible. Only the exact documented recovery may cancel that
  exact stale job and remove its registration; do not bulk-reap busy runners.

- **A new Vellum worktree appears to need a new runner.** → *Cause:* confusing
  checkout identity with repository/fleet identity. → *Fix:* reuse the Vellum
  profile's stable prefix, group, labels, lease, and hosted fallback. A
  worktree change is not a runner registration event. Require a fresh
  assignment and teardown proof after recovery before changing selectors.

- **The guest can reach `github.com`, but GitHub marks its Runner.Listener
  offline.** → *Cause:* the Actions broker/control-plane endpoint was blocked
  or reset; repository-web access alone is not sufficient. → *Fix:* test the
  resolved `pipelines*.actions.githubusercontent.com` endpoint and
  `broker.actions.githubusercontent.com` over HTTPS from the guest, record
  both in the proof, and keep hosted fallback enabled until they pass.
