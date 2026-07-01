# Host-health auto-yield for the macOS serve loop — 2026-07-01

## Problem

A tartci host that also carries an interactive / RAM-heavy workload can saturate
its memory and die: memory-pressure critical → jetsam kills processes →
WindowServer crashes → **unclean reboot**, taking the in-flight required `macos`
CI job down with it. This happened on the downstream Pulp Mac Studio on
2026-07-01: RepoPrompt + Figma + a heavy MCP stack + the CI runner on one host,
~20 minutes of escalating pressure signals, then a reboot that failed the
required leg for an open PR.

The crash was **predictable from cheap local metrics ~20 min out** (pressure
level, fresh `JetsamEvent-*` reports). The manual mitigation — "pause the pool
during a heavy session" — depends on a human noticing in time. This automates it.

## Design (shipped in `providers/tart-macos/runner.sh`)

An **opt-in, off-by-default** gate that stops booting NEW VMs while the host is
saturated, reading a shared `host_vitals` green/warn/critical signal, and
resumes automatically once the host recovers.

- `TARTCI_HOST_VITALS_YIELD=1` turns it on (unset = OFF → no probe, no behavior
  change; the primary gate runner stays byte-for-byte unchanged).
- Reads a `host_vitals.sh` on `PATH` (override `TARTCI_HOST_VITALS_BIN`) whose
  exit code is the contract: `0` green, `10` warn, `20` critical. tartci does
  **not** ship the probe — bring your own (Pulp's is `tools/scripts/host_vitals.sh`).
- New `host_health_yield()` prints `1` (yield) on critical, or on warn when
  `TARTCI_HOST_VITALS_YIELD_ON_WARN=1`; else `0` (boot). The loop boots only when
  `queued>0 && running<cap && priority_demand==0 && host_health_yield==0`.
- Probed only when this lane has its own work (a cheap local call, no gh
  round-trip). `--print-host-health` previews the decision.

## FAIL OPEN — the deliberate inverse of `priority_demand`

`priority_demand()` gates a shared VM cap (Apple's 2-guest limit) and so **fails
CLOSED**: a probe error → assume demand → yield, because letting a secondary
lane grab the required gate's slot during a gh outage is the harmful outcome.

`host_health_yield()` is a **crash-avoidance nicety, not a correctness gate**, so
it **fails OPEN**: a missing / unexecutable / erroring probe → print `0` (boot).
A broken `host_vitals` must never wedge the required macOS runner; the worst case
of fail-open is that we forgo avoidance we can't measure — exactly where we were
before the feature. Two gates, two risk classes, opposite failure directions on
purpose.

## Test

`scripts/test_idle_gate.py` gains `HostHealthYieldTests` (drives the real
`--print-host-health` hook with a stub `host_vitals.sh`: feature-off / missing
probe fails-open / critical yields / green boots / warn gated by the opt-in /
critical still yields with warn opt-in) and `HostHealthWiringTests` (feature
defaults off; the loop consults `host_health_yield`). No gh/tart/network needed.

## Relationship to the priority gate

Orthogonal and composable. A runner may enable neither (default), one, or both.
When both are on, host-health is checked first in the loop (a saturated host
should shed load regardless of which lane wants the slot). Neither enabled = the
loop is identical to before either feature existed.
