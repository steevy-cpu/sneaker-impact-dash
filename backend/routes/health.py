import sqlite3
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request

from backend.config import APP_MODE, MODEL_VERSION, IMAGES_DIR, SIM_IMAGES_DIR, IT_PASSWORD
from backend.database import get_db
from backend.routes.it_auth import is_authed
from backend.utils.brands import canonical_brand, norm_key, CANONICAL_BRANDS

router = APIRouter(prefix="/api", tags=["Health"])


# The full-tree walk takes seconds once the image store holds 100k+ files, and
# theme.js hits /api/health on every page load — so serve a cached size and
# only re-walk every few minutes.
_DIR_SIZE_TTL = 300  # seconds
_dir_size_cache: dict = {}  # path -> (monotonic_ts, size_mb)


def _dir_size_mb(path) -> float:
    """Return total size of a directory tree in megabytes (cached)."""
    cached = _dir_size_cache.get(path)
    if cached and time.monotonic() - cached[0] < _DIR_SIZE_TTL:
        return cached[1]
    if not path.exists():
        return 0.0
    total_bytes = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    size = round(total_bytes / (1024 * 1024), 2)
    _dir_size_cache[path] = (time.monotonic(), size)
    return size


@router.get("/health", summary="System health and stats")
def health_check(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    """
    Returns current system status, operating mode, and summary statistics.

    Useful for the System Health dashboard page and quick sanity checks.
    """
    shoe_count  = conn.execute("SELECT COUNT(*) FROM shoes").fetchone()[0]
    batch_count = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]

    last_row = conn.execute(
        "SELECT timestamp FROM shoes ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()

    error_count = conn.execute(
        "SELECT COUNT(*) FROM shoes WHERE validation_status != 'VALID'"
    ).fetchone()[0]

    pending_review_count = conn.execute(
        "SELECT COUNT(*) FROM shoes WHERE review_status = 'PENDING'"
    ).fetchone()[0]

    # Report storage for the active image directory
    image_dir  = SIM_IMAGES_DIR if APP_MODE == "simulation" else IMAGES_DIR
    storage_mb = _dir_size_mb(image_dir)

    return {
        "mode":                    APP_MODE,
        "status":                  "ok",
        "db_connected":            True,
        "shoe_count":              shoe_count,
        "batch_count":             batch_count,
        "last_capture_time":       last_row["timestamp"] if last_row else None,
        "validation_error_count":  error_count,
        "pending_review_count":    pending_review_count,
        "model_version":           MODEL_VERSION,
        "storage_usage_mb":        storage_mb,
        # IT gate (theme.js reads these on every page to show/hide gated nav):
        # it_authed is True for everyone while the gate is disabled.
        "it_gate":                 bool(IT_PASSWORD),
        "it_authed":               is_authed(request),
    }


def _check(key, label, status, value, detail, fix=None):
    return {"key": key, "label": label, "status": status, "value": value,
            "detail": detail, "fix": fix}


@router.get("/data-quality", summary="Data-integrity checks (proactive monitor)")
def data_quality(conn: sqlite3.Connection = Depends(get_db)):
    """Proactively watch the data-integrity problems we've hit reactively (garbage
    weights that broke the Airtable YTD email, malformed barcode scans, stuck
    jobs/sync, unknown-rate spikes, unpinned brand duplicates). Read-only + cheap
    (a handful of COUNT/GROUP-BY), so it never impacts the live site. Each check
    is ok | warn | fail; overall status is the worst."""
    cur = conn.execute
    since7 = (datetime.now() - timedelta(days=7)).isoformat()
    checks = []

    # 1) Garbage box weights (the 9.6e33 class that broke the YTD email).
    gw = cur("SELECT COUNT(*) FROM table_photos WHERE weight_of_box IS NOT NULL "
             "AND (weight_of_box < 0 OR weight_of_box > 150)").fetchone()[0]
    checks.append(_check(
        "garbage_weights", "Garbage box weights", "fail" if gw else "ok", gw,
        f"{gw} table photo(s) with an implausible weight (<0 or >150 lb)."
        if gw else "No out-of-range weights.",
        "Clamp is in capture.py; clean stragglers in table_photos + Airtable." if gw else None))

    # 2) Malformed barcode scans (routing-form 96-barcode or truncated), recent.
    bad_bc = cur(
        "SELECT COUNT(*) FROM table_photos WHERE barcode IS NOT NULL AND barcode != '' "
        "AND (LENGTH(barcode) > 18 OR (barcode GLOB '*[0-9]*' AND LENGTH(barcode) < 10)) "
        "AND created_at >= ?", (since7,)).fetchone()[0]
    checks.append(_check(
        "suspect_barcodes", "Malformed barcodes (7d)", "warn" if bad_bc else "ok", bad_bc,
        f"{bad_bc} capture(s) in the last 7 days with a suspect barcode "
        "(over-long routing scan or truncated)." if bad_bc
        else "No malformed barcode scans in the last 7 days.",
        "Check the station scanner; capture.py sanitizes 96-routing scans." if bad_bc else None))

    # 3) Stuck processing jobs (jobqueue health).
    stuck = cur("SELECT COUNT(*) FROM table_photos WHERE status = 'processing' "
                "AND created_at < ?",
                ((datetime.now() - timedelta(minutes=20)).isoformat(),)).fetchone()[0]
    checks.append(_check(
        "stuck_jobs", "Stuck processing jobs", "fail" if stuck else "ok", stuck,
        f"{stuck} job(s) stuck in 'processing' > 20 min." if stuck
        else "No stuck jobs.",
        "Check the engine/jobqueue sweeper; a run may have died." if stuck else None))

    # 4) Stuck Airtable sync (old pending outbox rows).
    try:
        ob = cur("SELECT COUNT(*), MIN(created_at) FROM airtable_outbox "
                 "WHERE status = 'pending'").fetchone()
        pend, oldest = ob[0] or 0, ob[1]
        old = bool(oldest and oldest < (datetime.now() - timedelta(days=1)).isoformat())
        checks.append(_check(
            "stuck_sync", "Stuck Airtable sync", "warn" if (pend and old) else "ok", pend,
            f"{pend} outbox row(s) pending, oldest since {oldest[:10]}." if pend
            else "Outbox clear.",
            "Unmatched shipments or bad scans; see Airtable Sync page." if (pend and old) else None))
    except sqlite3.Error:
        pass

    # 5) Unknown-brand rate spike (recent vs overall).
    def unk_rate(where_extra="", params=()):
        tot = cur(f"SELECT COUNT(*) FROM pairs WHERE 1=1 {where_extra}", params).fetchone()[0]
        unk = cur(f"SELECT COUNT(*) FROM pairs WHERE COALESCE(final_make, make) "
                  f"IN ('', 'unknown') OR COALESCE(final_make, make) IS NULL "
                  f"{where_extra}", params).fetchone()[0]
        return (round(100.0 * unk / tot, 1) if tot else 0.0), tot
    overall_pct, _ = unk_rate()
    recent_pct, recent_tot = unk_rate(" AND created_at >= ?", (since7,))
    spike = recent_tot >= 20 and recent_pct > overall_pct + 10 and recent_pct > 12
    checks.append(_check(
        "unknown_make_spike", "Unknown-brand rate", "warn" if spike else "ok",
        f"{recent_pct}%",
        f"Last 7 days {recent_pct}% unknown vs {overall_pct}% overall"
        + (" — a spike; check the identify pipeline." if spike else "."),
        "Cloud/local identify may be degraded." if spike else None))

    # 6) Unpinned brand duplicates (case/alias variants not in CANONICAL_BRANDS).
    rawmakes = cur("SELECT COALESCE(final_make, make) m, COUNT(*) n FROM pairs "
                   "WHERE COALESCE(final_make, make) NOT IN ('', 'unknown') "
                   "AND COALESCE(final_make, make) IS NOT NULL GROUP BY m").fetchall()
    groups = {}
    for r in rawmakes:
        c = canonical_brand(r["m"])
        if not c:
            continue
        groups.setdefault(c, set()).add(r["m"])
    unpinned = sorted(c for c, sp in groups.items()
                      if len(sp) > 1 and norm_key(c) not in CANONICAL_BRANDS)
    checks.append(_check(
        "brand_dupes", "Unpinned brand duplicates", "warn" if unpinned else "ok",
        len(unpinned),
        ("Auto-merged by most-common spelling but not pinned: "
         + ", ".join(unpinned[:8]) + (" …" if len(unpinned) > 8 else "")) if unpinned
        else "All multi-spelling brands are pinned.",
        "Add to CANONICAL_BRANDS in backend/utils/brands.py for a stable label." if unpinned else None))

    order = {"fail": 2, "warn": 1, "ok": 0}
    overall = max((c["status"] for c in checks), key=lambda s: order[s], default="ok")
    return {
        "status": overall,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "counts": {s: sum(1 for c in checks if c["status"] == s)
                   for s in ("ok", "warn", "fail")},
        "checks": checks,
    }
