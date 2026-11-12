# TartCI cache policy status ledger

- Scope: TartCI/Shipyard cache-policy improvements for repeat Windows/Linux builds.
- Base SHA: `c90edfb0795396fcc6c58846189cce3de4ea118c` (fresh worktree from `origin/main`).
- Review repair implementation SHA: `1a9a221026066c2a468c066902dc10d3e0788c4a`.
- Current implementation SHA: `c5716469880f7fde9898b1334dfe5329edc63d51`. Pure `--print-build-env-script` and `--print-listener-script` parsing now run before QEMU/GitHub prerequisite checks after CI exposed the fixture's missing-QEMU dependency.
- PR: https://github.com/danielraffel/tartci/pull/547
- Ownership limits: no live UTM or Proxmox builds, VM images, runners, macOS capacity, Windows runner registration, or TartCI PR 546.
- Evidence reviewed: Linux Tart runner already propagates the granted lease to `CMAKE_BUILD_PARALLEL_LEVEL`; Windows QEMU runner sized the VM from the lease but did not propagate the grant to the Actions job. Active build measurements supplied for comparison: ARM UTM governed at 1 core; Proxmox x64 SDK on `B:` with FetchContent junction governed at 4 cores. The active Pulp Windows workflow explicitly uses build `--parallel 4` and tests `-j8`, so those steps remain unchanged.
- Change: Windows JIT listener now exports the lease-granted `effective_win_cpus` as `CMAKE_BUILD_PARALLEL_LEVEL` and emits a diagnostic. Explicit workflow `-j` remains caller-owned.
- Behavior test: `--print-build-env-script 4` and `--print-listener-script 6` emit the generated PowerShell consumed by the runner; the listener test proves the placeholder is replaced before `Runner.Listener.exe`, and zero is rejected. Explicit workflow `--parallel`/`-j` overrides remain outside this change.
- CI failure evidence: https://github.com/danielraffel/tartci/actions/runs/38824131053/job/116727809000 failed because the behavior test reached `qemu-system-aarch64` before handling the pure print option (`qemu not installed`).
- Tests after repair: `bash -n providers/qemu-windows/runner.sh`; focused propagation/listener tests; `python3 -m unittest scripts.test_vm_lease_helper scripts.test_linux_runner_lifecycle` (85 tests); `./scripts/lint.sh` — all PASS.
- Limits: this changes the Windows job environment only; it does not add sccache, FetchContent persistence, or prove physical Windows cache reuse. A separate cache mount needs a Windows guest transport and exact cache ownership proof.
