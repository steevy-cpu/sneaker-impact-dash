"""
shipment_lookup.py — resolve a scanned barcode to its shipment/order record.

Pluggable (config.SHIPMENT_LOOKUP_SOURCES), mirroring how ShoeSort matches a
barcode (often a FedEx tracking #) against the company's Airtable, with the
fragile bits fixed:

  - AirtableShipmentLookup: Airtable filterByFormula {Barcode}='<last N digits>'
    against the "Shipments Received" table; resolves the linked Partner. Adds a
    TTL cache, retry/backoff, and configurable field mapping with fallbacks
    (so an Airtable field rename degrades instead of breaking).
  - FedExShipmentLookup: stub for a future live OAuth2 Track API (returns None
    so a chain falls through to Airtable).
  - ChainedShipmentLookup: try each source in order; first hit wins; one shared
    TTL cache in front of all of them.

Fully fail-safe: any error/missing-config yields None (never blocks capture).
Uses stdlib urllib only (no extra dependency).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from backend.config import (SHIPMENT_LOOKUP_SOURCES, SHIPMENT_CACHE_TTL,
                            SHIPMENT_BARCODE_TRIM, AIRTABLE_API_KEY,
                            AIRTABLE_BASE_ID, AIRTABLE_SHIPMENTS_TABLE,
                            AIRTABLE_PARTNERS_TABLE)


def normalize_barcode(barcode, trim=SHIPMENT_BARCODE_TRIM):
    """Match what ShoeSort stores: the last `trim` chars (handles short codes)."""
    bc = (barcode or "").strip()
    if trim and len(bc) > trim:
        return bc[-trim:]
    return bc


class _TTLCache:
    def __init__(self, ttl):
        self.ttl = ttl
        self._d = {}

    def get(self, key):
        hit = self._d.get(key)
        if not hit:
            return None
        ts, val = hit
        if time.monotonic() - ts > self.ttl:
            self._d.pop(key, None)
            return None
        return val

    def set(self, key, val):
        self._d[key] = (time.monotonic(), val)


class ShipmentLookup:
    name = "base"

    def resolve(self, barcode):
        return None


class AirtableShipmentLookup(ShipmentLookup):
    name = "airtable"

    # Field name -> ordered fallbacks (improves on ShoeSort's single-field
    # Casual/Mixed fallback; a rename degrades to None instead of crashing).
    FIELDS = {
        "weight":        ["Weight (lbs)", "Weight"],
        "partner":       ["Partner", "Partners"],
        "end_of_life":   ["End of Life"],
        "good_sneakers": ["Good Sneakers"],
        "casuals":       ["Casual/Mixed", "Casual/Mixed Total"],
        "brand_summary": ["Brand Summary"],
        "status":        ["Status"],
    }

    def __init__(self, api_key, base_id, shipments_table, partners_table, trim):
        self.api_key = api_key
        self.base_id = base_id
        self.shipments_table = shipments_table
        self.partners_table = partners_table
        self.trim = trim
        self.ok = bool(api_key and base_id)

    # -- HTTP with light retry/backoff -----------------------------------

    def _get(self, url):
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.api_key}"})
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                break
            except Exception as e:                     # noqa: BLE001 - retry net errors
                last = e
                if attempt < 2:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                break
        raise last or RuntimeError("airtable request failed")

    def _pick(self, fields, keys):
        for k in keys:
            v = fields.get(k)
            if v not in (None, ""):
                return v
        return None

    def _partner_name(self, fields):
        p = self._pick(fields, self.FIELDS["partner"])
        if not p:
            return None
        # Linked-record field -> [recXXacc...]; resolve the first to its name.
        if isinstance(p, list) and p and isinstance(p[0], str) and p[0].startswith("rec"):
            try:
                url = (f"https://api.airtable.com/v0/{self.base_id}/"
                       f"{urllib.parse.quote(self.partners_table)}/{p[0]}")
                rec = self._get(url)
                pf = rec.get("fields", {})
                return pf.get("Partner Name") or pf.get("Name") or p[0]
            except Exception:                          # noqa: BLE001 - fail safe
                return None
        if isinstance(p, list):
            return ", ".join(str(x) for x in p)
        return str(p)

    def resolve(self, barcode):
        if not self.ok:
            return None
        bc = normalize_barcode(barcode, self.trim)
        if not bc:
            return None
        try:
            formula = "{Barcode}='%s'" % bc.replace("'", "")
            url = (f"https://api.airtable.com/v0/{self.base_id}/"
                   f"{urllib.parse.quote(self.shipments_table)}"
                   f"?maxRecords=1&filterByFormula={urllib.parse.quote(formula)}")
            data = self._get(url)
            recs = data.get("records", [])
            if not recs:
                return {"found": False, "barcode": bc, "source": self.name}
            f = recs[0].get("fields", {})
            return {
                "found":         True,
                "barcode":       bc,
                "source":        self.name,
                "record_id":     recs[0].get("id"),
                "partner":       self._partner_name(f),
                "weight":        self._pick(f, self.FIELDS["weight"]),
                "end_of_life":   self._pick(f, self.FIELDS["end_of_life"]),
                "good_sneakers": self._pick(f, self.FIELDS["good_sneakers"]),
                "casuals":       self._pick(f, self.FIELDS["casuals"]),
                "brand_summary": self._pick(f, self.FIELDS["brand_summary"]),
                "status":        self._pick(f, self.FIELDS["status"]),
            }
        except Exception as exc:                       # noqa: BLE001 - fail safe
            print(f"[shipment] airtable lookup failed: {exc}")
            return None


class FedExShipmentLookup(ShipmentLookup):
    """Stub for a future live FedEx Track API (OAuth2). Returns None today so a
    chained lookup falls through to Airtable. Wire developer.fedex.com
    /track/v1/trackingnumbers here later."""
    name = "fedex"

    def __init__(self):
        self.ok = False

    def resolve(self, barcode):
        return None


class ChainedShipmentLookup(ShipmentLookup):
    name = "chained"

    def __init__(self, lookups, cache_ttl):
        self.lookups = lookups
        self.cache = _TTLCache(cache_ttl)

    def resolve(self, barcode):
        bc = (barcode or "").strip()
        if not bc:
            return None
        cached = self.cache.get(bc)
        if cached is not None:
            return cached
        result = None
        for lk in self.lookups:
            try:
                r = lk.resolve(bc)
            except Exception:                          # noqa: BLE001 - fail safe
                r = None
            if r and r.get("found"):
                result = r
                break
            if r is not None and result is None:
                result = r          # remember a not-found so we still cache it
        self.cache.set(bc, result)
        return result


def is_configured():
    """True if at least one lookup source has the credentials it needs."""
    sources = [s.strip().lower() for s in SHIPMENT_LOOKUP_SOURCES.split(",")]
    return "airtable" in sources and bool(AIRTABLE_API_KEY and AIRTABLE_BASE_ID)


def build_shipment_lookup():
    lookups = []
    for s in [s.strip().lower() for s in SHIPMENT_LOOKUP_SOURCES.split(",") if s.strip()]:
        if s == "airtable":
            lookups.append(AirtableShipmentLookup(
                AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AIRTABLE_SHIPMENTS_TABLE,
                AIRTABLE_PARTNERS_TABLE, SHIPMENT_BARCODE_TRIM))
        elif s == "fedex":
            lookups.append(FedExShipmentLookup())
    return ChainedShipmentLookup(lookups, SHIPMENT_CACHE_TTL)


_singleton = None


def get_shipment_lookup():
    """Process-wide singleton (so the TTL cache is shared across requests)."""
    global _singleton
    if _singleton is None:
        _singleton = build_shipment_lookup()
    return _singleton
