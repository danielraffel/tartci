#!/usr/bin/env python3
"""Post-transfer redirect probe and pure classification controls.

`probe()` accepts a transport callable and emits a receipt table.  Operators
can pass a real transport after transfer; tests use a recording transport so
request construction is exercised without creating an issue.
"""
from __future__ import annotations
import unittest
from dataclasses import dataclass

OLD = "danielraffel/tartci"
NEW = "Generous-Corp/tartci"
CLASSES = ("git_https", "git_ssh", "api", "raw", "issue")

@dataclass(frozen=True)
class Receipt:
    probe: str
    slug: str
    result: int
    via_wrapper: bool = False


def classify(receipt: Receipt) -> str:
    """Classify one observed result; this is intentionally transport-free."""
    if receipt.slug == NEW and receipt.result == 404:
        return "missing"
    if receipt.slug == OLD and receipt.result == 301 and receipt.via_wrapper:
        return "live_pin"
    if receipt.slug == NEW and receipt.result in (200, 201):
        return "new_ok"
    if receipt.slug == OLD and receipt.result in (0, 200, 201):
        return "old_ok"
    return "unexpected"


def probe(transport) -> list[Receipt]:
    """Run every redirect class against both slugs and return receipt rows."""
    rows = []
    for name in CLASSES:
        for slug in (OLD, NEW):
            result, via_wrapper = transport(name, slug)
            rows.append(Receipt(name, slug, result, via_wrapper))
    print("probe\tslug\tresult\tclassification")
    for row in rows:
        print(f"{row.probe}\t{row.slug}\t{row.result}\t{classify(row)}")
    return rows


class RecordingTransport:
    def __init__(self):
        self.calls = []
    def __call__(self, name, slug):
        self.calls.append((name, slug))
        if slug == OLD:
            return (301, name == "api")
        return (200, False)


class RedirectTests(unittest.TestCase):
    def test_each_class_probes_old_and_new_and_emits_receipts(self):
        transport = RecordingTransport()
        rows = probe(transport)
        self.assertEqual(10, len(rows))
        self.assertEqual([(name, slug) for name in CLASSES for slug in (OLD, NEW)], transport.calls)
        self.assertEqual(10, len({(r.probe, r.slug) for r in rows}))

    def test_classify_new_404_is_missing_control(self):
        self.assertEqual("missing", classify(Receipt("api", NEW, 404)))

    def test_classify_old_301_via_wrapper_is_live_pin_control(self):
        self.assertEqual("live_pin", classify(Receipt("api", OLD, 301, True)))

    def test_classify_old_301_without_wrapper_is_unexpected(self):
        self.assertEqual("unexpected", classify(Receipt("api", OLD, 301, False)))

    def test_new_success_is_new_ok(self):
        self.assertEqual("new_ok", classify(Receipt("raw", NEW, 200)))

    def test_old_success_is_old_ok(self):
        self.assertEqual("old_ok", classify(Receipt("git_https", OLD, 0)))


if __name__ == "__main__":
    unittest.main()
