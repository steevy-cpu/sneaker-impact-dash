"""Shipment match keys: FedEx keeps the last-12 rule, UPS 1Z uses the full number.
Run: venv/bin/python3 -m unittest backend.tests.test_shipment_keys"""
import unittest

from backend.services.airtable_sync import AirtableSync
from backend.services.shipment_lookup import (AirtableShipmentLookup, barcode_formula,
                                              is_ups_key, normalize_barcode, ups_key)

UPS = "1Z197BY20312704202"


class KeyTests(unittest.TestCase):
    def test_fedex_keys_are_unchanged(self):
        self.assertEqual(normalize_barcode("792553371340"), "792553371340")
        self.assertEqual(normalize_barcode("9622013700001205839600792553371340"), "792553371340")
        self.assertEqual(normalize_barcode(".792079399041"), "792079399041")
        self.assertEqual(normalize_barcode("CAMTEST"), "CAMTEST")

    def test_ups_key_is_the_full_uppercase_number(self):
        for scan in (UPS, UPS.lower(), "." + UPS, " " + UPS + " ", "1z" + UPS[2:].lower()):
            self.assertEqual(normalize_barcode(scan), UPS, scan)
        self.assertEqual(ups_key("792553371340"), None)
        self.assertTrue(is_ups_key(UPS)); self.assertFalse(is_ups_key("792553371340"))
        self.assertFalse(is_ups_key("1Z123"))                    # too short to be a UPS number

    def test_formula_fedex_exact_ups_case_insensitive_both_fields(self):
        self.assertEqual(barcode_formula("792553371340"), "{Barcode}='792553371340'")
        f = barcode_formula(UPS)
        self.assertIn("UPPER({Barcode}&'')='%s'" % UPS, f)
        self.assertIn("UPPER({Tracking Number}&'')='%s'" % UPS, f)

    def test_formula_cannot_be_broken_out_of(self):
        self.assertNotIn("'),", barcode_formula("12'),TRUE(),('"))


class _Sync(AirtableSync):
    """AirtableSync with the network replaced."""
    def __init__(self, existing=None):
        super().__init__("k", "app", "Shipments Received", 12)
        self.existing, self.created = existing or {}, []

    def _find_id(self, barcode):
        return self.existing.get(barcode)

    def _create(self, barcode, fields):
        self.created.append(barcode); return "recNEW"

    def _request(self, *a, **k):
        return {}


class PushTests(unittest.TestCase):
    def test_ups_row_is_created_when_missing(self):
        s = _Sync(); self.assertEqual(s.push(UPS, {"Weight (lbs)": 30}), ("synced", "recNEW"))
        self.assertEqual(s.created, [UPS])

    def test_ups_row_is_updated_when_present(self):
        s = _Sync({UPS: "recOLD"}); self.assertEqual(s.push(UPS, {}), ("synced", "recOLD"))
        self.assertEqual(s.created, [])

    def test_junk_and_truncated_keys_still_never_create(self):
        for bad in ("Y20312704202", "CAMTEST", "1Z123", "12345"):    # old UPS fragment, test scan, short
            s = _Sync(); self.assertEqual(s.push(bad, {}), ("no_row", None), bad)
            self.assertEqual(s.created, [])

    def test_fedex_create_rule_unchanged(self):
        s = _Sync(); self.assertEqual(s.push("792553371340", {}), ("synced", "recNEW"))


class _Lookup(AirtableShipmentLookup):
    def __init__(self, shipments, partners):
        super().__init__("k", "app", "Shipments Received", "Partners", 12)
        self.shipments, self.partners, self.urls = shipments, partners, []

    def _get(self, url):
        self.urls.append(url)
        return {"records": self.partners if "/Partners" in url else self.shipments}


class BatchMappingTests(unittest.TestCase):
    def test_lowercase_ups_barcode_row_is_found_under_the_uppercase_key(self):
        rec = {"id": "recS", "fields": {"Barcode": {"text": UPS.lower(), "type": "upc"},
                                        "Tracking Number": UPS, "Partner": ["recP"], "Weight (lbs)": 30}}
        lk = _Lookup([rec], [{"id": "recP", "fields": {"Partner Name": "Fleet Feet Test"}}])
        out = lk.resolve_many([UPS, "792553371340"])
        self.assertEqual(out[UPS]["partner"], "Fleet Feet Test")
        self.assertNotIn("792553371340", out)                     # asked for, not there = not found

    def test_ups_row_without_partner_is_found_with_partner_none(self):
        rec = {"id": "recS", "fields": {"Barcode": {"text": UPS, "type": "upc"}, "Tracking Number": UPS}}
        out = _Lookup([rec], []).resolve_many([UPS])
        self.assertTrue(out[UPS]["found"]); self.assertIsNone(out[UPS]["partner"])


if __name__ == "__main__":
    unittest.main()
