# Shared host-health auto-yield helper for every VM provider supervisor.
# shellcheck shell=bash
#
# One implementation of the "should we boot a NEW VM right now, or is the host
# too saturated?" decision, sourced by all three provider runners (tart-macos,
# tart-linux, qemu-windows) so the whole host backs off together and the policy
# can never drift between lanes. Extracted from the byte-identical copies each
# runner used to carry.
#
# Opt-in via TARTCI_HOST_VITALS_YIELD; off by default so a host that never
# installs host_vitals is byte-for-byte unchanged. Reads the shared external
# `host_vitals.sh` probe (name overridable via TARTCI_HOST_VITALS_BIN), which
# tartci deliberately does not ship — its contract is exit 0 green, 10 warn,
# 20 critical.
#
# FAIL OPEN (the deliberate opposite of priority_demand's fail-closed): if the
# probe is missing, unexecutable, or errors, print 0 (boot). Host-health yield
# is a crash-avoidance nicety, not a correctness gate — a broken probe must
# never wedge a required lane. The worst case of fail-open is that we forgo the
# avoidance we cannot measure, exactly where we were before the feature.
#
# Prints 1 when the loop should STOP booting new VMs (host saturated), 0 when it
# is safe to boot. Yields on CRITICAL (>=20) always, and on WARN (>=10) only
# when TARTCI_HOST_VITALS_YIELD_ON_WARN is set. Self-contained: reads its env
# directly so it can be sourced and exercised in isolation.
tartci_host_health_yield(){
  local yield="${TARTCI_HOST_VITALS_YIELD:-}"
  local bin="${TARTCI_HOST_VITALS_BIN:-host_vitals.sh}"
  local on_warn="${TARTCI_HOST_VITALS_YIELD_ON_WARN:-}"
  [ -n "$yield" ] && [ "$yield" != 0 ] || { printf '%s\n' 0; return 0; }
  command -v "$bin" >/dev/null 2>&1 || { printf '%s\n' 0; return 0; }
  # `local code=0; ... || code=$?` keeps set -e from aborting on a non-zero exit,
  # and declaring first avoids `local x=$(...)` masking the command's status.
  local code=0
  "$bin" >/dev/null 2>&1 || code=$?
  if [ "$code" -ge 20 ]; then
    printf '%s\n' 1
  elif [ "$code" -ge 10 ] && [ -n "$on_warn" ] && [ "$on_warn" != 0 ]; then
    printf '%s\n' 1
  else
    printf '%s\n' 0
  fi
}
