"""
Pairs API — one tied shoe pair detected within a table photo.

Pairs are created by the background pipeline (P3) from a table photo, then
optionally confirmed/overridden by a human reviewer. The dash's existing
review workflow now applies here, at the pair level.
"""
import csv
import io
import json
import random
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from backend.config import AUDIT_WINDOW_DAYS, IMAGES_DIR
from backend.database import get_db
from backend.models import PairReviewUpdate
from backend.utils.brands import canonical_brand
from backend.utils.models import clean_model, model_key, normalize_labels
from backend.utils.sources import source_family

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
        "sample_mode":      row["sample_mode"] if "sample_mode" in row.keys() else None,
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


@router.get("/audit-sample", summary="Random unreviewed pairs for blind accuracy audit")
def audit_sample(
    limit:      int = Query(5, ge=1, le=20),
    exclude_tp: Optional[str] = Query(None, description="table_photo_id to skip (the deck being labeled)"),
    conn:       sqlite3.Connection = Depends(get_db),
):
    """Draw audit cards for Quick Label: pairs no human has touched yet, from
    the last AUDIT_WINDOW_DAYS, REGARDLESS of review_status — the auto-trusted
    NOT_REQUIRED tiers (seen-shoe cache, confident cloud) must be audited too,
    they flow into training and Airtable with zero human eyes otherwise.

    STRATIFIED: uniform-random within each prediction-source family, round-robin
    across families, so low-volume tiers reach a useful sample size as fast as
    the busy ones while every per-family confirm-rate stays unbiased. Pairs
    with no real brand claim are excluded (the audit measures assertion
    precision; the unknown-rate is tracked elsewhere).

    Always 200 with {items:[...]}; empty on odd input. Read-only single indexed
    window scan (idx_pairs_created) — cannot slow the live site."""
    since = (datetime.now() - timedelta(days=AUDIT_WINDOW_DAYS)).isoformat()
    q = ("SELECT id, prediction_source FROM pairs "
         "WHERE label_action IS NULL AND created_at >= ? "
         "AND LOWER(COALESCE(make,'')) NOT IN ('', 'unknown')")
    params = [since]
    if exclude_tp:
        q += " AND table_photo_id != ?"; params.append(exclude_tp)
    pool = conn.execute(q, params).fetchall()

    families = {}
    for r in pool:
        families.setdefault(source_family(r["prediction_source"]), []).append(r["id"])
    for ids in families.values():
        random.shuffle(ids)
    chosen, queues = [], list(families.values())
    while queues and len(chosen) < limit:            # round-robin the families
        for ids in list(queues):
            if ids:
                chosen.append(ids.pop())
                if len(chosen) >= limit:
                    break
        queues = [ids for ids in queues if ids]

    items = []
    for pid in chosen:
        row = conn.execute("SELECT * FROM pairs WHERE id = ?", (pid,)).fetchone()
        if row:
            items.append(pair_to_dict(row))
    return {"pool_size": len(pool), "items": items}


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
        # Unbiased accuracy meter: ONLY random-audit reviews count (value-queue
        # reviews oversample hard cases; legacy NULL rows are biased the same
        # way). Grouped by source FAMILY so the cache tier isn't fragmented by
        # the similarity embedded in its prediction_source string.
        audit_rows = conn.execute(
            "SELECT prediction_source, label_action FROM pairs "
            "WHERE sample_mode = 'random' AND label_action IS NOT NULL").fetchall()
        audit = {}
        for r in audit_rows:
            fam = audit.setdefault(source_family(r["prediction_source"]),
                                   {"n": 0, "confirmed": 0})
            fam["n"] += 1
            if r["label_action"] == "confirmed":
                fam["confirmed"] += 1
        for fam in audit.values():
            fam["rate"] = round(fam["confirmed"] / fam["n"], 3) if fam["n"] else None
        data = {
            "total":     row["total"] or 0,
            "corrected": row["corrected"] or 0,
            "confirmed": row["confirmed"] or 0,
            "by_make":   [{"make": m["final_make"], "count": m["n"]} for m in makes],
            "audit":     audit,
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


# Colors are already a controlled vocab in the DB; normalize defensively.
_COLOR_FIX = {"grey": "gray"}


def _norm_color(c):
    c = (c or "").strip().lower()
    if c in ("", "unknown"):
        return "unknown"
    return _COLOR_FIX.get(c, c)


def _model_canon_map(conn):
    """Dataset-wide canonical MODEL spelling map: model_key -> most common
    brand-stripped spelling. Lets gold labels ('GEL-KAYANO') export with the
    dataset's dominant spelling ('Gel-Kayano') for consistent training targets."""
    rows = conn.execute(
        "SELECT COALESCE(final_make, make), COALESCE(final_model, model), COUNT(*) "
        "FROM pairs WHERE COALESCE(final_model, model) NOT IN ('', 'unknown') "
        "AND COALESCE(final_model, model) IS NOT NULL GROUP BY 1, 2").fetchall()
    agg = {}                                   # key -> {spelling: count}
    for make, model, cnt in rows:
        cleaned = clean_model(model, make)
        key = model_key(cleaned)
        if not key or key == "unknown":
            continue
        agg.setdefault(key, {})
        agg[key][cleaned] = agg[key].get(cleaned, 0) + cnt
    return {k: max(sp, key=sp.get) for k, sp in agg.items()}


@router.get("/gold.csv", summary="Download the cleaned, training-ready gold set")
def gold_csv(conn: sqlite3.Connection = Depends(get_db)):
    """Streams every human-verified gold pair with NORMALIZED labels — canonical
    brand (ASICS/Asics merged), brand-stripped + canonically-spelled model
    ('Hoka One One Clifton' -> 'Clifton'), and controlled-vocab color — plus the
    source-photo group id so training can split leak-free (pairs from one table
    photo are near-duplicates). Deduped by crop image. Read-only + cheap
    (threadpool), so it never impacts the live site."""
    canon_model = _model_canon_map(conn)
    # ONLY human-verified fields (final_*) — never fall back to the ML prediction,
    # or the "gold" set would be polluted with unverified guesses. GOLD_WHERE
    # guarantees final_make; final_model/final_color may be unset (blank/unknown).
    rows = conn.execute(
        f"SELECT id, table_photo_id, image_path, final_make, "
        f"final_model, final_color, label_action, sample_mode "
        f"FROM pairs WHERE {GOLD_WHERE} ORDER BY table_photo_id, id").fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    # sample_mode lets the dataset builder route rows: 'random' (unbiased) ->
    # val/test, 'value'/blank (hard-case-biased) -> train.
    writer.writerow(["pair_id", "source_photo", "image_path", "brand", "model",
                     "color", "label_action", "sample_mode"])
    seen_images = set()
    n_rows = n_model = 0
    brands = set()
    for r in rows:
        img = r["image_path"] or ""
        if img and img in seen_images:          # dedup identical crops
            continue
        seen_images.add(img)
        make = r["final_make"]
        brand = canonical_brand(make)
        if not brand:                           # gold requires a real brand
            continue
        cleaned = clean_model(r["final_model"], make)   # '' if unset/unknown
        key = model_key(cleaned)
        model_out = canon_model.get(key, cleaned) if key and key != "unknown" else ""
        color = _norm_color(r["final_color"])
        writer.writerow([r["id"], r["table_photo_id"], img, brand, model_out, color,
                         r["label_action"] or "", r["sample_mode"] or ""])
        n_rows += 1
        brands.add(brand)
        if model_out:
            n_model += 1

    filename = f"gold_set_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Handy summary without opening the file / a second request.
            "X-Gold-Rows": str(n_rows),
            "X-Gold-Brands": str(len(brands)),
            "X-Gold-With-Model": str(n_model),
        },
    )


