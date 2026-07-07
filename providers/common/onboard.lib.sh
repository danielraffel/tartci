# tartci host onboarding helpers — persist the derived role and prove the host
# is governed. Sourced by `tartci setup`; kept in a lib so it is unit-testable.
# shellcheck shell=bash

# Persist the host's governance role so a re-image / rename can't silently
# re-classify it. If ~/.config/tartci/role already exists it wins (operator
# intent); otherwise write the role host_profile derives from cores + model.
tartci_onboard_role() {
  local root="$1"
  local role_file="${TARTCI_ROLE_FILE:-$HOME/.config/tartci/role}"
  if [ -f "$role_file" ]; then
    printf 'role: %s (from %s)\n' "$(cat "$role_file")" "$role_file"
    return 0
  fi
  local role
  role="$(python3 "$root/scripts/host_profile.py" --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["role"])' 2>/dev/null)"
  if [ -n "$role" ]; then
    mkdir -p "$(dirname "$role_file")"
    printf '%s\n' "$role" > "$role_file"
    printf 'role: %s (auto-derived -> %s)\n' "$role" "$role_file"
    return 0
  fi
  printf 'role: could not derive (host_profile unavailable)\n'
  return 1
}

# Verification gate: the host is onboarded only if the profile advertises a
# build budget AND the lease store answers. Returns non-zero on failure so
# `tartci setup` surfaces a half-provisioned host instead of reporting success.
tartci_onboard_verify() {
  local root="$1" rc=0 jobs
  printf 'verify:\n'
  jobs="$(python3 "$root/scripts/host_profile.py" 2>/dev/null \
    | awk -F= '/^PULP_BUILD_JOBS=/{print $2; exit}')"
  if [ -n "$jobs" ] && [ "$jobs" -ge 1 ] 2>/dev/null; then
    printf '  host-profile: PULP_BUILD_JOBS=%s ok\n' "$jobs"
  else
    printf '  host-profile: did not emit a build budget FAIL\n'; rc=1
  fi
  if python3 "$root/scripts/leases.py" status >/dev/null 2>&1; then
    printf '  lease store: healthy ok\n'
  else
    printf '  lease store: unavailable FAIL\n'; rc=1
  fi
  if [ "$rc" -eq 0 ]; then
    printf '  host onboarded: role persisted + lease governor active\n'
  fi
  return "$rc"
}
