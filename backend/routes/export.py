"""
export.py — Data export endpoint.

  GET /api/export/shoes.csv  — download all shoe records as a CSV file
    Optional query params:
      start_date=YYYY-MM-DD  (inclusive lower bound on timestamp date)
      end_date=YYYY-MM-DD    (inclusive upper bound on timestamp date)

The response streams a UTF-8 CSV that includes all metadata fields
useful for analysis or audit purposes.
"""
import csv
import io
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from backend.database import get_db

router = APIRouter(prefix="/api/export", tags=["Export"])

# All columns included in the CSV download
CSV_COLUMNS = [
    "id", "batch_id", "timestamp", "operator_id",
    "shoe_brand", "shoe_color",
    "ai_prediction", "ai_confidence", "validation_status",
    "final_decision", "review_status", "human_override",
    "override_reason", "notes",
]


@router.get("/shoes.csv", summary="Export shoe records as CSV")
def export_shoes(
    start_date: Optional[str] = Query(None, description="Filter from YYYY-MM-DD (inclusive)"),
    end_date:   Optional[str] = Query(None, description="Filter to   YYYY-MM-DD (inclusive)"),
    conn: sqlite3.Connection = Depends(get_db),
):
    """
    Streams all shoe records as a CSV file download.
    The browser shows a Save-As dialog because of the Content-Disposition header.
    Date filters are applied against the date portion of the stored ISO timestamp.
    """
    clauses: list[str] = []
    params:  list[str] = []

    if start_date:
        clauses.append("date(timestamp) >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("date(timestamp) <= ?")
        params.append(end_date)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows  = conn.execute(
        f"SELECT {', '.join(CSV_COLUMNS)} FROM shoes {where} ORDER BY timestamp",
        params,
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([row[col] for col in CSV_COLUMNS])

    # Build a descriptive filename from the date filter(s), if any
    parts: list[str] = []
    if start_date: parts.append(f"from_{start_date}")
    if end_date:   parts.append(f"to_{end_date}")
    filename = f"sneaker_impact_{'_'.join(parts) if parts else 'all'}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
