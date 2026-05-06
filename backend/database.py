import sqlite3
from backend.config import DB_PATH


def get_connection() -> sqlite3.Connection:
    """Open and return a new SQLite connection with sensible defaults."""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row   # rows behave like dicts: row["column"]
    conn.execute("PRAGMA journal_mode=WAL")   # better concurrent read performance
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

        -- Indexes for the most common query patterns
        CREATE INDEX IF NOT EXISTS idx_shoes_batch      ON shoes(batch_id);
        CREATE INDEX IF NOT EXISTS idx_shoes_timestamp  ON shoes(timestamp);
        CREATE INDEX IF NOT EXISTS idx_shoes_prediction ON shoes(ai_prediction);
        CREATE INDEX IF NOT EXISTS idx_shoes_review     ON shoes(review_status);
    """)

    conn.commit()
    _add_columns_if_missing(conn)
    conn.close()


def _add_columns_if_missing(conn: sqlite3.Connection):
    """Idempotent migration: add new columns without breaking existing rows."""
    for table, col, defn in [
        ("shoes", "shoe_brand", "TEXT"),
        ("shoes", "shoe_color", "TEXT"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
        except Exception:
            pass  # column already exists
    conn.commit()
