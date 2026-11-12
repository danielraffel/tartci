"""The tartci-incident marker line on alert issues."""
from __future__ import annotations

import json
import unittest

import tartci_incident as incident


class TartciIncidentTests(unittest.TestCase):
    def line(self) -> str:
        return incident.marker("m5", "self_update_starved", "2026-10-13T08:00:00Z",
                               "2026-10-13T12:00:00Z")

    def test_the_opened_body_ends_with_one_parseable_marker(self) -> None:
        body = incident.with_marker("What happened.\n\nHow to verify.\n", self.line())
        last = body.split("\n")[-1]
        self.assertTrue(last.startswith("<!-- tartci-incident {") and last.endswith("} -->"))
        payload = json.loads(last[len("<!-- tartci-incident "):-len(" -->")])
        self.assertEqual(payload, {"host": "m5", "class": "self_update_starved",
                                   "symptom_at": "2026-10-13T08:00:00Z",
                                   "detected_at": "2026-10-13T12:00:00Z", "fixed_at": None})
        self.assertEqual(incident.parse(body), payload)

    def test_fixing_keeps_exactly_one_marker_carrying_fixed_at(self) -> None:
        body = incident.with_marker("What happened.", self.line())
        patched = incident.fixed(body, "2026-10-13T12:40:00Z")
        markers = [row for row in patched.split("\n") if row.startswith("<!-- tartci-incident")]
        self.assertEqual(len(markers), 1)
        self.assertEqual(incident.parse(patched)["fixed_at"], "2026-10-13T12:40:00Z")
        self.assertTrue(patched.startswith("What happened."))
        # Re-marking replaces rather than appends.
        self.assertEqual(incident.with_marker(patched, self.line()).count("tartci-incident"), 1)

    def test_a_body_without_exactly_one_marker_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            incident.fixed("no marker here", "2026-10-13T12:40:00Z")
        twice = "text\n" + self.line() + "\n" + self.line()
        with self.assertRaises(ValueError):
            incident.fixed(twice, "2026-10-13T12:40:00Z")
        self.assertIsNone(incident.parse("text\n<!-- tartci-incident {not json} -->"))


if __name__ == "__main__":
    unittest.main()
