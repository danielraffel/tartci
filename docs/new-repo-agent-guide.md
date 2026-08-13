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
   `[emulation]` table (`emulator = "rosetta"` on Linux, cross `toolchain`,
   `test_labels`), and treat the local lane as *smoke/debug*; keep a
   GitHub-hosted x64 runner as the authoritative gate. Do not pretend a
   cross+emulated local run replaces it. Drive it with `tartci up linux
   --target-arch x86_64` (the Linux/Rosetta lane is wired — see runbook §3.8
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
  `powershell -EncodedCommand` for short commands (scp'd `.cmd` mis-execute),
  but stream longer runner/preflight scripts into guest `.ps1` files to avoid
  cmd.exe's command-line limit. The MSVC silent-no-op trap and its nuke-reboot
  fix are in `docs/gotchas.md`.

## 3. Prove one green build, then golden it

1. Run the `[run]` contract by hand in the VM until `build` is green and `test`
   passes (expect a short triage of portability breaks — that's normal; capture
   each fix as a real upstream PR to the project, guarded by platform macros).
2. If the lane is served through GitHub Actions, prefer automatic runtime
   capture: set `TARTCI_RUNTIME_MEASURE=1` on the serving invocation and query
   `tartci runtime summary --repo owner/repo --run-id <id> --json` when the job
   completes. For older hand-driven bring-up, backfill existing timing files
   with `tartci runtime backfill --repo owner/repo --timing <log-root>`.
   `metrics.jsonl` remains a manual fallback for quick experiments.
3. **Tag the golden**: clean-shutdown the guest, then
   `qemu-img convert -c` (QEMU) or `tart export` (Tart) the powered-off disk to a
   dated, compressed golden under your goldens store. The golden stays
   **generic + headless** — no live Tailscale identity, no baked private keys, no
   repo checkout assumptions. Per-run state lives in the launch wrapper, not the
   image.

## 4. Wire it for repeat use

- CI clones the golden per job (ephemeral, unique hostname + hostfwd port).
- For macOS GitHub Actions serving, keep distinct workflow lanes on distinct
  labels. A build gate can use a shared VM pool label such as `pulp-build-vm`;
  a release workflow should use a separate label such as
  `pulp-build-vm-release`. When workflows in one physical pool have different
  urgency, keep one supervisor and use ordered
  `TARTCI_RUNNER_WORKFLOW_TIERS` lines (`class-label|workflow`). Give each
  tier mutually exclusive workflow labels; discovery order alone cannot enforce
  priority because GitHub chooses the job after JIT registration. For equal
  priority, use one repeated additional-label set and GitHub preserves FIFO.
  When more than one host serves a pool, add an extra host-specific label or
  explicit `--name-prefix` so JIT runner names do not collide.
- For Windows QEMU GitHub Actions serving, install the same qcow2 golden and
  home-backed tartci copy on each Apple Silicon host, keep
  `/opt/homebrew/bin` in the launchd PATH, and use
  `TARTCI_RUNNER_QUEUE_MATCH_LABELS=1` so supervisors only boot for queued jobs
  whose labels they can satisfy. Prove with a Windows-native workflow before
  setting a repo-level Windows `runs-on` variable. Speed comes first from moving
  deterministic preflight work into the golden and adding persistent Windows
  caches; keep warm VM pools as a later optimization after the cold CoW lane is
  reliable. If the workflow was written for GitHub-hosted Windows, explicitly
  bake the hosted-runner assumptions it uses, commonly Git Bash on `PATH`,
  Chocolatey, `ccache`, and `C:\tmp`.
- A human `bench` clone (`bench/bench.sh <os>`) is a *separate persistent* copy
  opened in UTM for GUI/DAW testing — neither CI nor UTM boots the golden
  directly.
- Add or update a routing profile when the repo will use local runners. Keep the
  profile parseable TOML (`.shipyard/ci-profiles/<name>.toml`,
  `.tartci/<name>.toml`, or tartci `profiles/<name>.toml`) and include comments
  for `pr`, `release`, `coverage`, `scheduled`, and `issue_on_failure` so agents
  do not need a second document to understand the policy. This is optional:
  repos can use tartci directly with `tartci up` / `tartci serve`, use Shipyard
  to resolve profiles and dispatch GitHub workflows, or stay entirely on
  GitHub-hosted runners.
- If `pulp` (or another supported CLI) is installed, it can soft-detect this
  toolkit and delegate (`pulp vm up <os>`); absence is never a punishment.

### Fleet onboarding and drift prevention

Use `profiles/normal-local-fast.toml` as the vocabulary contract shared with
Shipyard. Add a repository stanza for each workflow class you intend to route:
`pr`, `debug`, `release` (build), `coverage`, and `scheduled`. Declare signing,
deployment, privileged, and secret-bearing jobs as hosted-only unless a
separate security review establishes a trusted lane. Do not invent labels in a
workflow: add a target ID to the catalog, run `tartci profile validate`, then
have Shipyard resolve the exact selector before dispatch.

Target IDs and `host/lane/slot` identities are stable. Disposable GitHub
registration names are intentionally unique per boot; supervisors reclaim only
offline registrations from their own slot. Never restore a static GitHub runner
name to make monitoring easier. A new repository is hosted-only until its
profile, image, exact labels, fallback, and one real dispatch proof are present.

## Invariants (do not violate)

- ARM64 guests only; x86_64 is cross+emulate, GitHub stays the x64 gate.
- Golden is pristine/generic/headless; clones carry all per-run + GUI state.
- Keep the storage controller constant between golden and bench (NVMe on
  Windows); only the display profile + installed apps differ.
- Scripts/configs/docs are version-controlled; **images/ISOs/keys never are**
  (they're gitignored and live in local stores).
