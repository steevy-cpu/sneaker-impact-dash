"""
tableau.py — one cheap, cached aggregate feed for the Tableau visualization page.

Design for the "must not slow/freeze the site" rule:
  * ONE endpoint returns EVERY chart's data in a single response — the page makes
    one request, not one-per-chart.
  * Pure SQL GROUP BY / conditional aggregation over small tables (a few thousand
    rows) — sub-millisecond; no per-row Python, no disk/image access, no joins
    that could lock.
  * Sync `def` handler, so FastAPI runs it in the threadpool (never blocks the
    uvicorn event loop), and a 60s in-memory TTL cache means repeated views /
    multiple watchers don't re-query at all.

  GET /api/tableau/stats  ->  { kpis, status, brands, models, colors,
                                ai{...}, pairing{...}, airtable{...},
                                box{...}, timeline{...}, generated_at }
"""
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends

from backend.database import get_db

router = APIRouter(prefix="/api/tableau", tags=["Tableau"])

_TTL = 60.0                       # seconds; the dataset changes slowly
_CACHE = {"ts": 0.0, "data": None}
_LOCK = threading.Lock()


def _known(v):
    return v not in (None, "", "unknown", "Unknown", "UNKNOWN")


def _merge_ci(rows):
    """Merge (label, count) rows case-insensitively, keeping the most common
    original spelling as the display label (fixes ASICS vs Asics). Sorted desc."""
    agg = {}                       # key -> [count, {spelling: count}]
    for label, n in rows:
        if not _known(label):
            continue
        key = label.strip().lower()
        e = agg.setdefault(key, [0, {}])
        e[0] += n
        e[1][label] = e[1].get(label, 0) + n
    out = [(max(sp, key=sp.get), tot) for tot, sp in agg.values()]
    out.sort(key=lambda z: z[1], reverse=True)
    return out


def _bucket_conf(conn, col, where="1=1"):
    """Confidence histogram for a pairs column over fixed bands."""
    bands = [("0–50%", 0.0, 0.5), ("50–70%", 0.5, 0.7), ("70–80%", 0.7, 0.8),
             ("80–90%", 0.8, 0.9), ("90–100%", 0.9, 1.0001)]
    labels = [b[0] for b in bands]
    counts = []
    for _, lo, hi in bands:
        n = conn.execute(
            f"SELECT COUNT(*) FROM pairs WHERE {col} IS NOT NULL "
            f"AND {col} >= ? AND {col} < ? AND {where}", (lo, hi)).fetchone()[0]
        counts.append(n)
    return {"labels": labels, "counts": counts}


