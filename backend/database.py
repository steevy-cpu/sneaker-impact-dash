import sqlite3
from backend.config import DB_PATH


def get_connection() -> sqlite3.Connection:
    """Open and return a new SQLite connection with sensible defaults."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row   # rows behave like dicts: row["column"]
    conn.execute("PRAGMA journal_mode=WAL")   # better concurrent read performance
    conn.execute("PRAGMA busy_timeout=5000")  # wait for the worker's writes
                                              # instead of failing SQLITE_BUSY
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db():
    """
    FastAPI dependency.
    Yields one DB connection per request, then closes it automatically.

    Usage in a route:
        def my_route(conn: sqlite3.Connection = Depends(get_db)):
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Create all tables and indexes if they do not already exist."""
    conn = get_connection()

    conn.executescript("""
        -- ----------------------------------------------------------------
        -- batches: groups of shoes inspected together in one session
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS batches (
            id           TEXT    PRIMARY KEY,
            started_at   TEXT    NOT NULL,
            ended_at     TEXT,
            operator_id  TEXT    NOT NULL,
            shoe_count   INTEGER NOT NULL DEFAULT 0
        );

        -- ----------------------------------------------------------------
        -- shoes: one row per inspected shoe
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS shoes (
            id                TEXT    PRIMARY KEY,
            batch_id          TEXT    NOT NULL,
            operator_id       TEXT    NOT NULL,
            timestamp         TEXT    NOT NULL,

            -- image paths (relative URLs served by FastAPI static mounts)
            img_top           TEXT,
            img_left          TEXT,
            img_right         TEXT,
            img_angle_left    TEXT,
            img_angle_right   TEXT,

            -- capture quality
            validation_status TEXT    NOT NULL DEFAULT 'VALID',
            validation_errors TEXT,

            -- AI output
            ai_prediction     TEXT,
            ai_confidence     REAL,
            model_version     TEXT,

            -- final outcome (may differ from AI if a human overrides)
            final_decision    TEXT,
            human_override    INTEGER NOT NULL DEFAULT 0,
            override_reason   TEXT,

            -- review workflow
            review_status     TEXT    NOT NULL DEFAULT 'NOT_REQUIRED',

            -- free text
            notes             TEXT,

            FOREIGN KEY (batch_id) REFERENCES batches(id)
        );

        -- ----------------------------------------------------------------
        -- system_events: lightweight log for health monitoring
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS system_events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT    NOT NULL,
            message     TEXT,
            timestamp   TEXT    NOT NULL
        );

        -- ----------------------------------------------------------------
        -- table_photos: one whole-table photo + box metadata (new flow).
        -- Photographed at the station, processed in the background into
        -- per-pair rows (segment -> pair -> color -> brand -> model).
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS table_photos (
            id                   TEXT    PRIMARY KEY,   -- TBL-YYYYMMDD-NNNN
            batch_id             TEXT,                  -- optional -> batches(id)
            operator_id          TEXT,
            image_path           TEXT,                  -- stored table photo (URL); null = metadata-only
            barcode              TEXT,                  -- scanned/typed; may be a FedEx tracking #
            weight_of_box        REAL,
            total_good_sneakers  INTEGER NOT NULL DEFAULT 0,
            total_end_of_life    INTEGER NOT NULL DEFAULT 0,
            casuals              INTEGER NOT NULL DEFAULT 0,
            status               TEXT    NOT NULL DEFAULT 'pending',  -- pending|processing|completed|failed
            error_message        TEXT,
            num_pairs            INTEGER NOT NULL DEFAULT 0,
            shipment_info        TEXT,                  -- JSON: resolved partner/weight/address (P5)
            created_at           TEXT    NOT NULL,
            processed_at         TEXT,
            FOREIGN KEY (batch_id) REFERENCES batches(id)
        );

        -- ----------------------------------------------------------------
        -- pairs: one tied shoe pair detected within a table photo.
        -- Mirrors the sneaker_impact_training pair JSON schema; the dash's
        -- human-review workflow now applies at the pair level.
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS pairs (
            id                TEXT    PRIMARY KEY,       -- PAIR-YYYYMMDD-NNNN
            table_photo_id    TEXT    NOT NULL,
            image_path        TEXT,                      -- cropped pair image (URL)
            bbox              TEXT,                      -- JSON [x1,y1,x2,y2] in the source photo
            detected_color    TEXT,
            color_confidence  REAL,
            make              TEXT,
            make_confidence   REAL,
            model             TEXT,
            model_confidence  REAL,
            model_sources     TEXT,                      -- JSON list of source links
            review_status     TEXT    NOT NULL DEFAULT 'NOT_REQUIRED',  -- NOT_REQUIRED|PENDING|COMPLETED
            final_make        TEXT,                      -- human-confirmed overrides
            final_model       TEXT,
            notes             TEXT,
            created_at        TEXT    NOT NULL,
            FOREIGN KEY (table_photo_id) REFERENCES table_photos(id)
        );

        -- ----------------------------------------------------------------
        -- app_config: small server-side key/value store (e.g. station config)
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS app_config (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,   -- JSON
            updated_at TEXT
        );

        -- ----------------------------------------------------------------
        -- airtable_outbox: durable "send-when-available" queue (one row per
        -- capture). Box data + brand summary are saved here and retried until
        -- the shipment exists in Airtable and the update lands. Mirrors
        -- ShoeSort's metadata_queue so nothing is ever lost.
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS airtable_outbox (
            table_photo_id      TEXT PRIMARY KEY,   -- idempotent key (one per capture)
            match_barcode       TEXT NOT NULL,      -- normalized key matched against Airtable
            full_barcode        TEXT,               -- original scanned value
            good                INTEGER,
            eol                 INTEGER,
            casuals             INTEGER,
            weight              REAL,
            brand_summary       TEXT,               -- filled on completion (Stage 2)
            status              TEXT NOT NULL DEFAULT 'pending',  -- pending|synced
            attempts            INTEGER NOT NULL DEFAULT 0,
            last_error          TEXT,               -- why it's still pending (e.g. 'no_row')
            airtable_record_id  TEXT,
            created_at          TEXT NOT NULL,
            last_attempt_at     TEXT,
            synced_at           TEXT
        );

        -- Indexes for the most common query patterns
        CREATE INDEX IF NOT EXISTS idx_shoes_batch      ON shoes(batch_id);
        CREATE INDEX IF NOT EXISTS idx_shoes_timestamp  ON shoes(timestamp);
        CREATE INDEX IF NOT EXISTS idx_shoes_prediction ON shoes(ai_prediction);
        CREATE INDEX IF NOT EXISTS idx_shoes_review     ON shoes(review_status);

        CREATE INDEX IF NOT EXISTS idx_table_photos_status  ON table_photos(status);
        CREATE INDEX IF NOT EXISTS idx_table_photos_created ON table_photos(created_at);
        CREATE INDEX IF NOT EXISTS idx_pairs_table_photo    ON pairs(table_photo_id);
        CREATE INDEX IF NOT EXISTS idx_pairs_review         ON pairs(review_status);
        CREATE INDEX IF NOT EXISTS idx_outbox_status        ON airtable_outbox(status);
    """)

    conn.commit()
    _add_columns_if_missing(conn)
    conn.close()


def _add_columns_if_missing(conn: sqlite3.Connection):
    """Idempotent migration: add new columns without breaking existing rows."""
    for table, col, defn in [
        ("shoes", "shoe_brand", "TEXT"),
        ("shoes", "shoe_color", "TEXT"),
        ("pairs", "pair_score", "REAL"),   # visual similarity of the two shoes (0-1), null for singles
        ("pairs", "prediction_source", "TEXT"),   # 'local' | 'cloud:<backend>:<model>'
        ("pairs", "final_color", "TEXT"),         # human-confirmed color (gold-label workflow)
        ("pairs", "label_action", "TEXT"),        # 'confirmed' (kept AI label) | 'corrected'
                                                  # (human changed it) — a free AI-accuracy meter
        ("table_photos", "claimed_at", "TEXT"),   # when a worker claimed the job; lets the
                                                  # stale sweeper re-queue orphaned 'processing' rows
        ("table_photos", "label_claimed_by", "TEXT"),  # multi-worker gold labeling: who is
                                                       # labeling this table now (Quick Label)
        ("table_photos", "label_claimed_at", "TEXT"),  # claim timestamp; lease auto-expires so a
                                                       # closed tab never locks a table forever
    ]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
        except Exception:
            pass  # column already exists
    conn.commit()
