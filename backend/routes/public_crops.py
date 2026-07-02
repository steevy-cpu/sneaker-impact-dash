"""
public_crops.py — a locked-down PUBLIC endpoint that serves ONE pair crop by a
short-lived signed URL, so an external service (Google Lens, via Bright Data) can
fetch the image during a visual-search call.

Why this exists: Bright Data drives `lens.google.com/uploadbyurl?url=<crop>`, and
Google's servers then fetch `<crop>` — so the crop must be reachable from the
public internet. We expose it via a Cloudflare Tunnel, but ONLY through this one
endpoint, which:
  * serves nothing but a single crop JPEG from PAIRS_DIR (no DB, no other routes),
  * requires a valid HMAC signature over (name, expiry) — no signature, no image,
  * expires after PUBLIC_CROP_TTL seconds (long enough for one Lens call),
  * hard-validates the filename (no path traversal) and that it lives in PAIRS_DIR.

DISABLED by default: if PUBLIC_CROP_SECRET is unset, every request 404s. So this
is inert until you set the secret in .env (and point the tunnel at /public/*).
Config is read straight from the environment (not backend.config) to stay
self-contained and avoid touching shared config.
"""
import hashlib
import hmac
import os
import re
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from backend.config import PAIRS_DIR

router = APIRouter(prefix="/public", tags=["Public Crops"])

_SECRET = os.getenv("PUBLIC_CROP_SECRET", "").encode()
_TTL = int(os.getenv("PUBLIC_CROP_TTL", "300"))          # signed-URL lifetime (s)
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+\.jpg$")          # crop filenames only


def enabled() -> bool:
    return bool(_SECRET)


def _sig(name: str, exp: int) -> str:
    return hmac.new(_SECRET, f"{name}:{exp}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def public_crop_url(name: str, base_url: str, ttl: int | None = None) -> str:
    """Mint a signed public URL for a crop. `base_url` is the tunnel origin
    (e.g. https://crops.example.com). Raises if the endpoint isn't enabled."""
    if not enabled():
        raise RuntimeError("public crop serving disabled (PUBLIC_CROP_SECRET unset)")
    exp = int(time.time()) + (ttl or _TTL)
    return f"{base_url.rstrip('/')}/public/crop/{name}?exp={exp}&sig={_sig(name, exp)}"


@router.get("/crop/{name}", summary="Serve one crop by signed URL (for Lens fetch)")
def serve_crop(name: str, exp: int = Query(...), sig: str = Query(...)):
    # Every failure returns an identical 404 — never leak why.
    if not enabled():
        raise HTTPException(status_code=404, detail="not found")
    if not _NAME_RE.match(name) or exp < int(time.time()):
        raise HTTPException(status_code=404, detail="not found")
    if not hmac.compare_digest(sig, _sig(name, exp)):
        raise HTTPException(status_code=404, detail="not found")
    path = (PAIRS_DIR / name).resolve()
    # Belt-and-suspenders: the resolved path must be a file directly under PAIRS_DIR.
    if path.parent != PAIRS_DIR.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(str(path), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=600"})