def _compute(conn: sqlite3.Connection) -> dict:
    cur = conn.execute

    # ---- dataset / status -------------------------------------------------
    status = dict(cur("SELECT status, COUNT(*) FROM table_photos "
                      "GROUP BY status").fetchall())
    total_photos = sum(status.values())
    total_pairs = cur("SELECT COUNT(*) FROM pairs").fetchone()[0]
    singles = cur("SELECT COUNT(*) FROM pairs WHERE pair_score IS NULL").fetchone()[0]
    true_pairs = total_pairs - singles
    # one record can be a tied pair (2 shoes) or a single (1 shoe)
    total_shoes = true_pairs * 2 + singles

    known_make = cur("SELECT COUNT(*) FROM pairs WHERE "
                     "COALESCE(final_make, make) NOT IN ('unknown','') "
                     "AND COALESCE(final_make, make) IS NOT NULL").fetchone()[0]
    known_model = cur("SELECT COUNT(*) FROM pairs WHERE "
                      "COALESCE(final_model, model) NOT IN ('unknown','') "
                      "AND COALESCE(final_model, model) IS NOT NULL").fetchone()[0]

    brands_raw = cur("SELECT COALESCE(final_make, make), COUNT(*) FROM pairs "
                     "GROUP BY COALESCE(final_make, make)").fetchall()
    brands = _merge_ci(brands_raw)
    models_raw = cur("SELECT COALESCE(final_model, model), COUNT(*) FROM pairs "
                     "GROUP BY COALESCE(final_model, model)").fetchall()
    models = _merge_ci(models_raw)

    colors = [(c, n) for c, n in cur(
        "SELECT detected_color, COUNT(*) FROM pairs WHERE detected_color "
        "IS NOT NULL GROUP BY detected_color ORDER BY COUNT(*) DESC").fetchall()]

    # ---- AI classification ------------------------------------------------
    src_raw = dict(cur("SELECT COALESCE(prediction_source,'(none)'), COUNT(*) "
                       "FROM pairs GROUP BY prediction_source").fetchall())
    src = {"local": 0, "cloud · Gemini": 0, "cloud · OpenAI": 0, "unattributed": 0}
    for s, n in src_raw.items():
        if s == "local":
            src["local"] += n
        elif s.startswith("cloud:gemini"):
            src["cloud · Gemini"] += n
        elif s.startswith("cloud:openai"):
            src["cloud · OpenAI"] += n
        else:
            src["unattributed"] += n

    review = dict(cur("SELECT review_status, COUNT(*) FROM pairs "
                      "GROUP BY review_status").fetchall())

    ai = {
        "source": src,
        "make_conf": _bucket_conf(conn, "make_confidence"),
        "model_conf": _bucket_conf(conn, "model_confidence"),
        "color_conf": _bucket_conf(conn, "color_confidence"),
        "review": review,
        "known_make": known_make,
        "unknown_make": total_pairs - known_make,
        "known_model": known_model,
        "unknown_model": total_pairs - known_model,
    }

    # ---- pairing ----------------------------------------------------------
    ps_bands = [("<50%", 0, 0.5), ("50–65%", 0.5, 0.65), ("65–80%", 0.65, 0.8),
                ("80–90%", 0.8, 0.9), ("90–100%", 0.9, 1.0001)]
    ps_counts = [cur("SELECT COUNT(*) FROM pairs WHERE pair_score >= ? "
                     "AND pair_score < ?", (lo, hi)).fetchone()[0]
                 for _, lo, hi in ps_bands]
    avg_ps = cur("SELECT AVG(pair_score) FROM pairs WHERE pair_score "
                 "IS NOT NULL").fetchone()[0]
    pairing = {
        "pairs": true_pairs, "singles": singles,
        "score_hist": {"labels": [b[0] for b in ps_bands], "counts": ps_counts},
        "avg_score": round(avg_ps or 0, 3),
    }

    # ---- airtable sync ----------------------------------------------------
    ob_status = dict(cur("SELECT status, COUNT(*) FROM airtable_outbox "
                         "GROUP BY status").fetchall())
    ob_total = sum(ob_status.values())
    synced = ob_status.get("synced", 0)
    errs = [(e or "(none)", n) for e, n in cur(
        "SELECT last_error, COUNT(*) FROM airtable_outbox WHERE status!='synced' "
        "AND last_error IS NOT NULL GROUP BY last_error ORDER BY COUNT(*) "
        "DESC LIMIT 6").fetchall()]
    airtable = {
        "status": ob_status,
        "sync_rate": round(100.0 * synced / ob_total, 1) if ob_total else 0.0,
        "errors": errs,
    }

    # ---- box composition --------------------------------------------------
    b = cur("SELECT COALESCE(SUM(total_good_sneakers),0), "
            "COALESCE(SUM(total_end_of_life),0), COALESCE(SUM(casuals),0) "
            "FROM table_photos").fetchone()
    # Weight is operator-entered and has occasional garbage (a barcode typed into
    # the weight box -> 1e34). Bound to a sane physical range so one bad row
    # can't blow up the totals.
    w = cur("SELECT COALESCE(SUM(weight_of_box),0), COALESCE(AVG(weight_of_box),0), "
            "COUNT(*) FROM table_photos WHERE weight_of_box > 0 "
            "AND weight_of_box < 1000").fetchone()
    box = {"good": b[0], "eol": b[1], "casuals": b[2],
           "total_weight": round(w[0], 1), "avg_weight": round(w[1], 1),
           "weighed_boxes": w[2]}

    # ---- timeline (last 30 days, zero-filled) -----------------------------
    photo_by_day = dict(cur(
        "SELECT date(created_at), COUNT(*) FROM table_photos "
        "WHERE created_at >= ? GROUP BY date(created_at)",
        ((date.today() - timedelta(days=29)).isoformat(),)).fetchall())
    pair_by_day = dict(cur(
        "SELECT date(created_at), COUNT(*) FROM pairs WHERE created_at >= ? "
        "GROUP BY date(created_at)",
        ((date.today() - timedelta(days=29)).isoformat(),)).fetchall())
    days = [(date.today() - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    timeline = {
        "days": days,
        "photos": [photo_by_day.get(d, 0) for d in days],
        "pairs": [pair_by_day.get(d, 0) for d in days],
    }

    return {
        "kpis": {
            "table_photos": total_photos,
            "pairs": total_pairs,
            "shoes": total_shoes,
            "brands": len(brands),
            "models": len(models),
            "identified_pct": round(100.0 * known_make / total_pairs, 1)
            if total_pairs else 0.0,
            "sync_rate": airtable["sync_rate"],
        },
        "status": status,
        "brands": [{"label": l, "count": n} for l, n in brands[:15]],
        "models": [{"label": l, "count": n} for l, n in models[:12]],
        "colors": [{"label": c, "count": n} for c, n in colors],
        "ai": ai,
        "pairing": pairing,
        "airtable": airtable,
        "box": box,
        "timeline": timeline,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


@router.get("/stats", summary="All Tableau visualizations in one cached payload")
def tableau_stats(conn: sqlite3.Connection = Depends(get_db)):
    now = time.time()
    if _CACHE["data"] is not None and (now - _CACHE["ts"]) < _TTL:
        return _CACHE["data"]
    with _LOCK:
        # Re-check inside the lock so only one thread computes per TTL window.
        if _CACHE["data"] is not None and (time.time() - _CACHE["ts"]) < _TTL:
            return _CACHE["data"]
        data = _compute(conn)
        _CACHE["data"] = data
        _CACHE["ts"] = time.time()
        return data
