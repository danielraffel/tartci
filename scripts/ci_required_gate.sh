#!/usr/bin/env bash
# Aggregate verdict for the `ci-required` check.
#
# Usage: ci_required_gate.sh <job>=<result> [<job>=<result> ...]
#
# Every listed job must have result `success`. `failure`, `cancelled`,
# `skipped`, or an empty/unknown result (job never ran) all fail the gate.
# No job is allowed to be skipped: the workflow has no job-level `if`
# conditions on the aggregated jobs, so a skip is always unexpected.
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "ci-required: no job results supplied" >&2
  exit 2
fi

bad=0
for pair in "$@"; do
  job="${pair%%=*}"
  result="${pair#*=}"
  if [[ "$result" == "success" ]]; then
    echo "ok   $job: success"
  else
    echo "FAIL $job: ${result:-<no result>}" >&2
    bad=1
  fi
done
exit "$bad"
