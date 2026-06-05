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
        """Update the record matching this barcode, or create it. Returns action."""
        rid = self._find_id(barcode)
        if rid:
            self._request("PATCH", f"{self.base_url}/{rid}", {"fields": fields})
            return "updated"
        self._request("POST", self.base_url, {"fields": {"Barcode": barcode, **fields}})
        return "created"

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
