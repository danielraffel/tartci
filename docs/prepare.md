# Prepare VM images

`prepare` is not one generic image builder. The command selects a provider
script, and each provider has a different level of automation.

## Linux

```bash
tartci prepare linux
```

This runs `providers/tart-linux/provision.sh`. It is the repository's
Pulp-oriented Linux bake: it pulls the configured Ubuntu arm64 base, creates a
Tart VM, installs the build dependencies and ccache, enables Rosetta support,
and prepares the Pulp dependencies used by the Linux lane. The host cache is
mounted when the lane runs; it is not part of this bake command.

The default image name is `pulp-linux-build`. Use provider options such as
`--name`, `--base`, `--cpu`, `--memory`, and `--disk` to change the bake. Pin
`--base` to a digest when the image must be reproducible. This script is not a
generic project importer. For another repository, start with a base image and
follow [the repository onboarding guide](new-repo-agent-guide.md).

## macOS

```bash
tartci prepare macos list
tartci prepare macos resize <vm> <GB>
tartci prepare macos tag <source-vm> <base-name> [YYYY-MM-DD]
tartci prepare macos pulp-readiness
```

These are helper operations for a macOS golden. They list and resize VMs, tag a
powered-off VM, and report whether the current Pulp manifest has the required
inputs. They do not install Xcode, bake a complete macOS toolchain, or certify
that a VM is ready for every repository.

## Windows

Windows uses QEMU, so its image flow has a separate command:

```bash
tartci windows golden [name]
```

This snapshots a prepared, powered-off Windows VM, writes its provenance, and
stores it as a reusable golden. It does not install Windows or build the
toolchain. The image recipe and its prerequisites are in the
[Windows runbook section](runbook.md#4-windows-lane-qemu).

## After preparing an image

Run a disposable build before serving jobs:

```bash
tartci up <os>
tartci status --json
```

For a GitHub Actions pool, configure the repository, golden name, and labels,
then use `tartci serve <os>`. The default serving path uses one disposable VM
per job; a warm VM mode is available for selected lanes. See [the runbook](runbook.md) for host setup and
[the reference](reference.md) for provider settings.

## Repositories that use mise

TartCI does not manage mise for a repository. If a project has a mise config or
lock file, install the pinned mise version in the image recipe or in its
configure-on-boot command, then run `mise install` inside the VM. Make sure the
VM has the network access or cached downloads that the install needs. Keep the
mise files in the repository so a new golden can be rebuilt from the same tool
versions. This is a repository setup decision, not a host-wide TartCI setting.

The current TartCI mise work is narrower: it can pin and verify a mise binary
when a fleet profile declares one. That check does not install mise, change
`PATH`, or make a VM use mise-managed tools. A VM will use mise only after its
image or project setup explicitly installs it and runs `mise install`.

The manifest already has a `[toolchain]` section, but providers do not yet
interpret it as a mise environment. Treat that as a current limitation: image
recipes must install and verify their own mise toolchain until the provider
contract gains explicit support for a lock file.
