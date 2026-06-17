# Priority-aware idle gate for secondary macOS lanes — 2026-06-16

## Problem

macOS allows only **two running macOS guests per host** (Apple's Virtualization
limit). On Pulp's Mac Studio both slots belong to the **required `macos` build
gate**. Any advisory secondary macOS lane (coverage, sanitizer) is a *third*
conceptual guest and must never cost the required gate a slot.

Two prior shapes both failed:

- **Shared `TART_HOME`, no yield (coverage lane, #4036/#4066).** Booted whenever
  the host was idle, then held a slot for ~1h, throttling the required gate to
  one VM and ultimately wedging it (launchctl exit 126). Backed out 2026-06-16.
- **Separate `TART_HOME` (first-cut fix idea).** Hides the secondary VM from the
  gate's `running_macos_vms` count, so total running guests can reach 3 → the
  3rd `tart run` fails (Apple cap). Also duplicates the ~150 GB golden. Rejected.

## Design (shipped in `providers/tart-macos/runner.sh`)

Keep the secondary lane on the **same `TART_HOME`** as the gate (so
`running_macos_vms` is a true host-wide 2-guest semaphore) and add an **opt-in
yield** so the secondary defers to the priority lane:

- `TARTCI_YIELD_TO_WORKFLOW_NAME` + `TARTCI_YIELD_TO_LABELS` (unset = OFF; the
  gate runner and release lane are byte-for-byte unchanged).
- New `priority_demand()` counts queued + in-progress jobs of the yield workflow
  whose requested labels are a subset of the yield labels (GitHub's assignment
  rule). The loop boots only when `queued>0 && running<cap && priority_demand==0`.
- `priority_demand()` is only probed when this lane has its own work (saves a gh
  round-trip / API quota each idle poll). `--print-priority-demand` previews it.
- Behavioral test `scripts/test_idle_gate.py` extracts and runs the pure subset
  matcher (subset / extra-label / unrelated / case-insensitive / malformed /
  empty), and CI now actually executes the unittest suites (`ci.yml`).

Net: under a hosted-runner storm the gate always has work → the secondary never
boots → the gate keeps both slots. In a genuine idle window the secondary runs;
a gate burst mid-run still gets ≥1 slot immediately and reclaims the 2nd when the
single advisory job finishes.

## Consumer: Pulp sanitizer lane (#4101)

- Pilot = **TSan only** (longest: scoped `-j1` serial ~45 min on `macos-14`;
  highest value for a real-time audio framework; single-core-bound so it gains
  most from a local M-series host). ASan/UBSan stay on `macos-15`.
- The four sanitizers run in parallel on GitHub but serialize (~4x) on one cap=1
  local lane → localizing more than one is slower than hosted except during a
  backlog. Full parallel local sanitizers would need a 3rd macOS host.
- Pulp template: `tools/launchd/pulp-tart-runner-sanitizer-macos.plist.template`
  (label `pulp-sanitizer-vm-macos`, workflow `Sanitizer Tests`, cap=1, shared
  `$HOME/VMs`, idle-gate env wired). Ships **parked** until the idle-gate is
  deployed and a quiet window + healthy gate are confirmed.

## Status

- 2026-06-16 — Idle gate implemented + tested in the tart-macos provider; CI
  runs the unit suites; README documents it. Pulp side: deny-labels plumbing for
  all 3 macOS sanitizers + parked TSan LaunchAgent template + behavioral tests.
  **No live VM boot yet** (decision: wait for idle-gate deploy + quiet window).
  Re-enabling the coverage lane on this same idle-gate is the obvious follow-up.
