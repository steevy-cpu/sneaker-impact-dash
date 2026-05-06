import sqlite3
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.database import get_db
from backend.models import DecisionUpdate, ShoeCreate
from backend.utils.id_generator import generate_shoe_id

router = APIRouter(prefix="/api/shoes", tags=["Shoes"])


# ---------------------------------------------------------------------------
# Helper: convert a sqlite3.Row to a plain dict
# ---------------------------------------------------------------------------

def row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id":               row["id"],
        "batch_id":         row["batch_id"],
        "operator_id":      row["operator_id"],
        "timestamp":        row["timestamp"],
        "img_top":          row["img_top"],
        "img_left":         row["img_left"],
        "img_right":        row["img_right"],
        "img_angle_left":   row["img_angle_left"],
        "img_angle_right":  row["img_angle_right"],
        "validation_status":  row["validation_status"],
        "validation_errors":  row["validation_errors"],
        "ai_prediction":    row["ai_prediction"],
        "ai_confidence":    row["ai_confidence"],
        "model_version":    row["model_version"],
        "final_decision":   row["final_decision"],
        "human_override":   bool(row["human_override"]),
        "override_reason":  row["override_reason"],
        "review_status":    row["review_status"],
        "shoe_brand":       row["shoe_brand"],
        "shoe_color":       row["shoe_color"],
        "notes":            row["notes"],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("", summary="List shoe inspection records")
def list_shoes(
    page:              int            = Query(1,  ge=1,   description="Page number (starts at 1)"),
    page_size:         int            = Query(20, ge=1, le=500, description="Records per page"),
    batch_id:          Optional[str]  = Query(None, description="Filter by batch ID"),
    ai_prediction:     Optional[str]  = Query(None, description="REUSE | RECYCLE | REVIEW"),
    validation_status: Optional[str]  = Query(None, description="VALID | FAILED_CAPTURE | PARTIAL"),
    review_status:     Optional[str]  = Query(None, description="NOT_REQUIRED | PENDING | COMPLETED"),
    human_override:    Optional[bool] = Query(None, description="true = overridden by human"),
    conn:              sqlite3.Connection = Depends(get_db),
):
    """
    Returns a paginated list of shoe inspection records.
    All query parameters are optional and can be combined.
    """
    filters = []
    params  = []

    if batch_id is not None:
        filters.append("batch_id = ?");          params.append(batch_id)
    if ai_prediction is not None:
        filters.append("ai_prediction = ?");     params.append(ai_prediction)
    if validation_status is not None:
        filters.append("validation_status = ?"); params.append(validation_status)
    if review_status is not None:
        filters.append("review_status = ?");     params.append(review_status)
    if human_override is not None:
        filters.append("human_override = ?");    params.append(1 if human_override else 0)

    where  = ("WHERE " + " AND ".join(filters)) if filters else ""
    total  = conn.execute(f"SELECT COUNT(*) FROM shoes {where}", params).fetchone()[0]
    offset = (page - 1) * page_size

    rows = conn.execute(
        f"SELECT * FROM shoes {where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    ).fetchall()

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "items":     [row_to_dict(r) for r in rows],
    }


@router.get("/{shoe_id}", summary="Get a single shoe record")
def get_shoe(shoe_id: str, conn: sqlite3.Connection = Depends(get_db)):
    """Return all fields for one shoe inspection record."""
    row = conn.execute("SELECT * FROM shoes WHERE id = ?", (shoe_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Shoe '{shoe_id}' not found")
    return row_to_dict(row)


@router.post("", status_code=201, summary="Create a new shoe inspection record")
def create_shoe(shoe: ShoeCreate, conn: sqlite3.Connection = Depends(get_db)):
    """
    Create a new shoe inspection record.

    Used by the inspection station (actual mode) or test scripts (simulation mode).
    The batch referenced by batch_id must already exist.
    A unique shoe ID is generated automatically.

    Image paths (img_top etc.) are relative URL strings, not file blobs.
    Simulation mode uses shared placeholders (/sim_images/*.jpg).
    Actual mode uses per-shoe paths (/images/{batch_id}/{shoe_id}/*.jpg).
    """
    batch = conn.execute("SELECT id FROM batches WHERE id = ?", (shoe.batch_id,)).fetchone()
    if not batch:
        raise HTTPException(status_code=400, detail=f"Batch '{shoe.batch_id}' does not exist")

    shoe_id   = generate_shoe_id(conn)
    timestamp = datetime.now().isoformat()

    conn.execute(
        """INSERT INTO shoes (
            id, batch_id, operator_id, timestamp,
            img_top, img_left, img_right, img_angle_left, img_angle_right,
            validation_status, validation_errors,
            ai_prediction, ai_confidence, model_version,
            final_decision, human_override,
            review_status, shoe_brand, shoe_color, notes
        ) VALUES (
            ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, 0,
            ?, ?, ?, ?
        )""",
        (
            shoe_id, shoe.batch_id, shoe.operator_id, timestamp,
            shoe.img_top, shoe.img_left, shoe.img_right,
            shoe.img_angle_left, shoe.img_angle_right,
            shoe.validation_status, shoe.validation_errors,
            shoe.ai_prediction, shoe.ai_confidence, shoe.model_version,
            shoe.final_decision,
            shoe.review_status, shoe.shoe_brand, shoe.shoe_color, shoe.notes,
        ),
    )
    conn.execute(
        "UPDATE batches SET shoe_count = shoe_count + 1 WHERE id = ?",
        (shoe.batch_id,),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM shoes WHERE id = ?", (shoe_id,)).fetchone()
    return row_to_dict(row)


@router.patch("/{shoe_id}/decision", summary="Submit a human review decision")
def update_decision(
    shoe_id: str,
    data:    DecisionUpdate,
    conn:    sqlite3.Connection = Depends(get_db),
):
    """
    Submit a human review decision for a shoe.

    Rules:
    - final_decision must be REUSE, RECYCLE, or REVIEW.
    - If final_decision differs from ai_prediction, human_override is set to 1
      and override_reason becomes required.
    - review_status is always set to COMPLETED after submission.
    """
    VALID_DECISIONS = {"REUSE", "RECYCLE", "REVIEW"}

    if data.final_decision not in VALID_DECISIONS:
        raise HTTPException(
            status_code=400,
            detail=f"final_decision must be one of: {', '.join(VALID_DECISIONS)}",
        )

    shoe = conn.execute("SELECT * FROM shoes WHERE id = ?", (shoe_id,)).fetchone()
    if not shoe:
        raise HTTPException(status_code=404, detail=f"Shoe '{shoe_id}' not found")

    # Override = human chose differently from the AI (only when AI produced a result)
    is_override = (
        shoe["ai_prediction"] is not None
        and data.final_decision != shoe["ai_prediction"]
    )

    if is_override and not data.override_reason:
        raise HTTPException(
            status_code=422,
            detail="override_reason is required when the decision differs from the AI prediction",
        )

    conn.execute(
        """UPDATE shoes SET
               final_decision  = ?,
               human_override  = ?,
               override_reason = ?,
               review_status   = 'COMPLETED',
               notes           = ?
           WHERE id = ?""",
        (
            data.final_decision,
            1 if is_override else 0,
            data.override_reason,
            data.notes,
            shoe_id,
        ),
    )
    conn.commit()

    updated = conn.execute("SELECT * FROM shoes WHERE id = ?", (shoe_id,)).fetchone()
    return row_to_dict(updated)
