"""prepush_select picks the test modules a change can affect (runs under 3.9)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prepush_select as sel  # noqa: E402


class SelectTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        os.mkdir(os.path.join(self.root, "scripts"))
        self.write("scripts/widget.py", "x = 1\n")
        self.write("scripts/test_widget.py", "import widget\n")
        self.write("scripts/test_other.py", "    from widget import x\n")
        self.write("scripts/test_unrelated.py", "import os\n")
        self.write("scripts/test_paths.py", "P = 'providers/foo/run.sh'\n")

    def write(self, rel, body):
        with open(os.path.join(self.root, rel), "w") as fh:
            fh.write(body)

    def pick(self, *changed):
        return sel.select(list(changed), root=self.root)

    def test_changed_module_selects_its_test_and_importers(self):
        self.assertEqual(self.pick("scripts/widget.py"), ["test_other", "test_widget"])

    def test_changed_test_selects_itself_only(self):
        self.assertEqual(self.pick("scripts/test_unrelated.py"), ["test_unrelated"])

    def test_changed_non_python_file_selects_tests_naming_its_path(self):
        self.assertEqual(self.pick("providers/foo/run.sh"), ["test_paths"])

    def test_bare_name_file_selects_nothing(self):
        self.assertEqual(self.pick("tartci"), [])

    def test_unrelated_change_selects_nothing(self):
        self.assertEqual(self.pick("docs/readme.md"), [])

    def test_a_deleted_module_still_selects_the_tests_that_import_it(self):
        os.remove(os.path.join(self.root, "scripts", "widget.py"))
        self.assertEqual(self.pick("scripts/widget.py"), ["test_other", "test_widget"])

    def test_no_merge_base_is_an_error_not_an_empty_selection(self):
        with self.assertRaises(sel.NoBase):
            sel.changed_files("refs/does-not-exist")


if __name__ == "__main__":
    unittest.main()
