# Shared disk-capacity configuration parsing for VM provider helpers.
# shellcheck shell=bash

# Print a non-negative GiB value. The boolean spellings accepted by the
# pre-existing disk-floor helper remain supported and normalize to zero, so a
# rolling upgrade cannot turn an existing break-glass setting into a permanent
# admission failure.
tartci_disk_gb_or_zero(){
  local name="$1" value="${2:-}" default_value="$3"
  [ -n "$value" ] || value="$default_value"
  case "$value" in
    false|FALSE|off|OFF|no|NO) printf '%s' 0 ;;
    *[!0-9]*)
      printf 'invalid %s=%s (expected a non-negative integer or false/off/no)\n' \
        "$name" "$value" >&2
      return 75
      ;;
    *) printf '%s' "$value" ;;
  esac
}
