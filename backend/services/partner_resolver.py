"""
partner_resolver.py -- keeps table_photos.shipment_info (the partner name shown
on Table Photos) correct for EVERY table, not just the ones whose Airtable row
happened to exist at scan time.

Why this exists (2026-10-09 investigation): the scan-time lookup runs BEFORE the
outbox creates the "Shipments Received" row, so it usually finds nothing and was
never retried -- only ~1% of tables carried a partner although Airtable had one
for ~92%. Airtable links the Partner itself (from the Labels Created table) some
time after the row appears, and a missing/late label can be fixed by hand days
later. So the dash must keep looking, not look once.

Design (self-healing reconcile loop, no capture-time hook to forget):
  * shipment_resolve: one row per table photo with a barcode, holding its state
    (pending | blank | resolved | expired), attempt count and next_try_at.
  * discover(): registers any table that has no row yet -- covers new captures,
    overwrites, imports and the whole historical backlog alike. The 20 s poll only
    looks at the newest DISCOVER_WINDOW rows (a sub-millisecond rowid range scan,
    instead of a 22 ms anti-join over every table photo); a FULL sweep runs at
    startup and every FULL_SWEEP_SECONDS as the backstop for anything the window
    can't see (bulk imports, rowids reused after top rows were deleted).
  * claim(): a read-only "anything due?" check first, so an idle pass never takes
    the database write lock; only when something is due does it atomically lease a
    small batch (BEGIN IMMEDIATE, short) so the two uvicorn instances
    (:8000/:8443) never double-work a row.
  * The Airtable call happens with NO DB transaction open; one batched request
    covers ~40 barcodes and partner names are cached.
  * apply(): short write that stores shipment_info and the next state.
  * Backoff ladder 20s, 1m, 3m, 10m, 30m, 2h, 6h, 12h, then daily; a table that
    is still unresolved 30 days after capture becomes 'expired' (manual recheck
    can revive it). 'blank' = row exists in Airtable but has no Partner yet --
    retried on the same ladder so a hand-fix in Airtable is picked up on its own.
  * An Airtable/network error never consumes an attempt; the batch just waits.

Fail-safe: a daemon thread that never raises into the app, never blocks capture,
and does nothing unless an Airtable shipment lookup is configured.
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta

from backend.config import (PARTNER_RESOLVER_ENABLED, PARTNER_RESOLVER_SECONDS,
                            PARTNER_RESOLVER_BATCH, SHIPMENT_BARCODE_TRIM)
from backend.database import get_connection

# seconds to wait after the Nth unresolved attempt; the last entry repeats.
LADDER = [20, 60, 180, 600, 1800, 7200, 21600, 43200, 86400]
EXPIRE_AFTER_DAYS = 30        # stop re-checking a table this old (manual recheck revives)
LEASE_SECONDS = 300           # a claimed row is invisible to others this long
ERROR_RETRY_SECONDS = 120     # Airtable unreachable: try the batch again after this
MIN_BARCODE_LEN = 8           # shorter scans can never match a tracking number
DISCOVER_WINDOW = 200         # rows (by rowid) the cheap 20 s discovery looks back over
FULL_SWEEP_SECONDS = 3600     # how often the worker does the full-table discovery
CALL_PAUSE = 0.35             # between Airtable requests (base limit is 5 req/s, shared)

_SAFE = re.compile(r"[^A-Za-z0-9\-]")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def match_key(barcode, trim=SHIPMENT_BARCODE_TRIM):
    """Same normalisation the lookup and outbox use (last `trim` chars)."""
    bc = _SAFE.sub("", (barcode or "").strip())
    if trim and len(bc) > trim:
        bc = bc[-trim:]
    return bc


# -- schema ----------------------------------------------------------------

def ensure_schema(conn):
    conn.executescript("""
        -- shipment_resolve: one row per table photo with a barcode; drives the
        -- background partner resolver (see services/partner_resolver.py).
        CREATE TABLE IF NOT EXISTS shipment_resolve (
            table_photo_id TEXT PRIMARY KEY,
            match_barcode  TEXT NOT NULL,
            state          TEXT NOT NULL DEFAULT 'pending',  -- pending|blank|resolved|expired
            attempts       INTEGER NOT NULL DEFAULT 0,
            next_try_at    TEXT,
            lease_until    TEXT,
            last_error     TEXT,
            created_at     TEXT NOT NULL,
            resolved_at    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_resolve_due ON shipment_resolve(state, next_try_at);
    """)
    conn.commit()


# -- discovery -------------------------------------------------------------

def discover(conn, full=True):
    """Register table photos that have a usable barcode but no resolver row.
    full=True checks every table photo (startup / hourly / tests); full=False only
    the newest DISCOVER_WINDOW rows, which is where new captures always land.
    Tables that already carry a partner become 'resolved' without any API call."""
    now = _now()
    lo = 0
    if not full:
        top = conn.execute("SELECT MAX(rowid) FROM table_photos").fetchone()[0] or 0
        lo = max(0, top - DISCOVER_WINDOW)
    rows = conn.execute(
        "SELECT t.id, t.barcode, t.shipment_info, t.created_at FROM table_photos t "
        "WHERE t.rowid >= ? AND t.barcode IS NOT NULL AND LENGTH(TRIM(t.barcode)) >= ? "
        "AND NOT EXISTS (SELECT 1 FROM shipment_resolve r WHERE r.table_photo_id = t.id) "
        "LIMIT 5000", (lo, MIN_BARCODE_LEN)).fetchall()
    batch = []
    for r in rows:
        key = match_key(r["barcode"])
        if len(key) < MIN_BARCODE_LEN:
            key = ""                       # unusable scan; still register so we never rescan it
        has_partner = False
        if r["shipment_info"]:
            try:
                has_partner = bool((json.loads(r["shipment_info"]) or {}).get("partner"))
            except (ValueError, TypeError):
                pass
        if has_partner:
            batch.append((r["id"], key, "resolved", 0, None, now, now))
        elif not key:
            batch.append((r["id"], key, "expired", 0, None, now, None))
        else:
            batch.append((r["id"], key, "pending", 0, now, now, None))
    if batch:
        conn.executemany(
            "INSERT OR IGNORE INTO shipment_resolve "
            "(table_photo_id, match_barcode, state, attempts, next_try_at, created_at, resolved_at) "
            "VALUES (?,?,?,?,?,?,?)", batch)
        conn.commit()
    return len(batch)


# -- claim / apply ---------------------------------------------------------

def claim(conn, limit):
    """Atomically lease up to `limit` due rows (newest tables first). BEGIN
    IMMEDIATE makes this safe across the two uvicorn processes."""
    now = _now()
    # Read-only peek first (uses idx_resolve_due): the common idle case must not
    # take the write lock, which every capture/job-queue writer also wants.
    if conn.execute(
            "SELECT 1 FROM shipment_resolve WHERE state IN ('pending','blank') "
            "AND next_try_at <= ? AND (lease_until IS NULL OR lease_until < ?) LIMIT 1",
            (now, now)).fetchone() is None:
        return []
    lease = _iso(datetime.now() + timedelta(seconds=LEASE_SECONDS))
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT r.table_photo_id, r.match_barcode, r.attempts, r.state, t.created_at AS tp_created "
            "FROM shipment_resolve r JOIN table_photos t ON t.id = r.table_photo_id "
            "WHERE r.state IN ('pending','blank') AND r.next_try_at <= ? "
            "AND (r.lease_until IS NULL OR r.lease_until < ?) "
            "ORDER BY t.created_at DESC LIMIT ?", (now, now, limit)).fetchall()
        if rows:
            conn.executemany("UPDATE shipment_resolve SET lease_until=? WHERE table_photo_id=?",
                             [(lease, r["table_photo_id"]) for r in rows])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return rows


def _delay_for(attempts):
    return LADDER[min(attempts, len(LADDER) - 1)]


def apply_results(conn, claimed, found):
    """Store what the batch lookup found. `found` = {match_barcode: info dict};
    a barcode missing from it is 'not in Airtable (yet)'."""
    now_dt = datetime.now()
    now = _iso(now_dt)
    resolved = blank = pending = expired = 0
    for r in claimed:
        tid, key, attempts = r["table_photo_id"], r["match_barcode"], r["attempts"] + 1
        info = found.get(key)
        age_days = _age_days(r["tp_created"], now_dt)
        if info and info.get("partner"):
            conn.execute("UPDATE table_photos SET shipment_info=? WHERE id=?",
                         (json.dumps(info), tid))
            conn.execute("UPDATE shipment_resolve SET state='resolved', attempts=?, next_try_at=NULL, "
                         "lease_until=NULL, last_error=NULL, resolved_at=? WHERE table_photo_id=?",
                         (attempts, now, tid))
            resolved += 1
            continue
        state, err = ("blank", "no_partner") if info else ("pending", "no_row")
        if info:                                   # row exists, Partner empty: keep the data we do have
            conn.execute("UPDATE table_photos SET shipment_info=? WHERE id=?",
                         (json.dumps(info), tid))
        if age_days >= EXPIRE_AFTER_DAYS:
            conn.execute("UPDATE shipment_resolve SET state='expired', attempts=?, next_try_at=NULL, "
                         "lease_until=NULL, last_error=? WHERE table_photo_id=?", (attempts, err, tid))
            expired += 1
            continue
        nxt = _iso(now_dt + timedelta(seconds=_delay_for(attempts - 1)))
        conn.execute("UPDATE shipment_resolve SET state=?, attempts=?, next_try_at=?, "
                     "lease_until=NULL, last_error=? WHERE table_photo_id=?",
                     (state, attempts, nxt, err, tid))
        blank += state == "blank"
        pending += state == "pending"
    conn.commit()
    return {"resolved": resolved, "blank": blank, "pending": pending, "expired": expired}


def release_on_error(conn, claimed, error):
    """Airtable unreachable: give the rows back without burning an attempt."""
    nxt = _iso(datetime.now() + timedelta(seconds=ERROR_RETRY_SECONDS))
    conn.executemany(
        "UPDATE shipment_resolve SET lease_until=NULL, next_try_at=?, last_error=? WHERE table_photo_id=?",
        [(nxt, ("error: " + str(error))[:200], r["table_photo_id"]) for r in claimed])
    conn.commit()


def _age_days(iso, now_dt):
    try:
        return (now_dt - datetime.fromisoformat(iso)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 0.0


# -- one pass --------------------------------------------------------------

def run_pass(conn, lookup, limit=PARTNER_RESOLVER_BATCH, pause=CALL_PAUSE, sleep=None, full=True):
    """discover -> claim -> batched lookup -> apply. Returns a stats dict;
    stats['claimed'] == limit means there is probably more backlog. `full` selects
    the full-table discovery (default, safe) vs the cheap recent-rows window the
    worker uses between its hourly full sweeps."""
    stats = {"discovered": discover(conn, full=full), "claimed": 0}
    rows = claim(conn, limit)
    stats["claimed"] = len(rows)
    if not rows:
        return stats
    keys = sorted({r["match_barcode"] for r in rows if r["match_barcode"]})
    try:
        found = lookup.resolve_many(keys) if keys else {}      # network, no txn open
    except Exception as exc:                                    # noqa: BLE001 - fail safe
        release_on_error(conn, rows, exc)
        stats["error"] = str(exc)[:120]
        return stats
    stats.update(apply_results(conn, rows, found))
    if sleep and pause:
        sleep(pause * max(1, (len(keys) + 39) // 40))           # stay under the base rate limit
    return stats


# -- status / manual recheck (used by the routes + data-quality check) -----

def status(conn):
    counts = {r["state"]: r["n"] for r in conn.execute(
        "SELECT state, COUNT(*) n FROM shipment_resolve GROUP BY state")}
    overdue = conn.execute(
        "SELECT COUNT(*) FROM shipment_resolve WHERE state IN ('pending','blank') "
        "AND next_try_at < ? AND (lease_until IS NULL OR lease_until < ?)",
        (_iso(datetime.now() - timedelta(minutes=15)), _now())).fetchone()[0]
    return {"counts": counts, "overdue_15min": overdue, "enabled": PARTNER_RESOLVER_ENABLED}


def recheck(conn, scope="blank"):
    """Put rows back in the queue now. scope: blank | expired | all (unresolved only)."""
    states = {"blank": ("blank",), "expired": ("expired",),
              "all": ("pending", "blank", "expired")}[scope]
    ph = ",".join("?" * len(states))
    cur = conn.execute(
        f"UPDATE shipment_resolve SET state='pending', attempts=0, next_try_at=?, lease_until=NULL "
        f"WHERE state IN ({ph}) AND match_barcode != ''", (_now(), *states))
    conn.commit()
    return cur.rowcount


# -- background worker ------------------------------------------------------

class _Resolver:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self.last = {}                     # last pass stats, for /status

    def start(self):
        if not PARTNER_RESOLVER_ENABLED:
            print("[partner-resolver] disabled (PARTNER_RESOLVER_ENABLED=0)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="partner-resolver", daemon=True)
        self._thread.start()
        print(f"[partner-resolver] started (every {PARTNER_RESOLVER_SECONDS}s, batch {PARTNER_RESOLVER_BATCH}).")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self):
        from backend.services.shipment_lookup import get_airtable_lookup
        self._stop.wait(15)                # let startup settle; never competes with boot work
        last_full = 0.0                    # monotonic time of the last full discovery sweep
        while not self._stop.is_set():
            wait = PARTNER_RESOLVER_SECONDS
            try:
                lookup = get_airtable_lookup()
                if lookup is not None:
                    conn = get_connection()
                    try:
                        ensure_schema(conn)
                        do_full = time.monotonic() - last_full >= FULL_SWEEP_SECONDS
                        stats = run_pass(conn, lookup, sleep=self._stop.wait, full=do_full)
                        if do_full:
                            last_full = time.monotonic()
                        self.last = {**stats, "at": _now()}
                        if stats.get("claimed") or stats.get("discovered"):
                            print(f"[partner-resolver] {stats}")
                        if stats.get("claimed", 0) >= PARTNER_RESOLVER_BATCH:
                            wait = 2           # backlog: keep going, politely
                    finally:
                        conn.close()
            except Exception as exc:           # noqa: BLE001 - never die
                print(f"[partner-resolver] loop error: {exc}")
            self._stop.wait(wait)


resolver = _Resolver()
