"""ci-required must aggregate every CI job in .github/workflows/ci.yml.

Parses the workflow textually (no PyYAML: the 3.9 test job has none).
"""
import os
import re
import unittest

WORKFLOW = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows", "ci.yml"
)

# Jobs deliberately NOT aggregated by ci-required. ci-cause is advisory: it is
# continue-on-error, runs only after a failure, and must never gate the run.
ADVISORY_JOBS = {"ci-cause"}
AGGREGATE = "ci-required"


def _read(path=WORKFLOW):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def job_blocks(text):
    """Return {job_id: block_text} for the top-level `jobs:` mapping."""
    m = re.search(r"^jobs:\s*$", text, re.M)
    assert m, "no jobs: section"
    blocks, cur = {}, None
    for line in text[m.end():].splitlines():
        jm = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if jm:
            cur = jm.group(1)
            blocks[cur] = []
        elif cur is not None:
            blocks[cur].append(line)
    return {k: "\n".join(v) for k, v in blocks.items()}


def needs_of(block):
    m = re.search(r"^    needs:\s*\[([^\]]*)\]", block, re.M)
    assert m, "ci-required has no inline needs: [..] list"
    return {n.strip() for n in m.group(1).split(",") if n.strip()}


def missing_from_needs(text):
    blocks = job_blocks(text)
    assert AGGREGATE in blocks, "ci-required job missing"
    expected = set(blocks) - {AGGREGATE} - ADVISORY_JOBS
    return expected - needs_of(blocks[AGGREGATE])


class CiRequiredWorkflowTest(unittest.TestCase):
    def test_every_ci_job_is_needed(self):
        self.assertEqual(missing_from_needs(_read()), set())

    def test_gate_derives_jobs_from_needs_not_a_second_list(self):
        block = job_blocks(_read())[AGGREGATE]
        self.assertIn("toJSON(needs)", block)
        self.assertNotRegex(block, r"needs\.[A-Za-z0-9_-]+\.result")

    def test_always_runs(self):
        self.assertRegex(job_blocks(_read())[AGGREGATE], r"if:\s*\$\{\{\s*always\(\)\s*\}\}")

    def test_added_job_without_updating_needs_is_detected(self):
        dummy = _read() + "\n  dummy-new-job:\n    runs-on: ubuntu-latest\n    steps:\n      - run: 'true'\n"
        self.assertEqual(missing_from_needs(dummy), {"dummy-new-job"})


if __name__ == "__main__":
    unittest.main()
