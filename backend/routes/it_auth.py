"""
it_auth.py — password gate for the IT-only sections of the dash.

Operators only ever need Capture, Tableau and Config; everything else (table
photos, pairs review, labeling, sync, health, raw images, /docs) is for IT.
An IT person browses to /it, enters the shared password, and gets a signed
session cookie that unlocks the whole site in that browser.

Design notes:
  * DISABLED until IT_PASSWORD is set in .env — with it unset the site behaves
    exactly as before (same safe-deploy pattern as PUBLIC_CROP_SECRET).
  * The cookie is STATELESS: "<expiry_epoch>.<hmac>" signed with a key derived
    from IT_PASSWORD. Both uvicorn instances (:8000/:8443) share .env, so a
    login on one works on the other, and sessions survive service restarts.
    Changing the password invalidates every session at once.
  * No `Secure` flag on purpose: browsers ignore the port when matching
    cookies, so one login must cover the plain-HTTP :8000 instance too.
  * The enforcement itself lives in backend/main.py (it_gate middleware),
    which calls is_authed() below.
"""
import asyncio
import hashlib
import hmac
import time
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from backend.config import IT_PASSWORD, IT_SESSION_HOURS

router = APIRouter(tags=["IT Gate"])

COOKIE_NAME = "it_auth"


def _key() -> bytes:
    return hashlib.sha256(b"it-gate-v1:" + IT_PASSWORD.encode()).digest()


def _sig(exp: int) -> str:
    return hmac.new(_key(), str(exp).encode(), hashlib.sha256).hexdigest()


def is_authed(request: Request) -> bool:
    """True when the request carries a valid, unexpired session cookie.
    With the gate disabled everyone counts as authed (site fully open)."""
    if not IT_PASSWORD:
        return True
    token = request.cookies.get(COOKIE_NAME, "")
    exp_s, _, sig = token.partition(".")
    if not exp_s.isdigit() or int(exp_s) < time.time():
        return False
    return hmac.compare_digest(sig, _sig(int(exp_s)))


@router.get("/it", include_in_schema=False)
def it_entry(next: str = ""):
    """The URL IT people actually type. Hands off to the static login page."""
    # Only forward same-site paths so ?next can't become an open redirect.
    if next.startswith("/") and not next.startswith("//"):
        return RedirectResponse(f"/frontend/it.html?next={quote(next)}")
    return RedirectResponse("/frontend/it.html")


@router.post("/api/it/login")
async def it_login(payload: dict):
    if not IT_PASSWORD:
        return JSONResponse({"ok": False, "error": "IT gate is not enabled"},
                            status_code=400)
    password = str(payload.get("password", ""))
    if not hmac.compare_digest(password.encode(), IT_PASSWORD.encode()):
        await asyncio.sleep(0.8)  # blunt brute force; async, never blocks the loop
        return JSONResponse({"ok": False, "error": "Wrong password"},
                            status_code=401)
    exp = int(time.time()) + IT_SESSION_HOURS * 3600
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE_NAME, f"{exp}.{_sig(exp)}",
                    max_age=IT_SESSION_HOURS * 3600,
                    httponly=True, samesite="lax", path="/")
    return resp


@router.post("/api/it/logout")
def it_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
