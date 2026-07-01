"""
Capture API — the new whole-table-photo flow.

The operator photographs the whole table of tied shoe pairs and submits it with
box metadata (weight, good/end-of-life/casual counts) and a barcode. We store a
`table_photos` row in `pending` status; the background worker (P3) later
segments it into `pairs` and fills color/brand/model. Endpoints:

  POST /api/metadata            fast-track box metadata (no image yet)
  POST /api/capture             table photo + metadata (multipart) -> pending job
  GET  /api/table-photos        list
  GET  /api/table-photos/{id}   one + its pairs
  GET  /api/table-photos/{id}/status   lightweight status (for polling)
"""
import json
import math
import sqlite3
import threading
from datetime import datetime
from io import BytesIO
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile)
from fastapi.concurrency import run_in_threadpool

from backend.config import IMAGES_DIR, TABLE_PHOTOS_DIR
from backend.database import get_db
from backend.models import MetadataCreate
from backend.routes.pairs import pair_to_dict
from backend.utils.id_generator import generate_table_photo_id
from backend.utils.image_utils import (get_table_photo_thumb_url,
                                       get_table_photo_url,
                                       make_table_photo_thumb)

router = APIRouter(prefix="/api", tags=["Capture"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _thumb_path_or_none(tp_id: str, image_path):
    """Thumb URL when the file exists (older photos may not have one yet)."""
    if image_path and (TABLE_PHOTOS_DIR / "thumbs" / f"{tp_id}.jpg").exists():
        return get_table_photo_thumb_url(tp_id)
    return None


def table_photo_to_dict(row: sqlite3.Row) -> dict:
    info = row["shipment_info"]
    return {
        "id":                  row["id"],
        "batch_id":            row["batch_id"],
        "operator_id":         row["operator_id"],
        "image_path":          row["image_path"],
        "thumb_path":          _thumb_path_or_none(row["id"], row["image_path"]),
        "barcode":             row["barcode"],
        "weight_of_box":       row["weight_of_box"],
        "total_good_sneakers": row["total_good_sneakers"],
        "total_end_of_life":   row["total_end_of_life"],
        "casuals":             row["casuals"],
        "status":              row["status"],
        "error_message":       row["error_message"],
        "num_pairs":           row["num_pairs"],
        "shipment_info":       json.loads(info) if info else None,
        "created_at":          row["created_at"],
        "processed_at":        row["processed_at"],
    }


# A donation box is realistically a few-to-tens of lbs; cap generously at 150.
# The station scale occasionally emits a garbage burst (e.g. 9.6e33) that, left
# unguarded, poisons the box weight AND Airtable's "Weight (lbs)" field — which
# in turn broke the "YTD total" email automation (Airtable summed a 34-digit
# number). Drop any non-finite / non-positive / out-of-range reading to None so
# garbage never reaches storage or Airtable.
MAX_BOX_WEIGHT_LBS = 150.0


def _clean_weight(weight):
    """Return a sane box weight (lbs) or None for missing/garbage input."""
    if weight is None:
        return None
    try:
        w = float(weight)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(w) or w <= 0 or w > MAX_BOX_WEIGHT_LBS:
        return None
    return w


def _clean_barcode(barcode):
    """Collapse a FedEx GS1 routing-barcode scan down to its 12-digit tracking
    number before storing.

    A FedEx label carries both the 12-digit tracking number AND a long all-digit
    "96" routing/SSCC barcode; when the scanner grabs the routing one you get a
    22-34 digit string like 9622013700001205839600<tracking>. The last 12 digits
    ARE the tracking number — which is exactly what the shipment match already
    trims to (normalize_barcode, SHIPMENT_BARCODE_TRIM=12), so collapsing here
    changes NOTHING about what matches/syncs; it only keeps the stored barcode
    clean and stops the long form from ever being reinterpreted as a giant number
    downstream (a CSV re-import of 9.6e33 is how the old rows got poisoned).

    Also catches leading-junk and double-scan variants (e.g. "R9622…<tracking>",
    "<tracking>9622…<tracking>") by keying off the DIGIT count, not a "96" prefix:
    a routing/SSCC or double scan yields >=22 digits, and the real tracking is the
    trailing 12. Normal 12-15 digit tracking numbers, UPS "1Z…" codes (letters →
    few digits), and short/test scans pass through untouched. The trailing-12
    result is identical to what normalize_barcode already matches on, so this only
    cleans the STORED value — matching/sync are unchanged."""
    bc = (barcode or "").strip()
    digits = "".join(ch for ch in bc if ch.isdigit())
    if len(digits) >= 22:
        return digits[-12:]
    return bc


def _has_box_data(good, eol, casuals, weight) -> bool:
    """Mirror the desktop rule: at least one box field must be > 0."""
    return any([(good or 0) > 0, (eol or 0) > 0, (casuals or 0) > 0, (weight or 0) > 0])


def _insert_table_photo(conn, tp_id, *, operator_id, batch_id, image_path,
                        barcode, weight, good, eol, casuals):
    conn.execute(
        """INSERT INTO table_photos (
            id, batch_id, operator_id, image_path, barcode,
            weight_of_box, total_good_sneakers, total_end_of_life, casuals,
            status, num_pairs, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
        (tp_id, batch_id, operator_id, image_path, barcode,
         weight, good or 0, eol or 0, casuals or 0, datetime.now().isoformat()),
    )
    conn.commit()


def _attach_shipment(conn, tp_id, barcode):
    """Best-effort: resolve the barcode and store shipment_info on the row.
    Fail-safe — never blocks capture (returns silently if unconfigured)."""
    if not barcode:
        return
    try:
        from backend.services.shipment_lookup import get_shipment_lookup
        info = get_shipment_lookup().resolve(barcode)
        if info and info.get("found"):
            conn.execute("UPDATE table_photos SET shipment_info = ? WHERE id = ?",
                         (json.dumps(info), tp_id))
            conn.commit()
    except Exception as exc:                           # noqa: BLE001 - never block capture
        print(f"[capture] shipment attach failed: {exc}")


def _enqueue_outbox(conn, tp_id, barcode, box):
    """Stage 1: save box data to the durable outbox. It's sent to Airtable
    immediately if the shipment row exists, otherwise kept and retried until it
    does (so nothing is lost when a shipment is imported later). Fail-safe —
    never blocks or fails the capture."""
    try:
        from backend.services.airtable_outbox import enqueue, try_one_async
        if enqueue(conn, tp_id, barcode, box):
            try_one_async(tp_id)
    except Exception as exc:                            # noqa: BLE001 - never block capture
        print(f"[capture] outbox enqueue error: {exc}")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

@router.post("/metadata", status_code=201, summary="Fast-track box metadata (no image yet)")
def create_metadata(data: MetadataCreate, conn: sqlite3.Connection = Depends(get_db)):
    """Create a `table_photos` record from box metadata + barcode ahead of (or
    without) the photo — mirrors ShoeSort's fast-track /api/metadata. A photo
    can be attached later via /api/capture. Shipment lookup is wired in P5."""
    weight = _clean_weight(data.weight_of_box)
    barcode = _clean_barcode(data.barcode)
    if not _has_box_data(data.total_good_sneakers, data.total_end_of_life,
                         data.casuals, weight):
        raise HTTPException(
            status_code=422,
            detail="At least one box field (good / end-of-life / casuals / weight) must be > 0",
        )
    tp_id = generate_table_photo_id(conn)
    _insert_table_photo(
        conn, tp_id, operator_id=data.operator_id, batch_id=data.batch_id,
        image_path=None, barcode=barcode, weight=weight,
        good=data.total_good_sneakers, eol=data.total_end_of_life, casuals=data.casuals,
    )
    _attach_shipment(conn, tp_id, barcode)
    _enqueue_outbox(conn, tp_id, barcode, {"weight": weight,
                                                "good": data.total_good_sneakers,
                                                "eol": data.total_end_of_life,
                                                "casuals": data.casuals})
    row = conn.execute("SELECT * FROM table_photos WHERE id = ?", (tp_id,)).fetchone()
    return table_photo_to_dict(row)


@router.post("/capture", status_code=201, summary="Upload a table photo + box metadata")
async def capture(
    image:               UploadFile = File(..., description="Whole-table photo"),
    barcode:             Optional[str]   = Form(None),
    weight_of_box:       Optional[float] = Form(None),
    total_good_sneakers: int             = Form(0),
    total_end_of_life:   int             = Form(0),
    casuals:             int             = Form(0),
    operator_id:         Optional[str]   = Form(None),
    batch_id:            Optional[str]   = Form(None),
    conn:                sqlite3.Connection = Depends(get_db),
):
    """Store one whole-table photo + box metadata as a `pending` table_photos
    row, ready for background processing (the worker arrives in P3). Validation:
    a readable image AND at least one box field > 0."""
    weight_of_box = _clean_weight(weight_of_box)
    barcode = _clean_barcode(barcode)
    if not _has_box_data(total_good_sneakers, total_end_of_life, casuals, weight_of_box):
        raise HTTPException(
            status_code=422,
            detail="At least one box field (good / end-of-life / casuals / weight) must be > 0",
        )

    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty image upload")

    # Validate it is a real image before storing (Pillow). verify() consumes the
    # file object, so we re-open from the bytes to actually save it. Pillow is
    # CPU-bound, so it runs in the threadpool: this is an async handler on a
    # single-worker uvicorn, and decoding a multi-MB photo on the event loop
    # stalls every other request for the duration.
    from PIL import Image
    try:
        await run_in_threadpool(lambda: Image.open(BytesIO(raw)).verify())
    except Exception:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid image")

    tp_id = generate_table_photo_id(conn)
    TABLE_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    dest = TABLE_PHOTOS_DIR / f"{tp_id}.jpg"
    try:
        await run_in_threadpool(
            lambda: Image.open(BytesIO(raw)).convert("RGB").save(
                dest, "JPEG", quality=90))
    except Exception as exc:                       # pragma: no cover - disk/codec
        raise HTTPException(status_code=500, detail=f"Could not store image: {exc}")

    # Thumbnail for the review modal (best-effort: the full photo is the
    # source of truth, a missing thumb just falls back to it in the UI).
    try:
        await run_in_threadpool(
            make_table_photo_thumb, dest, TABLE_PHOTOS_DIR / "thumbs" / f"{tp_id}.jpg")
    except Exception as exc:                       # noqa: BLE001
        print(f"[capture] thumb generation failed for {tp_id}: {exc}", flush=True)

    _insert_table_photo(
        conn, tp_id, operator_id=operator_id, batch_id=batch_id,
        image_path=get_table_photo_url(tp_id), barcode=barcode, weight=weight_of_box,
        good=total_good_sneakers, eol=total_end_of_life, casuals=casuals,
    )
    # The shipment lookup is a blocking Airtable call (up to 8s) — threadpool
    # it for the same reason as the Pillow work above.
    await run_in_threadpool(_attach_shipment, conn, tp_id, barcode)
    _enqueue_outbox(conn, tp_id, barcode, {"weight": weight_of_box, "good": total_good_sneakers,
                                           "eol": total_end_of_life, "casuals": casuals})
    row = conn.execute("SELECT * FROM table_photos WHERE id = ?", (tp_id,)).fetchone()
    return table_photo_to_dict(row)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

@router.get("/table-photos", summary="List table photos")
def list_table_photos(
    page:      int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    status:    Optional[str] = Query(None, description="pending|processing|completed|failed"),
    batch_id:  Optional[str] = Query(None),
    conn:      sqlite3.Connection = Depends(get_db),
):
    filters, params = [], []
    if status is not None:
        filters.append("status = ?");   params.append(status)
    if batch_id is not None:
        filters.append("batch_id = ?"); params.append(batch_id)

    where  = ("WHERE " + " AND ".join(filters)) if filters else ""
    total  = conn.execute(f"SELECT COUNT(*) FROM table_photos {where}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT * FROM table_photos {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "items":     [table_photo_to_dict(r) for r in rows],
    }


@router.get("/table-photos/{tp_id}", summary="Get one table photo + its pairs")
def get_table_photo(tp_id: str, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM table_photos WHERE id = ?", (tp_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Table photo '{tp_id}' not found")
    pairs = conn.execute(
        "SELECT * FROM pairs WHERE table_photo_id = ? ORDER BY id", (tp_id,)
    ).fetchall()
    result = table_photo_to_dict(row)
    result["pairs"] = [pair_to_dict(p) for p in pairs]
    return result


@router.get("/table-photos/{tp_id}/status", summary="Lightweight status (for polling)")
def get_table_photo_status(tp_id: str, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute(
        "SELECT id, status, num_pairs, error_message, processed_at FROM table_photos WHERE id = ?",
        (tp_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Table photo '{tp_id}' not found")
    return {
        "id":            row["id"],
        "status":        row["status"],
        "num_pairs":     row["num_pairs"],
        "error_message": row["error_message"],
        "processed_at":  row["processed_at"],
    }


@router.post("/table-photos/{tp_id}/reprocess", status_code=202,
             summary="Re-queue a table photo for background processing")
def reprocess_table_photo(tp_id: str, conn: sqlite3.Connection = Depends(get_db)):
    """Reset a completed/failed table photo back to `pending` so the worker
    re-runs it. Deletes its existing pairs (+ crop files) first to avoid dupes."""
    row = conn.execute(
        "SELECT id, image_path FROM table_photos WHERE id = ?", (tp_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Table photo '{tp_id}' not found")
    if not row["image_path"]:
        raise HTTPException(status_code=400, detail="No image to process for this record")

    old = conn.execute(
        "SELECT image_path FROM pairs WHERE table_photo_id = ?", (tp_id,)
    ).fetchall()
    conn.execute("DELETE FROM pairs WHERE table_photo_id = ?", (tp_id,))
    conn.execute(
        "UPDATE table_photos SET status = 'pending', error_message = NULL, "
        "num_pairs = 0, processed_at = NULL WHERE id = ?",
        (tp_id,),
    )
    conn.commit()

    # Best-effort cleanup of old crop files.
    for p in old:
        if p["image_path"]:
            try:
                (IMAGES_DIR / p["image_path"].replace("/images/", "", 1)).unlink()
            except OSError:
                pass
    return {"id": tp_id, "status": "pending"}


@router.delete("/table-photos/{tp_id}",
               summary="Delete a table photo, its pairs, crops, and outbox row")
def delete_table_photo(tp_id: str, conn: sqlite3.Connection = Depends(get_db)):
    """Permanently remove a table photo and everything tied to it: every child
    pair row (+ crop files), the durable Airtable outbox row (so an orphaned
    sync can't keep retrying), the table-photo DB row, and the photo file on
    disk. No FK has ON DELETE CASCADE, so cleanup is explicit and ordered:
    children first, then the outbox, then the parent."""
    row = conn.execute(
        "SELECT id, image_path FROM table_photos WHERE id = ?", (tp_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Table photo '{tp_id}' not found")

    pair_rows = conn.execute(
        "SELECT image_path FROM pairs WHERE table_photo_id = ?", (tp_id,)
    ).fetchall()
    conn.execute("DELETE FROM pairs WHERE table_photo_id = ?", (tp_id,))
    conn.execute("DELETE FROM airtable_outbox WHERE table_photo_id = ?", (tp_id,))
    conn.execute("DELETE FROM table_photos WHERE id = ?", (tp_id,))
    conn.commit()

    # Best-effort cleanup of the table photo (+ its thumb) + all pair crop files.
    paths = [row["image_path"]] + [p["image_path"] for p in pair_rows]
    if row["image_path"]:
        paths.append(get_table_photo_thumb_url(tp_id))
    for path in paths:
        if path:
            try:
                (IMAGES_DIR / path.replace("/images/", "", 1)).unlink()
            except OSError:
                pass
    return {"deleted": True, "id": tp_id, "pairs_removed": len(pair_rows)}
