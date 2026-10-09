"""Partner resolver: discovery, claim leasing, backoff, expiry, error handling.
Run: venv/bin/python3 -m unittest backend.tests.test_partner_resolver"""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta

from backend.services import partner_resolver as pr


class StubLookup:
    """Stands in for AirtableShipmentLookup.resolve_many."""
    def __init__(self, data=None, error=None):
        self.data, self.error, self.calls = data or {}, error, []

    def resolve_many(self, keys):
        self.calls.append(list(keys))
        if self.error:
            raise self.error
        return {k: v for k, v in self.data.items() if k in keys}


def info(bc, partner="Fleet Feet X", weight=30):
    return {"found": True, "barcode": bc, "partner": partner, "weight": weight}


class ResolverTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("CREATE TABLE table_photos (id TEXT PRIMARY KEY, barcode TEXT, "
                          "shipment_info TEXT, created_at TEXT)")
        pr.ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close(); os.unlink(self.path)

    def add(self, tid, barcode, created=None, si=None):
        self.conn.execute("INSERT INTO table_photos VALUES (?,?,?,?)", (
            tid, barcode, json.dumps(si) if si else None,
            (created or datetime.now()).isoformat(timespec="seconds")))
        self.conn.commit()

    def row(self, tid):
        return self.conn.execute("SELECT * FROM shipment_resolve WHERE table_photo_id=?", (tid,)).fetchone()

    def partner_of(self, tid):
        si = self.conn.execute("SELECT shipment_info FROM table_photos WHERE id=?", (tid,)).fetchone()[0]
        return json.loads(si)["partner"] if si else None

    # -- discovery ---------------------------------------------------------
    def test_discover_registers_new_tables_once(self):
        self.add("T1", "1234567890123456")            # long scan -> last 12 digits
        self.add("T2", "123")                          # too short: never registered
        self.add("T3", None)
        self.assertEqual(pr.discover(self.conn), 1)
        self.assertEqual(pr.discover(self.conn), 0)    # idempotent
        self.assertEqual(self.row("T1")["match_barcode"], "567890123456")
        self.assertEqual(self.row("T1")["state"], "pending")
        self.assertIsNone(self.row("T2"))

    def test_table_already_holding_a_partner_is_resolved_without_a_call(self):
        self.add("T1", "123456789012", si=info("123456789012", "Already Here"))
        look = StubLookup()
        pr.run_pass(self.conn, look)
        self.assertEqual(self.row("T1")["state"], "resolved")
        self.assertEqual(look.calls, [])

    # -- the happy path / the bug being fixed ------------------------------
    def test_partner_found_is_stored_and_resolved(self):
        self.add("T1", "123456789012")
        stats = pr.run_pass(self.conn, StubLookup({"123456789012": info("123456789012", "Charm City Run")}))
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(self.partner_of("T1"), "Charm City Run")
        self.assertEqual(self.row("T1")["state"], "resolved")
        self.assertIsNone(self.row("T1")["next_try_at"])

    def test_two_tables_with_the_same_barcode_share_one_lookup(self):
        self.add("T1", "123456789012"); self.add("T2", "123456789012")
        look = StubLookup({"123456789012": info("123456789012")})
        pr.run_pass(self.conn, look)
        self.assertEqual(look.calls, [["123456789012"]])
        self.assertEqual({self.row("T1")["state"], self.row("T2")["state"]}, {"resolved"})

    # -- not there yet: retry, don't give up -------------------------------
    def test_row_not_in_airtable_yet_stays_pending_with_backoff(self):
        self.add("T1", "123456789012")
        pr.run_pass(self.conn, StubLookup())
        r = self.row("T1")
        self.assertEqual((r["state"], r["attempts"], r["last_error"]), ("pending", 1, "no_row"))
        self.assertGreater(r["next_try_at"], datetime.now().isoformat(timespec="seconds"))
        self.assertIsNone(self.partner_of("T1"))

    def test_not_due_rows_are_not_looked_up_again(self):
        self.add("T1", "123456789012")
        look = StubLookup()
        pr.run_pass(self.conn, look); pr.run_pass(self.conn, look)
        self.assertEqual(len(look.calls), 1)

    def test_partner_appearing_later_is_picked_up(self):
        self.add("T1", "123456789012")
        pr.run_pass(self.conn, StubLookup())                               # not there yet
        self.conn.execute("UPDATE shipment_resolve SET next_try_at=?", ("2000-01-01T00:00:00",)); self.conn.commit()
        pr.run_pass(self.conn, StubLookup({"123456789012": info("123456789012", None)}))   # row, no partner
        self.assertEqual(self.row("T1")["state"], "blank")
        self.assertIsNone(self.partner_of("T1"))                           # info stored, partner empty
        self.conn.execute("UPDATE shipment_resolve SET next_try_at=?", ("2000-01-01T00:00:00",)); self.conn.commit()
        pr.run_pass(self.conn, StubLookup({"123456789012": info("123456789012", "Hand Linked")}))
        self.assertEqual(self.row("T1")["state"], "resolved")
        self.assertEqual(self.partner_of("T1"), "Hand Linked")

    def test_ladder_grows_then_repeats_daily(self):
        self.assertEqual(pr._delay_for(0), 20)
        self.assertEqual(pr._delay_for(3), 600)
        self.assertEqual(pr._delay_for(50), 86400)

    # -- cheap discovery window / read-only claim ----------------------------
    def test_window_discovery_sees_new_rows_but_not_ancient_ones_full_sweep_does(self):
        self.add("ANCIENT", "111111111111")
        for i in range(pr.DISCOVER_WINDOW + 5):
            self.add(f"N{i:04d}", f"2222222{i:05d}", si=info(f"2222222{i:05d}"))   # already resolved
        pr.discover(self.conn, full=True)
        self.conn.execute("DELETE FROM shipment_resolve WHERE table_photo_id IN ('ANCIENT','N0200')")
        self.conn.commit()
        self.add("FRESH", "333333333333")
        found = pr.discover(self.conn, full=False)
        self.assertIsNotNone(self.row("FRESH"))                 # newest rows: always seen
        self.assertIsNotNone(self.row("N0200"))                 # still inside the window
        self.assertIsNone(self.row("ANCIENT"))                  # outside the window...
        pr.discover(self.conn, full=True)
        self.assertIsNotNone(self.row("ANCIENT"))               # ...found by the full sweep
        self.assertGreaterEqual(found, 2)

    def test_window_survives_top_rows_being_deleted_and_rowids_reused(self):
        for i in range(5):
            self.add(f"T{i}", f"44444444444{i}")
        pr.discover(self.conn, full=True)
        self.conn.execute("DELETE FROM table_photos WHERE id IN ('T3','T4')"); self.conn.commit()
        self.add("REUSED", "555555555555")                        # takes a rowid BELOW the old maximum
        pr.discover(self.conn, full=False)
        self.assertIsNotNone(self.row("REUSED"))

    def test_idle_claim_never_takes_the_write_lock(self):
        self.add("T1", "123456789012"); pr.discover(self.conn)
        self.conn.execute("UPDATE shipment_resolve SET next_try_at='2999-01-01T00:00:00'"); self.conn.commit()
        other = sqlite3.connect(self.path, timeout=0.2)
        other.execute("BEGIN IMMEDIATE")                          # someone else holds the write lock
        try:
            self.assertEqual(pr.claim(self.conn, 10), [])         # would raise 'database is locked' if it tried to write
        finally:
            other.rollback(); other.close()

    # -- expiry and manual recheck -----------------------------------------
    def test_old_unresolved_table_expires_and_recheck_revives_it(self):
        self.add("T1", "123456789012", created=datetime.now() - timedelta(days=31))
        pr.run_pass(self.conn, StubLookup({"123456789012": info("123456789012", None)}))
        self.assertEqual(self.row("T1")["state"], "expired")
        self.assertEqual(pr.recheck(self.conn, "expired"), 1)
        self.assertEqual(self.row("T1")["state"], "pending")
        pr.run_pass(self.conn, StubLookup({"123456789012": info("123456789012", "Fixed Later")}))
        self.assertEqual(self.partner_of("T1"), "Fixed Later")

    # -- failure handling ---------------------------------------------------
    def test_airtable_error_does_not_burn_an_attempt(self):
        self.add("T1", "123456789012")
        stats = pr.run_pass(self.conn, StubLookup(error=RuntimeError("timed out")))
        self.assertIn("error", stats)
        r = self.row("T1")
        self.assertEqual((r["state"], r["attempts"]), ("pending", 0))
        self.assertIn("timed out", r["last_error"])
        self.assertIsNone(r["lease_until"])

    # -- two instances ------------------------------------------------------
    def test_claimed_rows_are_leased_so_a_second_worker_skips_them(self):
        for i in range(3):
            self.add(f"T{i}", f"12345678901{i}")
        pr.discover(self.conn)
        first = pr.claim(self.conn, 2)
        second = pr.claim(self.conn, 10)
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 1)
        self.assertFalse({r["table_photo_id"] for r in first} & {r["table_photo_id"] for r in second})

    def test_crashed_worker_lease_expires(self):
        self.add("T1", "123456789012"); pr.discover(self.conn)
        self.assertEqual(len(pr.claim(self.conn, 10)), 1)
        self.conn.execute("UPDATE shipment_resolve SET lease_until=?", ("2000-01-01T00:00:00",)); self.conn.commit()
        self.assertEqual(len(pr.claim(self.conn, 10)), 1)

    def test_newest_tables_are_resolved_first_during_backlog(self):
        self.add("OLD", "111111111111", created=datetime.now() - timedelta(days=5))
        self.add("NEW", "222222222222")
        pr.discover(self.conn)
        self.assertEqual(pr.claim(self.conn, 1)[0]["table_photo_id"], "NEW")


if __name__ == "__main__":
    unittest.main()
