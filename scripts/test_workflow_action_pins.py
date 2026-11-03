"""Every third-party `uses:` in .github/workflows must pin a full commit SHA."""
import glob
import os
import re
import unittest

WORKFLOWS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows"
)
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)")
PINNED = re.compile(r"@[0-9a-f]{40}$")


def unpinned(text):
    """Return [(lineno, ref)] for remote `uses:` refs not pinned to a 40-hex SHA."""
    bad = []
    for no, line in enumerate(text.splitlines(), 1):
        m = USES.match(line)
        if not m:
            continue
        ref = m.group(1).strip("\"'")
        if ref.startswith("./") or ref.startswith("docker://"):
            continue
        if not PINNED.search(ref):
            bad.append((no, ref))
    return bad


class WorkflowActionPins(unittest.TestCase):
    def test_detector_rejects_tag_and_branch_refs(self):
        self.assertEqual(unpinned("- uses: actions/checkout@v4\n"), [(1, "actions/checkout@v4")])
        self.assertEqual(unpinned("  uses: a/b@main # x\n"), [(1, "a/b@main")])
        self.assertEqual(unpinned("- uses: a/b@" + "a" * 39 + "\n"), [(1, "a/b@" + "a" * 39)])

    def test_detector_accepts_sha_and_local_refs(self):
        self.assertEqual(unpinned("- uses: a/b@" + "a" * 40 + " # v1\n"), [])
        self.assertEqual(unpinned("- uses: ./.github/actions/x\n"), [])

    def test_all_workflows_are_pinned(self):
        files = sorted(glob.glob(os.path.join(WORKFLOWS, "*.y*ml")))
        self.assertTrue(files)
        bad = []
        for path in files:
            with open(path, encoding="utf-8") as fh:
                bad += [(os.path.basename(path), n, r) for n, r in unpinned(fh.read())]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
