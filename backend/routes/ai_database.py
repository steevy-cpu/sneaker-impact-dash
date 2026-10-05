"""
AI Database — read-only search over every pair the AI has identified.

  GET /api/ai-database/search   ?q=&make=&page=&page_size=   paged results
  GET /api/ai-database/facets                                brand list + totals

Backs the AI Database page (frontend/ai_database.html). Deliberately its own
router (not a change to /api/pairs) so nothing existing moves. Read-only,
sync def -> threadpool, a LIKE scan over ~100k rows costs ~150 ms worst case.
Gated by the IT middleware like every /api/ path not on the public list.
"""
import sqlite3
import time
from typing import Optional

from fastapi import APIRouter, Depends, Query

from backend.database import get_db

router = APIRouter(prefix="/api/ai-database", tags=["AI Database"])

# Human-verified value wins over the AI's; "unknown" reads as empty.
_MAKE = "COALESCE(NULLIF(final_make, ''), NULLIF(make, ''))"
_MODEL = "COALESCE(NULLIF(final_model, ''), NULLIF(model, ''))"
_COLOR = "COALESCE(NULLIF(final_color, ''), NULLIF(detected_color, ''))"


def _row(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "table_photo_id": r["table_photo_id"],
        "image_path": r["image_path"],
        "make": r["disp_make"],
        "model": r["disp_model"],
        "color": r["disp_color"],
        "make_confidence": r["make_confidence"],
        "model_confidence": r["model_confidence"],
        "verified": bool(r["final_make"] or r["final_model"]),
        "review_status": r["review_status"],
        "source": r["prediction_source"],
        "created_at": r["created_at"],
    }


@router.get("/search", summary="Search identified pairs by brand / model / color")
def search_pairs(q: Optional[str] = Query(None, description="free text over brand, model, color"),
                 make: Optional[str] = Query(None, description="exact brand filter"),
                 page: int = Query(1, ge=1),
                 page_size: int = Query(48, ge=1, le=200),
                 conn: sqlite3.Connection = Depends(get_db)):
    where, args = [], []
    q = (q or "").strip()
    if q:
        for term in q.split():                     # every word must match somewhere
            like = f"%{term}%"
            where.append(f"({_MAKE} LIKE ? OR {_MODEL} LIKE ? OR {_COLOR} LIKE ? OR id LIKE ?)")
            args += [like, like, like, like]
    if make:
        where.append(f"{_MAKE} = ?")
        args.append(make)
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM pairs {sql_where}", args).fetchone()[0]
    rows = conn.execute(
        f"""SELECT id, table_photo_id, image_path, make_confidence, model_confidence,
                   final_make, final_model, review_status, prediction_source, created_at,
                   {_MAKE} AS disp_make, {_MODEL} AS disp_model, {_COLOR} AS disp_color
            FROM pairs {sql_where}
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?""",
        args + [page_size, (page - 1) * page_size]).fetchall()
    return {"items": [_row(r) for r in rows], "total": total, "page": page,
            "page_size": page_size, "pages": max(1, -(-total // page_size))}


_FACETS_TTL = 300        # s; three aggregates over ~100k rows cost ~1s, and the
_facets_cache = {"at": 0.0, "data": None}   # answer only changes as tables land


@router.get("/facets", summary="Brand list with counts + dataset totals")
def facets(conn: sqlite3.Connection = Depends(get_db)):
    if _facets_cache["data"] and time.time() - _facets_cache["at"] < _FACETS_TTL:
        return _facets_cache["data"]
    makes = conn.execute(
        f"""SELECT {_MAKE} AS m, COUNT(*) AS n FROM pairs
            WHERE {_MAKE} IS NOT NULL AND LOWER({_MAKE}) != 'unknown'
            GROUP BY m ORDER BY n DESC, m LIMIT 400""").fetchall()
    totals = conn.execute(
        f"""SELECT COUNT(*),
                   COUNT(DISTINCT CASE WHEN LOWER({_MAKE}) != 'unknown' THEN {_MAKE} END),
                   COUNT(DISTINCT CASE WHEN LOWER({_MODEL}) != 'unknown' THEN {_MODEL} END)
            FROM pairs""").fetchone()
    data = {"makes": [{"make": m, "count": n} for m, n in makes],
            "totals": {"pairs": totals[0], "makes": totals[1], "models": totals[2]}}
    _facets_cache.update(at=time.time(), data=data)
    return data
