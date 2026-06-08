"""
fedex_track.py — resolve a scanned FedEx tracking number to shipment details.

Diagnostic helper for the Airtable Sync page: when a captured box can't sync
because its shipment isn't in Airtable yet ("no_row"), an operator can look the
tracking number up with FedEx to see whether it's a real, delivered shipment
(partner / weight / delivery date) vs a bad scan.

Uses the FedEx OAuth2 client-credentials flow + the Track API over plain HTTPS
(urllib) so the dash venv needs no extra dependency. The bearer token is cached
in-process until it nears expiry. Fail-safe: any error returns a dict with
{"found": False, "error": ...}; never raises.

Set FEDEX_API_KEY + FEDEX_SECRET (+ optional FEDEX_BASE_URL for sandbox) in .env.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from backend.config import (FEDEX_API_KEY, FEDEX_SECRET, FEDEX_BASE_URL,
                            FEDEX_TIMEOUT, FEDEX_ENABLED)

_token = {"value": None, "exp": 0.0}   # in-process bearer-token cache


def fedex_enabled() -> bool:
    return bool(FEDEX_ENABLED)


def _now() -> float:
    return time.time()


def _get_token():
    """Client-credentials OAuth2 token, cached until ~60s before expiry."""
    if _token["value"] and _now() < _token["exp"] - 60:
        return _token["value"]
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": FEDEX_API_KEY,
        "client_secret": FEDEX_SECRET,
    }).encode()
    req = urllib.request.Request(
        f"{FEDEX_BASE_URL}/oauth/token", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=FEDEX_TIMEOUT) as r:
        tok = json.loads(r.read().decode())
    _token["value"] = tok["access_token"]
    _token["exp"] = _now() + float(tok.get("expires_in", 3600))
    return _token["value"]


def _first(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def _parse(result: dict) -> dict:
    """Pull the few fields we care about out of one trackingResults entry."""
    err = result.get("error")
    if err:
        return {"found": False, "error": err.get("message") or err.get("code") or "not found"}
    status = result.get("latestStatusDetail") or {}
    code = status.get("code")
    # Delivery date: prefer ACTUAL_DELIVERY among dateAndTimes.
    delivered_date = None
    for dt in result.get("dateAndTimes", []) or []:
        if dt.get("type") in ("ACTUAL_DELIVERY", "ACTUAL_TENDER"):
            delivered_date = dt.get("dateTime"); break
    # Weight (shipment-level if present).
    weight = None
    for w in (result.get("shipmentDetails") or {}).get("weight", []) or []:
        if w.get("value"):
            weight = f"{w.get('value')} {w.get('unit', '')}".strip(); break
    # Shipper city/state — our closest proxy to "partner".
    ship_addr = (result.get("shipperInformation") or {}).get("address") or {}
    shipper = ", ".join([x for x in (ship_addr.get("city"),
                                     ship_addr.get("stateOrProvinceCode"),
                                     ship_addr.get("countryCode")) if x]) or None
    service = (result.get("serviceDetail") or {}).get("description")
    return {
        "found":          True,
        "status":         _first(status, "statusByLocale", "description", default=code or "unknown"),
        "status_code":    code,
        "delivered":      code == "DL",
        "delivery_date":  delivered_date,
        "weight":         weight,
        "shipper":        shipper,
        "service":        service,
        "error":          None,
    }


def track(tracking_number: str) -> dict:
    """Look up one tracking number. Returns a normalized dict (never raises)."""
    if not fedex_enabled():
        return {"found": False, "error": "FedEx lookup disabled (no FEDEX_API_KEY)"}
    if not tracking_number:
        return {"found": False, "error": "no tracking number"}
    try:
        token = _get_token()
        body = json.dumps({
            "includeDetailedScans": False,
            "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": str(tracking_number)}}],
        }).encode()
        req = urllib.request.Request(
            f"{FEDEX_BASE_URL}/track/v1/trackingnumbers", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}",
                     "X-locale": "en_US"})
        with urllib.request.urlopen(req, timeout=FEDEX_TIMEOUT) as r:
            resp = json.loads(r.read().decode())
        results = (resp.get("output") or {}).get("completeTrackResults") or []
        if not results:
            return {"found": False, "error": "no results"}
        tr = (results[0].get("trackingResults") or [])
        if not tr:
            return {"found": False, "error": "no results"}
        out = _parse(tr[0])
        out["tracking_number"] = str(tracking_number)
        return out
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()[:200]
        except Exception:
            pass
        return {"found": False, "error": f"HTTP {exc.code}: {body}"}
    except Exception as exc:                            # noqa: BLE001 - fail safe
        return {"found": False, "error": str(exc)}
