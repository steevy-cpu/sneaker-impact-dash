"""
Reidentify API — backfill brand+model for unknown pairs and re-sync Airtable.

  POST /api/reidentify/all          re-identify every table photo with unknowns
  POST /api/reidentify/photo/{id}   re-identify one table photo's unknown pairs
  GET  /api/reidentify/status       progress of the current/last run

Each run re-asks the cloud model (forced to give a best guess) for every pair
whose make/model is still 'unknown', updates the pair + label_data, then pushes
the recomputed brand summary to Airtable. Background + single-flight.
"""
from fastapi import APIRouter

from backend.services import reidentify

router = APIRouter(prefix="/api/reidentify", tags=["Reidentify"])


@router.post("/all", status_code=202, summary="Re-identify all photos with unknown pairs")
def reidentify_all():
    return reidentify.start()


@router.post("/photo/{tp_id}", status_code=202, summary="Re-identify one table photo")
def reidentify_one(tp_id: str):
    return reidentify.start(tp_id=tp_id)


@router.get("/status", summary="Current/last reidentify run progress")
def reidentify_status():
    return reidentify.status()
