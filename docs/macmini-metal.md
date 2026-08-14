# Intel Mac mini — native Metal compatibility lane

The Intel Mac mini is a bare-metal provider. It does not run TartCI, Tart, or
Apple Silicon VMs. Its value is current Intel macOS/Xcode/Metal compatibility
that cannot be represented by an ARM64 Tart guest.

## Contract

| Property | Policy |
| --- | --- |
| Provider | native macOS metal, supervised by Shipyard/TartCI fleet policy |
| Architecture | Intel x86_64 |
| Tart | not installed and not required |
| Work | isolated Intel compatibility/canary jobs only |
| Credentials | ephemeral runner registration; no reusable checkout credentials |
| Workspace | wiped before and after every job; caches remain outside the workspace |
| Release role | compatibility evidence only; not an ARM64 release substitute |
| Fallback | hosted Intel macOS when the machine is offline, unhealthy, or busy |

The host must not receive privileged signing, deployment, `pull_request_target`,
or secret-bearing jobs. It is native metal rather than a disposable VM, so the
workspace wipe and cache boundary must be audited explicitly; this does not
claim VM-grade OS isolation.

## Fleet relationship

The capability map is intentionally asymmetric:

- `macpro` supplies native x86_64 Linux through Proxmox/systemd and is not a
  TartCI provider; its Windows x64 candidate follows the same separate
  Proxmox provider contract.
- M1, M3, and M5 supply disposable ARM64 macOS VMs through Tart/TartCI.
- `macmini` supplies native Intel macOS/Metal directly on hardware.

Shipyard selects by repository-scoped capability label and live health lease.
TartCI owns the disposable Tart provider, while the Proxmox and bare-metal
providers expose the same health, assignment, teardown, and hosted-fallback
contract without pretending they are Tart VMs.

## Bring-up checklist

1. Confirm Intel identity, supported macOS/Xcode/Metal versions, disk space,
   and native runner service health.
2. Register a unique ephemeral runner in the repository-scoped Intel group;
   never reuse a static GitHub runner name.
3. Verify the pre-job workspace wipe, post-job wipe, cache exclusion, and
   teardown/revocation with an actual canary job.
4. Record the exact label, group, host health lease, and fallback selector in
   the repository profile.
5. Enable the selector only after the canary passes; expire it immediately on
   stale heartbeat, failed wipe, or capacity loss.

