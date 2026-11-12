# shellcheck shell=bash
# The Tart store a provider script uses: $TART_HOME, else the installed fleet
# profile's host.tart_home. There is no built-in default path: a host without
# either is refused rather than pointed at a store it does not have.
# shellcheck source=toml-python.lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/toml-python.lib.sh"

tartci_default_tart_home() {
  if [ -n "${TART_HOME:-}" ]; then
    printf '%s\n' "$TART_HOME"
    return 0
  fi
  local profile="${TARTCI_FLEET_PROFILE:-$HOME/.config/tartci/macos-fleet-profile.toml}"
  if [ ! -f "$profile" ]; then
    echo "tartci: set TART_HOME, or install a fleet profile with host.tart_home ($profile)" >&2
    return 1
  fi
  tartci_toml_python -c '
import sys, tomllib
with open(sys.argv[1], "rb") as handle:
    home = (tomllib.load(handle).get("host") or {}).get("tart_home")
if not isinstance(home, str) or not home.startswith("/"):
    raise SystemExit(f"tartci: {sys.argv[1]} has no absolute host.tart_home; set TART_HOME")
print(home)' "$profile"
}
