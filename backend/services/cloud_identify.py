"""
cloud_identify.py — cloud vision fallback for shoe color/brand/model.

When the LOCAL pipeline isn't confident, the worker calls this to get a better
prediction from a cloud multimodal model. Swappable by config.CLOUD_BACKEND;
the only backend wired today is Google Gemini (public API, cheap, native JSON
schema output). Implemented over plain HTTPS (urllib) so the dash venv needs no
extra dependency.

identify(crop_path) -> {"color","brand","model",
                        "color_confidence","brand_confidence","model_confidence",
                        "source"}  or  None on failure / disabled.

Fail-safe: any error (no key, network, bad JSON) returns None and the caller
keeps the local prediction. Never raises.
"""
import base64
import json
import time
import urllib.error
import urllib.request

from backend.config import (CLOUD_BACKEND, CLOUD_IDENTIFY_ENABLED, CLOUD_TIMEOUT,
                            GEMINI_API_KEY, GEMINI_MODEL, GEMINI_URL)

_PROMPT = (
    "You are identifying ONE used sneaker from a cropped, top-down photo taken "
    "at a shoe-recycling station (used shoes, not clean product shots). "
    "Identify, as best you can from the logo, shape and any visible text:\n"
    "- color: the single dominant color (basic name: black, white, gray, brown, "
    "red, orange, yellow, green, blue, purple, pink).\n"
    "- brand: the manufacturer (e.g. Nike, Adidas, New Balance, Hoka, Saucony). "
    "Use \"unknown\" if you genuinely cannot tell.\n"
    "- model: the specific silhouette / model name (e.g. \"Air Jordan 1\", "
    "\"Nike Dunk Low\", \"Adidas Superstar\"). Use \"unknown\" if unsure.\n"
    "Give a calibrated confidence in [0,1] for each field — be honest, use low "
    "values when guessing. Return ONLY the JSON object."
)

# Gemini structured-output schema (OpenAPI subset) -> guaranteed JSON shape.
_SCHEMA = {
    "type": "object",
    "properties": {
        "color":            {"type": "string"},
        "brand":            {"type": "string"},
        "model":            {"type": "string"},
        "color_confidence": {"type": "number"},
        "brand_confidence": {"type": "number"},
        "model_confidence": {"type": "number"},
    },
    "required": ["color", "brand", "model",
                 "color_confidence", "brand_confidence", "model_confidence"],
}


def cloud_enabled() -> bool:
    return bool(CLOUD_IDENTIFY_ENABLED)


def _clamp01(v):
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None


def _gemini(crop_path):
    """One Gemini generateContent call on the crop. Returns parsed dict or None."""
    with open(crop_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    body = {
        "contents": [{
            "parts": [
                {"text": _PROMPT},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            ],
        }],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
        },
    }
    url = f"{GEMINI_URL}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    data = json.dumps(body).encode()
    # Retry transient throttling/overload (429 rate limit, 503 overloaded) with a
    # short backoff. Free-tier per-minute limits are common; a couple of waits
    # recover many calls. A persistent quota error still falls through to None.
    for attempt in range(3):
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=CLOUD_TIMEOUT) as r:
                resp = json.loads(r.read().decode())
            text = resp["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))    # 5s, then 10s
                continue
            raise


def identify(crop_path):
    """Cloud-identify one shoe crop. Returns a normalized dict or None."""
    if not cloud_enabled():
        return None
    try:
        if CLOUD_BACKEND == "gemini":
            raw = _gemini(crop_path)
        else:
            return None
        if not isinstance(raw, dict):
            return None
        out = {
            "color":            (raw.get("color") or "unknown").strip().lower(),
            "brand":            (raw.get("brand") or "unknown").strip(),
            "model":            (raw.get("model") or "unknown").strip(),
            "color_confidence": _clamp01(raw.get("color_confidence")),
            "brand_confidence": _clamp01(raw.get("brand_confidence")),
            "model_confidence": _clamp01(raw.get("model_confidence")),
            "source":           f"cloud:{CLOUD_BACKEND}:{GEMINI_MODEL}",
        }
        return out
    except urllib.error.HTTPError as exc:               # noqa: BLE001 - fail safe
        body = ""
        try:
            body = exc.read().decode()[:200]
        except Exception:
            pass
        print(f"[cloud_identify] HTTP {exc.code}: {body}")
        return None
    except Exception as exc:                            # noqa: BLE001 - fail safe
        print(f"[cloud_identify] error: {exc}")
        return None
