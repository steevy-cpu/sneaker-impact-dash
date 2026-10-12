"""Tableau 'Top Partners' aggregation (routes/tableau._partner_stats).
Run: venv/bin/python3 -m unittest backend.tests.test_tableau_partners"""
import json
import sqlite3
import unittest

from backend.routes.tableau import _partner_stats


class PartnerStatsTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        self.c.execute("CREATE TABLE table_photos (id INTEGER PRIMARY KEY, shipment_info TEXT, "
                       "total_good_sneakers INTEGER, total_end_of_life INTEGER, casuals INTEGER, created_at TEXT)")

    def add(self, partner, good=0, eol=0, cas=0, created="2026-10-01T10:00:00", raw=None):
        info = raw if raw is not None else (json.dumps({"found": True, "partner": partner}) if partner is not None else None)
        self.c.execute("INSERT INTO table_photos (shipment_info, total_good_sneakers, total_end_of_life, casuals, created_at) "
                       "VALUES (?,?,?,?,?)", (info, good, eol, cas, created))

    def stats(self, since=None, total=1000, top_n=10):
        tw, P = ("created_at >= ?", (since,)) if since else ("1=1", ())
        return _partner_stats(self.c, tw, P, total, top_n)

    def test_shoes_are_good_plus_eol_plus_casual_and_sorted_descending(self):
        self.add("A", 10, 2, 1); self.add("B", 30, 0, 0); self.add("A", 5, 0, 0)
        top = self.stats()["top"]
        self.assertEqual([(t["label"], t["shoes"], t["boxes"]) for t in top], [("B", 30, 1), ("A", 18, 2)])
        self.assertEqual((top[1]["good"], top[1]["eol"], top[1]["casual"]), (15, 2, 1))

    def test_case_and_whitespace_variants_are_one_partner_shown_with_the_biggest_spelling(self):
        self.add("Blue Ox Running Store", 5); self.add("BLUE OX RUNNING STORE", 20); self.add("blue ox  running store\n", 1)
        s = self.stats(); self.assertEqual(len(s["top"]), 1); self.assertEqual(s["count"], 1)
        self.assertEqual((s["top"][0]["label"], s["top"][0]["shoes"], s["top"][0]["boxes"]),
                         ("BLUE OX RUNNING STORE", 26, 3))

    def test_trailing_newline_and_spaces_are_tidied_in_the_label(self):
        self.add("Philadelphia Runner - Center City\n", 4); self.add(" MOTI Physiotherapy ", 3)
        self.assertEqual(sorted(t["label"] for t in self.stats()["top"]),
                         ["MOTI Physiotherapy", "Philadelphia Runner - Center City"])

    def test_boxes_without_a_usable_partner_are_ignored_and_never_crash(self):
        self.add(None, 50)                                   # no shipment_info at all
        self.add(None, 50, raw='{"found": true}')            # matched but no partner key
        self.add("", 50); self.add("   ", 50)                # blank names
        self.add(None, 50, raw="{not json")                  # malformed row must not fail the query
        self.add(None, 50, raw=json.dumps({"partner": 12345}))   # non-string partner
        self.add("Real Partner", 7)
        s = self.stats(); self.assertEqual([t["label"] for t in s["top"]], ["Real Partner"])
        self.assertEqual(s["shoes_matched"], 7)

    def test_only_the_top_n_are_returned_but_matched_counts_everyone(self):
        for i in range(15):
            self.add(f"P{i:02d}", 100 - i)
        s = self.stats(top_n=10)
        self.assertEqual(len(s["top"]), 10); self.assertEqual(s["count"], 15)
        self.assertEqual(s["top"][0]["label"], "P00")
        self.assertEqual(s["top_shoes"], sum(100 - i for i in range(10)))
        self.assertEqual(s["shoes_matched"], sum(100 - i for i in range(15)))

    def test_zero_shoe_partners_are_not_charted_and_ties_break_by_boxes_then_name(self):
        self.add("Empty", 0, 0, 0)
        self.add("B", 10); self.add("A", 5); self.add("A", 5); self.add("C", 10)
        self.assertEqual([t["label"] for t in self.stats()["top"]], ["A", "B", "C"])   # A has 2 boxes; B before C by name

    def test_null_counts_are_treated_as_zero(self):
        self.c.execute("INSERT INTO table_photos (shipment_info, total_good_sneakers, total_end_of_life, casuals, created_at) "
                       "VALUES (?, NULL, NULL, 4, '2026-10-01T10:00:00')", (json.dumps({"partner": "N"}),))
        self.assertEqual(self.stats()["top"][0]["shoes"], 4)

    def test_time_range_filter_applies(self):
        self.add("Old", 100, created="2026-01-01T10:00:00"); self.add("New", 5, created="2026-10-05T10:00:00")
        self.assertEqual([t["label"] for t in self.stats(since="2026-10-01")["top"]], ["New"])

    def test_total_is_passed_through_and_empty_is_safe(self):
        s = self.stats(total=1234); self.assertEqual((s["top"], s["count"], s["shoes_matched"], s["shoes_total"]), ([], 0, 0, 1234))


if __name__ == "__main__":
    unittest.main()
