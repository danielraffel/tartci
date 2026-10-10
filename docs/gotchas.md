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

So that agents running `ssh host tartci ...` find it, every fleet host puts
`~/.local/bin` on PATH in `~/.zshenv`, the only startup file a non-interactive
zsh reads. `~/.zshrc` and `~/.zprofile` do not count: m3 had it in both and
`ssh m3 'command -v tartci'` still returned nothing (2026-10-05). Use the same
guarded line on every host:

```sh
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac
```

Check it from another host with `ssh HOST 'command -v tartci'`, and run the
same check against a host known to work as the control.

## `fleet-macos install` reports "installed support member failed verification"

**Symptom:** `tartci fleet-macos install PROFILE` run through the installed
`~/.local/bin/tartci` fails with
`support-manifest: installed support member failed verification: fleet/README.md`,
while the running supervisors are healthy.

**Cause:** with no `--support-source`, the installer used its own root as the
source. Through the installed entrypoint that root is a sealed generation under
`~/.local/share/tartci-generations/`: its files are mode 0444 and it is not a
git checkout, so it can never be a support source. The mode mismatch was the
first thing to trip. Nothing on the host is broken.

**Fix:** the installer now refuses this case with a message that says so. To
update a host, run `tartci fleet-macos self-update`. To install a specific
tree, pass `--support-source` with a clean tartci checkout.

## A support generation will not install on macOS 15 (2026-10-05)

*Symptom:* `stage_install` (the self-update install step) fails with
`PermissionError: [Errno 13]` renaming the staged generation into place.
*Cause:* macOS 15 refuses to rename a directory whose own mode is 0555, even
within the same parent; macOS 27 allows it. The installer used to seal the
staged root to 0555 before the rename. Found on hosted macos-15 (15.7.9); every
fleet host runs 27. *Guard:* the root is renamed while 0755, then chmodded 0555,
fsynced and verified immutable under its final name; a generation that fails
that check is removed rather than left where the next install would refuse it.
`test_tartci_support_manifest` records the mode at the rename, so the order is
checked on every host.

## Timer jobs stop running while the lanes look healthy (m3, 2026-10-04)

**Symptom:** a host falls many commits behind main and its self-update log has
not changed in hours, yet `launchctl print` shows the agent with `last exit
code = 0` and the watchdog prints a checkmark for it. The lane supervisors keep
serving jobs.

**Cause:** launchd's gui domain stopped starting StartInterval jobs on its own.
`launchctl print gui/$UID/<label>` shows `pended nondemand spawn = interval`
and a `runs` counter that does not move. A clean last exit says nothing about
whether the job still runs. On m3 the last update attempt had been correctly
refused (a peer was draining), and launchd never started the retry, so the fix
for this stall could not install itself.

**Fix:** `scripts/launchd_interval_guard.py` runs inside the lane supervisors
and kicks any timer agent whose run count has not moved for twice its interval.
A host on a generation from before that guard needs one manual
`launchctl kickstart gui/$UID/com.danielraffel.tartci.self-update`; after that
it heals on its own. To spot it, compare the age of the newest entry in
`~/Library/Logs/tartci/tartci-self-update.log` with the 30-minute interval,
not the exit code.

