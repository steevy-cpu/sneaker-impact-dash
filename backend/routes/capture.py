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
import sqlite3
from datetime import datetime
from io import BytesIO
from typing import Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile)

from backend.config import TABLE_PHOTOS_DIR
from backend.database import get_db
from backend.models import MetadataCreate
from backend.routes.pairs import pair_to_dict
from backend.utils.id_generator import generate_table_photo_id
from backend.utils.image_utils import get_table_photo_url

router = APIRouter(prefix="/api", tags=["Capture"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def table_photo_to_dict(row: sqlite3.Row) -> dict:
    info = row["shipment_info"]
    return {
        "id":                  row["id"],
        "batch_id":            row["batch_id"],
        "operator_id":         row["operator_id"],
        "image_path":          row["image_path"],
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


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

@router.post("/metadata", status_code=201, summary="Fast-track box metadata (no image yet)")
def create_metadata(data: MetadataCreate, conn: sqlite3.Connection = Depends(get_db)):
    """Create a `table_photos` record from box metadata + barcode ahead of (or
    without) the photo — mirrors ShoeSort's fast-track /api/metadata. A photo
    can be attached later via /api/capture. Shipment lookup is wired in P5."""
    if not _has_box_data(data.total_good_sneakers, data.total_end_of_life,
                         data.casuals, data.weight_of_box):
        raise HTTPException(
            status_code=422,
            detail="At least one box field (good / end-of-life / casuals / weight) must be > 0",
        )
    tp_id = generate_table_photo_id(conn)
    _insert_table_photo(
        conn, tp_id, operator_id=data.operator_id, batch_id=data.batch_id,
        image_path=None, barcode=data.barcode, weight=data.weight_of_box,
        good=data.total_good_sneakers, eol=data.total_end_of_life, casuals=data.casuals,
    )
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
    if not _has_box_data(total_good_sneakers, total_end_of_life, casuals, weight_of_box):
        raise HTTPException(
            status_code=422,
            detail="At least one box field (good / end-of-life / casuals / weight) must be > 0",
        )

    raw = await image.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty image upload")

    # Validate it is a real image before storing (Pillow). verify() consumes the
    # file object, so we re-open from the bytes to actually save it.
    from PIL import Image
    try:
        Image.open(BytesIO(raw)).verify()
    except Exception:
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid image")

    tp_id = generate_table_photo_id(conn)
    TABLE_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    dest = TABLE_PHOTOS_DIR / f"{tp_id}.jpg"
    try:
        Image.open(BytesIO(raw)).convert("RGB").save(dest, "JPEG", quality=90)
    except Exception as exc:                       # pragma: no cover - disk/codec
        raise HTTPException(status_code=500, detail=f"Could not store image: {exc}")

    _insert_table_photo(
        conn, tp_id, operator_id=operator_id, batch_id=batch_id,
        image_path=get_table_photo_url(tp_id), barcode=barcode, weight=weight_of_box,
        good=total_good_sneakers, eol=total_end_of_life, casuals=casuals,
    )
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
