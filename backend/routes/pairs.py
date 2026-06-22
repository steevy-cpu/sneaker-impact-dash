"""
Pairs API — one tied shoe pair detected within a table photo.

Pairs are created by the background pipeline (P3) from a table photo, then
optionally confirmed/overridden by a human reviewer. The dash's existing
review workflow now applies here, at the pair level.
"""
import json
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.config import IMAGES_DIR
from backend.database import get_db
from backend.models import PairReviewUpdate

router = APIRouter(prefix="/api/pairs", tags=["Pairs"])

VALID_REVIEW = {"NOT_REQUIRED", "PENDING", "COMPLETED"}


def _json_or_none(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def pair_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id":               row["id"],
        "table_photo_id":   row["table_photo_id"],
        "image_path":       row["image_path"],
        "bbox":             _json_or_none(row["bbox"]),
        "pair_score":       row["pair_score"],
        "prediction_source": row["prediction_source"],
        "detected_color":   row["detected_color"],
        "color_confidence": row["color_confidence"],
        "make":             row["make"],
        "make_confidence":  row["make_confidence"],
        "model":            row["model"],
        "model_confidence": row["model_confidence"],
        "model_sources":    _json_or_none(row["model_sources"]),
        "review_status":    row["review_status"],
        "final_make":       row["final_make"],
        "final_model":      row["final_model"],
        "notes":            row["notes"],
        "created_at":       row["created_at"],
    }


@router.get("", summary="List pairs")
def list_pairs(
    table_photo_id: Optional[str] = Query(None, description="Filter to one table photo"),
    review_status:  Optional[str] = Query(None, description="NOT_REQUIRED | PENDING | COMPLETED"),
    page:           int = Query(1, ge=1),
    page_size:      int = Query(50, ge=1, le=500),
    conn:           sqlite3.Connection = Depends(get_db),
):
    """Paginated list of pairs, optionally filtered by table photo / review status."""
    filters, params = [], []
    if table_photo_id is not None:
        filters.append("table_photo_id = ?"); params.append(table_photo_id)
    if review_status is not None:
        filters.append("review_status = ?");  params.append(review_status)

    where  = ("WHERE " + " AND ".join(filters)) if filters else ""
    total  = conn.execute(f"SELECT COUNT(*) FROM pairs {where}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT * FROM pairs {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()
    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "items":     [pair_to_dict(r) for r in rows],
    }


@router.get("/{pair_id}", summary="Get a single pair")
def get_pair(pair_id: str, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM pairs WHERE id = ?", (pair_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Pair '{pair_id}' not found")
    return pair_to_dict(row)


@router.patch("/{pair_id}/review", summary="Human confirm/override a pair")
def review_pair(
    pair_id: str,
    data:    PairReviewUpdate,
    conn:    sqlite3.Connection = Depends(get_db),
):
    """Record a human review of a pair: optional make/model overrides + a
    review status. `final_make`/`final_model` left null keep the AI values."""
    if data.review_status not in VALID_REVIEW:
        raise HTTPException(
            status_code=400,
            detail=f"review_status must be one of: {', '.join(sorted(VALID_REVIEW))}",
        )

    row = conn.execute("SELECT id FROM pairs WHERE id = ?", (pair_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Pair '{pair_id}' not found")

    conn.execute(
        """UPDATE pairs SET
               final_make    = ?,
               final_model   = ?,
               review_status = ?,
               notes         = ?
           WHERE id = ?""",
        (data.final_make, data.final_model, data.review_status, data.notes, pair_id),
    )
    conn.commit()
    return pair_to_dict(conn.execute("SELECT * FROM pairs WHERE id = ?", (pair_id,)).fetchone())


@router.delete("/{pair_id}", summary="Delete a single pair (+ its crop file)")
def delete_pair(pair_id: str, conn: sqlite3.Connection = Depends(get_db)):
    """Permanently remove one pair: its DB row and crop file on disk. Also
    decrements the parent table photo's `num_pairs` so the count stays honest,
    then recomputes the box's Airtable Brand Summary from the remaining pairs
    (a deleted YOLO false-positive must not keep inflating the brand counts)."""
    row = conn.execute(
        "SELECT image_path, table_photo_id FROM pairs WHERE id = ?", (pair_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Pair '{pair_id}' not found")

    conn.execute("DELETE FROM pairs WHERE id = ?", (pair_id,))
    conn.execute(
        "UPDATE table_photos SET num_pairs = MAX(0, num_pairs - 1) WHERE id = ?",
        (row["table_photo_id"],),
    )
    conn.commit()

    if row["image_path"]:
        try:
            (IMAGES_DIR / row["image_path"].replace("/images/", "", 1)).unlink()
        except OSError:
            pass

    # Deleting a pair changes the box's brand makeup, so the Airtable "Brand
    # Summary" must be recomputed from the REMAINING pairs and re-pushed. This is
    # best-effort and fully isolated: it can never make the delete fail, and the
    # outbox retry worker is the durable fallback if the live push doesn't land.
    try:
        _resync_brand_summary(conn, row["table_photo_id"])
    except Exception as exc:                            # noqa: BLE001
        print(f"[pairs] brand-summary re-sync after delete failed for "
              f"{row['table_photo_id']}: {exc}", flush=True)

    return {"deleted": True, "id": pair_id}


def _resync_brand_summary(conn: sqlite3.Connection, table_photo_id: str):
    """Recompute the box's brand summary from its remaining pairs and re-arm the
    Airtable outbox so the corrected value is pushed. No-op when this photo has
    no outbox row (no barcode/shipment was scanned -> nothing syncs).

    An empty summary (last branded pair removed) is intentionally NOT pushed:
    `_fields()` already omits an empty Brand Summary, so this matches the
    existing behaviour rather than clearing the Airtable field. The push is an
    idempotent upsert-by-barcode, so re-running it is always safe."""
    has_outbox = conn.execute(
        "SELECT 1 FROM airtable_outbox WHERE table_photo_id = ?", (table_photo_id,)
    ).fetchone()
    if not has_outbox:
        return
    pairs = [dict(r) for r in conn.execute(
        "SELECT make, final_make FROM pairs WHERE table_photo_id = ?",
        (table_photo_id,),
    ).fetchall()]
    from backend.services.airtable_sync import brand_summary_from_pairs
    from backend.services.airtable_outbox import set_brand_summary, try_one_async
    summary = brand_summary_from_pairs(pairs)
    if summary:
        set_brand_summary(conn, table_photo_id, summary)   # re-arms status=pending
        try_one_async(table_photo_id)
