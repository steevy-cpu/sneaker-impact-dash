import sqlite3

from fastapi import APIRouter, Depends

from backend.config import APP_MODE, MODEL_VERSION, IMAGES_DIR, SIM_IMAGES_DIR
from backend.database import get_db

router = APIRouter(prefix="/api", tags=["Health"])


def _dir_size_mb(path) -> float:
    """Return total size of a directory tree in megabytes."""
    if not path.exists():
        return 0.0
    total_bytes = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return round(total_bytes / (1024 * 1024), 2)


@router.get("/health", summary="System health and stats")
def health_check(conn: sqlite3.Connection = Depends(get_db)):
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
    }
