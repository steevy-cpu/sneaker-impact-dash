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
import re
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

    PARTNER_CACHE_TTL = 3600          # seconds
    BATCH = 40                        # barcodes per Airtable request (URL stays short)

    def __init__(self, api_key, base_id, shipments_table, partners_table, trim):
        self.api_key = api_key
        self.base_id = base_id
        self.shipments_table = shipments_table
        self.partners_table = partners_table
        self.trim = trim
        self.ok = bool(api_key and base_id)
        self._partner_cache = {}      # rec id -> (monotonic ts, name); partners rarely rename

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
            hit = self._partner_cache.get(p[0])
            if hit and time.monotonic() - hit[0] < self.PARTNER_CACHE_TTL:
                return hit[1]
            try:
                url = (f"https://api.airtable.com/v0/{self.base_id}/"
                       f"{urllib.parse.quote(self.partners_table)}/{p[0]}")
                rec = self._get(url)
                pf = rec.get("fields", {})
                name = pf.get("Partner Name") or pf.get("Name") or p[0]
                self._partner_cache[p[0]] = (time.monotonic(), name)
                return name
            except Exception:                          # noqa: BLE001 - fail safe
                return None
        if isinstance(p, list):
            return ", ".join(str(x) for x in p)
        return str(p)

    def _info(self, rec, bc, partner):
        f = rec.get("fields", {})
        return {
            "found":         True,
            "barcode":       bc,
            "source":        self.name,
            "record_id":     rec.get("id"),
            "partner":       partner,
            "weight":        self._pick(f, self.FIELDS["weight"]),
            "end_of_life":   self._pick(f, self.FIELDS["end_of_life"]),
            "good_sneakers": self._pick(f, self.FIELDS["good_sneakers"]),
            "casuals":       self._pick(f, self.FIELDS["casuals"]),
            "brand_summary": self._pick(f, self.FIELDS["brand_summary"]),
            "status":        self._pick(f, self.FIELDS["status"]),
        }

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
            return self._info(recs[0], bc, self._partner_name(recs[0].get("fields", {})))
        except Exception as exc:                       # noqa: BLE001 - fail safe
            print(f"[shipment] airtable lookup failed: {exc}")
            return None

    # -- batch path (used by the partner resolver) ------------------------
    # Unlike resolve() this RAISES on any API error, so the caller can tell
    # "Airtable is unreachable, retry soon" from "this shipment isn't there".

    _SAFE = re.compile(r"[^A-Za-z0-9\-]")

    def _get_fields(self, url, fparam, flag="_no_field_filter"):
        """GET with a fields[] filter to keep responses small. Airtable answers
        422 for a field name that doesn't exist (a rename, or one of our
        fallback candidates), so on 422 retry unfiltered and remember it."""
        if not getattr(self, flag, False):
            try:
                return self._get(url + fparam)
            except urllib.error.HTTPError as e:
                if e.code != 422:
                    raise
                setattr(self, flag, True)
                print(f"[shipment] fields[] filter rejected (422, {flag}); using unfiltered batch reads")
        return self._get(url)

    def _partner_names(self, rec_ids):
        """{rec id: name} for linked Partner records, via the cache + one batched
        request for the misses. Raises on API error."""
        now = time.monotonic()
        out, miss = {}, []
        for rid in rec_ids:
            hit = self._partner_cache.get(rid)
            if hit and now - hit[0] < self.PARTNER_CACHE_TTL:
                out[rid] = hit[1]
            else:
                miss.append(rid)
        base = (f"https://api.airtable.com/v0/{self.base_id}/"
                f"{urllib.parse.quote(self.partners_table)}")
        for i in range(0, len(miss), 50):
            chunk = miss[i:i + 50]
            formula = "OR(" + ",".join("RECORD_ID()='%s'" % self._SAFE.sub("", r) for r in chunk) + ")"
            url = base + "?pageSize=100&filterByFormula=" + urllib.parse.quote(formula)
            # "Partner Name" is the real field; "Name" is only a fallback candidate
            # and Airtable 422s on unknown names, so ask for the one we know.
            data = self._get_fields(url, "&fields%5B%5D=Partner%20Name", flag="_no_partner_filter")
            got = {r["id"]: r.get("fields", {}) for r in data.get("records", [])}
            for rid in chunk:
                pf = got.get(rid, {})
                name = pf.get("Partner Name") or pf.get("Name") or rid
                self._partner_cache[rid] = (time.monotonic(), name)
                out[rid] = name
        return out

    def resolve_many(self, barcodes):
        """{normalized barcode: info} for the barcodes that exist in Shipments
        Received (absent key = not found). ~40 barcodes per request."""
        if not self.ok:
            raise RuntimeError("airtable not configured")
        wanted = [b for b in dict.fromkeys(barcodes) if b]
        result = {}
        base = (f"https://api.airtable.com/v0/{self.base_id}/"
                f"{urllib.parse.quote(self.shipments_table)}")
        want_fields = {"Barcode", "Tracking Number"}
        for names in self.FIELDS.values():
            want_fields.update(names)
        fparam = "".join("&fields%5B%5D=" + urllib.parse.quote(n) for n in sorted(want_fields))
        for i in range(0, len(wanted), self.BATCH):
            chunk = wanted[i:i + self.BATCH]
            formula = "OR(" + ",".join(
                "{Barcode}='%s'" % self._SAFE.sub("", b) for b in chunk) + ")"
            recs, offset = [], None
            while True:
                url = (base + "?pageSize=100&filterByFormula=" + urllib.parse.quote(formula)
                       + (f"&offset={offset}" if offset else ""))
                data = self._get_fields(url, fparam)
                recs += data.get("records", [])
                offset = data.get("offset")
                if not offset:
                    break
            # Index each record under its Barcode text AND its Tracking Number
            # (both normalized), preferring a record that already has a partner
            # when a barcode appears on more than one row.
            partner_ids = {}
            for rec in recs:
                f = rec.get("fields", {})
                p = self._pick(f, self.FIELDS["partner"])
                if isinstance(p, list) and p and isinstance(p[0], str) and p[0].startswith("rec"):
                    partner_ids[rec["id"]] = p[0]
            names = self._partner_names(sorted(set(partner_ids.values()))) if partner_ids else {}
            for rec in recs:
                f = rec.get("fields", {})
                p = self._pick(f, self.FIELDS["partner"])
                if rec["id"] in partner_ids:
                    partner = names.get(partner_ids[rec["id"]])
                elif isinstance(p, list) and p:
                    partner = ", ".join(str(x) for x in p)
                else:
                    partner = str(p) if p else None
                bcf = f.get("Barcode")
                keys = []
                for raw in (bcf.get("text") if isinstance(bcf, dict) else bcf, f.get("Tracking Number")):
                    if raw:
                        keys.append(normalize_barcode(str(raw), self.trim))
                for key in dict.fromkeys(keys):
                    if key in wanted:
                        have = result.get(key)
                        if have is None or (not have["partner"] and partner):
                            result[key] = self._info(rec, key, partner)
        return result


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


def get_airtable_lookup():
    """The AirtableShipmentLookup inside the shared chain, or None if airtable
    isn't a configured source (the partner resolver then stays inert)."""
    for lk in get_shipment_lookup().lookups:
        if isinstance(lk, AirtableShipmentLookup) and lk.ok:
            return lk
    return None
