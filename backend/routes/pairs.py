"""
Pairs API — one tied shoe pair detected within a table photo.

Pairs are created by the background pipeline (P3) from a table photo, then
optionally confirmed/overridden by a human reviewer. The dash's existing
review workflow now applies here, at the pair level.
"""
import json
import sqlite3
import threading
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.config import IMAGES_DIR
from backend.database import get_db
from backend.models import PairReviewUpdate

router = APIRouter(prefix="/api/pairs", tags=["Pairs"])

VALID_REVIEW = {"NOT_REQUIRED", "PENDING", "COMPLETED"}

# A "gold" pair = a human confirmed it with a real brand (Quick Label / Pairs
# Review). Indexed on review_status, so this predicate is cheap.
GOLD_WHERE = ("review_status = 'COMPLETED' AND final_make IS NOT NULL "
              "AND final_make NOT IN ('', 'unknown')")
_gold_stats = {"ts": 0.0, "data": None}      # 15s cache so the header is free
_gold_lock = threading.Lock()


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
        "final_color":      row["final_color"] if "final_color" in row.keys() else None,
        "label_action":     row["label_action"] if "label_action" in row.keys() else None,
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


@router.get("/gold-queue", summary="Pending pairs ordered for gold-label value")
def gold_queue(
    limit: int = Query(20, ge=1, le=100),
    conn:  sqlite3.Connection = Depends(get_db),
):
    """Serve the next batch of PENDING pairs for the fast gold-labeling page,
    ordered for VALUE not FIFO:
      * mid-confidence first — a prediction near ~0.6 make_confidence is where
        the AI is most likely wrong, so a human's confirm/correct carries the
        most signal (verifying a 0.99 Nike teaches little).
      * brand diversity — we pull a value-ranked candidate pool then round-robin
        by AI make so consecutive cards aren't all the same brand, building a
        representative gold set instead of 200 Brooks in a row.

    Read-only (plain SELECT, sync def -> threadpool); cannot lock or slow the
    live site. Each labeled pair flips to COMPLETED so it won't reappear."""
    total = conn.execute(
        "SELECT COUNT(*) FROM pairs WHERE review_status = 'PENDING'").fetchone()[0]
    # Value-ranked candidate pool (cheap: a few hundred rows, indexed scan).
    pool = conn.execute(
        """SELECT * FROM pairs
           WHERE review_status = 'PENDING'
           ORDER BY ABS(COALESCE(make_confidence, 0.5) - 0.6) ASC, id ASC
           LIMIT 400""").fetchall()
    # Round-robin by AI make for diversity, preserving value order within a brand.
    from collections import OrderedDict
    buckets = OrderedDict()
    for r in pool:
        buckets.setdefault((r["make"] or "").lower(), []).append(r)
    ordered, queues = [], list(buckets.values())
    while queues and len(ordered) < limit:
        for q in list(queues):
            if q:
                ordered.append(q.pop(0))
                if len(ordered) >= limit:
                    break
        queues = [q for q in queues if q]
    return {"total_pending": total, "items": [pair_to_dict(r) for r in ordered]}


def _gold_stats_cached(conn):
    """Header stats over the gold set (total, confirmed/corrected split, brand
    counts). Cached 15s so the Gold Labels page header is effectively free even
    if several people watch it. Cheap anyway (indexed on review_status)."""
    now = time.time()
    if _gold_stats["data"] is not None and (now - _gold_stats["ts"]) < 15:
        return _gold_stats["data"]
    with _gold_lock:
        if _gold_stats["data"] is not None and (time.time() - _gold_stats["ts"]) < 15:
            return _gold_stats["data"]
        row = conn.execute(
            f"""SELECT COUNT(*) AS total,
                   SUM(CASE WHEN label_action='corrected' THEN 1 ELSE 0 END) AS corrected,
                   SUM(CASE WHEN label_action='confirmed' THEN 1 ELSE 0 END) AS confirmed
                FROM pairs WHERE {GOLD_WHERE}""").fetchone()
        makes = conn.execute(
            f"SELECT final_make, COUNT(*) AS n FROM pairs WHERE {GOLD_WHERE} "
            "GROUP BY final_make ORDER BY n DESC").fetchall()
        data = {
            "total":     row["total"] or 0,
            "corrected": row["corrected"] or 0,
            "confirmed": row["confirmed"] or 0,
            "by_make":   [{"make": m["final_make"], "count": m["n"]} for m in makes],
        }
        _gold_stats["data"], _gold_stats["ts"] = data, time.time()
        return data


@router.get("/gold", summary="Browse the human-verified gold labels")
def gold_labels(
    page:      int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=200),
    make:      Optional[str] = Query(None, description="filter to one confirmed brand"),
    conn:      sqlite3.Connection = Depends(get_db),
):
    """Paginated view of human-confirmed pairs (the gold training set) — newest
    first. Returns light header stats + one page of items. Indexed + paginated,
    so it stays fast as the set grows; the page replaces its grid each turn so
    the browser never holds more than page_size images."""
    where, params = GOLD_WHERE, []
    if make:
        where += " AND final_make = ?"; params.append(make)
    total = conn.execute(f"SELECT COUNT(*) FROM pairs WHERE {where}", params).fetchone()[0]
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT * FROM pairs WHERE {where} ORDER BY created_at DESC, id DESC "
        "LIMIT ? OFFSET ?", params + [page_size, offset]).fetchall()
    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "stats":     _gold_stats_cached(conn),
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

    # final_color/label_action come from the gold-labeling page; the live Pairs
    # Review page omits them (they arrive as None) so its behaviour is unchanged.
    conn.execute(
        """UPDATE pairs SET
               final_make    = ?,
               final_model   = ?,
               final_color   = ?,
               label_action  = ?,
               review_status = ?,
               notes         = ?
           WHERE id = ?""",
        (data.final_make, data.final_model, data.final_color, data.label_action,
         data.review_status, data.notes, pair_id),
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
