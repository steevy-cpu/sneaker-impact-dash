"""
Pydantic models used by the API for request validation and response shaping.

  *Create  models  — what the client sends in a POST body
  *Response models — what the API returns (used for docs / type safety)
"""
from pydantic import BaseModel, ConfigDict
from typing import Any, List, Optional


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
# Table photos & pairs (new whole-table-photo flow)
# ---------------------------------------------------------------------------

class MetadataCreate(BaseModel):
    """Fast-track box metadata (mirrors the ShoeSort /api/metadata contract)."""
    barcode:             Optional[str]   = None
    weight_of_box:       Optional[float] = None
    total_good_sneakers: int             = 0
    total_end_of_life:   int             = 0
    casuals:             int             = 0
    operator_id:         Optional[str]   = None
    batch_id:            Optional[str]   = None


class TablePhotoResponse(BaseModel):
    id:                  str
    batch_id:            Optional[str]   = None
    operator_id:         Optional[str]   = None
    image_path:          Optional[str]   = None
    barcode:             Optional[str]   = None
    weight_of_box:       Optional[float] = None
    total_good_sneakers: int
    total_end_of_life:   int
    casuals:             int
    status:              str
    error_message:       Optional[str]   = None
    num_pairs:           int
    shipment_info:       Optional[Any]   = None
    created_at:          str
    processed_at:        Optional[str]   = None


class PairResponse(BaseModel):
    # `model_*` fields collide with pydantic v2's reserved namespace; allow them.
    model_config = ConfigDict(protected_namespaces=())

    id:               str
    table_photo_id:   str
    image_path:       Optional[str]   = None
    bbox:             Optional[Any]   = None
    detected_color:   Optional[str]   = None
    color_confidence: Optional[float] = None
    make:             Optional[str]   = None
    make_confidence:  Optional[float] = None
    model:            Optional[str]   = None
    model_confidence: Optional[float] = None
    model_sources:    Optional[Any]   = None
    review_status:    str
    final_make:       Optional[str]   = None
    final_model:      Optional[str]   = None
    notes:            Optional[str]   = None
    created_at:       str


class PairReviewUpdate(BaseModel):
    """Human confirm/override of a pair's make/model (+ optional color and a
    confirmed/corrected flag from the gold-labeling workflow). The extra fields
    default to None so existing callers (the live Pairs Review page) are
    unaffected — nothing about that flow changes."""
    final_make:    Optional[str] = None
    final_model:   Optional[str] = None
    final_color:   Optional[str] = None
    label_action:  Optional[str] = None          # 'confirmed' | 'corrected'
    review_status: str           = "COMPLETED"   # NOT_REQUIRED | PENDING | COMPLETED
    notes:         Optional[str] = None


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
