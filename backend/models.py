"""
Pydantic models used by the API for request validation and response shaping.

  *Create  models  — what the client sends in a POST body
  *Response models — what the API returns (used for docs / type safety)
"""
from pydantic import BaseModel
from typing import Optional


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------

class BatchCreate(BaseModel):
    operator_id: str


class BatchResponse(BaseModel):
    id:          str
    started_at:  str
    ended_at:    Optional[str] = None
    operator_id: str
    shoe_count:  int


# ---------------------------------------------------------------------------
# Shoes
# ---------------------------------------------------------------------------

class ShoeCreate(BaseModel):
    batch_id:          str
    operator_id:       str

    # images — optional because a capture failure may have no images
    img_top:           Optional[str] = None
    img_left:          Optional[str] = None
    img_right:         Optional[str] = None
    img_angle_left:    Optional[str] = None
    img_angle_right:   Optional[str] = None

    # capture quality
    validation_status: str            = "VALID"
    validation_errors: Optional[str]  = None

    # AI output
    ai_prediction:     Optional[str]  = None
    ai_confidence:     Optional[float] = None
    model_version:     Optional[str]  = None

    # final outcome
    final_decision:    Optional[str]  = None

    # review workflow: NOT_REQUIRED | PENDING | COMPLETED
    review_status:     str            = "NOT_REQUIRED"

    shoe_brand:        Optional[str]  = None
    shoe_color:        Optional[str]  = None
    notes:             Optional[str]  = None


class ShoeResponse(BaseModel):
    id:                str
    batch_id:          str
    operator_id:       str
    timestamp:         str

    img_top:           Optional[str]  = None
    img_left:          Optional[str]  = None
    img_right:         Optional[str]  = None
    img_angle_left:    Optional[str]  = None
    img_angle_right:   Optional[str]  = None

    validation_status: str
    validation_errors: Optional[str]  = None

    ai_prediction:     Optional[str]  = None
    ai_confidence:     Optional[float] = None
    model_version:     Optional[str]  = None

    final_decision:    Optional[str]  = None
    human_override:    bool
    override_reason:   Optional[str]  = None

    review_status:     str
    shoe_brand:        Optional[str]  = None
    shoe_color:        Optional[str]  = None
    notes:             Optional[str]  = None


# ---------------------------------------------------------------------------
# Human decision override
# ---------------------------------------------------------------------------

class DecisionUpdate(BaseModel):
    final_decision:  str                    # REUSE | RECYCLE | REVIEW
    override_reason: Optional[str] = None  # required when overriding AI
    notes:           Optional[str] = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    mode:                    str
    status:                  str
    db_connected:            bool
    shoe_count:              int
    batch_count:             int
    last_capture_time:       Optional[str] = None
    validation_error_count:  int
    model_version:           str
    storage_usage_mb:        float