@router.get("/gold/readiness", summary="Gold-set training-readiness breakdown")
def gold_readiness(min_count: int = Query(15, ge=1, le=1000),
                   conn: sqlite3.Connection = Depends(get_db)):
    """Training-readiness view over the gold set: per-canonical-brand counts +
    ML accuracy (confirmed = ML brand was right, corrected = human overrode) +
    human model coverage, so you can see which brands have enough gold (>=
    min_count), which are the long tail, and where the classifier is weakest —
    i.e. where to aim labeling. Cheap (233-ish rows), read-only, threadpool."""
    rows = conn.execute(
        f"SELECT final_make, final_model, final_color, label_action "
        f"FROM pairs WHERE {GOLD_WHERE}").fetchall()
    agg = {}                                   # canonical brand -> counters
    tot = conf = corr = with_model = with_color = 0
    for r in rows:
        brand = canonical_brand(r["final_make"])
        if not brand:
            continue
        e = agg.setdefault(brand, {"brand": brand, "count": 0, "confirmed": 0,
                                   "corrected": 0, "with_model": 0})
        e["count"] += 1
        tot += 1
        if r["label_action"] == "confirmed":
            e["confirmed"] += 1; conf += 1
        elif r["label_action"] == "corrected":
            e["corrected"] += 1; corr += 1
        has_model = bool(clean_model(r["final_model"], r["final_make"]))
        if has_model:
            e["with_model"] += 1; with_model += 1
        if (r["final_color"] or "").strip().lower() not in ("", "unknown"):
            with_color += 1

    brands = sorted(agg.values(), key=lambda b: -b["count"])
    for b in brands:
        graded = b["confirmed"] + b["corrected"]
        b["ml_accuracy"] = round(100.0 * b["confirmed"] / graded, 1) if graded else None
        b["ready"] = b["count"] >= min_count
    ready = [b for b in brands if b["ready"]]

    def pctf(n, d):
        return round(100.0 * n / d, 1) if d else 0.0

    return {
        "total": tot,
        "distinct_brands": len(brands),
        "min_count": min_count,
        "ready_brands": len(ready),
        "thin_brands": len(brands) - len(ready),
        "ml_accuracy": pctf(conf, conf + corr),
        "confirmed": conf,
        "corrected": corr,
        "model_coverage": {"with_model": with_model, "pct": pctf(with_model, tot)},
        "color_coverage": {"with_color": with_color, "pct": pctf(with_color, tot)},
        "brands": brands,
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
    if data.sample_mode not in (None, "value", "random"):
        raise HTTPException(
            status_code=400, detail="sample_mode must be 'value', 'random', or omitted")

    row = conn.execute("SELECT id FROM pairs WHERE id = ?", (pair_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Pair '{pair_id}' not found")

    # Human labels get the same write-time cleanup as predictions ([[brand-model-
    # dedup]] Phase B): a typed "ultraboost" or "ASICS gel-kayano 28" stores as
    # the canonical spelling. None ('keep AI value') and '' pass through as-is.
    final_make, final_model = normalize_labels(data.final_make, data.final_model)

    # final_color/label_action/sample_mode come from the gold-labeling page; the
    # live Pairs Review page omits them (arrive as None) so its behaviour is
    # unchanged. sample_mode='random' rows feed the unbiased accuracy meter.
    conn.execute(
        """UPDATE pairs SET
               final_make    = ?,
               final_model   = ?,
               final_color   = ?,
               label_action  = ?,
               review_status = ?,
               notes         = ?,
               sample_mode   = ?
           WHERE id = ?""",
        (final_make, final_model, data.final_color, data.label_action,
         data.review_status, data.notes, data.sample_mode, pair_id),
    )
    conn.commit()
    _gold_stats["data"] = None          # gold set changed → drop the header cache
    return pair_to_dict(conn.execute("SELECT * FROM pairs WHERE id = ?", (pair_id,)).fetchone())


@router.delete("/{pair_id}", summary="Delete a single pair (+ its crop file)")
def delete_pair(pair_id: str, conn: sqlite3.Connection = Depends(get_db)):
    """Permanently remove one pair: its DB row and crop file on disk. Also
    decrements the parent table photo's `num_pairs` so the count stays honest,
    then recomputes the box's Airtable Brand Summary from the remaining pairs
    (a deleted YOLO false-positive must not keep inflating the brand counts)."""
    row = conn.execute(
        "SELECT image_path, table_photo_id, pair_score FROM pairs WHERE id = ?", (pair_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Pair '{pair_id}' not found")

    conn.execute("DELETE FROM pairs WHERE id = ?", (pair_id,))
    # num_pairs counts TRUE pairs only — decrement solely when a pair (not a
    # single-shoe record) is removed.
    if row["pair_score"] is not None:
        conn.execute(
            "UPDATE table_photos SET num_pairs = MAX(0, num_pairs - 1) WHERE id = ?",
            (row["table_photo_id"],),
        )
    conn.commit()
    _gold_stats["data"] = None          # may have removed a gold pair → drop cache

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
