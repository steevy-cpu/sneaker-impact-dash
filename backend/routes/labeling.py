"""
labeling.py — multi-worker table claiming for the Quick Label gold workflow.

Lets 2-3 people label in parallel without ever doing the same table twice:
each worker CLAIMS a whole table photo, which removes it from everyone else's
list until they finish or their claim expires. The claim is a LEASE (not a hard
lock) refreshed by a heartbeat, so a closed tab / crash never strands a table —
it returns to the pool after LEASE_SECONDS of silence.

  GET  /api/labeling/tables?worker=X     tables with pending pairs (mine + free)
  POST /api/labeling/claim/{tp}?worker=X  atomically claim a table (409 if taken)
  POST /api/labeling/heartbeat/{tp}?worker=X   keep my claim alive
  POST /api/labeling/release/{tp}?worker=X     give a table back

All endpoints are sync `def` (threadpool) and do tiny indexed queries, so they
never block the event loop or slow the live site. The per-pair save still uses
the existing PATCH /api/pairs/{id}/review; the per-table pair list uses the
existing GET /api/pairs?table_photo_id=..&review_status=PENDING.
"""
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.database import get_db
from backend.utils.image_utils import get_table_photo_thumb_url

router = APIRouter(prefix="/api/labeling", tags=["Labeling"])

LEASE_SECONDS = 900          # 15 min; a claim older than this is up for grabs

# The pending-per-table aggregate (a GROUP BY over all PENDING pairs) is the only
# costly part of the list — and it barely changes second to second. Cache it
# briefly so N workers polling the picker never re-run it concurrently. Claim
# state is read LIVE (cheap, single-table) so reservations stay real-time.
_AGG_TTL = 8.0
_agg = {"ts": 0.0, "rows": None}
_agg_lock = threading.Lock()


def _pending_by_table(conn):
    now = time.time()
    if _agg["rows"] is not None and (now - _agg["ts"]) < _AGG_TTL:
        return _agg["rows"]
    with _agg_lock:
        if _agg["rows"] is not None and (time.time() - _agg["ts"]) < _AGG_TTL:
            return _agg["rows"]
        rows = conn.execute(
            """SELECT tp.id, tp.image_path, tp.barcode, tp.num_pairs, tp.created_at,
                      COUNT(p.id) AS pending
               FROM table_photos tp
               JOIN pairs p ON p.table_photo_id = tp.id AND p.review_status = 'PENDING'
               GROUP BY tp.id"""
        ).fetchall()
        _agg["rows"] = [dict(r) for r in rows]
        _agg["ts"] = time.time()
        return _agg["rows"]


def _now():
    return datetime.now().isoformat()


def _expiry_cutoff():
    return (datetime.now() - timedelta(seconds=LEASE_SECONDS)).isoformat()


def _active(claimed_by, claimed_at):
    """A claim is active if held by someone and refreshed within the lease."""
    return bool(claimed_by) and bool(claimed_at) and claimed_at >= _expiry_cutoff()


@router.get("/tables", summary="Table photos with pending pairs, claim-aware")
def list_tables(worker: str = Query(..., min_length=1),
                conn: sqlite3.Connection = Depends(get_db)):
    """Tables that still have PENDING pairs. Excludes tables ACTIVELY claimed by
    someone else; includes the caller's own claims (to resume). Ordered: my
    claimed tables first, then oldest backlog first."""
    agg = _pending_by_table(conn)                     # cached heavy aggregate
    # Live claim state (cheap single-table read) so reservations are real-time.
    claims = {r["id"]: (r["label_claimed_by"], r["label_claimed_at"]) for r in conn.execute(
        "SELECT id, label_claimed_by, label_claimed_at FROM table_photos "
        "WHERE label_claimed_by IS NOT NULL").fetchall()}
    out = []
    for r in agg:
        by, at = claims.get(r["id"], (None, None))
        mine = _active(by, at) and by == worker
        if _active(by, at) and not mine:
            continue                                 # someone else is on it → hide
        out.append({
            "id":            r["id"],
            "thumb_path":    get_table_photo_thumb_url(r["id"]) if r["image_path"] else None,
            "image_path":    r["image_path"],
            "barcode":       r["barcode"],
            "pending":       r["pending"],
            "total":         r["num_pairs"],
            "created_at":    r["created_at"],
            "mine":          mine,                    # → show as "Resume"
        })
    out.sort(key=lambda t: (not t["mine"], t["created_at"]))
    return {"worker": worker, "count": len(out), "tables": out}


@router.post("/claim/{tp_id}", summary="Atomically claim a table for labeling")
def claim_table(tp_id: str, worker: str = Query(..., min_length=1),
                conn: sqlite3.Connection = Depends(get_db)):
    """Claim a table iff it's free, mine, or expired — a single atomic UPDATE
    guarded by WHERE, so two workers racing for the same table can't both win
    (exactly one UPDATE affects a row)."""
    exists = conn.execute("SELECT id FROM table_photos WHERE id = ?", (tp_id,)).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail=f"Table '{tp_id}' not found")
    cur = conn.execute(
        """UPDATE table_photos
              SET label_claimed_by = ?, label_claimed_at = ?
            WHERE id = ?
              AND (label_claimed_by IS NULL
                   OR label_claimed_by = ?
                   OR label_claimed_at < ?)""",
        (worker, _now(), tp_id, worker, _expiry_cutoff()),
    )
    conn.commit()
    if cur.rowcount != 1:
        holder = conn.execute(
            "SELECT label_claimed_by FROM table_photos WHERE id = ?", (tp_id,)
        ).fetchone()
        raise HTTPException(
            status_code=409,
            detail=f"Table is being labeled by {holder['label_claimed_by'] or 'someone else'}.")
    return {"claimed": True, "id": tp_id, "worker": worker}


@router.post("/heartbeat/{tp_id}", summary="Refresh my claim so it doesn't expire")
def heartbeat(tp_id: str, worker: str = Query(..., min_length=1),
              conn: sqlite3.Connection = Depends(get_db)):
    cur = conn.execute(
        "UPDATE table_photos SET label_claimed_at = ? WHERE id = ? AND label_claimed_by = ?",
        (_now(), tp_id, worker),
    )
    conn.commit()
    return {"ok": cur.rowcount == 1}


@router.post("/release/{tp_id}", summary="Release a table back to the pool")
def release(tp_id: str, worker: str = Query(..., min_length=1),
            conn: sqlite3.Connection = Depends(get_db)):
    cur = conn.execute(
        """UPDATE table_photos SET label_claimed_by = NULL, label_claimed_at = NULL
           WHERE id = ? AND label_claimed_by = ?""",
        (tp_id, worker),
    )
    conn.commit()
    return {"released": cur.rowcount == 1, "id": tp_id}
