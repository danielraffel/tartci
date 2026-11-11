# Fleet operator scripts

One-off, idempotent host changes an operator runs by hand on a fleet Mac.
tartci never runs them. Each defaults to a read-only plan; the privileged
step needs `sudo` and refuses without it, printing the exact command.

| Script | Plan | Apply | What apply changes |
|---|---|---|---|
| `sshd-pubkey-only.sh` | `fleet/ops/sshd-pubkey-only.sh` | `sudo fleet/ops/sshd-pubkey-only.sh --apply` | Writes `/etc/ssh/sshd_config.d/100-fleet-pubkey-only.conf` (`PasswordAuthentication no`, `KbdInteractiveAuthentication no`) after proving the sudo user has key login to localhost. Undo: `sudo fleet/ops/sshd-pubkey-only.sh --rollback`. |
| `install-tartci-heal-tcc.sh` | `fleet/ops/install-tartci-heal-tcc.sh` | `sudo fleet/ops/install-tartci-heal-tcc.sh --apply` | Installs `tartci-heal-tcc` to `/usr/local/sbin/` (root:wheel 0755) and `/etc/sudoers.d/tartci-heal-tcc` (root:wheel 0440, `visudo -cf` validated before and after): the sudo user may run that one path with **no arguments** (empty argument spec), NOPASSWD. The wrapper runs exactly `/usr/bin/killall tccd sandboxd`, at most once per 30 min (root-owned stamp in `/var/run`), logging to `/var/log/tartci-heal-tcc.log`. The launchd watchdog invokes it only after `scripts/tcc_wedge.py` classifies a Tart-store hang as a wedged tccd (consecutive timeouts, internal control path answers, sandboxd/tccd Removable Volumes failures in the log). This is the only tartci-run privileged action, and it is the exception to "tartci never runs them". Undo: `sudo fleet/ops/install-tartci-heal-tcc.sh --rollback`. |
| `create-ci-runner-user.sh` | `fleet/ops/create-ci-runner-user.sh` | `sudo fleet/ops/create-ci-runner-user.sh --apply` | Creates the hidden, non-admin `ci` user with its own primary group (not `staff`), no password (login disabled), no SSH keys and no keychain secrets; sets the invoking user's home to 0700 if group/other can enter it (a stock home is 0750 owner:staff) and reports, without changing, `~/.ssh`, `~/.config/pulp/secrets` and `~/.config/shipyard/github-apps` modes; then prints the shell-quoted per-runner commands that move each persistent runner under `~/actions-ci/*/` onto it. It re-registers nothing itself. |

**sshd lockout guard scope.** It proves one key path only: the sudo user,
from this host, to localhost. Other accounts and keys on other machines are
not checked. If SSH access is lost after `--apply`, sign in at the local
console or over Screen Sharing (neither uses sshd) and run
`sudo fleet/ops/sshd-pubkey-only.sh --rollback`.

`--help` on either script explains why it exists. The dry-run tests are
`tests/test_fleet_ops_scripts.sh` (stub binaries and a temp dir; nothing on
the host changes).
