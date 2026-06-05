"""
airtable_sync.py — OUTBOUND 2-stage sync (dash -> Airtable), mirroring ShoeSort.

  Stage 1 (on capture): upsert the "Shipments Received" record by Barcode with
           box metadata — End of Life / Good Sneakers / Casual/Mixed / Weight (lbs).
  Stage 2 (on completion): set Brand Summary, e.g. "Nike: 3, Adidas: 2".

DORMANT until AIRTABLE_API_KEY + AIRTABLE_BASE_ID are set (sync_enabled() is
False), so this is a no-op placeholder today; it auto-activates on the next
server start once credentials are present. Fully fail-safe (never raises to the
caller) and stdlib-only (urllib).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from backend.config import (AIRTABLE_API_KEY, AIRTABLE_BASE_ID,
                            AIRTABLE_SHIPMENTS_TABLE, SHIPMENT_BARCODE_TRIM,
                            AIRTABLE_SYNC_ENABLED)
from backend.services.shipment_lookup import normalize_barcode


class AirtableSync:
    def __init__(self, api_key, base_id, table, trim):
        self.api_key = api_key
        self.base_id = base_id
        self.trim = trim
        self.ok = bool(api_key and base_id)
        self.base_url = (f"https://api.airtable.com/v0/{base_id}/"
                         f"{urllib.parse.quote(table)}")

    def _request(self, method, url, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                raise
            except Exception:                          # noqa: BLE001 - retry net errors
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                raise

    def _find_id(self, barcode):
        formula = "{Barcode}='%s'" % barcode.replace("'", "")
        url = self.base_url + "?maxRecords=1&filterByFormula=" + urllib.parse.quote(formula)
        recs = self._request("GET", url).get("records", [])
        return recs[0]["id"] if recs else None

    def _upsert(self, barcode, fields):
        """Update the "Shipments Received" record matching this barcode. Returns
        "updated", or None if no row exists for this barcode.

        We do NOT create a row: "Barcode" is a read-only `barcode`-type field
        (Airtable rejects writes to it -> HTTP 422), and shipments are
        pre-imported from the FedEx invoice export — so an unmatched scan just
        means that shipment isn't in Airtable yet, which we skip and log rather
        than fabricate a row with no barcode."""
        rid = self._find_id(barcode)
        if not rid:
            return None
        self._request("PATCH", f"{self.base_url}/{rid}", {"fields": fields})
        return "updated"

    def push(self, match_barcode, fields):
        """Low-level update used by the durable outbox. `match_barcode` is the
        already-normalized key. Updates the matching shipment row with `fields`.
        Returns one of: 'synced' (row found+updated), 'no_row' (no shipment yet —
        retry later), 'no_barcode', 'disabled', or 'error: <msg>' (retry later).
        Second tuple element is the Airtable record id on success, else None."""
        if not self.ok:
            return ("disabled", None)
        if not match_barcode:
            return ("no_barcode", None)
        try:
            rid = self._find_id(match_barcode)
            if not rid:
                return ("no_row", None)
            self._request("PATCH", f"{self.base_url}/{rid}", {"fields": fields})
            return ("synced", rid)
        except Exception as exc:                           # noqa: BLE001 - retry later
            return (f"error: {exc}", None)

    def sync_box_metadata(self, barcode, box):
        """Stage 1: upsert box metadata. `box` keys: weight, good, eol, casuals."""
        if not self.ok:
            return False
        bc = normalize_barcode(barcode, self.trim)
        if not bc:
            return False
        fields = {}
        if box.get("weight") is not None:
            fields["Weight (lbs)"] = box["weight"]
        if box.get("good") is not None:
            fields["Good Sneakers"] = int(box["good"])
        if box.get("eol") is not None:
            fields["End of Life"] = int(box["eol"])
        if box.get("casuals") is not None:
            fields["Casual/Mixed"] = int(box["casuals"])
        try:
            action = self._upsert(bc, fields)
            if action is None:
                print(f"[airtable-sync] box metadata: no shipment row for {bc} "
                      "— skipped (import the shipment into Airtable first)")
                return False
            print(f"[airtable-sync] box metadata {action} for {bc}")
            return True
        except Exception as exc:                       # noqa: BLE001 - fail safe
            print(f"[airtable-sync] box metadata failed for {bc}: {exc}")
            return False

    def sync_brand_summary(self, barcode, summary):
        """Stage 2: set Brand Summary (e.g. 'Nike: 3, Adidas: 2')."""
        if not self.ok or not summary:
            return False
        bc = normalize_barcode(barcode, self.trim)
        if not bc:
            return False
        try:
            action = self._upsert(bc, {"Brand Summary": summary})
            if action is None:
                print(f"[airtable-sync] brand summary: no shipment row for {bc} — skipped")
                return False
            print(f"[airtable-sync] brand summary {action} for {bc}")
            return True
        except Exception as exc:                       # noqa: BLE001 - fail safe
            print(f"[airtable-sync] brand summary failed for {bc}: {exc}")
            return False


_singleton = None


def get_airtable_sync():
    global _singleton
    if _singleton is None:
        _singleton = AirtableSync(AIRTABLE_API_KEY, AIRTABLE_BASE_ID,
                                  AIRTABLE_SHIPMENTS_TABLE, SHIPMENT_BARCODE_TRIM)
    return _singleton


def sync_enabled():
    """True only when writes are toggled on AND credentials are present."""
    return AIRTABLE_SYNC_ENABLED and bool(AIRTABLE_API_KEY and AIRTABLE_BASE_ID)


def brand_summary_from_pairs(pairs):
    """Aggregate makes into 'Nike: 3, Adidas: 2' (skips unknown), most-common first."""
    counts = {}
    for p in pairs:
        mk = (p.get("final_make") or p.get("make") or "").strip()
        if mk and mk.lower() != "unknown":
            counts[mk] = counts.get(mk, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{k}: {v}" for k, v in ordered)
