# Onboarding a new repo to a tartci CI VM — agent playbook

Audience: an automation agent (or a human moving fast) standing up a local CI
VM for a repo that has never used tartci. The human-oriented, gotcha-by-gotcha
bring-up lives in `runbook.md`; this is the *decision-first* path for wiring an
existing project into the proven lanes.

The whole job is: **write one `vm-image` manifest, pick bake-vs-clone, declare
the run contract, prove one green build, then snapshot a golden.** Everything
else is already solved in `providers/` + `docs/gotchas.md`.

## 0. Decide the shape (answer these before touching a VM)

1. **Which OS lane(s)?** linux / windows / macos. Most projects start with one.
2. **Guest arch is ARM64. Always.** Apple Virtualization and QEMU-on-hvf both
   run ARM64 guests on Apple Silicon — there is no x86 guest. If the project
   ships x86_64, set `target_arch = "x86_64"`, `cross = true`, add an
   `[emulation]` table (`emulator = "qemu-user"`, cross `toolchain`,
   `test_labels`), and treat the local lane as *smoke/debug*; keep a
   GitHub-hosted x64 runner as the authoritative gate. Do not pretend a
   cross+emulated local run replaces it. Drive it with `tartci up linux
   --target-arch x86_64` (the Linux/qemu-user lane is wired — see runbook §3.8
   and `manifests/example.x64.toml`).
3. **bake or configure-on-boot?**
   - `bake` — pre-bake a project golden. Choose for hot repos / slow toolchains
     (MSVC, large dep trees): clones are instant and offline.
   - `configure-on-boot` — clone a bare base, apply the manifest on first boot.
     Choose for new / low-volume repos: nothing to re-bake when deps change.
4. **What's the warm cache?** ccache (POSIX) or sccache (Windows). The cache is
   the durable artifact host-mounted across ephemeral clones; build dirs are
   disposable.
5. **Does it need a prebuilt Skia (or other large baked dep)?** Match the
   archive arch to the guest arch (`linux-arm64`, not `linux-x64`). There is no
   prebuilt Windows Skia yet — Windows builds GPU-off until one exists.

## 1. Write the manifest

Copy `manifests/example.toml` to the repo (convention: `.shipyard/vm-image.toml`
or `.tartci/vm-image.toml`) and fill it in. `manifests/pulp.linux.toml` and
`pulp.windows.toml` are real, proven instances to crib from. Minimum viable:

```toml
schema = 2
name   = "<project>-<os>-build"
provider = "tart"            # tart=linux/macos, qemu=windows
strategy = "configure-on-boot"
os = "linux"  arch = "arm64"  target_arch = "x86_64"  cross = true
base = "ghcr.io/cirruslabs/ubuntu:24.04"
[apt]   packages = [ ...build deps... ]
[run]   configure = "..."  build = "..."  test = "..."
```

The **`[run]` contract is the load-bearing part**: keep `configure`/`build`/
`test` identical in spirit across providers so CI is hypervisor-agnostic. The
job runner clones the golden, mounts the cache, then executes these.

## 2. Bring up the base (once per OS)

- **Linux / macOS (Tart):** pull the OCI base, `tart clone`, `tart set` the
  CPU/RAM/disk, boot. Durable SSH keys go in an *unmanaged*
  `authorized_keys_ci` + an sshd drop-in (cloud-init reverts the managed file +
  `/etc/fstab` every boot — see `docs/gotchas.md`). Mount the host ccache via a
  systemd `.mount` (virtio-fs), not `/etc/fstab`.
- **Windows (QEMU):** follow the `providers/qemu-windows/` recipe verbatim — 24H2
  ISO (not 25H2), 512-byte pad, NVMe disk, `ramfb` display, ALL install media on
  `usb-storage`, edk2 vars seeded from the template. Provision over SSH with
  `powershell -EncodedCommand` (scp'd `.cmd` mis-execute). The MSVC silent-no-op
  trap and its nuke-reboot fix are in `docs/gotchas.md`.

## 3. Prove one green build, then golden it

1. Run the `[run]` contract by hand in the VM until `build` is green and `test`
   passes (expect a short triage of portability breaks — that's normal; capture
   each fix as a real upstream PR to the project, guarded by platform macros).
2. Record the run to `metrics.jsonl` (one JSON line: os/arch/provider/mode +
   `configure_s`/`build_s`/`ctest_s`/cache% — see `metrics/sample.jsonl`).
3. **Tag the golden**: clean-shutdown the guest, then
   `qemu-img convert -c` (QEMU) or `tart export` (Tart) the powered-off disk to a
   dated, compressed golden under your goldens store. The golden stays
   **generic + headless** — no live Tailscale identity, no baked private keys, no
   repo checkout assumptions. Per-run state lives in the launch wrapper, not the
   image.

## 4. Wire it for repeat use

- CI clones the golden per job (ephemeral, unique hostname + hostfwd port).
- A human `bench` clone (`bench/bench.sh <os>`) is a *separate persistent* copy
  opened in UTM for GUI/DAW testing — neither CI nor UTM boots the golden
  directly.
- If `pulp` (or another supported CLI) is installed, it can soft-detect this
  toolkit and delegate (`pulp vm up <os>`); absence is never a punishment.

## Invariants (do not violate)

- ARM64 guests only; x86_64 is cross+emulate, GitHub stays the x64 gate.
- Golden is pristine/generic/headless; clones carry all per-run + GUI state.
- Keep the storage controller constant between golden and bench (NVMe on
  Windows); only the display profile + installed apps differ.
- Scripts/configs/docs are version-controlled; **images/ISOs/keys never are**
  (they're gitignored and live in local stores).
