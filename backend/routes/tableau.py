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
import csv
import io
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from backend.database import get_db
from backend.utils.brands import (canonical_brand, merge_brand_counts,
                                  norm_key, variants_for)

router = APIRouter(prefix="/api/tableau", tags=["Tableau"])

_TTL = 60.0                       # seconds; the dataset changes slowly
_CACHE = {}                       # range key -> {"ts": float, "data": dict}
_LOCK = threading.Lock()

# Time windows the page can request. "all" == since the new system launched
# (the data itself starts 2026-06-05, so no explicit floor is needed).
_RANGES = ("today", "week", "30d", "90d", "all")

# CO2e headline estimate. Calibrated to Airtable's existing per-box formula
# ("total estimated CO2e kg per box" = weight_lbs * ~7.468, verified tightly
# clustered 7.44-7.50 across live rows) so the dash headline agrees with what the
# org already reports. It's a weight-based ESTIMATE — labeled as such in the UI.
CO2E_KG_PER_LB = 7.46


def _since_for(range_key):
    """ISO lower-bound for a range key, or None for 'all' (no floor). created_at
    is stored as ISO 8601, which sorts lexicographically, so a string compare is
    correct and index-friendly."""
    today = date.today()
    now = datetime.now()
    if range_key == "today":
        return today.isoformat()                       # 'YYYY-MM-DD' <= any same-day ts
    if range_key == "week":                            # calendar week, Monday start
        monday = today - timedelta(days=today.weekday())
        return monday.isoformat()
    if range_key == "30d":
        return (now - timedelta(days=30)).isoformat()
    if range_key == "90d":
        return (now - timedelta(days=90)).isoformat()
    return None                                        # all


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


def _bucket_conf(conn, col, where="1=1", params=()):
    """Confidence histogram for a pairs column over fixed bands."""
    bands = [("0–50%", 0.0, 0.5), ("50–70%", 0.5, 0.7), ("70–80%", 0.7, 0.8),
             ("80–90%", 0.8, 0.9), ("90–100%", 0.9, 1.0001)]
    labels = [b[0] for b in bands]
    counts = []
    for _, lo, hi in bands:
        n = conn.execute(
            f"SELECT COUNT(*) FROM pairs WHERE {col} IS NOT NULL "
            f"AND {col} >= ? AND {col} < ? AND {where}", (lo, hi) + tuple(params)
        ).fetchone()[0]
        counts.append(n)
    return {"labels": labels, "counts": counts}


