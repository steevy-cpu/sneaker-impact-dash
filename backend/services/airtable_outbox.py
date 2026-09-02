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
from datetime import datetime, timedelta

from backend.config import (AIRTABLE_NOTES_FIELD, OUTBOX_PURGE_DAYS,
                            OUTBOX_RETRY_SECONDS, SHIPMENT_BARCODE_TRIM)
from backend.database import get_connection
from backend.services.airtable_sync import get_airtable_sync, sync_enabled
from backend.services.shipment_lookup import normalize_barcode


def _now():
    return datetime.now().isoformat()


# --- enqueue / update --------------------------------------------------------

def enqueue(conn, table_photo_id, barcode, box):
    """Save one capture's box data to the outbox (status pending). Idempotent by
    table_photo_id. No-op without a usable barcode (nothing to match later).

    box may carry insoles_currex / insoles_superfeet texts (combined boxes:
    the operator's parsed manual counts, known at capture time). Insole-ONLY
    captures leave them None here — the engine fills them at stage 2 via
    set_insole_summaries."""
    match = normalize_barcode(barcode, SHIPMENT_BARCODE_TRIM)
    if not match:
        return False
    conn.execute(
        """INSERT INTO airtable_outbox
             (table_photo_id, match_barcode, full_barcode, good, eol, casuals,
              singles, weight, notes, insoles_currex, insoles_superfeet,
              status, attempts, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending', 0, ?)
           ON CONFLICT(table_photo_id) DO UPDATE SET
             match_barcode=excluded.match_barcode, full_barcode=excluded.full_barcode,
             good=excluded.good, eol=excluded.eol, casuals=excluded.casuals,
             singles=excluded.singles, weight=excluded.weight, notes=excluded.notes,
             insoles_currex=excluded.insoles_currex,
             insoles_superfeet=excluded.insoles_superfeet, status='pending'""",
        (table_photo_id, match, barcode, box.get("good"), box.get("eol"),
         box.get("casuals"), box.get("singles"), box.get("weight"), box.get("notes"),
         box.get("insoles_currex"), box.get("insoles_superfeet"), _now()),
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


def set_insole_summaries(conn, table_photo_id, summaries):
    """Stage 2, insole runs: attach the per-brand pairs/singles texts (from
    insole_summaries_from_pairs) and re-arm the row. The shoe brand_summary
    stays untouched — an insole shipment never writes 'Brand Summary'."""
    if not summaries:
        return
    conn.execute(
        "UPDATE airtable_outbox SET insoles_currex=?, insoles_superfeet=?, "
        "status='pending' WHERE table_photo_id=?",
        (summaries.get("insoles_currex"), summaries.get("insoles_superfeet"),
         table_photo_id),
    )
    conn.commit()


# --- sync attempts -----------------------------------------------------------

def _fields(row):
    """Airtable field payload from an outbox row (writable fields only).

    The operator note is deliberately NOT here. Airtable rejects the ENTIRE
    request with 422 UNKNOWN_FIELD_NAME if a single key isn't a real field, so a
    note in this payload would take the counts down with it. It goes out in its
    own PATCH afterwards — see _push_note."""
    f = {}
    if row["weight"] is not None:
        f["Weight (lbs)"] = row["weight"]
    if row["good"] is not None:
        f["Good Sneakers"] = int(row["good"])
    if row["eol"] is not None:
        f["End of Life"] = int(row["eol"])
    if row["casuals"] is not None:
        f["Casual/Mixed"] = int(row["casuals"])
    # keys() guard: rows from a pre-migration DB (field verified via the
    # metadata API 2026-09-02: "Singles", number).
    if "singles" in row.keys() and row["singles"] is not None:
        f["Singles"] = int(row["singles"])
    if row["brand_summary"]:
        f["Brand Summary"] = row["brand_summary"]
    # Insole-capture columns. The Airtable fields are capitalized
    # ("Insoles_currex" — verified via the metadata API 2026-07-27); the local
    # DB columns are lowercase. keys() guard: rows from a pre-migration DB.
    for col, field in (("insoles_currex", "Insoles_currex"),
                       ("insoles_superfeet", "Insoles_superfeet")):
        if col in row.keys() and row[col]:
            f[field] = row[col]
    return f


def _push_note(rid, row):
    """Best-effort: write the operator note to Airtable in its OWN PATCH, after
    the counts have already landed.

    Dark unless AIRTABLE_NOTES_FIELD names a field that really exists on
    "Shipments Received" — the base has no generic Notes field today (only
    ESG/EPR Notes and Marketing Notes, which belong to other workflows), so
    until the owner adds one this is a no-op. Separating it from the counts
    payload is the whole point: if the field name is wrong, Airtable 422s THIS
    request only, and the box metadata is already safely written."""
    note = row["notes"] if "notes" in row.keys() else None
    if not AIRTABLE_NOTES_FIELD or not note or not rid:
        return
    try:
        get_airtable_sync().patch_fields(rid, {AIRTABLE_NOTES_FIELD: note})
    except Exception as exc:                               # noqa: BLE001 - fail safe
        # Never re-raise and never mark the row unsynced: the counts DID land.
        print(f"[outbox] note push failed for {row['table_photo_id']} "
              f"(field '{AIRTABLE_NOTES_FIELD}'): {exc}")


def _attempt(conn, row):
    """Try to sync one row; update its status/attempts/error. Returns status."""
    status, rid = get_airtable_sync().push(row["match_barcode"], _fields(row))
    now = _now()
    if status == "synced":
        _push_note(rid, row)
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


# --- delete / purge ----------------------------------------------------------

def delete_one(table_photo_id):
    """Delete one PENDING row (operator gave up on it — usually a bad scan).
    Synced rows are kept as history and can't be deleted here. Returns True if
    a row was removed."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "DELETE FROM airtable_outbox WHERE table_photo_id=? AND status='pending'",
            (table_photo_id,))
        conn.commit()
        if cur.rowcount:
            print(f"[outbox] deleted pending row {table_photo_id}")
        return bool(cur.rowcount)
    finally:
        conn.close()


def purge_stale(days=None):
    """Delete pending rows older than `days` (default OUTBOX_PURGE_DAYS) — the
    shipment never showed up in Airtable, so retrying forever just clutters the
    queue. Age-based on purpose (see OUTBOX_PURGE_DAYS in config). Returns the
    list of purged table_photo_ids."""
    days = OUTBOX_PURGE_DAYS if days is None else days
    if days <= 0:
        return []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT table_photo_id, match_barcode, created_at FROM airtable_outbox "
            "WHERE status='pending' AND created_at < ?", (cutoff,)).fetchall()
        if not rows:
            return []
        ids = [r["table_photo_id"] for r in rows]
        conn.execute(
            "DELETE FROM airtable_outbox WHERE status='pending' AND created_at < ?",
            (cutoff,))
        conn.commit()
        for r in rows:
            print(f"[outbox] purged {r['table_photo_id']} (barcode "
                  f"{r['match_barcode']!r}, pending since {r['created_at'][:10]})")
        return ids
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
                purge_stale()
            except Exception as exc:                       # noqa: BLE001 - never die
                print(f"[outbox] retry loop error: {exc}")


retrier = _Retrier()
