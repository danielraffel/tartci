# launchd templates — serve the GitHub Actions pool at boot

These are LaunchAgent templates that run the per-job ephemeral runner supervisors
(`providers/tart-linux/runner.sh`, `providers/qemu-windows/runner.sh`) under
`launchd` so a host serves the pool across reboots. They are the persistent
counterpart of `tartci serve <os> --loop`.

## Generic toolkit vs. Pulp's concrete instance

The **runner scripts are project-agnostic** — repo, golden, labels, and the
"is there queued work?" workflow name are all env-driven
(`TARTCI_RUNNER_REPO`, `TARTCI_LINUX_GOLDEN` / `TARTCI_WIN_GOLDEN`,
`TARTCI_RUNNER_LABELS`, `TARTCI_RUNNER_WORKFLOW_NAME`).

The **two templates here are Pulp's concrete instance** — the first consumer.
Their `Label`s are `com.danielraffel.pulp.tart-runner-linux` and
`com.danielraffel.pulp.qemu-runner-windows` because the
[shipyard-macos-gui](https://github.com/danielraffel/shipyard-macos-gui) "Serve
CI builds from this Mac" switch hard-codes those labels (its
`CIServingLane.known`) to `launchctl load/unload` them. Keep the labels exactly
as-is when serving Pulp.

## Serving a different repo

1. Copy a template to `com.<you>.<repo>.<provider>.plist.template`.
2. Change the `<Label>` and the `--labels` argument to your repo's runner labels.
3. Point `TARTCI_RUNNER_REPO` (and golden/labels) at your repo via the plist's
   `EnvironmentVariables` or the `runner.sh` env defaults.
4. If you drive it from the Shipyard macOS GUI, add a matching row to that app's
   `CIServingLane.known` with your new label.

## Install (Pulp)

See the header comment in each `.plist.template` for the exact `sed` install
recipe (launchd does **not** expand `$HOME`/`$TARTCI_REPO` — the install `sed`
must write absolute paths). Logs land in `~/Library/Logs/tartci/`.
