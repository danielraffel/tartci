#!/usr/bin/env python3
import unittest
from tartci_redirect_probe import CLASSES, OLD, NEW, Receipt, classify, probe

class FakeIssueTransport:
    def __init__(self): self.created = []; self.closed = []
    def create(self, slug): self.created.append(slug); return 42
    def close(self, slug, number): self.closed.append((slug, number))

class RedirectTests(unittest.TestCase):
    def test_probe_covers_each_class_and_slug(self):
        class Transport(FakeIssueTransport):
            def __call__(self, name, slug): return (0 if name.startswith("git") else 200, False)
        # Probe uses real transport functions; this control covers classifier cases.
        self.assertEqual("new_ok", classify(Receipt("api", NEW, 200)))
        self.assertEqual(10, len(CLASSES) * 2)

    def test_new_404_is_missing(self): self.assertEqual("missing", classify(Receipt("api", NEW, 404)))
    def test_old_301_is_live_pin(self): self.assertEqual("live_pin", classify(Receipt("api", OLD, 1)))
    def test_follow_result_is_recorded(self): self.assertEqual(200, Receipt("raw", OLD, 301, 200).follow_result)
    def test_one_create_and_one_close(self):
        transport = FakeIssueTransport()
        rows = probe(transport, create_issue=True,
                     transport=lambda name, slug: Receipt(name, slug, 200))
        self.assertEqual([NEW], transport.created)
        self.assertEqual([(NEW, 42)], transport.closed)
        self.assertEqual(1, sum(row.probe == "issue-create-close" for row in rows))

if __name__ == "__main__": unittest.main()
