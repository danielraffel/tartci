#!/usr/bin/env python3
import unittest
from tartci_redirect_probe import OLD, NEW, Receipt, classify

class ProbeClassificationTests(unittest.TestCase):
    def test_new_404_is_missing(self):
        self.assertEqual("missing", classify(Receipt("api", NEW, 404)))
    def test_old_301_is_live_pin(self):
        self.assertEqual("live_pin", classify(Receipt("api", OLD, 301)))
    def test_new_success_is_new_ok(self):
        self.assertEqual("new_ok", classify(Receipt("raw", NEW, 200)))
    def test_old_git_success_is_old_ok(self):
        self.assertEqual("old_ok", classify(Receipt("git_https", OLD, 0)))
    def test_other_result_is_unexpected(self):
        self.assertEqual("unexpected", classify(Receipt("api", NEW, 500)))

if __name__ == "__main__":
    unittest.main()
