"""
Partner-name resolver: status + manual recheck (IT-gated like every non-station
route). The resolver itself runs on its own in the background; these endpoints
only report on it and let IT force a re-check after fixing partners in Airtable.
"""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from backend.database import get_db
from backend.services import partner_resolver as pr

router = APIRouter(prefix="/api/partner-resolver", tags=["partner-resolver"])


@router.get("/status", summary="Partner resolver queue state")
def resolver_status(conn: sqlite3.Connection = Depends(get_db)):
    out = pr.status(conn)
    out["last_pass"] = pr.resolver.last or None     # only populated in the process that ran it
    return out


@router.post("/recheck", summary="Re-queue unresolved tables for a partner lookup now")
def resolver_recheck(scope: str = "blank", conn: sqlite3.Connection = Depends(get_db)):
    """scope: blank (row exists, Partner empty) | expired (older than 30 days) | all."""
    if scope not in ("blank", "expired", "all"):
        raise HTTPException(422, "scope must be blank, expired or all")
    return {"requeued": pr.recheck(conn, scope), "scope": scope}
