"""
visual_search.py — Google Lens reverse-image search for a shoe crop, via
Cloudflare Images (hosting) + Bright Data (anti-bot proxy).

Chain (the old desktop ShoeSort's proven pipeline, ported):
  1. upload the crop to Cloudflare Images  -> public imagedelivery.net URL
  2. POST api.brightdata.com/request with url=lens.google.com/uploadbyurl?...
     (&brd_json=html makes Bright Data return PARSED JSON: images[].title etc.)
  3. collect the visual-match titles — the LLM-ready signal ("Nike Free TR
     Fit 4" appears verbatim in result titles)
  4. ALWAYS delete the hosted image afterwards (the old system once hit its
     Cloudflare Images storage quota; searches must not accumulate images).

Direct/free Lens scraping from this server is NOT possible — Google serves its
/sorry reCAPTCHA wall on the first attempt from this IP (verified 2026-07-02);
Bright Data absorbs that. Cost ~$0.0015 per Lens call.

Fail-safe: lens_titles() returns [] on any error, never raises to the caller.
Blocking (Lens takes ~40s) — call from worker/background threads only, NEVER
from the event loop ([[website-speed-constraint]]).
"""
import json
import urllib.parse

import requests

from backend.config import (BRIGHTDATA_BEARER, BRIGHTDATA_ZONE,
                            CF_IMAGES_ACCOUNT_ID, CF_IMAGES_TOKEN,
                            VISUAL_SEARCH_ENABLED, VISUAL_SEARCH_MAX_TITLES,
                            VISUAL_SEARCH_TIMEOUT)

_CF_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_IMAGES_ACCOUNT_ID}/images/v1"
_BD_URL = "https://api.brightdata.com/request"


def visual_search_enabled() -> bool:
    return bool(VISUAL_SEARCH_ENABLED and CF_IMAGES_ACCOUNT_ID and CF_IMAGES_TOKEN
                and BRIGHTDATA_BEARER and BRIGHTDATA_ZONE)


def _cf_upload(crop_path):
    """Upload one crop; returns (image_id, public_url) or (None, None)."""
    with open(crop_path, "rb") as fh:
        r = requests.post(
            _CF_BASE, headers={"Authorization": f"Bearer {CF_IMAGES_TOKEN}"},
            files={"file": ("shoe.jpg", fh, "image/jpeg")}, timeout=40)
    if r.status_code != 200:
        print(f"[visual-search] CF upload failed: HTTP {r.status_code} {r.text[:200]}",
              flush=True)
        return None, None
    res = r.json().get("result") or {}
    img_id = res.get("id")
    url = (res.get("variants") or [None])[0] or (
        f"https://imagedelivery.net/{CF_IMAGES_ACCOUNT_ID}/{img_id}/public" if img_id else None)
    return img_id, url


def _cf_delete(img_id):
    """Best-effort delete of a hosted crop (quota hygiene)."""
    try:
        requests.delete(f"{_CF_BASE}/{img_id}",
                        headers={"Authorization": f"Bearer {CF_IMAGES_TOKEN}"},
                        timeout=20)
    except Exception as exc:                            # noqa: BLE001 - best effort
        print(f"[visual-search] CF delete failed for {img_id}: {exc}", flush=True)


def _lens(url):
    """Bright Data -> Google Lens on a public image URL. Returns parsed dict.
    Bright Data intermittently returns a non-JSON/empty body (~20% in testing),
    so retry once on a JSON-decode failure before giving up."""
    lens_url = (f"https://lens.google.com/uploadbyurl"
                f"?url={urllib.parse.quote(url, safe='')}&brd_json=html")
    payload = {"zone": BRIGHTDATA_ZONE, "url": lens_url, "format": "raw"}
    headers = {"Authorization": f"Bearer {BRIGHTDATA_BEARER}",
               "Content-Type": "application/json"}
    last = None
    for attempt in range(2):
        r = requests.post(_BD_URL, headers=headers, json=payload,
                          timeout=VISUAL_SEARCH_TIMEOUT)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as exc:                       # non-JSON body → retry once
            last = exc
    raise last


# Unambiguous sneaker-brand keywords for a title-consensus read (the "what does
# Google think" signal, logged next to Gemini's answer). "On" is deliberately
# omitted — too many false hits ("on sale"). Multi-word forms first.
_BRAND_KEYWORDS = [
    "new balance", "under armour", "hoka one one", "topo athletic", "la sportiva",
    "golden goose", "xero shoes", "on running", "nike", "adidas", "hoka", "brooks",
    "saucony", "asics", "mizuno", "altra", "reebok", "puma", "skechers", "salomon",
    "merrell", "vans", "converse", "fila", "allbirds", "diadora", "jordan", "oofos",
    "li-ning", "inov-8", "anta", "peak", "nobull", "ecco", "keen", "ryka", "avia",
    "k-swiss", "crocs", "vivobarefoot",
]


def title_consensus(titles):
    """Best-guess brand from the Lens titles + how strongly they agree.
    Returns (brand_or_None, strength 0..1 = share of titles mentioning it).
    A cheap, no-LLM read used ONLY for logging today — data to later decide if a
    strong consensus can skip the paid Gemini call."""
    import re
    from collections import Counter
    if not titles:
        return None, 0.0
    cnt = Counter()
    for t in titles:
        tl = (t or "").lower()
        for b in _BRAND_KEYWORDS:
            if re.search(r"\b" + re.escape(b) + r"\b", tl):
                cnt[b] += 1
                break                                    # one brand per title
    if not cnt:
        return None, 0.0
    brand, n = cnt.most_common(1)[0]
    return brand, n / len(titles)


def lens_titles(crop_path):
    """Visual-match titles for one crop, deduped, capped. [] on any failure."""
    if not visual_search_enabled():
        return []
    img_id = None
    try:
        img_id, url = _cf_upload(crop_path)
        if not url:
            return []
        data = _lens(url)
        titles, seen = [], set()
        for im in (data.get("images") or []):
            t = (im.get("title") or "").strip() if isinstance(im, dict) else ""
            key = t.lower()
            if t and key not in seen:
                seen.add(key)
                titles.append(t)
            if len(titles) >= VISUAL_SEARCH_MAX_TITLES:
                break
        print(f"[visual-search] {crop_path.rsplit('/', 1)[-1]}: "
              f"{len(titles)} Lens titles", flush=True)
        return titles
    except Exception as exc:                            # noqa: BLE001 - fail safe
        print(f"[visual-search] failed for {crop_path}: {exc}", flush=True)
        return []
    finally:
        if img_id:
            _cf_delete(img_id)
