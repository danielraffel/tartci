# Additive Shipyard stewardship scheduler

`com.danielraffel.shipyard.steward-scheduler` is a separate, default-off
controller for durable multi-repository PR stewardship. It does not replace or
modify the legacy `shipyard.queue-tick` service during the initial rollout.
The scheduler obtains its entire authority from the user-owned, mode-600
`~/.config/shipyard/steward-scheduler.json`; unknown fields, non-canonical
checkouts, origin mismatches, duplicate repositories, and an enabled config
without `authority=true` fail closed.

Each tick acquires one nonblocking host lock, strips ambient GitHub tokens, and
runs the following externally bounded sequence:

1. One `shipyard runner steward --repo OWNER/REPO --apply` in each configured
   checkout. A failure or timeout is recorded for that repository and does not
   prevent the remaining repositories from being considered.
2. After every deterministic repository pass, exactly one
   `shipyard runner recovery-worker --once --apply`. Shipyard owns request
   deduplication, provenance revalidation, and recovery selection; this
   scheduler never invokes Codex, Claude, or another repair agent directly.

It atomically publishes a bounded report and health verdict under
`~/Library/Logs`, and rotates its own operational log. Launchd stdout/stderr go
to `/dev/null`, so they cannot grow outside that rotation policy. Command output
is drained only into fixed in-memory caps; a detached descendant that retains a
pipe after timeout cannot extend the drain deadline. It durably quarantines the
controller before any peer or recovery mutation, and later ticks remain inert
until an operator proves no descendant remains and removes the quarantine file
under `~/.local/state/tartci`. Because a detached process can close its pipes,
every mutation-command timeout takes this same fail-closed quarantine path.

Start with a plan, then install the inert service:

```sh
scripts/install_shipyard_steward_scheduler.sh \
  --repo Generous-Corp/pulp=/absolute/pulp \
  --repo Generous-Corp/forge=/absolute/forge \
  --repo Generous-Corp/vellum=/absolute/vellum

scripts/install_shipyard_steward_scheduler.sh \
  --repo Generous-Corp/pulp=/absolute/pulp \
  --repo Generous-Corp/forge=/absolute/forge \
  --repo Generous-Corp/vellum=/absolute/vellum \
  --install
```

The installer stages and byte-verifies the executable, writes the protected
config, performs a full bootout/bootstrap, lets `RunAtLoad` start exactly one
tick without a duplicate `kickstart -k`, checks the live registration, and
requires a fresh `disabled` health receipt. A live install requires a separate
fresh startup receipt so the installer is not coupled to the maximum duration
of a complete bounded tick. That receipt is installation evidence only; the
first terminal health report remains the live canary gate. If any install step
fails, the installer restores the prior executable/config/plist and reloads the
previously loaded job.

Only after exact install, inert canary, and rollback evidence is reviewed should
one controller be armed with `--mode live --authority --install`. Roll back to
the inert state by rerunning the installer without `--mode live`; do not edit
the installed plist or config by hand. Keep the legacy queue tick until the new
service has separate live canary and rollback proof.
