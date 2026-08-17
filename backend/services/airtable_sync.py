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

    def _create(self, barcode, fields):
        """Create a new "Shipments Received" row for this barcode. The "Barcode"
        field is a barcode-type field: written as {text, type:'upc'} with
        typecast=True (writable on CREATE, not on PATCH). Mirrors the old
        ShoeSort client, which created the row on each scan. Returns the new id.

        Why create: the scanned FedEx tracking numbers are auto-added to the
        "Labels Created" table, but the box's sort results belong here in
        "Shipments Received" — and that row only exists if something creates it.
        The old client did; when the dash went update-only the rows stopped
        appearing (every scan -> no_row). So we create the row on first sync."""
        payload = {
            "fields": {**fields, "Barcode": {"text": barcode, "type": "upc"}},
            "typecast": True,
        }
        rec = self._request("POST", self.base_url, payload)
        return rec.get("id")

    def _upsert(self, barcode, fields):
        """Update the matching "Shipments Received" row, or CREATE it if none
        exists (mirrors the old client). Returns "updated" or "created"."""
        rid = self._find_id(barcode)
        if rid:
            self._request("PATCH", f"{self.base_url}/{rid}", {"fields": fields})
            return "updated"
        self._create(barcode, fields)
        return "created"

    def patch_fields(self, record_id, fields):
        """PATCH arbitrary fields onto a known record. Used for side-writes that
        must NOT ride along with the box-metadata payload (an unknown field name
        422s the whole request), e.g. the operator note. Raises on failure — the
        caller decides whether that matters."""
        if not self.ok or not record_id or not fields:
            return False
        self._request("PATCH", f"{self.base_url}/{record_id}", {"fields": fields})
        return True

    def push(self, match_barcode, fields):
        """Low-level upsert used by the durable outbox. `match_barcode` is the
        already-normalized key. Updates the matching shipment row, or creates it
        if none exists. Returns one of: 'synced' (updated or created),
        'no_barcode', 'disabled', or 'error: <msg>' (retry later). Second tuple
        element is the Airtable record id on success, else None."""
        if not self.ok:
            return ("disabled", None)
        if not match_barcode:
            return ("no_barcode", None)
        try:
            rid = self._find_id(match_barcode)
            if rid:
                self._request("PATCH", f"{self.base_url}/{rid}", {"fields": fields})
                return ("synced", rid)
            # Only CREATE for a plausible tracking number (all digits, >=12) so
            # camera/test scans (e.g. "…CAMTEST") never fabricate junk rows.
            if not (match_barcode.isdigit() and len(match_barcode) >= 12):
                return ("no_row", None)
            new_id = self._create(match_barcode, fields)
            return ("synced", new_id)
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


def insole_summaries_from_pairs(pairs):
    """Aggregate an insole run into the two Airtable column texts:
    {'insoles_currex': '1 pair, 2 singles', 'insoles_superfeet': '0 pairs, ...'}.

    Pairs vs singles by pair_score (set = a true pair, NULL = a single — the
    singles get grinded, which is why the split matters). Unknown-brand insoles
    are counted in neither column: they stay in the dash for review and never
    reach Airtable. Both columns are always written for an insole run, zeros
    included, so an insole shipment shows an explicit result."""
    from backend.utils.insole_text import format_insole_counts
    counts = {"currex": [0, 0], "superfeet": [0, 0]}   # brand -> [pairs, singles]
    for p in pairs:
        mk = str(p.get("final_make") or p.get("make") or "").strip().lower()
        if mk in counts:
            counts[mk][0 if p.get("pair_score") is not None else 1] += 1
    return {"insoles_currex": format_insole_counts(*counts["currex"]),
            "insoles_superfeet": format_insole_counts(*counts["superfeet"])}


def brand_summary_from_pairs(pairs):
    """Aggregate makes into 'Nike: 3, Adidas: 2' (skips unknown), most-common
    first. Brands are canonicalized so case/alias variants (ASICS vs Asics) merge
    into one entry instead of 'ASICS: 2, Asics: 2'. Insole brands (Currex,
    Superfeet, …) are excluded: their counts are entered manually at capture and
    live in the Insoles_* columns, so counting them here would double-count."""
    from backend.utils.brands import canonical_brand, is_insole_brand
    counts = {}
    for p in pairs:
        mk = canonical_brand(p.get("final_make") or p.get("make"))
        if mk and not is_insole_brand(mk):
            counts[mk] = counts.get(mk, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ", ".join(f"{k}: {v}" for k, v in ordered)
