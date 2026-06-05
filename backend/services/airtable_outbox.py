"""
airtable_outbox.py — durable "send-when-available" queue (dash -> Airtable).

Problem it solves: a box is scanned + counted, but its shipment may not be in
Airtable yet (shipments are imported from the FedEx invoice export). Instead of
losing that box data, every capture (with a barcode) is saved to the
`airtable_outbox` table and stays 'pending' until the shipment row exists and
the update lands — then 'synced'. A background worker retries pending rows on an
interval, so a shipment imported AFTER the box was scanned still gets its data
(box metadata + brand summary) with no manual step.

Mirrors ShoeSort's metadata_queue (status / attempts / last_error, idempotent by
table_photo_id). Fully fail-safe: never raises to the caller; the PATCH is
idempotent (upsert by barcode), so retries can't duplicate data.
"""
import threading
from datetime import datetime

from backend.config import SHIPMENT_BARCODE_TRIM, OUTBOX_RETRY_SECONDS
from backend.database import get_connection
from backend.services.airtable_sync import get_airtable_sync, sync_enabled
from backend.services.shipment_lookup import normalize_barcode


def _now():
    return datetime.now().isoformat()


# --- enqueue / update --------------------------------------------------------

def enqueue(conn, table_photo_id, barcode, box):
    """Save one capture's box data to the outbox (status pending). Idempotent by
    table_photo_id. No-op without a usable barcode (nothing to match later)."""
    match = normalize_barcode(barcode, SHIPMENT_BARCODE_TRIM)
    if not match:
        return False
    conn.execute(
        """INSERT INTO airtable_outbox
             (table_photo_id, match_barcode, full_barcode, good, eol, casuals,
              weight, status, attempts, created_at)
           VALUES (?,?,?,?,?,?,?, 'pending', 0, ?)
           ON CONFLICT(table_photo_id) DO UPDATE SET
             match_barcode=excluded.match_barcode, full_barcode=excluded.full_barcode,
             good=excluded.good, eol=excluded.eol, casuals=excluded.casuals,
             weight=excluded.weight, status='pending'""",
        (table_photo_id, match, barcode, box.get("good"), box.get("eol"),
         box.get("casuals"), box.get("weight"), _now()),
    )
    conn.commit()
    return True


def set_brand_summary(conn, table_photo_id, summary):
    """Stage 2: attach the brand summary to an existing outbox row and re-arm it
    (status -> pending) so the summary gets pushed on the next attempt."""
    if not summary:
        return
    conn.execute(
        "UPDATE airtable_outbox SET brand_summary=?, status='pending' WHERE table_photo_id=?",
        (summary, table_photo_id),
    )
    conn.commit()


# --- sync attempts -----------------------------------------------------------

def _fields(row):
    """Airtable field payload from an outbox row (writable fields only)."""
    f = {}
    if row["weight"] is not None:
        f["Weight (lbs)"] = row["weight"]
    if row["good"] is not None:
        f["Good Sneakers"] = int(row["good"])
    if row["eol"] is not None:
        f["End of Life"] = int(row["eol"])
    if row["casuals"] is not None:
        f["Casual/Mixed"] = int(row["casuals"])
    if row["brand_summary"]:
        f["Brand Summary"] = row["brand_summary"]
    return f


def _attempt(conn, row):
    """Try to sync one row; update its status/attempts/error. Returns status."""
    status, rid = get_airtable_sync().push(row["match_barcode"], _fields(row))
    now = _now()
    if status == "synced":
        conn.execute(
            "UPDATE airtable_outbox SET status='synced', airtable_record_id=?, "
            "synced_at=?, last_attempt_at=?, attempts=attempts+1, last_error=NULL "
            "WHERE table_photo_id=?", (rid, now, now, row["table_photo_id"]))
    else:
        # no_row / error / no_barcode -> stay pending, record why for visibility.
        conn.execute(
            "UPDATE airtable_outbox SET last_attempt_at=?, attempts=attempts+1, "
            "last_error=? WHERE table_photo_id=?", (now, status, row["table_photo_id"]))
    conn.commit()
    return status


def try_one_async(table_photo_id):
    """Best-effort immediate sync of one row in a background thread (so capture
    never blocks). The retry worker is the durable fallback."""
    if not sync_enabled():
        return

    def run():
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT * FROM airtable_outbox WHERE table_photo_id=? AND status='pending'",
                (table_photo_id,)).fetchone()
            if row:
                _attempt(conn, row)
        except Exception as exc:                           # noqa: BLE001 - fail safe
            print(f"[outbox] try_one error for {table_photo_id}: {exc}")
        finally:
            conn.close()

    threading.Thread(target=run, daemon=True).start()


def flush(limit=500):
    """Attempt to sync every pending row. Returns {synced, pending, total}."""
    if not sync_enabled():
        return {"synced": 0, "pending": 0, "total": 0, "note": "sync disabled"}
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM airtable_outbox WHERE status='pending' "
            "ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        synced = still = 0
        for row in rows:
            try:
                if _attempt(conn, row) == "synced":
                    synced += 1
                else:
                    still += 1
            except Exception as exc:                       # noqa: BLE001 - fail safe
                print(f"[outbox] attempt error for {row['table_photo_id']}: {exc}")
                still += 1
        if rows:
            print(f"[outbox] flush: {synced} synced, {still} still pending (of {len(rows)})")
        return {"synced": synced, "pending": still, "total": len(rows)}
    finally:
        conn.close()


def counts():
    """{'pending': n, 'synced': m} for the dashboard."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM airtable_outbox GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}
    finally:
        conn.close()


def list_items(status=None, limit=200):
    conn = get_connection()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM airtable_outbox WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM airtable_outbox ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- background retry worker -------------------------------------------------

class _Retrier:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="airtable-outbox", daemon=True)
        self._thread.start()
        print(f"[outbox] retry worker started (every {OUTBOX_RETRY_SECONDS}s).")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self):
        while not self._stop.is_set():
            self._stop.wait(OUTBOX_RETRY_SECONDS)   # wait first; capture already tries immediately
            if self._stop.is_set():
                break
            try:
                if sync_enabled():
                    flush()
            except Exception as exc:                       # noqa: BLE001 - never die
                print(f"[outbox] retry loop error: {exc}")


retrier = _Retrier()
