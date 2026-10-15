# Invariants

Each line is a rule the code holds. The source that enforces it is in
parentheses.

## Scheduling and ownership

- GitHub Actions assigns jobs to runners by label. tartci only decides whether
  to boot a VM for a queued job whose labels it can satisfy.
- tartci never merges, enqueues, dequeues, arms or cancels a pull request.
  Queue policy belongs to Shipyard or the repository.
- Each VM serves at most one job. Its runner is a just-in-time (JIT)
  registration, and the VM is deleted after the job (`providers/*/runner.sh`).
- Runner registrations get a unique name per boot. A lane reclaims only an
  offline registration that belongs to its own slot.

## Capacity

- A VM boot takes a lease first. The lease has CPU, memory and per-volume disk
  axes; admission is the minimum over all axes (`scripts/leases.py`).
  `TARTCI_VM_LEASES=0` disables leases for break-glass debugging only.
- At most two macOS guests run per host. This is Apple's limit and a hard
  ceiling no setting may exceed (`TARTCI_MACOS_HARD_MAX`,
  `providers/tart-macos/macos-vm-cap.lib.sh`). Lanes that share a host must
  share one `TART_HOME` so the count is host-wide.
- `tartci pool drain` and `tartci pool off` refuse when this host is the last
  one serving a required gate label (`scripts/capacity_floor.py`). An
  indeterminate answer also refuses.

## Reading state

- An unknown is never reported as healthy. `doctor fleet`, `doctor --reap` and
  `observe macos` each have a distinct exit code for "could not determine".
- A failed GitHub queue scan prints `ERR`, never `0`. After about three minutes
  of continuous failed scans the serving loop exits 75 so launchd restarts it
  with fresh credentials.
- A successful scan that returns `0` means no matching job in the scanned
  window. It does not prove the repository queue is empty.
- Zero online self-hosted runners at idle is normal: runners exist only while a
  job is being served.

## Credentials

- `TARTCI_GH_CLI` selects the CLI for every GitHub call a lane makes (default
  `gh`). The lane uses only that CLI and exits when it is missing; it has no
  fallback to another credential.
- `TARTCI_JIT_GH_CLI`, when set, is used only for `generate-jitconfig`.
- On the macOS lane, a 401, 403 or 404 on JIT registration writes a denial
  receipt (`jit-admission-denied.<key>` in the lane's state directory) keyed by repo,
  runner group, labels and CLI. The lane boots no further VM for that key until
  the route is repaired and the receipt cleared.

## Deletion

- `doctor --reap --fix` deletes a VM or overlay only when its name has an owned
  CI prefix and a tartci state file marks it owned, or when it is a provably
  orphaned, stopped lane VM older than `--orphan-age-secs`. Goldens, bench VMs
  and `held-` VMs are protected.
- `tartci image-prune` and `tartci retention` only plan unless given `--apply`.
- VM images are never committed to this repository.

## Rollout

- Fleet changes go to one host first. `tartci fleet-macos self-update --apply`
  updates one host at a time and defers while another host is draining,
  updating or unreadable (`scripts/fleet_self_update.py`).
- A host's running supervisors execute the installed support generation, not a
  git checkout. Readiness is judged against the installed generation.
- Off-by-default features stay off until turned on per host
  (examples: `[debug_hold]`, admission-clean `required` mode, host-health
  yield).
