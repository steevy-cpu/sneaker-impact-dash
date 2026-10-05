"""
label_index — a persistent index of LABEL_DATA_DIR in the dash DB.

Why (perf review 2026-10-05): the folder holds ~66k crops + 66k JSON sidecars.
The Label Data page stat'ed every entry on EVERY request just to test cache
freshness (1.6 s) and re-parsed all 66k sidecars whenever anything changed
(6.4 s -> 13 s page loads), and label_export._already_exported() parsed all
66k sidecars for EVERY exported pair inside the live table pipeline.

Now: one table, `label_index`, mirrors the folder (filename -> sidecar mtime/
size + the metadata the page shows). refresh() does a single scandir (~1 s
for 132k entries -- the floor) and re-reads ONLY sidecars whose mtime/size
changed, so steady state is one directory listing with zero file reads.
Readers query SQL (indexed, paged, filtered); the exporter's duplicate check
is an indexed lookup. The folder stays the source of truth -- training
scripts keep reading files; this is a cache that can be dropped and rebuilt.

In-place sidecar rewrites (reidentify) change the sidecar's mtime, which the
scan sees. Zero-byte sidecars (152 of them, 2026-10-05) index as metadata-less
rows, same as before.
"""
import json
import os
import sqlite3
import threading
import time

from backend.config import LABEL_DATA_DIR

_lock = threading.Lock()
_last_refresh = 0.0
# The scandir itself is the floor: ~1.8 s for 132k entries, so it must NOT run
# per request. Our own writers (label_export, delete) update the index
# directly; the scan only exists to catch out-of-band changes (reidentify's
# sidecar rewrites, manual file edits, training-script moves). Once every
# few minutes is plenty for that -- a page view in between costs one SELECT.
REFRESH_MIN_INTERVAL = 300.0    # s

_META_COLS = ("make", "model", "detected_color", "make_confidence",
              "model_confidence", "color_confidence", "prediction_source",
              "source_photo", "source_pair", "timestamp", "exported_by")


def ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS label_index (
            filename          TEXT PRIMARY KEY,      -- shoes_<color>_<make>_<N>.jpg
            sidecar_mtime_ns  INTEGER,
            sidecar_size      INTEGER,
            make              TEXT,
            model             TEXT,
            detected_color    TEXT,
            make_confidence   REAL,
            model_confidence  REAL,
            color_confidence  REAL,
            prediction_source TEXT,
            source_photo      TEXT,
            source_pair       TEXT,
            timestamp         TEXT,
            exported_by       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_label_index_ts     ON label_index(timestamp DESC, filename DESC);
        CREATE INDEX IF NOT EXISTS idx_label_index_make   ON label_index(make);
        CREATE INDEX IF NOT EXISTS idx_label_index_color  ON label_index(detected_color);
        CREATE INDEX IF NOT EXISTS idx_label_index_source ON label_index(source_photo, source_pair);
    """)


def _read_meta(path):
    try:
        with open(path) as fh:
            m = json.load(fh)
        if not isinstance(m, dict):
            return {}
        return m
    except Exception:                                  # noqa: BLE001 - zero-byte / corrupt sidecar
        return {}


def refresh(conn: sqlite3.Connection, force=False) -> dict:
    """Sync label_index with the folder. Returns counts of what changed.
    Cheap when nothing changed: one scandir + one SELECT, no file reads."""
    global _last_refresh
    with _lock:
        now = time.monotonic()
        if not force and now - _last_refresh < REFRESH_MIN_INTERVAL:
            return {"skipped": True}
        _last_refresh = now
        ensure_schema(conn)
        folder = str(LABEL_DATA_DIR)
        if not os.path.isdir(folder):
            return {"added": 0, "updated": 0, "removed": 0}

        # 1. one pass over the folder: jpgs present + sidecar (mtime, size)
        jpgs, sides = set(), {}
        with os.scandir(folder) as it:
            for de in it:
                n = de.name
                if n.endswith(".jpg"):
                    jpgs.add(n)
                elif n.endswith(".json"):
                    try:
                        st = de.stat()
                        sides[n[:-5] + ".jpg"] = (st.st_mtime_ns, st.st_size, de.path)
                    except OSError:
                        pass

        # 2. what the index knows
        known = {r[0]: (r[1], r[2]) for r in
                 conn.execute("SELECT filename, sidecar_mtime_ns, sidecar_size FROM label_index")}

        # 3. diff
        added = updated = 0
        upserts = []
        for fn in jpgs:
            mt, sz, path = sides.get(fn, (None, None, None))
            if fn in known and known[fn] == (mt, sz):
                continue                                 # unchanged: no read
            meta = _read_meta(path) if path else {}
            upserts.append((fn, mt, sz) + tuple(
                (str(meta.get(k)) if k == "source_pair" and meta.get(k) is not None else meta.get(k))
                for k in _META_COLS))
            if fn in known:
                updated += 1
            else:
                added += 1
        gone = [fn for fn in known if fn not in jpgs]
        if upserts:
            conn.executemany(
                "INSERT OR REPLACE INTO label_index (filename, sidecar_mtime_ns, sidecar_size, "
                + ", ".join(_META_COLS) + ") VALUES (" + ", ".join("?" * (3 + len(_META_COLS))) + ")",
                upserts)
        if gone:
            conn.executemany("DELETE FROM label_index WHERE filename = ?", [(g,) for g in gone])
        if upserts or gone:
            conn.commit()
        return {"added": added, "updated": updated, "removed": len(gone)}


def already_exported(conn: sqlite3.Connection, source_photo, source_pair) -> bool:
    """Indexed replacement for label_export's 66k-file scan."""
    ensure_schema(conn)
    return conn.execute(
        "SELECT 1 FROM label_index WHERE source_photo = ? AND source_pair = ? LIMIT 1",
        (source_photo, str(source_pair))).fetchone() is not None


def record_export(conn: sqlite3.Connection, filename, meta: dict):
    """Index a just-written crop immediately (no scan needed)."""
    ensure_schema(conn)
    side = str(LABEL_DATA_DIR / (filename[:-4] + ".json"))
    try:
        st = os.stat(side); mt, sz = st.st_mtime_ns, st.st_size
    except OSError:
        mt = sz = None
    conn.execute(
        "INSERT OR REPLACE INTO label_index (filename, sidecar_mtime_ns, sidecar_size, "
        + ", ".join(_META_COLS) + ") VALUES (" + ", ".join("?" * (3 + len(_META_COLS))) + ")",
        (filename, mt, sz) + tuple(
            (str(meta.get(k)) if k == "source_pair" and meta.get(k) is not None else meta.get(k))
            for k in _META_COLS))
    conn.commit()


def forget(conn: sqlite3.Connection, filename):
    ensure_schema(conn)
    conn.execute("DELETE FROM label_index WHERE filename = ?", (filename,))
    conn.commit()