def _compute(conn: sqlite3.Connection, since=None) -> dict:
    cur = conn.execute

    # created_at filter fragments — injected into each table's WHERE. `since` is
    # None for 'all' (fragments collapse to a no-op so the SQL is unchanged).
    pw = "created_at >= ?" if since else "1=1"        # pairs
    tw = "created_at >= ?" if since else "1=1"        # table_photos
    ow = "created_at >= ?" if since else "1=1"        # airtable_outbox
    P = (since,) if since else ()                      # one bound param per query

    # ---- dataset / status -------------------------------------------------
    status = dict(cur(f"SELECT status, COUNT(*) FROM table_photos "
                      f"WHERE {tw} GROUP BY status", P).fetchall())
    total_photos = sum(status.values())
    total_pairs = cur(f"SELECT COUNT(*) FROM pairs WHERE {pw}", P).fetchone()[0]
    singles = cur(f"SELECT COUNT(*) FROM pairs WHERE pair_score IS NULL "
                  f"AND {pw}", P).fetchone()[0]
    true_pairs = total_pairs - singles
    # one record can be a tied pair (2 shoes) or a single (1 shoe)
    total_shoes = true_pairs * 2 + singles

    known_make = cur(f"SELECT COUNT(*) FROM pairs WHERE "
                     f"COALESCE(final_make, make) NOT IN ('unknown','') "
                     f"AND COALESCE(final_make, make) IS NOT NULL "
                     f"AND {pw}", P).fetchone()[0]
    known_model = cur(f"SELECT COUNT(*) FROM pairs WHERE "
                      f"COALESCE(final_model, model) NOT IN ('unknown','') "
                      f"AND COALESCE(final_model, model) IS NOT NULL "
                      f"AND {pw}", P).fetchone()[0]

    brands_raw = cur(f"SELECT COALESCE(final_make, make), COUNT(*) FROM pairs "
                     f"WHERE {pw} GROUP BY COALESCE(final_make, make)", P).fetchall()
    brands = merge_brand_counts(brands_raw)     # canonical: ASICS/Asics -> one
    models_raw = cur(f"SELECT COALESCE(final_model, model), COUNT(*) FROM pairs "
                     f"WHERE {pw} GROUP BY COALESCE(final_model, model)", P).fetchall()
    models = _merge_ci(models_raw)

    colors = [(c, n) for c, n in cur(
        f"SELECT detected_color, COUNT(*) FROM pairs WHERE detected_color "
        f"IS NOT NULL AND {pw} GROUP BY detected_color "
        f"ORDER BY COUNT(*) DESC", P).fetchall()]

    # ---- AI classification ------------------------------------------------
    src_raw = dict(cur(f"SELECT COALESCE(prediction_source,'(none)'), COUNT(*) "
                       f"FROM pairs WHERE {pw} GROUP BY prediction_source", P).fetchall())
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

    review = dict(cur(f"SELECT review_status, COUNT(*) FROM pairs "
                      f"WHERE {pw} GROUP BY review_status", P).fetchall())

    ai = {
        "source": src,
        "make_conf": _bucket_conf(conn, "make_confidence", pw, P),
        "model_conf": _bucket_conf(conn, "model_confidence", pw, P),
        "color_conf": _bucket_conf(conn, "color_confidence", pw, P),
        "review": review,
        "known_make": known_make,
        "unknown_make": total_pairs - known_make,
        "known_model": known_model,
        "unknown_model": total_pairs - known_model,
    }

    # ---- pairing ----------------------------------------------------------
    ps_bands = [("<50%", 0, 0.5), ("50–65%", 0.5, 0.65), ("65–80%", 0.65, 0.8),
                ("80–90%", 0.8, 0.9), ("90–100%", 0.9, 1.0001)]
    ps_counts = [cur(f"SELECT COUNT(*) FROM pairs WHERE pair_score >= ? "
                     f"AND pair_score < ? AND {pw}", (lo, hi) + P).fetchone()[0]
                 for _, lo, hi in ps_bands]
    avg_ps = cur(f"SELECT AVG(pair_score) FROM pairs WHERE pair_score "
                 f"IS NOT NULL AND {pw}", P).fetchone()[0]
    pairing = {
        "pairs": true_pairs, "singles": singles,
        "score_hist": {"labels": [b[0] for b in ps_bands], "counts": ps_counts},
        "avg_score": round(avg_ps or 0, 3),
    }

    # ---- airtable sync ----------------------------------------------------
    ob_status = dict(cur(f"SELECT status, COUNT(*) FROM airtable_outbox "
                         f"WHERE {ow} GROUP BY status", P).fetchall())
    ob_total = sum(ob_status.values())
    synced = ob_status.get("synced", 0)
    errs = [(e or "(none)", n) for e, n in cur(
        f"SELECT last_error, COUNT(*) FROM airtable_outbox WHERE status!='synced' "
        f"AND last_error IS NOT NULL AND {ow} GROUP BY last_error ORDER BY COUNT(*) "
        f"DESC LIMIT 6", P).fetchall()]
    airtable = {
        "status": ob_status,
        "sync_rate": round(100.0 * synced / ob_total, 1) if ob_total else 0.0,
        "errors": errs,
    }

    # ---- box composition --------------------------------------------------
    b = cur(f"SELECT COALESCE(SUM(total_good_sneakers),0), "
            f"COALESCE(SUM(total_end_of_life),0), COALESCE(SUM(casuals),0) "
            f"FROM table_photos WHERE {tw}", P).fetchone()
    # Weight is operator-entered and has occasional garbage (a barcode typed into
    # the weight box -> 1e34). Bound to a sane physical range so one bad row
    # can't blow up the totals.
    w = cur(f"SELECT COALESCE(SUM(weight_of_box),0), COALESCE(AVG(weight_of_box),0), "
            f"COUNT(*) FROM table_photos WHERE weight_of_box > 0 "
            f"AND weight_of_box < 1000 AND {tw}", P).fetchone()
    box = {"good": b[0], "eol": b[1], "casuals": b[2],
           "total_weight": round(w[0], 1), "avg_weight": round(w[1], 1),
           "weighed_boxes": w[2]}

    # ---- impact / ESG headline --------------------------------------------
    # Operator-counted box composition is the defensible "shoes handled" figure;
    # CO2e is a weight-based estimate (see CO2E_KG_PER_LB). All range-filtered.
    good_ct, eol_ct, cas_ct = b[0], b[1], b[2]
    shoes_ct = good_ct + eol_ct + cas_ct
    total_wt = round(w[0], 1)
    co2e_kg = total_wt * CO2E_KG_PER_LB
    impact = {
        "shoes_diverted": shoes_ct,
        "reused": good_ct,
        "recycled": eol_ct,
        "casual": cas_ct,
        "reuse_rate": round(100.0 * good_ct / shoes_ct, 1) if shoes_ct else 0.0,
        "weight_lbs": total_wt,
        "weighed_boxes": w[2],
        "co2e_kg": round(co2e_kg),
        "co2e_tonnes": round(co2e_kg / 1000.0, 1),
    }

    # ---- timeline (daily, spanning the selected range, zero-filled) -------
    # Start at the range floor; for 'all' start at the first photo (system
    # launch). Cap the drawn span so the chart stays readable.
    if since:
        start_day = date.fromisoformat(since[:10])
    else:
        first = cur("SELECT MIN(date(created_at)) FROM table_photos").fetchone()[0]
        start_day = date.fromisoformat(first) if first else date.today() - timedelta(days=29)
    ndays = max(1, min((date.today() - start_day).days + 1, 120))
    days = [(date.today() - timedelta(days=i)).isoformat() for i in range(ndays - 1, -1, -1)]
    photo_by_day = dict(cur(
        "SELECT date(created_at), COUNT(*) FROM table_photos "
        "WHERE date(created_at) >= ? GROUP BY date(created_at)",
        (days[0],)).fetchall())
    pair_by_day = dict(cur(
        "SELECT date(created_at), COUNT(*) FROM pairs WHERE date(created_at) >= ? "
        "GROUP BY date(created_at)",
        (days[0],)).fetchall())
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
        "impact": impact,
        "timeline": timeline,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


