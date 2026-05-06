import sqlite3
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from backend.database import get_db
from backend.models import BatchCreate
from backend.utils.id_generator import generate_batch_id

router = APIRouter(prefix="/api/batches", tags=["Batches"])


def row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id":          row["id"],
        "started_at":  row["started_at"],
        "ended_at":    row["ended_at"],
        "operator_id": row["operator_id"],
        "shoe_count":  row["shoe_count"],
    }


@router.get("", summary="List all batches")
def list_batches(conn: sqlite3.Connection = Depends(get_db)):
    """Return all inspection batches, most recent first."""
    rows = conn.execute(
        "SELECT * FROM batches ORDER BY started_at DESC"
    ).fetchall()
    return [row_to_dict(r) for r in rows]


@router.get("/{batch_id}", summary="Get a single batch")
def get_batch(batch_id: str, conn: sqlite3.Connection = Depends(get_db)):
    """Return a single batch record by ID."""
    row = conn.execute(
        "SELECT * FROM batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    return row_to_dict(row)


@router.post("", status_code=201, summary="Open a new inspection batch")
def create_batch(batch: BatchCreate, conn: sqlite3.Connection = Depends(get_db)):
    """
    Open a new inspection batch.

    A unique batch ID is generated automatically.
    The batch remains open (ended_at = null) until it is closed
    via a PATCH endpoint (added in a later phase).
    """
    batch_id = generate_batch_id(conn)
    now      = datetime.now().isoformat()

    conn.execute(
        "INSERT INTO batches (id, started_at, operator_id, shoe_count) VALUES (?, ?, ?, 0)",
        (batch_id, now, batch.operator_id),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    return row_to_dict(row)
