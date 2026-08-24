"""
Airtable Outbox API — see and flush the durable "send-when-available" queue.

  GET  /api/airtable-outbox            list rows (filter by status) + counts
  POST /api/airtable-outbox/flush      retry all pending rows now

The outbox saves every capture's box data + brand summary and keeps retrying
until its shipment exists in Airtable and the update lands (see airtable_outbox).
"""
from fastapi import APIRouter, HTTPException, Query

from backend.database import get_connection
from backend.services import airtable_outbox as outbox
from backend.services import fedex_track
from backend.services.airtable_sync import sync_enabled

router = APIRouter(prefix="/api/airtable-outbox", tags=["Airtable Outbox"])


@router.get("", summary="List outbox rows + counts")
def list_outbox(status: str = Query(None, description="pending | synced")):
    return {
        "sync_enabled": sync_enabled(),
        "fedex_enabled": fedex_track.fedex_enabled(),
        "counts": outbox.counts(),
        "items": outbox.list_items(status=status),
    }


@router.get("/{tp_id}/fedex", summary="Look up a queued box's FedEx tracking")
def fedex_lookup(tp_id: str):
    """Resolve the scanned tracking number for one outbox row via FedEx Track —
    a diagnostic for 'no_row' boxes (is it a real shipment not yet in Airtable,
    or a bad scan?). Returns FedEx shipment details or {found: false, error}."""
    if not fedex_track.fedex_enabled():
        raise HTTPException(status_code=400,
                            detail="FedEx lookup disabled (set FEDEX_API_KEY + FEDEX_SECRET)")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT match_barcode, full_barcode FROM airtable_outbox WHERE table_photo_id = ?",
            (tp_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail=f"No outbox row for '{tp_id}'")
    # match_barcode is the normalized last-12 = the FedEx tracking number.
    tracking = row["match_barcode"] or row["full_barcode"]
    return fedex_track.track(tracking)


@router.delete("/{tp_id}", summary="Delete one pending row (give up on it)")
def delete_outbox_row(tp_id: str):
    """Remove a pending row the operator has given up on (typically a bad
    barcode scan that will never match a shipment). Synced rows are history and
    can't be deleted. The table photo and its pairs are untouched. Rows still
    pending after OUTBOX_PURGE_DAYS are purged automatically by the retry
    worker — this is the manual version of the same give-up."""
    if not outbox.delete_one(tp_id):
        raise HTTPException(status_code=404,
                            detail=f"No PENDING outbox row for '{tp_id}' (synced rows can't be deleted)")
    return {"deleted": tp_id}


@router.post("/flush", summary="Retry all pending rows now")
def flush_outbox():
    if not sync_enabled():
        return {"flushed": False, "note": "Airtable writes are disabled (AIRTABLE_SYNC_ENABLED=0)"}
    return {"flushed": True, **outbox.flush()}
