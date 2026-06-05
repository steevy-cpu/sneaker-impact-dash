"""
Airtable Outbox API — see and flush the durable "send-when-available" queue.

  GET  /api/airtable-outbox            list rows (filter by status) + counts
  POST /api/airtable-outbox/flush      retry all pending rows now

The outbox saves every capture's box data + brand summary and keeps retrying
until its shipment exists in Airtable and the update lands (see airtable_outbox).
"""
from fastapi import APIRouter, Query

from backend.services import airtable_outbox as outbox
from backend.services.airtable_sync import sync_enabled

router = APIRouter(prefix="/api/airtable-outbox", tags=["Airtable Outbox"])


@router.get("", summary="List outbox rows + counts")
def list_outbox(status: str = Query(None, description="pending | synced")):
    return {
        "sync_enabled": sync_enabled(),
        "counts": outbox.counts(),
        "items": outbox.list_items(status=status),
    }


@router.post("/flush", summary="Retry all pending rows now")
def flush_outbox():
    if not sync_enabled():
        return {"flushed": False, "note": "Airtable writes are disabled (AIRTABLE_SYNC_ENABLED=0)"}
    return {"flushed": True, **outbox.flush()}
