# tartci runbook — from-scratch setup (macOS + Linux + Windows)

This is the honest, command-first guide to standing up local CI build VMs on a
fresh Apple Silicon Mac. It mirrors the proven bring-up: **Linux + macOS on Tart
(Apple Virtualization), Windows on standalone QEMU** (AVF can't install Windows).

What's **scripted** vs **manual** is called out per section. Where a wrapper
exists (`./tartci up <os>`), use it; where it doesn't yet, the raw commands here
are the ground truth.

Conventions used below (no operator-specific data — substitute your own):

| Placeholder | Meaning |
|---|---|
| `$TARTCI_HOME` | This repo's checkout root |
| `<vm-store>` | Directory holding VM disks / qcow2 images (gitignored, Spotlight-excluded) |
| `~/.ssh/id_ed25519.pub` | A public key you want injected into guests (any number) |
| `<iso-store>` | Directory holding ISOs (Windows install media, virtio-win) |
| `pulp-linux` / `pulp-win` | SSH host aliases this toolkit maintains for you |

> **One fact that shapes everything: Tart is ARM-only.** Apple
> Virtualization.framework boots **ARM64 guests only** on Apple Silicon. Every VM
> here is arm64. You build arm64 natively; you reach x86_64 via cross-compile +
> emulation (Rosetta on Linux, Prism on Windows) as a *signal*, not an
> authoritative gate. GitHub-hosted x64 stays the required check.

---

## 1. Prereqs + host setup

**Tools (scripted by `./tartci setup`; manual fallback shown):**

```bash
brew install cirruslabs/cli/tart qemu sshpass
# qemu     — Windows VM substrate (hvf accel)
# tart     — macOS + Linux VM substrate (Apple Virtualization)
# sshpass  — non-interactive first-boot SSH during provisioning
```

**A VM store directory, excluded from Spotlight** (large disks should never be
indexed — it wastes IO and CPU):

```bash
mkdir -p <vm-store> <iso-store>
touch <vm-store>/.metadata_never_index    # tells Spotlight to skip this tree
```

Tart keeps its own VM registry under `~/.tart` by default; point heavy disk
stores and qcow2 images at `<vm-store>` and gitignore them. **This repo ships
scripts + configs + docs only — never the multi-GB images.**

**SSH key(s).** You can inject any number of public keys; private keys are
*never* baked into a golden. Have at least one:

```bash
ls ~/.ssh/id_ed25519.pub    # or generate: ssh-keygen -t ed25519
```

Configure the set the provisioner injects via either:

- env `PULP_CI_PUBKEYS` — colon-separated pubkey *file* paths, or
- the manifest `[access].authorized_keys` list (pubkey files),
- default `~/.ssh/id_ed25519.pub`.

**Tailscale (optional but recommended).** Bake it into the Tier 0 golden and run
`tailscale up` once per golden (or headless `--authkey`). You then get a stable
MagicDNS name and can `ssh`/log-pull from anywhere without port juggling. Prefer
the MagicDNS name over the per-boot vmnet IP. Disable Tailscale SSH on persistent
operator boxes to avoid re-auth prompts.

**Secondary Apple Silicon hosts (M-series pool members).** Keep host-specific
aliases in your local SSH and Shipyard config, not in this repo. The reusable
shape is:

```bash
# Prove non-interactive SSH and the host's Tart install/store.
ssh <m-series-ssh-alias> 'hostname; sw_vers -productVersion; sysctl -n machdep.cpu.brand_string'
ssh <m-series-ssh-alias> 'TART_HOME=/Users/<you>/VMs /opt/homebrew/bin/tart list --format json'
```

Use an explicit Homebrew Tart path because non-interactive SSH may not load
Homebrew's PATH. Prefer a home-backed Tart store for launchd-operated macOS CI:
`TART_HOME=/Users/<you>/VMs` on each host, with the macOS golden copied or baked
there. In Shipyard, keep the matching operator-local capacity config outside the
committed repo config:

```toml
[host_class.secondary]
ssh = "<m-series-ssh-alias>"
cap = 2
tart_bin = "/opt/homebrew/bin/tart"
tartci_bin = "/Users/<you>/.local/bin/tartci"
tart_home = "/Users/<you>/VMs" # absolute path; no shell/tilde expansion
labels = ["self-hosted", "macos", "arm64", "<repo>-build-secondary"]
```

The key invariant: the LaunchAgent, `tartci doctor`, and Shipyard capacity must
all point at the same Tart store. If one uses default `tart` state and another
uses `TART_HOME`, capacity and cleanup will disagree.
Shipyard's fleet health probe also shells `tartci doctor --reap --json` on each
host, so set `tartci_bin` to the same home-backed wrapper the LaunchAgent uses.

**Wire Shipyard's GitHub auth to the App token (do NOT skip).** After installing
Shipyard on a host, its GitHub auth must point at the GitHub-App **installation**
token — the `[github.auth]` `source = "command"` block (the
`shipyard-github-app-token` helper + App ID + private-key path) in
`~/Library/Application Support/shipyard/config.toml`. If that config is absent,
Shipyard silently falls back to the ambient `gh` token. For a personal GitHub
App with no user login, that ambient token is the **anonymous 60/hr** bucket, so
Shipyard runs unauthenticated and its menu bar shows **"updates paused"** with no
error surfaced — the exact failure seen on host `m1` (2026-07-06), which had no
`config.toml` at all and stayed paused for hours before anyone noticed.

Copy the config from an already-correct host with the supported
`shipyard auth` commands (the exported bundle is sanitized — it carries the
`token_command` + App ID + key **path**, never a secret):

```bash
# On a known-good host — emit a sanitized auth bundle (no secrets):
shipyard auth export > shipyard-auth.bundle

# On the new host — apply it globally, then confirm:
shipyard auth import shipyard-auth.bundle --scope global
shipyard auth doctor
#   github-auth: ok command helper (github-app-installation)   ← want this
#   github-auth: ... gh-cli (ambient)                          ← DEGRADED (60/hr)
```

The private key referenced by `token_command` is **not** in the bundle — it must
already exist at the referenced path on the new host (copy it out-of-band via
your own secret-transfer path; never commit it). `tartci doctor` runs
`shipyard auth doctor` for you and WARNs when the effective source is
`gh-cli (ambient)` rather than `github-app-installation`, so a degraded host is
caught the next time anyone runs `tartci doctor` on it. Hosts without Shipyard
installed stay green (the check is skipped, non-fatal).

---

## 2. macOS lane (Tart)

Native, fast, CoW clones. **Layered golden tiers** keep re-bakes cheap:

```
base  (cirruslabs macOS) → toolchain (Xcode CLT, brew deps, ccache) → project (Skia/Dawn baked, ccache warm)
```

Summary recipe (the macOS plan has the full detail):

```bash
# 1. Pull the cirruslabs macOS base image
tart pull ghcr.io/cirruslabs/macos-sequoia-xcode:latest

# 2. Bake Tier 0 (toolchain): clone the base, install brew deps + ccache, inject keys
tart clone ghcr.io/cirruslabs/macos-sequoia-xcode:latest macos-build-base
#   ...provision inside (brew bundle, ccache, sshd keys, Tailscale)...

# 3. Bake Tier 1 (project): clone Tier 0, bake immutable/expensive artifacts
#    (Skia/Dawn static libs) into the golden so each clone gets them CoW-free
tart clone macos-build-base pulp-mac-build
```

**Cache split (applies to every OS):**
- **Immutable + expensive → baked into the golden** (Skia/Dawn static libs).
  CoW-shared, ~free per clone.
- **Mutable + growing → host-mounted virtio-fs** (ccache, FetchContent). Match
  guest/host uid so the shared cache is writable both ways.

**Ephemeral runner concept:** an ephemeral per-job GitHub Actions runner clones
the golden, mounts the host caches, runs **one** job, and self-destructs. The
golden is never mutated; all per-run state lives in the disposable clone.

> macOS-guest **2-running-VM kernel cap** applies (Apple Virtualization limit).
> Linux/Windows guests are **uncapped** — that's where local parallelism lives.

---

## 3. Linux lane (Tart) — the easy, fully-proven win

End-to-end: pull base → bump resources → durable keys + mounts → deps → Skia →
build → test. Native arm64 builds at full speed; x64 is cross+emulate (§3.8).

### 3.1 Pull a **pinned** Ubuntu 24.04 arm64 base (never `:latest`)

`:latest` drifts glibc/sysroot underneath your golden. Pin a concrete tag or
digest and record it in the manifest:

```bash
tart pull ghcr.io/cirruslabs/ubuntu:24.04        # then re-pin to a digest you record
# manifest: base = "ghcr.io/cirruslabs/ubuntu:24.04@sha256:<pin>"
tart clone ghcr.io/cirruslabs/ubuntu:24.04 pulp-linux-build
```

### 3.2 Bump disk / RAM / CPU (on a **stopped** VM)

`tart set` only works while the VM is stopped. Cloud-init auto-grows the root
partition to fill the larger disk on next boot.

```bash
tart set pulp-linux-build --disk-size 80 --memory 16384 --cpu 8
tart run  pulp-linux-build --no-graphics &     # boot once; cloud-init grows the FS
```

### 3.3 Durable SSH keys (work around cloud-init)

cirruslabs cloud-init images **re-apply the default `~/.ssh/authorized_keys` on
every boot**, so anything you add there is wiped. The durable pattern:

1. Write your injected keys to an **unmanaged** file cloud-init doesn't touch:
   `~/.ssh/authorized_keys_ci`.
2. Add an sshd drop-in that tells sshd to read it:

   ```
   # /etc/ssh/sshd_config.d/10-ci-keys.conf
   AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys_ci
   ```

This survives reboot; the standard file is **not** durable here. Never bake
private keys.

Then sync your host `~/.ssh/config` so `ssh pulp-linux` works (a managed block
between `# >>> pulp-ci` / `# <<< pulp-ci` markers, one `Host` stanza per running
VM pointing at the current `tart ip`):

```bash
# tart-sshconfig.sh sync   (vmnet IPs are per-boot; this keeps the alias current)
ssh pulp-linux 'echo ok'
```

### 3.4 Durable host-ccache mount (systemd .mount, NOT fstab)

cloud-init **reverts `/etc/fstab` on every boot**, so an fstab line for the
virtio-fs cache won't stick. Use a **systemd `.mount` unit** instead (or mount at
job runtime). The virtio-fs mechanics:

```bash
# Each `tart run --dir="NAME:host-path"` exposes the share under the automount tag.
tart run pulp-linux-build --dir="ccache:<vm-store>/linux-ccache" --no-graphics &

# Inside the guest, the automount tag is com.apple.virtio-fs.automount;
# each named --dir appears as <mnt>/NAME:
sudo mount -t virtiofs com.apple.virtio-fs.automount <mnt>
ls <mnt>/ccache        # the rw named subdir (the share ROOT is perm-restricted)
```

> **ccache hashing config MUST match** between the cache-populating build and the
> warm build, or the keys differ and you get near-zero hits. Set `CCACHE_BASEDIR`
> / `CCACHE_NOHASHDIR` identically in both. A mismatched prime once gave a
> misleading 10.69%; matched config gave **99.93%**.

### 3.5 Install the full build dependency set

Mirror the project's canonical Linux dependency list **first**, then add the CI
extras. (For Pulp this is `build.yml`'s "Install Linux dependencies" step.)

```bash
ssh pulp-linux sudo apt-get update
ssh pulp-linux sudo apt-get install -y \
  libasound2-dev libdbus-1-dev libdrm-dev libegl1-mesa-dev \
  libfontconfig1-dev libgbm-dev libgl1-mesa-dev libx11-dev libxext-dev \
  libxfixes-dev libxi-dev libxinerama-dev libxkbcommon-dev libxrandr-dev \
  libxrender-dev libxss-dev libxtst-dev libwayland-dev wayland-protocols \
  libicu-dev \
  cmake ninja-build clang lld ccache git git-lfs python3 \
  gcc-x86-64-linux-gnu g++-x86-64-linux-gnu binfmt-support
```

- `libicu-dev` is needed because Pulp opts into direct `icu::Locale` /
  BreakIterator calls when Skia + ICU public headers are present; libskia exports
  SkUnicode, not ICU's own symbols (see gotchas: ICU link).
- `libjack-jackd2-dev` is **deliberately omitted** — base Linux compiles fine
  without it; only add it for a JACK-enabled lane.
- `gcc/g++-x86-64-linux-gnu` + `binfmt-support` are for the x64 smoke lane
  (§3.8). The provision script also installs Rosetta-for-Linux and the amd64
  runtime libraries needed by dynamic x64 binaries.

### 3.6 Fetch prebuilt Skia (linux-arm64) + bake it

Bake the static lib into the golden so each clone gets it CoW-free:

```bash
ssh pulp-linux 'cd pulp && python3 tools/deps/fetch_skia_for_release.py --arch linux-arm64'
```

> **Arch-path collision (matters for §3.8):** the fetch script maps **both**
> `linux-arm64` and `linux-x64` to the **same** `linux-gpu/lib/Release/libskia.a`
> (`arch_subdir=""` for Linux), and Skia is selected by OS, not target arch. You
> **cannot bake both Linux arches into one tree** as-is — use separate `SKIA_DIR`
> roots per target arch, or add a Linux arch-subdir to the fetch script. Native
> arm64 alone (this section) is unaffected.

### 3.7 Configure Release, build, test

```bash
ssh pulp-linux 'cd pulp && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release'
ssh pulp-linux 'cmake --build pulp/build -j8'
ssh pulp-linux 'ctest --test-dir pulp/build --output-on-failure'
```

**Proven results (Tart Linux golden, arm64):** build **1003/1003 green**
(VST3+CLAP+standalone); `ctest` **99% (9366/9370)** — the 4 failures are
env/golden-baseline (no git remote, arm64 raster goldens, fs iteration order),
not regressions. Cold build ≈ **1:47** compile (configure ≈ 273 s); **warm build
20.8 s @ 99.93% ccache hits** across a CoW clone (beats the macOS lane's 88%).

A fast inner-loop variant: `-DPULP_ENABLE_GPU=OFF` for a no-Skia smoke.

### 3.8 Linux x86_64 — cross-compile + Rosetta-emulated test (wired)

The guest is ARM64; you reach x86_64 by cross-compiling in-guest and running the
test subset under Rosetta-for-Linux (binfmt). This is wired into the provider —
`tartci up linux --target-arch x86_64` (or `providers/tart-linux/run.sh
--target-arch x86_64`). The manifest declares it with `target_arch = "x86_64"`,
`cross = true`, and an `[emulation]` table (see `manifests/example.x64.toml`).

What the provider does when `target_arch != arch`:

1. **Toolchain** — install-if-missing `gcc-x86-64-linux-gnu` /
   `g++-x86-64-linux-gnu`, plus Rosetta binfmt registration. CMake is
   configured with `-DCMAKE_SYSTEM_PROCESSOR=x86_64
   -DCMAKE_C_COMPILER=x86_64-linux-gnu-gcc -DCMAKE_CXX_COMPILER=…-g++`.
2. **Rosetta runtime** — host Rosetta is installed with
   `softwareupdate --install-rosetta --agree-to-license`, Tart boots x64-smoke
   clones with `--rosetta=rosetta`, the guest mounts the Rosetta virtiofs share
   at `/mnt/rosetta`, and systemd re-registers binfmt after reboot. The golden
   also carries an amd64 apt source + `libc6:amd64 libstdc++6:amd64
   libgcc-s1:amd64 zlib1g:amd64 libtinfo6:amd64 libxml2:amd64`, so dynamic x64
   binaries have `/lib64/ld-linux-x86-64.so.2`. The binfmt register string must
   be written with literal `\xHH` escapes (`printf '%s'`); `binfmt_misc` decodes
   them itself.
3. **GPU off by default.** The fetch script maps both `linux-arm64` and
   `linux-x64` to the SAME `build/linux-gpu/lib/Release/libskia.a`
   (`arch_subdir=""` — §3.6), so you can't reuse the baked arm64 Skia for an x64
   link. The cross build therefore defaults `-DPULP_ENABLE_GPU=OFF`. To build
   GPU-on, fetch the `linux-x64` Skia into a **separate** tree and pass
   `--gpu --skia-dir <that tree>` (or `[emulation].skia_dir`); without an
   explicit x64 `SKIA_DIR` the provider refuses `--gpu` rather than silently
   linking the arm64 lib.
4. **Full cross-LINK also needs x64 system libs.** ALSA / X11 / wayland / etc.
   must be present for x64 (`dpkg --add-architecture amd64` + the `:amd64` -dev
   packages, or a baked x64 sysroot). If absent, the configure/link fails with a
   clear missing-lib error — it never emits an arm64 artifact under an x64 name.
5. **Tests** run via `ctest` under Rosetta binfmt, excluding
   `sanitizer|simd|gpu|timing` labels.

**Prove just the chain, golden-agnostic:** `tartci up linux --target-arch
x86_64 --self-test` cross-compiles a dynamic trivial program and runs it under
Rosetta — no Pulp checkout or Skia needed. A project with V8's bundled clang can
also verify the real workload toolchain with
`third_party/llvm-build/Release+Asserts/bin/clang --version`; it should print
`Target: x86_64-unknown-linux-gnu`.

**Treat emulated-x64 green as a smoke signal, NOT a gate.** Sanitizers
(ASan/TSan/UBSan/MSan/RTSan) don't translate reliably under emulation — run those on real
x64 (GitHub). SIMD/Highway dispatch, futex/signal semantics, and RT-audio timing
are all unreliable emulated. GitHub-hosted x64 stays authoritative.

> **Windows x86_64 (Prism).** The Windows-on-ARM analog runs x64 binaries under
> Prism, but the cross-build toolchain story there (MSVC x64 cross + x64 deps) is
> heavier and not yet wired — `--target-arch` is Linux/Rosetta today. Tracked
> as a follow-up; until then the Windows lane builds native ARM64.

---

## 4. Windows lane (QEMU) — the hard-won recipe

AVF can't install Windows (no inbox virtio-blk driver → 0 bytes written; black
installer display), so Windows is **first-class on standalone QEMU/hvf**, not a
fallback. The golden ends up a qcow2 you can also open in UTM as a GUI bench.

> **Two ways to get a host its Windows golden — copy first, bake only if you must.**
> The golden is a portable ~26 GB qcow2, so a new host almost never needs to bake
> its own:
> - **(A) Copy from the pool (preferred, fast).** If any host already has the
>   canonical golden, run **on the new host** (once tartci is deployed there):
>   `tartci goldens sync --from <peer-that-has-it>` — it picks the fastest link
>   (Thunderbolt → LAN → Tailscale), verifies the sha, and points the local runner
>   at it. (Or push from a host that has it: `tartci goldens sync --to <newhost>`.)
>   See `docs/golden-sync.md`.
> - **(B) Bake from scratch (below).** Only for the **first** golden in the pool
>   or a **new Windows version** — the ISO → autounattend → provision recipe in
>   §4.1–§4.8. Once baked, other hosts get it via (A).

Approximate timing once set up: **Windows cold build ≈ 7 min** on an 8-core arm
QEMU VM.

### 4.1 Get a Win11 **24H2** ARM64 ISO (NOT 25H2)

25H2 install media fails to boot with BCD `0xc000000d` across every QEMU/AVF
permutation — it's a media/version incompatibility, not config. **Use 24H2.**

If Microsoft's download page blocks your IP (anti-VPN, e.g. via Tailscale), build
the ISO with **UUP dump**'s macOS converter, which pulls directly from the
Windows Update CDN:

```bash
# UUP dump → "Download using aria2 + convert" → run the macOS converter script.
# chntpw won't build on Apple Silicon → stub it (no-op). The autounattend handles
# the registry bypass chntpw would otherwise do, so the stub is harmless.
```

### 4.2 512-byte-pad the ISO (and any disk image)

AVF/QEMU reject disk/ISO images whose byte size isn't a multiple of 512
("Invalid disk image. The disk image format is not recognized.").

```bash
# Pad <iso-store>/win11-24h2-arm64.iso up to the next 512-byte boundary.
# (hdiutil-produced ISOs are already aligned; UUP/MS ones often are not.)
```

### 4.3 Author `autounattend.xml`

Generated by `providers/qemu-windows/make-autounattend.sh` (keys come from a
**configurable** set; never bake private keys). It must include:

- **LabConfig bypass** for TPM / SecureBoot / RAM / CPU checks.
- **Local admin + autologon**, OOBE-skip attempt.
- **OpenSSH server enabled**, with your **public** keys injected into
  `administrators_authorized_keys`.
- **viostor arm64 DriverPaths** (`Microsoft-Windows-PnpCustomizationsWinPE`) so
  Setup can see disks if needed.

```bash
providers/qemu-windows/make-autounattend.sh \
  --pubkey ~/.ssh/id_ed25519.pub \
  --out <vm-store>/win-provision/autounattend.xml
```

### 4.4 The QEMU flags that matter

Reference `providers/qemu-windows/qemu-run.sh`. The load-bearing choices:

- `-accel hvf -machine virt,highmem=on -cpu host`
- **NVMe** system disk (`-device nvme,...`) — Win-ARM has an **inbox NVMe
  driver**, sidestepping the AVF virtio-blk wall.
- **`-device ramfb`** display — **NOT** virtio-gpu (WinPE has no virtio-gpu driver
  → boot-splash hang).
- **ALL install media on `-device usb-storage`** — **NOT** virtio-scsi (WinPE
  can't read virtio-scsi → autounattend.xml is never found).
- `-netdev user,hostfwd=tcp::2222-:22` + `-device virtio-net-pci` (network works
  via netkvm → FOD/downloads + SSH on `localhost:2222`).
- `-vnc 127.0.0.1:N` so you can drive the headless install.

```bash
providers/qemu-windows/qemu-run.sh \
  --disk    <vm-store>/win-provision/win.qcow2 \
  --install <iso-store>/win11-24h2-arm64.iso \
  --virtio  <iso-store>/virtio-win.iso \
  --autounattend <vm-store>/win-provision/autounattend.xml
```

### 4.5 Drive the headless install via vncdotool

```bash
pip install --user vncdotool
```

Flow: **UEFI Boot Manager → boot the install CD → dense-keyspam the "press any
key to boot from CD" prompt → WinPE/Setup picks up the autounattend.** vncdotool
mistypes some shifted chars (e.g. `>`); it's fine for `:` and `\`.

### 4.6 Make the ESP self-booting (auto-boot on reboot)

After the image is applied, copy the boot manager into the fallback path so
reboots boot Windows directly — no UEFI-shell babysitting:

```powershell
mountvol S: /s
copy S:\EFI\Microsoft\Boot\bootmgfw.efi S:\EFI\Boot\BOOTAA64.EFI
```

Verified: reboot → SSH back in ~15 s.

### 4.7 Get SSH up, then provision via **direct SSH commands**

```bash
ssh -p 2222 admin@localhost 'whoami'    # alias this as pulp-win in ~/.ssh/config
```

**Do NOT scp `.cmd` batch files and run them** — they mis-execute. Run commands
directly: `ssh pulp-win '<cmd>'`. The OpenSSH default shell is `cmd.exe`; for
complex PowerShell, dodge cmd quoting/`%`/`>` mangling with base64:

```bash
ssh pulp-win "powershell -EncodedCommand $(printf '%s' "$PS_SCRIPT" \
  | iconv -t UTF-16LE | base64)"
```

Install the toolchain over SSH:

- **CMake / Ninja / Git** (msi / zip / exe).
- **Python (arm64)**.
- **MSVC Build Tools** — arm64 VCTools + Win11 SDK.

> **MSVC gotcha (the big one):** a *partial* VS install makes the installer
> silently no-op — exits 0, installs nothing. Fully nuke `BuildTools` +
> `Packages` + `Setup`, **reboot**, then clean-install. **Verify `cl.exe`
> exists** under `VC\Tools\MSVC\<ver>\bin\Hostarm64\arm64` — do not trust the
> installer's exit code. (Prefer an offline VS layout / host-side cache over a
> live web installer for reproducibility.)

### 4.8 Build + test

```bash
# Git bash: fetch deps only
ssh pulp-win 'C:\path\to\bash setup.sh --ci --deps-only'

# Configure under the MSVC env, GPU off (no Windows Skia yet)
ssh pulp-win 'vcvarsall arm64 && cmake -S pulp -B pulp\build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DPULP_ENABLE_GPU=OFF'

ssh pulp-win 'cmake --build pulp\build'
ssh pulp-win 'ctest --test-dir pulp\build'   # apply the CI exclude set (gpu/visual labels)
```

Two Windows-specific musts:

- **Create `C:\tmp`** — tests use POSIX `/tmp/...` paths, which resolve to
  `C:\tmp` on Windows. Without it those tests fail.
- **Bake hosted-runner-compatible command paths** — Pulp's current GitHub
  workflow assumes `bash`, `choco`, and `ccache` can be found on `PATH`. Install
  Chocolatey, install `ccache`, and add `C:\Program Files\Git\bin`,
  `C:\Program Files\Git\usr\bin`, and `C:\ProgramData\chocolatey\bin` to the
  machine PATH before tagging the golden.
- **If `cl` hangs on a translation unit, kill it and resume** — Ninja is
  incremental, and the arm→x64 emulation can transiently stall a TU.

Tag the golden when green: `pulp-windows-build:<date>`. Prefer **sccache** for
new Windows-native cache work; if the consuming workflow still calls `ccache`,
bake `ccache` too so the first job does not have to install it.

Before tagging, run the golden optimizer against the booted single-operator VM:

```bash
tartci windows optimize
# Optional x64/Prism smoke validation in the same booted ARM64 Windows guest:
TARTCI_WIN_VCVARS_ARCHES=arm64,x64 tartci windows optimize
```

The optimizer is idempotent. It creates `C:\tmp`, persists the standard Git
Bash/Chocolatey/ccache PATH entries when those directories exist, prewarms common
PowerShell module analysis, preinstalls the configured Windows ARM64 Actions
runner version, creates the standard cache roots, configures ccache, fails if
hosted-runner compatibility tools are missing, and verifies `vcvarsall` + `cl`
for each requested architecture before `tartci windows golden <name>` shuts the
VM down and snapshots it.

Windows cache contract for projects:

- **C/C++ object cache:** use `ccache` first unless the project has already
  standardized on `sccache`. CMake projects should set
  `CMAKE_C_COMPILER_LAUNCHER=ccache` and `CMAKE_CXX_COMPILER_LAUNCHER=ccache`
  or auto-detect `ccache` like Pulp does. Restore/save
  `~/AppData/Local/ccache` in the workflow. This is the cache that turns repeated
  compile-heavy Pulp jobs from "compile the world" into "compile only changed
  translation units".
- **Rust or mixed-language cache:** if a project uses `sccache`, set
  `SCCACHE_DIR=%LOCALAPPDATA%\sccache`, `RUSTC_WRAPPER=sccache`, and for CMake
  use `sccache` as the compiler launcher. Restore/save `~/AppData/Local/sccache`.
  Do not enable both ccache and sccache for the same C/C++ target.
- **Dependency/source cache:** restore/save the project's source cache, not build
  outputs. For Pulp that is `~/AppData/Local/Pulp/fetchcontent-src`, which backs
  `PULP_SHARED_FETCHCONTENT_SOURCE_DIR` / the default `PulpFetchContent.cmake`
  lookup. This avoids re-fetching/re-unpacking dependencies; ccache then avoids
  recompiling them.

The current QEMU Windows lane has disposable overlays and no proven host-mounted
Windows filesystem cache yet. Until an SMB/virtiofs-style host mount is proven,
durability comes from workflow cache restore/save into the above guest paths.
Measure with the workflow's `Ccache stats` step plus `tartci timings`; a faster
boot without cache hits is not the win.

### 4.9 Serve Windows jobs from QEMU hosts

The Windows pool is intentionally QEMU, not Tart. Each GitHub job gets a fresh
qcow2 overlay from the golden, a dynamic localhost SSH port, a one-time JIT
Actions runner, and then the overlay is discarded. Use the same setup on every
Apple Silicon host that should participate in the Windows pool.

Host prerequisites:

```bash
brew install qemu
gh auth status -h github.com
mkdir -p "$HOME/.tartci/goldens" "$HOME/VMs/tmp" "$HOME/VMs/logs"
```

Install the Windows golden on each host:

```bash
cp /path/to/pulp-windows-build-24h2-arm64-YYYY-MM-DD.qcow2 \
  "$HOME/.tartci/goldens/pulp-windows-build-24h2-arm64-2026-06-12-cacheopt.qcow2"
shasum -a 256 "$HOME/.tartci/goldens/pulp-windows-build-24h2-arm64-2026-06-12-cacheopt.qcow2"
```

Keep the runner code on a home-backed path so launchd and non-interactive SSH do
not depend on a mounted workspace:

```bash
mkdir -p "$HOME/.local/share/tartci"
rsync -a --delete --exclude .git ./ "$HOME/.local/share/tartci/"
```

One-shot proof, with the same PATH launchd will use:

```bash
PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin" \
TARTCI_WIN_GOLDEN="$HOME/.tartci/goldens/pulp-windows-build-24h2-arm64-2026-06-12-cacheopt.qcow2" \
TARTCI_RUNNER_REPO=OWNER/REPO \
TARTCI_RUNNER_LABELS=self-hosted,Windows,ARM64,pulp-build-windows \
TARTCI_WIN_WORK="$HOME/VMs/tmp/tartci-win-proof" \
TARTCI_WIN_LOGS="$HOME/VMs/logs/tartci-win-proof" \
"$HOME/.local/share/tartci/providers/qemu-windows/runner.sh" --once
```

To create a matching queued job, prefer a Windows-native workflow. For Pulp,
use `Build and Test` with a per-run selector override for a full proof:

```bash
gh workflow run build.yml -R danielraffel/pulp --ref main \
  -f runner_provider=github-hosted \
  -f 'windows_runner_selector_json=["self-hosted","Windows","ARM64","pulp-build-windows"]'
```

A tiny workflow can also prove assignment, but it must use a Windows-compatible
shell. A Unix-shell step such as `chmod +x tools/check-docs.sh` will correctly
prove that the runner claimed the job, then fail under Windows PowerShell. Treat
that as an availability probe only, not as a green-lane proof.

After a proof, inspect both GitHub and the host logs:

```bash
gh api repos/OWNER/REPO/actions/runs/RUN_ID/jobs \
  --jq '.jobs[] | [.name,.status,(.conclusion//""),.created_at,.started_at,.completed_at,((.labels//[])|join("|")),(.runner_name//"")] | @tsv'

find "$HOME/VMs/logs/tartci-win-proof" -name timing.tsv -print -exec cat {} \;
tail -F "$HOME/Library/Logs/tartci/qemu-runner-windows.log"
```

The supervisor writes:

- `preflight.log`: guest clock sync, PowerShell execution policy, GitHub/broker
  TCP checks, runner version, JIT config byte count, `vcvarsall` discovery, and
  `cl.exe` visibility after the MSVC environment import.
- `early-clock.log`: minimal guest clock sync before any HTTPS runner download.
- `runner-output.log`: stdout/stderr from `Runner.Listener.exe run --jitconfig`,
  including the runner-process `vcvarsall` import and `cl.exe` diagnostic before
  the Actions agent starts.
- `runner-diag.log`: tail of the latest Actions runner `_diag` logs.
- `qemu.log`: QEMU stderr.
- `timing.tsv`: `boot_to_ssh`, `preflight`, `runner_process`, `post_diag`, and
  `total` seconds for rough host-to-host and hosted-runner comparisons.

Only enable normal routing after a Windows-native workflow proves the lane:

```bash
gh variable set PULP_LOCAL_WINDOWS_RUNS_ON_JSON -R danielraffel/pulp \
  --body '["self-hosted","Windows","ARM64","pulp-build-windows"]'
```

Until that variable is set, ordinary Pulp Windows jobs continue to use
GitHub-hosted `windows-latest`. The QEMU supervisor is still safe to leave loaded
because `TARTCI_RUNNER_QUEUE_MATCH_LABELS=1` makes `--loop` boot only when a
fresh queued job's requested labels can be satisfied by this runner's labels.

### 4.10 Speed up the Windows QEMU lane

Optimize from `timing.tsv`, not from intuition. The first smoke proofs showed
QEMU startup was not the dominant cost: boot-to-SSH was roughly 25 seconds,
while preflight diagnostics took about a minute. A full Build and Test run will
mostly be build and test time, so keep both the host timing file and the GitHub
job timestamps when comparing against `windows-latest`.

```bash
tartci timings
tartci timings "$HOME/VMs/logs/tartci-win" "$HOME/VMs/logs/tartci-linux"
```

The Windows runner defaults to `TARTCI_WIN_PREFLIGHT_MODE=fast`: sync the clock,
verify the JIT config landed, record the runner listener version, then launch
the job. The old verbose probe path is still available with
`TARTCI_WIN_PREFLIGHT_MODE=full` when diagnosing a new golden or network/toolchain
issue.

Highest-return changes, in order:

1. **Move deterministic preflight into the golden.** The normal supervisor path
   verifies only clock, runner version, and JIT config by default. Toolchain
   discovery, PATH fixes, execution policy, certificate setup, SDK validation,
   and GitHub broker probes should be baked into the golden and proven during
   image creation. Run `tartci windows optimize` before tagging the qcow2, then
   keep `TARTCI_WIN_PREFLIGHT_MODE=full` for debug rather than paying for those
   probes on every job.
2. **Add a real Windows build cache.** Use `sccache` for C/C++ and Rust
   compilation, plus the project-specific package caches that matter
   (`CMake` downloads, `NuGet`, `Cargo`, `pnpm`/`npm`, and similar). The cache
   must be restored into the guest at job start or live outside the disposable
   qcow2 overlay so every fresh VM can reuse it.
3. **Prefer host-backed cache storage once the clean lane is stable.** A
   VirtIO-backed or otherwise host-mounted cache on local NVMe avoids virtual
   disk churn and survives VM recreation. Treat source trees and build outputs
   as disposable unless a project deliberately opts into an incremental build
   directory.
4. **Keep QEMU on the fast device path.** The runner already uses HVF
   acceleration, virtio networking, NVMe storage, and an ARM64 Windows guest on
   Apple Silicon. Do not spend time on hypervisor swaps until the guest-side
   timings show QEMU itself is the problem.
5. **Trim Windows background work in the golden.** Disable noisy services only
   when the lane is isolated for CI and the effect is measured. Search indexing,
   scheduled maintenance, update orchestration, and Defender scans can affect
   consistency, but they are not a substitute for build caches.
6. **Consider warm workers last.** A pool of already-booted VMs can remove most
   boot latency, but it complicates per-job cleanup, runner registration, and
   rollback. Keep the cold CoW overlay lane as the reliable baseline first; add
   warm workers only after the full Windows proof is green and cache behavior is
   understood.

For ARM64 Windows workloads, this lane can beat GitHub-hosted Windows when the
golden is current and caches are warm because there is no hosted-runner queue and
no x64 translation tax. For x64 coverage or test execution, Windows-on-ARM still
runs through Microsoft's x64 translation layer, so local hardware mainly helps
availability and cache locality rather than raw CPU efficiency.

Treat x64-on-Windows-ARM as a separate smoke lane until proven. The QEMU provider
boots an ARM64 Windows guest with `qemu-system-aarch64`; it does not emulate a
full Intel Windows machine. A repo can try the x64 MSVC environment with
`TARTCI_WIN_VCVARS_ARCH=x64`, but release-fidelity x64 gates should stay on
GitHub-hosted `windows-latest` until those smoke runs are consistently clean.

---

## 5. Per-project manifest, bench (UTM), and metrics

### Per-project manifest (`vm-image` v2)

A repo plugs in by dropping one `.shipyard/vm-image.<os>.toml` (or an
`[[images]]` array) declaring `os` / `arch` / `target_arch` / `cross` / `base` /
OS-scoped `[packages]` / `[caches]` / `[[mounts]]`. tartci bakes or clones a
golden from it with zero hand-provisioning — keys, ssh-config, Tailscale, log
collection, and cache mounts are inherited framework defaults. See `manifests/`
in this repo and the `README` "Per-project use" section.

### Bench (UTM) for GUI / DAW testing

The **golden** is pristine, generic, headless, and never mutated. A **bench** is
a separate **persistent, snapshot-able** clone you open in UTM to install DAWs +
test plugins by hand:

1. Start from the golden of the target OS (never customize the golden).
2. Clone → bench (one persistent copy); snapshot before big changes.
3. Open the bench in UTM with a GUI display profile (`ramfb` → `virtio-gpu` for
   Windows) and install your DAWs / plugins.

Keep the NVMe controller constant between golden and bench; only the display
profile + installed apps differ. The bench may carry a live Tailscale identity +
activation; the golden must not. See the README "bench" section and the design
notes for the per-OS UTM story (Windows imports its qcow2 directly; Linux needs a
qcow2 export; macOS is recreate-from-IPSW).

### Metrics

Each VM job wraps configure → build → ctest with a timer and emits **one
structured JSONL record per run** (os, arch, git_sha, provider, phase wall-times,
ccache/sccache hit %, test pass/fail, ctest label times, cold/warm). Append to a
per-OS host store; a small reporter computes rolling median + flags >N%
deviation. `.ninja_log` + `ninjatracing` gives a per-target flamegraph on demand.
Graduate to a dashboard (VictoriaMetrics + Grafana, or Grafana + SQLite) only if
at-a-glance trends are wanted. See `metrics/` and the README.

---

## 6. Serve the GitHub Actions pool (per-job ephemeral runners)

§3–§4 cover **on-demand** builds (`tartci up <os>` — one build, then discard).
To make a host **serve the GitHub Actions pool** instead — boot a throwaway VM
per *queued job* and let the workflow drive the build — use the runner
supervisors. They are the pool-serving siblings of the `run.sh` provider
scripts, ported from Pulp's proven `tools/ci/{tart-runner-linux,qemu-runner-windows}.sh`.

```bash
# One job then exit (pilot-safe): mint a JIT runner, boot a clone, run one job, discard.
tartci serve macos
tartci serve linux
tartci serve windows

# Keep serving (what the LaunchAgents run):
tartci serve macos --loop --labels self-hosted,macOS,ARM64,pulp-build,pulp-build-vm
tartci serve linux --loop --labels self-hosted,Linux,ARM64,pulp-build-linux,pulp-host-macstudio
tartci serve windows --loop --labels self-hosted,Windows,ARM64,pulp-build-windows,pulp-host-macstudio
```

What the supervisor does each job: mint a **Just-In-Time** (single-job) runner
config via `gh api .../generate-jitconfig` (needs repo admin), clone the golden
(Linux) or CoW-overlay it on a free SSH port (Windows), run the Actions agent
once with that JIT config, then discard the VM. The agent processes exactly one
job and deregisters — no long-lived runner state. The `--loop` gate only boots
when there is queued work matching `TARTCI_RUNNER_WORKFLOW_NAME`, default
`Build and Test`.
Linux and Windows scan queued and in-progress workflow runs for queued jobs and
add two more default guards: they ignore queued jobs older than
`TARTCI_RUNNER_MAX_QUEUED_AGE_SECONDS` (default six hours), and
`TARTCI_RUNNER_QUEUE_MATCH_LABELS=1` requires a queued job's requested labels to
be satisfiable by the configured runner labels before a VM boots. Set it to `0`
only for debugging broad workflow polling. For coordinated multi-host routing,
add a host label such as `pulp-host-macstudio` or `pulp-host-m5` after the shared
`pulp-build-*` label, then point the workflow's primary and overflow selectors
at those exact label sets. Linux/Windows runners are JIT ephemeral, so they are
not visible as idle registered GitHub runners before a job is queued; the
workflow resolver should compare configured per-host capacity with in-progress
jobs already using each exact host selector. A GitHub Actions job cannot change
`runs-on` after it is queued, so GitHub-hosted fallback must be selected before
the job enters the queue. If multiple Windows hosts are accidentally configured
to race the same queued job, any VM that does not claim work exits after
`TARTCI_RUNNER_IDLE_TIMEOUT_SECS` (15 minutes by default), deletes its stale
GitHub runner registration by ephemeral runner name, and discards the overlay.
Ephemeral Windows runner names include a host-derived prefix by default; set
`TARTCI_RUNNER_NAME_PREFIX` only when a host needs a stable custom prefix.
Windows writes per-job timing to `$TARTCI_WIN_LOGS/<runner>/timing.tsv`; Linux
writes the same shape to `$TARTCI_LINUX_LOGS/<runner>/timing.tsv` (default
`$HOME/VMs/logs/tartci-linux`). Compare those files with `tartci timings` and
GitHub job timestamps before promoting local routing.
macOS supervisors atomically replace their heartbeat state file; `doctor`,
`observe`, and Shipyard fleet probes should treat an unreadable state file as a
real health problem, not as "no active runner."

Everything is env-driven for genericity: `TARTCI_RUNNER_REPO`,
`TARTCI_MACOS_GOLDEN` / `TARTCI_LINUX_GOLDEN` / `TARTCI_WIN_GOLDEN`,
`TARTCI_RUNNER_LABELS`, `TARTCI_RUNNER_GROUP_ID`,
`TARTCI_RUNNER_WORKFLOW_NAME`, `TARTCI_RUNNER_VERSION` (Windows agent),
`TARTCI_WIN_VCVARS_ARCH` (Windows MSVC environment, default `arm64`),
`TARTCI_WIN_PREFLIGHT_MODE` (`fast` by default, `full` for diagnostics),
`TARTCI_WIN_CPUS`, `TARTCI_WIN_MEMORY_MB`, `TARTCI_WIN_WORK`, and
`TARTCI_WIN_LOGS`. Defaults target `danielraffel/pulp`
(the first consumer). When multiple macOS hosts serve the same selector, keep
the workflow selector shared and make the runner name unique by adding an extra
host-specific label after the shared `pulp-build-*` pool label or by passing a
unique `--name-prefix` in the installed plist.

VM runners participate in the host-core lease store by default. Set
`TARTCI_MACOS_VM_CORES`, `TARTCI_LINUX_VM_CORES`, or `TARTCI_WIN_VM_CORES` when a
host needs a provider-specific lease size; otherwise the host profile's
`vm_pool_cores` value is used. The macOS hard cap remains a separate <=2 guest
semaphore and fails closed: if `tart list` is unavailable or malformed, the
macOS serve loop treats the cap as already full and waits. Disable the lease
consumer with `TARTCI_VM_LEASES=0` only during operator-controlled break-glass
debugging.

**Windows gotchas preserved from the Pulp original** (debugged live; don't
"simplify" them away): the multi-KB JIT blob is **streamed via ssh stdin into a
file**, never on the outer ssh command line (cmd.exe's 8191-char limit blows
through the ssh→cmd→powershell chain); the agent is started through
`Runner.Listener.exe run --jitconfig` directly while the blob is read from that
file inside PowerShell; the configured Actions runner version is enforced before every JIT run;
stale `C:\actions-runner` registration files are removed because a golden may
cache the runner binary but must not cache `.runner` or `.credentials`;
long preflight / runner PowerShell probes are **streamed into guest `.ps1`
files** and executed there, because adding toolchain diagnostics can push
`powershell -EncodedCommand` past cmd.exe's command-line limit; `vcvarsall` is
discovered via `Get-ChildItem` (vswhere returns empty for a BuildTools-only
install) and imported before both preflight diagnostics and the Actions runner
process so workflow Bash steps can see MSVC; the supervisor **bails the moment
QEMU dies** (`kill -0 $qpid`) so a free-port TOCTOU surfaces fast instead of
burning the full ~10 min SSH window; and a post-extract integrity check asserts
`Runner.Listener.exe` exists before running.

### Serve across reboots (LaunchAgent)

Install one of the templates in `launchd/` so the supervisor runs under
`launchd` and survives reboot. The shipped templates are **Pulp's concrete
instance** — their known labels (`com.danielraffel.pulp.tart-runner`,
`com.danielraffel.pulp.tart-runner-macos-release`,
`com.danielraffel.pulp.tart-runner-linux`, and
`com.danielraffel.pulp.qemu-runner-windows`) are what the
[shipyard-macos-gui](https://github.com/danielraffel/shipyard-macos-gui) "Serve
CI builds from this Mac" switch toggles via `launchctl load/unload` once the
GUI knows about the label. See `launchd/README.md` for the install `sed` recipe
and how to serve a different repo. Two traps carried over from the Pulp lane:
launchd does **not** expand `$HOME`/`$TARTCI_REPO` (the install `sed` must write
absolute paths), and a LaunchAgent can't read a `/Volumes` golden store without
**Full Disk Access**.

For Pulp, keep the macOS workflow lanes distinct:

```text
Build and Test -> self-hosted,macOS,ARM64,pulp-build,pulp-build-vm
Release CLI    -> self-hosted,macOS,ARM64,pulp-build-vm-release
```

Load the Release CLI VM lane only as a separate LaunchAgent filtered with
`TARTCI_RUNNER_WORKFLOW_NAME=Release CLI`. Do not point Release CLI at the
Build and Test `pulp-build-vm` lane, and do not flip
`PULP_RELEASE_MACOS_RUNS_ON_JSON` away from the fallback lane until a real
Release CLI proof has claimed `pulp-build-vm-release` and completed.

### Emulation note

Pool jobs build whatever arch the **workflow** targets. The emulated **x86_64**
lane belongs on the **on-demand** side (`tartci up <os>`, see the cross-arch
manifest work), not as a pool-serving lane — a serving runner that "picks up
jobs" would misrepresent an emulated local build. GitHub-hosted x64 stays the
authoritative gate.

---

## Where the lanes stand

- **Linux:** done — green build + 99% ctest + 99.93% warm ccache, golden tagged.
- **Windows:** 24H2-ARM golden boots headless + auto-boots; toolchain installs;
  non-GPU build/test is the MVP target. GPU/Skia lane is a tracked follow-up
  (needs Windows skia-builder slices + the Windows GPU-host product work).
- **macOS:** the proven lane this toolkit generalizes from.
- **Pool serving:** `tartci serve macos|linux|windows` wired (ported from Pulp's
  proven `tools/ci` supervisors and the macOS tartci provider); LaunchAgent
  templates in `launchd/`.

## Host resource governance

A tartci host is shared: CI validation builds, agent builds, and VM runners all
land on the same Mac. tartci is the per-host **governor** that keeps them from
oversubscribing it (two hosts melted in July 2026 — one CPU-bound, one
memory-bound/OOM — before this existed). Three pieces tie together:

- **Weighted lease store** (`scripts/leases.py`) — every build and VM runner
  acquires a lease before it starts. Priority classes (`background` < `build` <
  `vm` < `runner` < `gate`) order contention, and a reserved gate-core headroom
  (`reserved_gate_cores`) keeps the required `macos` gate schedulable even when
  non-gate work fills the host. `tartci leases` inspects/acquires/releases it.
- **Memory as a second axis** — leases carry a memory weight (`--mem-mb`,
  capacity via `--capacity-mem-mb`); admission is `min(core-budget,
  memory-budget)`, so a build is refused when it would exhaust RAM even if cores
  are free. Legacy core-only records are estimated as `cores × per-job memory`
  so a mixed store never over-admits.
- **Role profiles** (`scripts/host_profile.py`) — each host derives a role from
  its cores + `hw.model` — **dedicated-builder**, **dev-overflow**, or
  **light** — each carrying a core budget *and* a memory budget. `tartci
  host-profile` emits the derived budget (`PULP_BUILD_JOBS`,
  `PULP_BUILD_MEM_BUDGET_MB`) that a consumer's build path reads; `tartci status`
  shows the resolved role + capacity. Onboarding persists the role and verifies
  the host is governed — see [Onboarding a new host](#onboarding-a-new-host).

Fleet-level placement on top of these per-host budgets is the Orchard shadow
phase, below.

## Orchard fleet placement (shadow phase)

Orchard (`brew install cirruslabs/cli/orchard`) is the fleet VM-placement layer.
It is adopted **shadow-first**: registered + observable, but placing nothing.
Four safety rails keep it off the required `macos` gate's back:

1. **No lane selects it.** `provider = "orchard"` is valid vocabulary, but
   `tartci profile validate` hard-fails if any lane's `targets` list references
   an orchard target. The dormant `macstudio.macos-arm64-orchard` target in
   `profiles/normal-local-fast.toml` exists only to declare the shape.
2. **Workers are paused.** After each `orchard worker run` (via the
   `orchard-worker` LaunchAgent), run `orchard pause worker <host>` — the
   scheduler skips paused workers.
3. **`org.cirruslabs.tart-vms=0`** in each worker's advertised resources — a hard
   "no VM fits here" even if unpaused.
4. **Killable controller.** The controller runs on always-on m3 as its own
   `orchard-controller` LaunchAgent with an isolated `ORCHARD_HOME`
   (`~/.orchard-shadow`); `launchctl bootout` is the one-command rollback.

Workers advertise **derated** capacity — each host's `vm_pool_cores` from
`tartci host-profile` (m3=14, m5=6, m1=3), not physical — so Orchard leaves room
for the host lease governor and native builds. `tartci status` shows an
`orchard` block (workers + paused count) when `TARTCI_ORCHARD_URL` is set, and
`orchard: not configured` otherwise. Host-side macOS≤2 enforcement
(`macos-vm-cap.lib.sh`, fail-closed) stays authoritative; Orchard placement is
advisory. Templates: `launchd/com.danielraffel.tartci.orchard-{controller,worker}.plist.template`.

## Onboarding a new host

`tartci setup` is the one command to bring a fresh Mac into the pool. Beyond
installing prereqs + creating stores, it now:

1. **Persists the role** — writes `~/.config/tartci/role` from the role
   `host_profile.py` derives (cores + `hw.model`), unless an explicit role file
   already exists (operator intent wins; a re-image/rename can't silently
   re-classify the host).
2. **Runs a verification gate** — confirms `host-profile` advertises a build
   budget (`PULP_BUILD_JOBS`) and the lease store answers. If either fails,
   `tartci setup` reports the host is not fully onboarded instead of exiting
   clean, so a half-provisioned host is visible.

After `tartci setup`, deploy the tartci snapshot to `~/.local/share/tartci`
(rsync) and — for a CI host — register runners. For fleet placement, follow the
Orchard shadow steps above. Helpers: `providers/common/onboard.lib.sh`.

## Opt a host out of the CI pool (`tartci pool`)

`tartci pool {on|off|status}` is the host-level participation switch — "this
machine, not now". It is deliberately decoupled from the per-lane GUI toggles
and from any placement engine, so opting a Mac out can't silently vanish when
lanes change.

- `tartci pool off` — write `~/.config/tartci/native-build-participation=0`
  (the lease governor then refuses native-build leases here) **and**
  `launchctl unload` this host's CI runner agents (`com.danielraffel.pulp.*-runner-*`
  and `actions.runner.*`). Runners are long-lived, so this drains gracefully:
  in-flight jobs finish, GitHub routes new jobs to other Macs.
- `tartci pool on` — participation=1 + `launchctl load` the runner agents.
- `tartci pool status [--json]` — participation state + each runner agent's
  loaded/stopped state.

If `TARTCI_ORCHARD_URL` is set and a worker is configured, `pool off`/`on` also
best-effort `orchard pause`/`resume` this host's worker, so the switch stays
correct if a lane is ever cut over to Orchard placement.
