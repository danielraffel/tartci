# Host network-interface policy shared by every provider supervisor.
# shellcheck shell=bash

tartci_network_interface_preflight() {
  local root="$1"
  case "${TARTCI_NETWORK_POLICY:-apply}" in
    apply) python3 "$root/scripts/network_interface_policy.py" --apply ;;
    report) python3 "$root/scripts/network_interface_policy.py" ;;
    off)
      printf 'network-policy: disabled by TARTCI_NETWORK_POLICY=off\n'
      return 0
      ;;
    *)
      printf 'network-policy: invalid TARTCI_NETWORK_POLICY=%s (apply|report|off)\n' \
        "$TARTCI_NETWORK_POLICY" >&2
      return 2
      ;;
  esac
}