The one agent the guard does not kick is self-update, for as long as the stall
lasts (`PAUSE_DURING_STALL`): an update stops every supervisor, so no guard
would run and any lane restart left to launchd would pend. Self-update then
writes no log line and no attempt for days (m3 from 2026-10-05, 44 commits
behind). `tartci fleet-macos self-update --status` and `tartci doctor fleet`
say so (`self_update_paused`, with the stall's start) rather than reading like
a failed update. The remedy is the stall's: reboot when the lanes are idle.
Do not kickstart self-update during the stall.

Hard-won, one bullet each. Grouped by lane. If a build/install behaves
inexplicably on a fresh Apple Silicon host, the answer is almost certainly here.

## A frozen guest holds its slot for two hours (durability audit, 2026-10-09)

**Symptom:** a lane reads `job-running` for up to two hours while its VM is
frozen; the watchdog's frozen-lane heal skips a host with a running VM, so
nothing frees the slot before `TARTCI_JOB_TIMEOUT_SECS` (7200).

**Cause:** the host waits on the ssh session that runs the guest's listener. A
guest that hangs can keep that TCP session open, so the host sees neither
output nor an exit.

**Fix:** the guest launcher writes `TARTCI_GUEST_HEARTBEAT <epoch>` every
`TARTCI_GUEST_HEARTBEAT_SECS` (30) into that session. Once the first one
arrives, the host tears the VM down when the listener log has been silent for
`TARTCI_GUEST_HEARTBEAT_STALE_SECS`: it records `guest_heartbeat_stale`,
cancels an assigned job's run, and frees the slot. Every lane in the shipped
profiles sets it with `guest_heartbeat_stale_seconds = 600`. Before the first
heartbeat (runner still starting, or a guest launcher from an older
generation) silence proves nothing and the idle and job timeouts apply.
Heartbeat lines are dropped from the runner log the host echoes.

## A gate lane sits in `booting` for over an hour with a stale heartbeat (m1, 2026-10-09)

**Symptom:** `tartci doctor fleet` reports `fleet_not_ready` with
`heartbeat_stale`, while the host still serves jobs on its other lanes. The
stale lane's state file shows `phase: booting`, its VM has an IP
(`boot_ip` in the lane's `events.jsonl`), there is no actions-runner log for
the VM, and `ps` shows the supervisor running one `ssh admin@<vm-ip> true`
after another, each lasting about a minute.

**Cause:** the boot helper waited for the guest's sshd with 90 attempts and a
2 s sleep, meant as 180 s. `ConnectTimeout=10` bounds only the TCP connect,
so a guest that accepts the connection but never finishes the handshake holds
each attempt for about 60 s. The 90 attempts then take about 93 minutes, and
nothing writes a heartbeat meanwhile. On m1, `pulp-gate.slot2` sat there for
71+ minutes.

**Fix:** the wait is bounded by wall-clock time. Each attempt is killed at
`TARTCI_BOOT_SSH_ATTEMPT_SECS` (default 15) and the whole wait ends at
`TARTCI_BOOT_SSH_DEADLINE_SECS` (default 180), after which the VM is discarded
with `boot_failed no_ssh` and `waited_s`/`attempts` fields. Each failed
attempt refreshes the boot phase's heartbeat, which is honest because the wait
has a hard end. The doctor's `fleet_not_ready` line now names each problem's
lane and heartbeat age, for example
`heartbeat_stale (m1.pulp-gate.slot2, heartbeat 71m old)`.
A host on a generation from before this fix discards such a VM only after the
attempts run out; it heals once it self-updates.

## A `while read` loop ends early after a peer read over ssh (2026-10-04)

*Symptom:* the supervisor observed only the first class with young demand;
every later class in `while read ... done <<< "$classes"` went unobserved, with
no error. *Cause:* an ssh client forwards its stdin to the remote command, so
an ssh anywhere under the loop body drains the rest of the loop's input. In
#371 the ssh was inside a Python helper (`gate_supply.py decide`), which
inherits stdin. shellcheck's SC2095 (run by `scripts/lint.sh`) catches only an
ssh written directly in the loop; it cannot see through a function or a
subprocess. *Guard:* `scripts/ssh_stdin_check.py`, run by
`scripts/test_ssh_stdin.py` in CI, requires every ssh invocation in the repo
to state its stdin, wherever it is:

- shell: `ssh -n`, an input redirect on the same command (`</dev/null`,
  `<<EOF`, `< file`), or ssh as the right side of a pipe (`ssh -G` is exempt);
- Python: an argv list or tuple whose first element is the ssh client must
  contain `"-n"`. That is the string `"ssh"` or a path ending in `/ssh`; a
  name or attribute named like the client (`ssh`, `args.ssh`, `self.ssh_bin`,
  `ssh_path`, `remote_ssh`); or a name, parameter or argparse option whose
  value or default is such a string. The rule first matched only `"ssh"` and
  a bare name `ssh`, so `[args.ssh, "-o", ...]` in `job_claim.gather_peers`
  passed it without `-n`.

A wrapper whose callers pipe a script into it is the one legitimate exception;
mark it `# ssh-stdin: <why>` on its line or the line above.

## A test passes in CI and fails on the hosts' Python (2026-10-05)

*Symptom:* a change is green in CI, then its tests fail under
`/usr/bin/python3` with `No module named 'tomllib'` (#379, #390, #383, #391 on
one day). *Cause:* `/usr/bin/python3` is 3.9.6 on all four hosts and has no
tomllib. It runs the tartci shim's support-manifest check, the pinned launch
interpreter, every explicit `/usr/bin/python3` call site, and each
`tartci_toml_exec_or_python3` fallback on a host without a tomllib Python;
interactive ssh shells on m1 also resolve `python3` to it. (Under launchd's
PATH a bare `python3` is a Homebrew 3.11+ on all four hosts.) CI ran the tests
only on ubuntu's 3.12+ (`python-floor` only compiles, under 3.11); on
2026-10-05 main itself failed 203 of 2283 tests under 3.9. *Guard:* the
`python-39-tests` CI job runs every test module under Python 3.9 (the newest
3.9 setup-python offers; 3.9.6 itself is not built for current ubuntu
images), asserted to be 3.9 with no tomllib and first on PATH. A test that genuinely needs tomllib
says so through `scripts/testing_support.py` and is skipped there, and every
module that degrades without tomllib has a running test of that branch
(`scripts/test_no_tomllib_fallbacks.py`):

```python
import testing_support
testing_support.skip_module_without_tomllib()   # whole module, before its imports

@testing_support.requires_tomllib                # one test or class
def test_reads_the_profile(self): ...
```

Import a tomllib-only module (`macos_fleet_lanes`, `fleet_self_update`, ...)
inside the test that needs it, not at module level, so the module's other
tests still run on the hosts' Python.

That skip is not available everywhere. For a module a 3.9 interpreter runs
(reachable by import from an explicit `/usr/bin/python3` site, or declaring
itself "3.9-safe"), a skip on 3.9 removes exactly the coverage the job exists
for, so `scripts/test_system_python_tests_run.py` fails on any
tomllib-conditional skip in that module's tests unless it is listed in
`ALLOWED` with the 3.11-only behaviour it guards. Make the test run on 3.9
first; list it only when what it asserts really needs tomllib.

## M3 external-volume privacy attribution (2026-09-01)

- **System Settings repeatedly asks about Bash, Node, Python, or `env`, while
  M3 runner LaunchAgents fail or leave `/Volumes/Workshop/VMs` idle.**
  → *Cause:* launchd started `/bin/bash` directly. macOS attributed external-
  volume access to mutable/shared interpreter identities rather than to TartCI;
  static runner labels and free VM slots could then look healthy while the
  responsible process was denied. Live TCC records named `/bin/bash` and
  `/usr/bin/env`, while interactive Tart runs were attributed to the signed
  terminal app. This is SYSTEMIC: every regenerated Bash supervisor can repeat
  it. → *Fix:* M3 alone uses the stable Developer-ID
  `com.danielraffel.tartci.launcher` app, whose signature seals the exact
  support cohort and lane environments and whose interface accepts no arbitrary
  executable, argument vector, or store path. Its exact identity is profile-
  and receipt-bound. `pool on` proves launchd-context write/read/delete access
  before admission. Never grant broad interpreter access, edit
  TCC state, or fall back to Bash plus `/Volumes`; use `$HOME/VMs` until the
  signed path passes. The mechanism is falsifiable: wrong signature/digest,
  denied access, timeout, or failed child cleanup keeps the pool off.

  **Resolution:** implemented by the signed-launcher change; rollout remains
  incomplete until its PR lands and M3 passes the naturally idle two-slot JIT
  canary without another interpreter prompt. Confidence is HIGH for the
  diagnosis and pre-admission detector (live attribution plus focused tests),
  MEDIUM for final TCC durability until that replacement-build canary runs.

## A webhook 403 has three causes and only one is a credential (2026-09-21)

- **`shipyard-daemon-health` clears the token cache and refreshes every five
  minutes until it hits its escalation limit, and the daemon still cannot
  register its webhook.** → *Cause:* the GitHub App installation is missing
  `repository_hooks`. GitHub reports that as HTTP 403 "Resource not accessible
  by integration", which is byte-for-byte as much a 403 as a dead credential —
  so the watchdog applied the credential remedy to a credential that was
  working perfectly, failed identically every cycle, and reported nothing an
  operator could act on. Three different faults arrive as 403: a missing App
  permission (a human must grant it), a classic token missing
  `admin:repo_hook` (a one-time `gh auth refresh`), and a genuinely dead or
  anonymous credential (the only one worth clearing anything for). → *Fix:* the
  watchdog now classifies before it heals, and answers a permission fault by
  escalating and touching nothing. Never read "403" alone as "rotate the
  credential".

- **Every webhook delivery fails to connect and no alarm fires anywhere.** →
  *Cause:* this host's tailnet name changed — Tailscale re-registers a
  duplicate node under a `-N` suffix and the old name stops resolving — while
  the registered hook kept the old name. The daemon printed a correct tunnel
  URL and GitHub served a correct hook record; each side was individually
  truthful and nobody compared them. Nothing consumed the feed either, so its
  failure had no symptom. → *Fix:* `shipyard daemon reconcile` performs the
  comparison (exit 0 in sync, 1 warn, 2 alarm, 3 blocked on a human) and the
  watchdog routes on it. When diagnosing by hand, read the host's own identity
  from `tailscale status --json` → `.Self.DNSName` (strip the trailing dot).
  The CLI is **not** on a non-interactive PATH, so `command -v tailscale`
  returns empty on a perfectly healthy host — resolve
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale` explicitly and treat a
  failure to read the identity as UNKNOWN, never as "no drift".

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
  `/usr/bin` on the runner's PATH. Fleet profile/readiness commands also resolve
  a TOML-capable interpreter explicitly; set `TARTCI_PYTHON` to an absolute
  Python 3.11+ path in the host's launchd environment when the default
  Homebrew locations do not apply. Verify by *parsing the config*, not by
  checking the file exists.

- **`ssh <host> 'tart list'` shows no gate VMs while the lanes are running
  them.** → *Cause:* the lanes' LaunchAgents set `TART_HOME` to the host's
  store (on m1 and m5 `/Users/<you>/VMs`, on m3 `/Volumes/Workshop/VMs`); a
  shell over ssh has none, so `tart` reads its default `~/.tart`. Before
  2026-10-09 tartci's own commands did the same: over ssh to m1, `tartci
  doctor --reap --json`, which Shipyard's fleet health probe runs, reported 0
  running VMs and 2 free slots while two gate VMs ran. → *Fix:* `tartci
  doctor` and `tartci observe` now export the fleet profile's
  `[host].tart_home` when the shell has no `TART_HOME`, and print the store
  they read and where it came from (`tart store: /Users/<you>/VMs
  ([host].tart_home from ...)`); the reap digest carries it as
  `config.tart_home`. A shell `TART_HOME` that differs from the profile's is
  kept but flagged. For raw Tart over ssh, pass the store yourself:
  `ssh <host> 'TART_HOME=/Users/<you>/VMs /opt/homebrew/bin/tart list'`.
  `python3 scripts/tart_home.py` prints what a command on that host resolves.

- **`ssh <host> 'tart list'` says `command not found`, but Tart is installed.**
  → *Cause:* non-interactive SSH sessions often do not load Homebrew's PATH.
  → *Fix:* TartCI's watchdog/network-profile inventory probe resolves
  `TARTCI_TART_CLI` first, then PATH, then the canonical
  `/opt/homebrew/bin/tart` and `/usr/local/bin/tart` locations. Set
  `TARTCI_TART_CLI` to an absolute path for a nonstandard install, and pass the
  intended store explicitly as `TART_HOME=/Users/<you>/VMs` (or the host's
  absolute Tart store path). When `TART_HOME` is absent, the probe reads
  `[host].tart_home` from the installed macOS fleet profile; it never silently
  inspects Tart's unrelated default store. Treat
  `installed but unreachable from this launch environment` as a distinct
  status from `running`, `idle`, or `absent`; never infer any of those from
  ambient `command -v` alone. `tartci launchd status --json` reports the
  resolved executable and a typed `running|idle|unavailable` probe state.

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

- **Undefined symbol at link, on ONE host only, for a function whose definition
  is plainly compiled into a linked archive.** (m3, 2026-09-26: every Pulp
  `macos` gate on a `studio-*` runner failed linking `pulp-osc-render-wav`;
  m1/m5 built the same commit green.)
  → *Cause:* the shared host ccache held direct-mode manifests that list ZERO
  include files but name another source's result (the object for
  `audio_doctor.cpp` served for `wav_bridge.cpp`). With no includes to
  re-check, such a manifest matches every lookup, so the archive silently
  contains the wrong object. How they were written is unproven (concurrent
  writers or a VM torn down mid-write over virtio-fs are the suspects).
  → *Detect:* `tartci ccache scan --host-cache` (read-only). By hand:
  `ccache --inspect <entry>` shows `Entry type: 1 (manifest)` and
  `File paths (0)`; the raw magic bytes are `cc ac`, not ASCII "ccac". A
  zero-include manifest is NOT proof of poison: Pulp compiles ~94 include-less
  TUs (generated `*_control_shipping_marker.cpp`, `placeholder.cpp`), and m1/m5
  each hold ~5,000 legitimate ones. The discriminator is the result it names:
  `ccache --extract-result` it and read the `.d`; a list of headers (or a
  missing result) means the manifest cannot describe that object. The scan
  reports these as `zero_include_suspect` and the rest as
  `zero_include_consistent`. Blind spot: a poisoned manifest that names
  ANOTHER include-less TU's object looks consistent; `--all-zero-include`
  (and `reset`) quarantine those too, at one preprocessor-mode lookup per
  include-less TU on the next build.
  → *Clean:* `tartci ccache quarantine --host-cache` renames suspects into
  `<cache>-quarantine/<stamp>/` (never deletes; `guard.log` keeps both counts;
  consistent verdicts are remembered in `verdicts.json`). It scans the legacy
  root and `tartci-layers-v1/shared`, never the per-job `jobs/`, `green/` or
  `discard/` layers.
  `tartci ccache reset --reset` moves the whole cache aside; it refuses while
  a VM runs or holds a lease unless `--force`. The macOS runner runs the
  quarantine before every VM boot (fail-open, `TARTCI_CCACHE_GUARD=0` disables;
  events `ccache_guard` in the lane's event log). A budget-exhausted run
  records where it stopped in `cursor.json` and the next run resumes there;
  without that, a cache too large for the budget had its late fan-outs
  checked only by the runs that happened to finish.

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

- **A `launchctl bootstrap` of a present, untouched runner plist fails with
  `Bootstrap failed: 5: Input/output error`.**
  → *Cause:* the service is `disable`d in the launchd user domain, and
  `bootstrap` will not load a disabled service. The error names neither the
  service nor the cause, survives a settle and a retry, and therefore reads as a
  transient I/O fault. Confirm with `launchctl print-disabled "gui/$(id -u)" |
  grep <label>`, which is the only surface that says so.
  → *Fix:* enable first, then bootstrap — a bare bootstrap cannot work:

  ```sh
  launchctl enable    "gui/$(id -u)/actions.runner.OWNER-REPO.RUNNER-NAME"
  launchctl bootstrap "gui/$(id -u)" \
    "$HOME/Library/LaunchAgents/actions.runner.OWNER-REPO.RUNNER-NAME.plist"
  ```

  → *How a service got disabled without anyone disabling it:* `tartci pool off`
  and `tartci pool drain` used to `launchctl disable` every runner agent on
  disk, while `pool on` re-enables only the services the fleet receipt names.
  Scoped since; see "a pool transition stops only what it can start" below.

- **A pool transition stops only what it can start.**
  `pool on` activates exactly the services the installed fleet receipt names and
  refuses to start unreceipted persistent or legacy runner services, which carry
  their own install authority. `pool off` and `pool drain` are scoped to that
  same set, and name on stdout every runner agent they deliberately left alone.
  → *Why:* the unscoped version took down a foreign repository's persistent
  Actions runner twice (2026-09-05, 2026-09-21). Both times it was the sole
  server of a required check, both times the loss was invisible — the plist
  stayed on disk, so every file-existence check passed — and the second time it
  blocked PRs for ~18 hours. An operation that stops more than its inverse
  starts is not a pause, it is a deletion.
  → *Consequence:* on a receipted host, stopping an unreceipted runner is now a
  deliberate act with its own authority (`launchctl bootout` it directly, or
  uninstall it through whatever installed it). `tartci pool status` marks which
  runner agents are outside the receipt, in text and under `pool_owned` in
  `--json`.

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
  scripts/runner_census.py --repo OWNER/REPO --label pulp-build-vm --json
  ```

  → *Fix:* add gate-eligible capacity, or accept the concurrency. Do **not**
  raise the merge queue's `max_entries_to_build` to compensate: extra entries
  contend for the same eligible runners and the wait simply moves from GitHub's
  queue into the host lease store.

- **A runner census counts only half the fleet.** `repos/<owner>/<repo>/actions/
  runners` lists repository-registered runners and omits organization-registered
  ones; `orgs/<owner>/actions/runners` lists the other half. Neither endpoint
  says the other exists, so a single-scope census answers "how many runners
  serve this label" with a confident wrong number — measured on one live fleet
  as 3 at repository scope while 4 more sat at organization scope.
  → *Diagnose:* `scripts/runner_census.py --repo OWNER/REPO --label LABEL`
  reads both and prints UNREACHABLE for a scope it could not read, because a
  scope that went unread is not a scope that was empty.
  → *Fix:* decide capacity from both scopes. A zero from one endpoint is the
  dangerous reading: it looks like there is nothing to protect.
  → *User-owned repositories* have no organization scope: `orgs/<user>/...`
  is a 404 by construction. The census confirms the owner is a user account
  (`users/<owner>` type `User`) and reports that scope `n/a`, so the census
  stays complete. A 404 for an organization, or an owner type that cannot be
  read, still leaves the scope UNREACHABLE.

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
  → pool lock and assignment/admission rechecks → JIT mint. On macOS the first
  two are *started* beside the clone and boot and *consumed* in that order at
  the boundary; a stale or missing parallel result is re-asked synchronously. Do not use an online org row or
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
  `TARTCI_QUEUE_OBSERVATION_LOCK_TIMEOUT_SECS` (120 seconds by default), and
  the exhaustive assignment scanner's `TARTCI_ASSIGNMENT_SCAN_TIMEOUT_SECS`
  (180 seconds by default) budgets the scan that follows it. The two are
  sequential, not nested: the scan deadline starts when the lock is acquired,
  so a lane that queued behind four other supervisors still scans with a full
  budget, and a scan can take at most lock timeout plus scan timeout overall.
  They were nested until 2026-09-21, which made the budget
  `180 - however long this host's queue happened to be`. A waiter then ran its
  exhaustive pass on the remainder and passed that remainder to `gh` as a
  shortened per-call timeout, so the call was killed mid-pass and the lane
  reported the queue unobservable. Measured on M1 over 24 hours, 96 scans died
  on a GitHub call clamped below the lane's configured 30-second limit, with a
  median of 14.6 seconds left; the failure is invisible in the logs because it
  arrives as `GitHub API failed ... timed out`, indistinguishable at a glance
  from a slow API.
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
  A measured event-class lane may also set
  `assignment_top_tier_receipt_max_age_seconds` (at most 300). Only tier zero,
  which has no higher class to preempt it, may use its supervisor's exact recent
  exhaustive receipt across VM boot. Cancellation can then cost one bounded
  idle JIT runner, but lower tiers still require live exhaustive pre-mint proof
  and can never bypass newly arrived higher-priority work.
  Shortening `TARTCI_QUEUE_OBSERVATION_LOCK_TIMEOUT_SECS` is not a throughput
  lever. Five contending lanes were measured at 120, 60, 30 and 10 seconds and
  completed the same 12-13 scans each time, because the lock -- not the wait --
  is what rations observation; only the wasted attempts grew, from 48 to 203.
  A shorter wait does buy slightly fairer sharing, and caps how long a
  supervisor's poll can block (that ceiling is the lock timeout plus the scan
  timeout, so the 120-second default admits a 300-second poll). Choose it for
  those two properties, never expecting more scans.
  Override `TARTCI_QUEUE_OBSERVATION_LOCK_FILE` only when every provider on the
  host is explicitly pointed at the same replacement path. Tests must always
  override it: the suite runs on hosts whose lanes are scanning through the
  default path, so a test that inherits it both fails on production contention
  and adds to it.
  What the scan costs depends on the answer, not on the queue. A run listing is
  walked page by page and each page is handed straight to the job scan, so a
  matching queued job on the first page ends the scan there: the later pages,
  the `in_progress` listing, and the snapshot reconciliation that makes an
  empty listing trustworthy are never bought. Measured against the same
  fixture, presence costs 3 calls where absence costs 21. Absence is unchanged
  and still exhaustive, because only looking everywhere can establish it, so an
  idle lane still pays the full enumeration every poll. A torn listing is
  therefore fatal only when nothing matched: a scan holding a witness reports
  it rather than going blind, which on M3 is the `pagination ended before
  total_count` family, 235 of 956 blind polls.
  Workflow name to id is the one input that does not change between polls, so
  it is cached in `~/.tartci/state/assignment-workflow-ids.json` for 300
  seconds and shared by every lane on the host. That call alone was 240 of
  those 956 blind polls. A cached id is only ever spent on its own workflow's
  listing call, so an id that stops resolving fails the scan closed there and
  the cache cannot turn a broken lookup into an empty queue; the lifetime
  bounds the narrower case of a second workflow appearing under an
  already-cached display name. Set
  `TARTCI_ASSIGNMENT_WORKFLOW_ID_CACHE_TTL_SECS=0` to disable it and resolve
  the ids on every poll. Tests must override
  `TARTCI_ASSIGNMENT_WORKFLOW_ID_CACHE_FILE` for the same reason they override
  the lock path, and additionally because a cache shared between tests is a
  channel between them: one test's resolved id satisfies the next test's
  lookup, so the listing call that test exists to exercise is never made.

- **The Shipyard push feed can rescue a blind scan; it can never report an
  empty queue.** When `assignment_feed_rescue` is set on an event-class lane,
  a tier scan that failed closed asks the local Shipyard daemon's
  `workflow_job` webhook feed whether demand exists before the supervisor
  idles blind. The feed is a second opinion from outside the GitHub REST API,
  so it survives exactly the per-call timeouts that make the scan fail.
  → *The asymmetry is the whole design.* A witness on the feed proves presence
  and nothing can retract it. Silence proves nothing at all: a severed feed, an
  unregistered webhook and a genuinely empty queue are the same bytes. So the
  rescue only ever converts `ERR` into "there is demand"; every refusal leaves
  the blind verdict exactly as the scanner left it, and `scan_blind` handling —
  including the ~180s self-restart — runs unchanged.
  → *It runs only after the scan already failed*, so a healthy lane never
  consults it and no feed defect can regress one. The exhaustive caller is
  excluded: a 100-entry replay ring is not a queue census, and that caller
  wants a magnitude.
  → *Age comes from the subscriber's own first sighting, not the job.*
  Shipyard's normalized `workflow_job` payload carries no timestamp
  (`action`, `run_id`, `job_id`, `repo`, `name`, `status`, `conclusion`,
  `runner_name`, `labels`) and the daemon replays its ring unstamped, so a
  frame is undatable. The ledger at
  `$STATE_DIR/<runner>.feed-ledger.json` records when this subscriber first saw
  each job id. A webhook cannot arrive before the job was queued, so that
  measures *less* than the true wait and can only withhold a job that is in
  fact old enough — never release one that is too young. A lost ledger costs a
  delay, never a premature boot.
  → *Every outcome is announced*: `assignment_feed_rescue` on a rescue,
  `assignment_feed_degraded` with the daemon's own reason on any refusal. A
  failing feed must never look like a quiet lane.
  → *Diagnose by hand* with
  `python3 scripts/shipyard_event_feed.py --repo … --require-label … --labels …
  --min-observed-age-seconds … --ledger /tmp/probe.json`. Exit 0 means demand
  was observed; exit 3 means the feed licensed no decision. Exit 3 is **not**
  "the queue is empty", and the CLI deliberately prints no count at all rather
  than a `0` a caller could misread.

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

- **The queue tick runs every five minutes and never merges anything.**
  → *Cause:* by design. The tick is a ship-state reaper; the GitHub merge queue
  lands pull requests. Its health reason counts what it did
  (`reaped`, `open`, `stalled`, `errs`), not merges.
  → *Fix:* none needed. If the log or health names
  `legacy_full_live_ignored`, the host still carries retired full-live
  settings; re-run the installer in reap mode to drop them.

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

- **A `merge_group` run sits `queued` forever and every scan pass fetches its
  jobs.**
  → *Cause:* a dequeued merge queue entry can leave its workflow run reporting
  `queued` permanently — queue branch deleted, `jobs: []`, cancel saying
  "already completed", force-cancel saying "not queued", and delete returning
  403 to both the App and a maintainer. Nothing in the run's status
  distinguishes it from a live entry and no operation removes it.
  → *Effect, precisely:* it does **not** inflate a merge-group demand count —
  its zero jobs match no class label. It costs one extra `runs/<id>/jobs` call
  on every scan pass of every lane, permanently, and a scan fails closed when
  any single request exceeds `TARTCI_GH_TIMEOUT_SECS`, so the added call makes
  a blind scan more likely.
  → *Fix:* `StaleDemandClassifier` quarantines a `merge_group` run whose queue
  branch is confirmed absent AND which carries no queued job, on POSITIVE
  determination only — a timeout still counts the run. Full account:
  `docs/stale-merge-group-demand.md`.


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
## Worktree cleanup is not a generic low-disk hook

The optional cleanup provider is M3/Pulp-only and accepts only a fresh,
machine-readable disk-axis lease denial. Do not broaden it to old receipts,
CPU/RAM failures, mount probes, timers, arbitrary repositories, or PR scans.
Incomplete `lsof`, Git, branch, or main-fetch evidence is a stop,
not permission to force-remove a worktree.
