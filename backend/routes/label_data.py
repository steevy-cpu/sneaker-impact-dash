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
import json
import os
import re

from fastapi import APIRouter, HTTPException, Query

from backend.config import LABEL_DATA_DIR

router = APIRouter(prefix="/api/label-data", tags=["Label Data"])

# A label_data image filename is a plain basename ending in .jpg — no path
# separators, no traversal. Matches the exporter's shoes_<color>_<make>_<N>.jpg
# but stays permissive on the stem so manually-added crops still delete.
_SAFE_NAME = re.compile(r"^[\w.\-]+\.jpg$")

_IMG_URL = "/label_images"


def _load_entries():
    """Read every <name>.jpg in LABEL_DATA_DIR, attaching its .json sidecar
    metadata when present. Newest first (by timestamp, then filename)."""
    folder = str(LABEL_DATA_DIR)
    if not os.path.isdir(folder):
        return []
    entries = []
    for fn in os.listdir(folder):
        if not fn.lower().endswith(".jpg"):
            continue
        meta = {}
        side = os.path.join(folder, fn[:-4] + ".json")
        if os.path.isfile(side):
            try:
                with open(side) as fh:
                    meta = json.load(fh)
            except Exception:                            # noqa: BLE001 - fail safe
                meta = {}
        entries.append({
            "filename":         fn,
            "image_url":        f"{_IMG_URL}/{fn}",
            "make":             meta.get("make"),
            "model":            meta.get("model"),
            "detected_color":   meta.get("detected_color"),
            "make_confidence":  meta.get("make_confidence"),
            "model_confidence": meta.get("model_confidence"),
            "source_photo":     meta.get("source_photo"),
            "source_pair":      meta.get("source_pair"),
            "timestamp":        meta.get("timestamp"),
            "exported_by":      meta.get("exported_by"),
        })
    entries.sort(key=lambda e: (e.get("timestamp") or "", e["filename"]), reverse=True)
    return entries


def _stats(entries):
    by_make, by_color = {}, {}
    for e in entries:
        mk = (e.get("make") or "unknown")
        col = (e.get("detected_color") or "unknown")
        by_make[mk] = by_make.get(mk, 0) + 1
        by_color[col] = by_color.get(col, 0) + 1
    return {
        "total":    len(entries),
        "by_make":  dict(sorted(by_make.items(), key=lambda kv: kv[1], reverse=True)),
        "by_color": dict(sorted(by_color.items(), key=lambda kv: kv[1], reverse=True)),
    }


@router.get("", summary="List curated label_data entries")
def list_label_data(
    make:      str = Query(None, description="filter by make (case-insensitive)"),
    color:     str = Query(None, description="filter by detected_color"),
    page:      int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=500),
):
    entries = _load_entries()
    stats = _stats(entries)                    # stats over the FULL set
    if make:
        entries = [e for e in entries if (e.get("make") or "").lower() == make.lower()]
    if color:
        entries = [e for e in entries if (e.get("detected_color") or "").lower() == color.lower()]
    total = len(entries)
    start = (page - 1) * page_size
    items = entries[start:start + page_size]
    return {"items": items, "total": total, "page": page,
            "page_size": page_size, "stats": stats}


@router.get("/stats", summary="Label_data counts (total, by make, by color)")
def label_data_stats():
    return _stats(_load_entries())


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
    return {"deleted": True, "removed": removed}
