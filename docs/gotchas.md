# tartci gotchas — symptom → cause → fix

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

- **`ssh <host> 'tart list'` says `command not found`, but Tart is installed.**
  → *Cause:* non-interactive SSH sessions often do not load Homebrew's PATH.
  → *Fix:* configure fleet tools with the absolute Tart path, usually
  `/opt/homebrew/bin/tart`, and pass the intended store explicitly as
  `TART_HOME=/Users/<you>/VMs` (or the host's absolute Tart store path).

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

- **All three Macs look healthy, but the required front job is still queued.**
  → *Cause:* runner process health is not useful-progress health. A bounded
  scanner can legitimately report `queued=0` for its current window, or GitHub
  can assign an optional job that shares the required job's labels.
  → *Fix:* inspect `shipyard runner fleet-status --repo OWNER/REPO --json`,
  configure M1/M3/M5 gate supervisors with
  `TARTCI_VM_LEASE_PRIORITY=gate`, and separate required-gate labels from
  advisory labels.

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
