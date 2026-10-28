# Where state lives

Paths are defaults. Most can be overridden by the environment variable named
beside them. `~` is the serving user's home directory.

## Code

| path | what |
|---|---|
| `~/.local/bin/tartci` | installed shim; runs the installed support generation |
| `~/.local/share/tartci-generations/` | immutable support generations, one per installed commit |
| `~/.local/bin/agent-hygiene` | wrapper for `scripts/agent-hygiene` |

A git checkout of this repository is not what a host's supervisors run.

## Configuration (`~/.config/tartci/`)

| path | what |
|---|---|
| `governor.toml` | per-host governor settings (`tartci governor set`) |
| `macos-fleet-profile.toml` | the host's fleet profile (`TARTCI_FLEET_PROFILE`) |
| `macos-fleet-install.json` | install receipt: the services `pool on` restores |
| `self-update.toml` | self-update settings (`[launcher] auto_apply = false` requires the manual launcher ack) |
| `native-build-participation` | `1` participating, `0` opted out (`TARTCI_POOL_PARTICIPATION_FILE`) |
| `pool-state` | `on`, `draining` or `off` (`TARTCI_POOL_STATE_FILE`) |
| `macos-vm-cap` | live macOS guest cap, clamped to 1 to 2 (`TARTCI_MACOS_CAP_FILE`) |
| `macos-vm-reservations/` | per-boot macOS slot reservations (`TARTCI_MACOS_RESV_DIR`) |

## Runtime state (`~/.tartci/`, `TARTCI_HOME`)

| path | what |
|---|---|
| `goldens/`, `bench/`, `windows/`, `iso/` | image stores created by `tartci setup` |
| `state/leases/leases.json` | host lease store (`TARTCI_LEASE_DIR`) |
| `state/macos/`, `state/linux/` | per-lane state files and heartbeats; macOS JIT denial receipts (`TARTCI_STATE_DIR`) |
| `state/job-claims/` | live boot claims |
| `state/pool-log/events.jsonl` | pool status history |
| `state/self-update/` | last attempt, waiting ticket, `launcher-auto-apply.json` (a launcher target whose automatic apply failed and is held) |
| `state/gh-identity.json` | cached GitHub credential identity probe |
| `state/vm-dhcp/breaker.json` | VM DHCP breaker |
| `runtime/` | runtime records when `TARTCI_RUNTIME_MEASURE=1` (`TARTCI_RUNTIME_STORE`) |

## VM images

`TART_HOME` holds Tart VMs. The macOS lanes default it to `~/VMs`. Set it
explicitly for the Linux lane, whose script default is a machine-specific
path. Lanes that share a host must share one `TART_HOME`.

## Logs and agents

| path | what |
|---|---|
| `~/Library/Logs/tartci/` | lane and support-agent logs |
| `~/Library/LaunchAgents/com.danielraffel.*.plist` | installed LaunchAgents (templates in `launchd/`) |

## Data that is not in this repository

Host addresses and the fleet's SSH table live in a separate private repository
that a few scripts read at runtime through the GitHub App CLI. This repository
holds no addresses. Treat that table as read-only.
