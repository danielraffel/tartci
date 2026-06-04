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