@router.get("/stats", summary="All Tableau visualizations in one cached payload")
def tableau_stats(range: str = "all", conn: sqlite3.Connection = Depends(get_db)):
    rng = range if range in _RANGES else "all"
    now = time.time()
    ent = _CACHE.get(rng)
    if ent and (now - ent["ts"]) < _TTL:
        return ent["data"]
    with _LOCK:
        # Re-check inside the lock so only one thread computes per TTL window.
        ent = _CACHE.get(rng)
        if ent and (time.time() - ent["ts"]) < _TTL:
            return ent["data"]
        data = _compute(conn, _since_for(rng))
        data["range"] = rng
        _CACHE[rng] = {"ts": time.time(), "data": data}
        return data


@router.get("/brands.csv", summary="Download top-brand quantities as CSV")
def brands_csv(range: str = "all", conn: sqlite3.Connection = Depends(get_db)):
    """
    Streams every identified brand with its pair quantity as a CSV download,
    sorted highest-first, with a TOTAL row. Honors the same ?range= window as the
    stats page. Same cheap GROUP BY (case-insensitively merged via _merge_ci), so
    it's sub-millisecond and runs in FastAPI's threadpool — no event-loop impact.
    """
    rng = range if range in _RANGES else "all"
    since = _since_for(rng)
    pw = "created_at >= ?" if since else "1=1"
    brands_raw = conn.execute(
        f"SELECT COALESCE(final_make, make), COUNT(*) FROM pairs "
        f"WHERE {pw} GROUP BY COALESCE(final_make, make)",
        (since,) if since else ()).fetchall()
    brands = merge_brand_counts(brands_raw)  # canonical, [(label, count)] desc

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["brand", "quantity"])
    for label, count in brands:
        writer.writerow([label, count])
    writer.writerow(["TOTAL", sum(n for _, n in brands)])

    filename = f"top_brands_{rng}_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _brand_data(conn, name, rng):
    """Drill into one canonical brand for a range: its top models + color palette
    + total pairs. Merges all raw spellings that map to the brand (ASICS/Asics,
    On/On Running). Cheap: one DISTINCT scan for spellings, then two GROUP BYs
    over an indexed IN clause. Returns full (untruncated) lists."""
    rng = rng if rng in _RANGES else "all"
    since = _since_for(rng)
    pw = "created_at >= ?" if since else "1=1"
    P = (since,) if since else ()
    canon = canonical_brand(name)
    empty = {"brand": canon or (name or "").strip(), "range": rng, "pairs": 0,
             "spellings": [], "models": [], "colors": []}
    if not canon:
        return empty

    makes = [r[0] for r in conn.execute(
        "SELECT DISTINCT COALESCE(final_make, make) FROM pairs "
        "WHERE COALESCE(final_make, make) IS NOT NULL").fetchall()]
    variants = variants_for(name, makes)
    if not variants:
        return empty

    ph = ",".join("?" * len(variants))
    where = f"COALESCE(final_make, make) IN ({ph}) AND {pw}"
    params = tuple(variants) + P

    total = conn.execute(
        f"SELECT COUNT(*) FROM pairs WHERE {where}", params).fetchone()[0]
    models_raw = conn.execute(
        f"SELECT COALESCE(final_model, model), COUNT(*) FROM pairs "
        f"WHERE {where} GROUP BY COALESCE(final_model, model)", params).fetchall()
    models = _merge_ci(models_raw)               # case-merge model spellings
    colors = [(c, n) for c, n in conn.execute(
        f"SELECT detected_color, COUNT(*) FROM pairs WHERE detected_color "
        f"IS NOT NULL AND {where} GROUP BY detected_color "
        f"ORDER BY COUNT(*) DESC", params).fetchall()]

    return {
        "brand": canon, "range": rng, "pairs": total,
        "spellings": sorted(variants),
        "models": [{"label": l, "count": n} for l, n in models],
        "colors": [{"label": c, "count": n} for c, n in colors],
    }


@router.get("/brand", summary="Drill into one brand: top models + color palette")
def brand_detail(name: str, range: str = "all",
                 conn: sqlite3.Connection = Depends(get_db)):
    data = _brand_data(conn, name, range)
    data["models"] = data["models"][:15]         # page shows the top slice
    return data


@router.get("/brand.csv", summary="Download one brand's models + palette as CSV")
def brand_csv(name: str, range: str = "all",
              conn: sqlite3.Connection = Depends(get_db)):
    """Streams one brand's full model list + color palette as a CSV
    (category,label,quantity). Honors ?range=."""
    data = _brand_data(conn, name, range)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["category", "label", "quantity"])
    for m in data["models"]:
        writer.writerow(["model", m["label"], m["count"]])
    for c in data["colors"]:
        writer.writerow(["color", c["label"], c["count"]])
    writer.writerow(["total_pairs", data["brand"], data["pairs"]])

    slug = norm_key(data["brand"]).replace(" ", "_") or "brand"
    filename = f"brand_{slug}_{data['range']}_{date.today().isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
