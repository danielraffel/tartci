#!/usr/bin/env bash
# Aggregate verdict for the `ci-required` check.
#
# Usage: ci_required_gate.sh '<toJSON(needs)>'
#
# The input is the workflow's own `toJSON(needs)`, so the job list is whatever
# `ci-required.needs` names; there is no second list to keep in sync.
# Every needed job must have result `success`. `failure`, `cancelled`,
# `skipped`, or a missing/unknown result all fail the gate. No job may be
# skipped: the aggregated jobs have no job-level `if`, so a skip is unexpected.
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "ci-required: expected one argument (toJSON(needs))" >&2
  exit 2
fi

python3 -I - "$1" <<'PY'
import json, sys

try:
    needs = json.loads(sys.argv[1])
except ValueError as exc:
    print(f"ci-required: needs JSON unparseable: {exc}", file=sys.stderr)
    sys.exit(2)
if not isinstance(needs, dict) or not needs:
    print("ci-required: no needed jobs supplied", file=sys.stderr)
    sys.exit(2)

bad = False
for job in sorted(needs):
    value = needs[job]
    result = value.get("result") if isinstance(value, dict) else None
    if result == "success":
        print(f"ok   {job}: success")
    else:
        print(f"FAIL {job}: {result or '<no result>'}", file=sys.stderr)
        bad = True
sys.exit(1 if bad else 0)
PY
