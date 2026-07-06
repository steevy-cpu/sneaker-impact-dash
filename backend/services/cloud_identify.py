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
                            GEMINI_API_KEY, GEMINI_MODEL, GEMINI_URL,
                            OPENAI_API_KEY, OPENAI_MODEL, OPENAI_URL)

_PROMPT = (
    "You are identifying ONE used sneaker from a cropped, top-down photo taken "
    "at a shoe-recycling station (used shoes, not clean product shots). "
    "From the logo, silhouette, midsole/outsole and any visible text, determine:\n"
    "- color: the single dominant color (basic name: black, white, gray, brown, "
    "red, orange, yellow, green, blue, purple, pink).\n"
    "- brand: the manufacturer (e.g. Nike, Adidas, New Balance, Hoka, Saucony).\n"
    "- model: the specific silhouette / model name (e.g. \"Air Jordan 1\", "
    "\"Nike Dunk Low\", \"Adidas Superstar\").\n"
    "IMPORTANT: NEVER answer \"unknown\" or leave a field blank. Always commit to "
    "your single BEST GUESS for every field, even when you are not sure — then "
    "express how sure you are in the confidence score, NOT by refusing. Give a "
    "calibrated confidence in [0,1] per field: high when the logo/model is "
    "clearly visible, low (e.g. 0.1-0.4) when it is mostly a guess. "
    "Return ONLY the JSON object."
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


def _lens_context(titles):
    """Prompt block carrying Google Lens visual-match titles. The titles are
    web-page names of visually similar products — strong evidence, but noisy
    (marketplace listings mix sizes/colors/wrong models), so the model is told
    to weigh them against what it SEES, not follow them blindly."""
    lines = "\n".join(f"- {t}" for t in titles)
    return (
        "\nADDITIONAL EVIDENCE — a Google Lens reverse-image search on this "
        "exact photo returned these visually-similar product titles (noisy web "
        "listings; use them as strong hints for brand/model, but only when "
        "consistent with what you actually see in the image):\n" + lines + "\n"
    )


def _gemini(crop_path, lens_titles=None):
    """One Gemini generateContent call on the crop. Returns parsed dict or None."""
    with open(crop_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    prompt = _PROMPT + (_lens_context(lens_titles) if lens_titles else "")
    body = {
        "contents": [{
            "parts": [
                {"text": prompt},
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
    # Read timeouts retry too: gemini-2.5-pro is a reasoning model that can
    # think past even the long CLOUD_TIMEOUT; one more attempt usually lands.
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
        except (TimeoutError, urllib.error.URLError) as exc:
            timed_out = isinstance(exc, TimeoutError) or isinstance(
                getattr(exc, "reason", None), TimeoutError)
            if timed_out and attempt < 2:
                continue                         # fresh attempt right away
            raise


def _openai(crop_path):
    """One OpenAI chat.completions call on the crop. Returns parsed dict or None.

    Uses Structured Outputs (response_format json_schema, strict) so the model
    must return exactly our shape. Image is sent as a base64 data URL."""
    with open(crop_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    body = {
        "model": OPENAI_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "shoe_id",
                "strict": True,
                "schema": {**_SCHEMA, "additionalProperties": False},
            },
        },
    }
    url = f"{OPENAI_URL}/chat/completions"
    data = json.dumps(body).encode()
    # gpt-5 is a reasoning model: don't send temperature (only the default is
    # accepted). Retry transient throttling/overload the same way as Gemini.
    for attempt in range(3):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {OPENAI_API_KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=CLOUD_TIMEOUT) as r:
                resp = json.loads(r.read().decode())
            text = resp["choices"][0]["message"]["content"]
            return json.loads(text)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise


def identify(crop_path, lens_titles=None):
    """Cloud-identify one shoe crop. Returns a normalized dict or None.
    `lens_titles` (optional): Google Lens visual-match titles to give the model
    as extra brand/model evidence (gemini backend only)."""
    if not cloud_enabled():
        return None
    try:
        if CLOUD_BACKEND == "gemini":
            raw = _gemini(crop_path, lens_titles=lens_titles)
        elif CLOUD_BACKEND == "openai":
            raw = _openai(crop_path)
        else:
            return None
        if not isinstance(raw, dict):
            return None
        model_id = OPENAI_MODEL if CLOUD_BACKEND == "openai" else GEMINI_MODEL
        out = {
            "color":            (raw.get("color") or "unknown").strip().lower(),
            "brand":            (raw.get("brand") or "unknown").strip(),
            "model":            (raw.get("model") or "unknown").strip(),
            "color_confidence": _clamp01(raw.get("color_confidence")),
            "brand_confidence": _clamp01(raw.get("brand_confidence")),
            "model_confidence": _clamp01(raw.get("model_confidence")),
            "source":           f"cloud:{CLOUD_BACKEND}:{model_id}",
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
