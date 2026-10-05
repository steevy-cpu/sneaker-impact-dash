"""
Label Data API — browse the curated, auto-approved training set.

`label_export.py` copies every confident (auto-approved) pair crop into
`LABEL_DATA_DIR` as `shoes_<color>_<make>_<N>.jpg` + a `.json` sidecar. This
route exposes that folder read-only so the dash can show the growing dataset:

  GET /api/label-data          list entries (filterable by make/color) + stats
  GET /api/label-data/stats    just the counts (total, by make, by color)

Images themselves are served by the `/label_images` static mount (see main.py).
Fail-safe: a missing/empty folder yields an empty list, never an error.
"""
import os
import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.config import LABEL_DATA_DIR
from backend.database import get_db
from backend.services import label_index

router = APIRouter(prefix="/api/label-data", tags=["Label Data"])

# A label_data image filename is a plain basename ending in .jpg — no path
# separators, no traversal. Matches the exporter's shoes_<color>_<make>_<N>.jpg
# but stays permissive on the stem so manually-added crops still delete.
_SAFE_NAME = re.compile(r"^[\w.\-]+\.jpg$")

_IMG_URL = "/label_images"


_META_SELECT = ("filename, make, model, detected_color, make_confidence, "
                "model_confidence, source_photo, source_pair, timestamp, exported_by")


def _row_to_entry(r):
    fn = r["filename"]
    return {
        "filename":         fn,
        "image_url":        f"{_IMG_URL}/{fn}",
        "make":             r["make"],
        "model":            r["model"],
        "detected_color":   r["detected_color"],
        "make_confidence":  r["make_confidence"],
        "model_confidence": r["model_confidence"],
        "source_photo":     r["source_photo"],
        "source_pair":      r["source_pair"],
        "timestamp":        r["timestamp"],
        "exported_by":      r["exported_by"],
    }


def _stats(conn):
    """Counts over the FULL set, straight from the index (two GROUP BYs)."""
    total = conn.execute("SELECT COUNT(*) FROM label_index").fetchone()[0]
    by_make = {(m or "unknown"): n for m, n in conn.execute(
        "SELECT make, COUNT(*) FROM label_index GROUP BY make ORDER BY COUNT(*) DESC")}
    by_color = {(c or "unknown"): n for c, n in conn.execute(
        "SELECT detected_color, COUNT(*) FROM label_index GROUP BY detected_color ORDER BY COUNT(*) DESC")}
    return {"total": total, "by_make": by_make, "by_color": by_color}


@router.get("", summary="List curated label_data entries")
def list_label_data(
    make:      str = Query(None, description="filter by make (case-insensitive)"),
    color:     str = Query(None, description="filter by detected_color"),
    page:      int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=500),
    conn: sqlite3.Connection = Depends(get_db),
):
    # Perf review 2026-10-05: served from the label_index table (see
    # backend/services/label_index.py) -- was a 132k-entry stat scan per
    # request plus a 66k-sidecar re-parse on any change (13 s page loads).
    label_index.refresh(conn)
    where, params = [], []
    if make:
        where.append("LOWER(make) = LOWER(?)"); params.append(make)
    if color:
        where.append("LOWER(detected_color) = LOWER(?)"); params.append(color)
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM label_index {sql_where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_META_SELECT} FROM label_index {sql_where} "
        f"ORDER BY timestamp DESC, filename DESC LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size]).fetchall()
    return {"items": [_row_to_entry(r) for r in rows], "total": total, "page": page,
            "page_size": page_size, "stats": _stats(conn)}


@router.get("/stats", summary="Label_data counts (total, by make, by color)")
def label_data_stats(conn: sqlite3.Connection = Depends(get_db)):
    label_index.refresh(conn)
    return _stats(conn)


@router.delete("/{filename}", summary="Delete one label_data crop (+ its JSON)")
def delete_label_data(filename: str):
    """Remove a crop and its sidecar from the curated set. Strictly validated:
    `filename` must be a bare .jpg basename and resolve INSIDE LABEL_DATA_DIR."""
    if os.path.basename(filename) != filename or not _SAFE_NAME.match(filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    folder = os.path.realpath(str(LABEL_DATA_DIR))
    target = os.path.realpath(os.path.join(folder, filename))
    # Defense in depth: the resolved path must stay within the folder.
    if os.path.dirname(target) != folder:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="Not found")

    removed = []
    for path in (target, target[:-4] + ".json"):       # crop + JSON sidecar
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed.append(os.path.basename(path))
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")
    try:
        from backend.database import get_connection
        c = get_connection()
        try:
            label_index.forget(c, filename)
        finally:
            c.close()
    except Exception:                                  # noqa: BLE001 - next refresh reconciles
        pass
    return {"deleted": True, "removed": removed}
