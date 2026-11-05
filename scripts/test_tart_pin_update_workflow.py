"""The Tart pin-update PR must be opened with the App token, not GITHUB_TOKEN.

Pushes and pull requests made with the default workflow token start no
workflows, so a PR opened with it never runs the required checks and can never
go green. The workflow therefore mints a shipyard-local GitHub App installation
token, scoped to this repository, and fails when the App credentials are absent
instead of falling back to the default token.
"""
import os
import re
import unittest

WORKFLOW = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    ".github",
    "workflows",
    "tart-pin-update.yml",
)
APP_TOKEN_ACTION = "actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510"


def steps(text):
    """Return {step name: step text} for the workflow's named steps."""
    found = {}
    for chunk in re.split(r"(?m)^      - ", text)[1:]:
        m = re.match(r"name: (.+)", chunk)
        if m:
            found[m.group(1).strip()] = chunk
    return found


class TartPinUpdateWorkflow(unittest.TestCase):
    def setUp(self):
        with open(WORKFLOW, encoding="utf-8") as fh:
            self.text = fh.read()
        self.steps = steps(self.text)

    def test_default_token_never_writes(self):
        self.assertNotIn("github.token", self.text)
        self.assertNotIn("secrets.GITHUB_TOKEN", self.text)
        top = re.search(r"(?m)^permissions:\n((?:  .*\n)+)", self.text).group(1)
        self.assertEqual(top.strip(), "contents: read")
        self.assertIn("persist-credentials: false", self.steps["Check out main"])

    def test_pr_and_push_steps_use_the_app_token(self):
        for name in ("Create the reviewed pin branch", "Open or refresh the pin PR"):
            step = self.steps[name]
            self.assertIn("GH_TOKEN: ${{ steps.app-token.outputs.token }}", step)
            self.assertIn('test -n "$GH_TOKEN"', step)
        self.assertIn("gh auth setup-git", self.steps["Create the reviewed pin branch"])

    def test_app_token_is_pinned_and_narrowly_scoped(self):
        mint = self.steps["Mint the pin-update App token"]
        self.assertIn("uses: " + APP_TOKEN_ACTION, mint)
        # No owner/repositories input: the token covers this repository only.
        # A schedule event carries no repository object, so an
        # `github.event.repository.name` value would be empty and, with owner
        # set, widen the token to every repository of the owner.
        self.assertNotIn("owner:", mint)
        self.assertNotIn("repositories:", mint)
        self.assertIn("client-id: Iv23liTCzDQWqmMjkDTI", mint)
        permissions = sorted(re.findall(r"permission-([a-z-]+): (\w+)", mint))
        self.assertEqual(permissions, [("contents", "write"), ("pull-requests", "write"),
                                       ("workflows", "write")])

    def test_app_key_is_read_only_inside_the_main_only_environment(self):
        # The key is an environment secret of `automation` (deployment policy:
        # branch main). A job outside that environment would read a repo-level
        # copy from any branch, so the only job that names the secret must
        # declare the environment.
        job = re.search(r"(?ms)^  discover:\n(.*)", self.text).group(1)
        self.assertRegex(job, r"(?m)^    environment: automation$")
        self.assertEqual(self.text.count("secrets."), 2)
        self.assertEqual(len(re.findall(r"(?m)^  [a-z][a-z-]*:$", self.text.split("\njobs:\n", 1)[1])), 1)

    def test_missing_app_credentials_fail_before_minting(self):
        names = list(self.steps)
        require = names.index("Require the pin-update App credentials")
        self.assertLess(require, names.index("Mint the pin-update App token"))
        self.assertIn("exit 1", self.steps["Require the pin-update App credentials"])
        self.assertIn("App credentials are required", self.text)


if __name__ == "__main__":
    unittest.main()
