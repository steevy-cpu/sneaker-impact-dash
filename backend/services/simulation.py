"""
Simulation mode helpers.

Two responsibilities:
  1. create_placeholder_images() — generate simple colored JPEG files so
     the frontend has something to display in simulation mode.
  2. seed_database()             — populate the database with realistic
     fake shoe inspection records on first startup.
"""
import random
import sqlite3
from datetime import datetime, timedelta
from typing import Tuple

from backend.config import SEED_COUNT, MODEL_VERSION, SIM_IMAGES_DIR
from backend.utils.id_generator import generate_shoe_id, generate_batch_id
from backend.utils.image_utils import get_simulation_image_paths


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPERATORS = ["OP-001", "OP-002", "OP-003", "OP-004", "OP-005"]

BRANDS = [
    "Nike", "Adidas", "Puma", "New Balance", "ASICS",
    "Reebok", "Converse", "Vans", "Under Armour", "Skechers",
]

# Small additive offset applied to the picked confidence score (clamped to [0.35, 0.99]).
# Keeps brand a minor realism signal, not a dominant factor.
BRAND_CONFIDENCE_MODIFIERS = {
    "ASICS":        +0.03,
    "New Balance":  +0.02,
    "Nike":         +0.01,
    "Adidas":       +0.01,
    "Skechers":     -0.02,
    "Vans":         -0.02,
    "Converse":     -0.01,
}

# AI prediction weights and the realistic confidence range for each class
PREDICTIONS = {
    "REUSE":   {"weight": 0.50, "confidence_range": (0.75, 0.98)},
    "RECYCLE": {"weight": 0.30, "confidence_range": (0.70, 0.95)},
    "REVIEW":  {"weight": 0.20, "confidence_range": (0.45, 0.72)},
}

# Batch condition profiles for on-demand generation.
# Each profile has a predictions dict (weights + confidence ranges per class)
# and capture quality rates (failed + partial). VALID rate = 1 - failed - partial.
BATCH_CONDITIONS = {
    "normal_mix": {
        "label": "Normal Mix",
        "predictions": {
            "REUSE":   {"weight": 0.50, "confidence_range": (0.75, 0.98)},
            "RECYCLE": {"weight": 0.30, "confidence_range": (0.70, 0.95)},
            "REVIEW":  {"weight": 0.20, "confidence_range": (0.45, 0.72)},
        },
        "failed_capture_rate": 0.08,
        "partial_rate": 0.07,
    },
    "mostly_reusable": {
        "label": "Mostly Reusable",
        "predictions": {
            "REUSE":   {"weight": 0.78, "confidence_range": (0.82, 0.98)},
            "RECYCLE": {"weight": 0.12, "confidence_range": (0.70, 0.88)},
            "REVIEW":  {"weight": 0.10, "confidence_range": (0.50, 0.72)},
        },
        "failed_capture_rate": 0.05,
        "partial_rate": 0.05,
    },
    "mostly_damaged": {
        "label": "Mostly Damaged",
        "predictions": {
            "REUSE":   {"weight": 0.10, "confidence_range": (0.75, 0.92)},
            "RECYCLE": {"weight": 0.72, "confidence_range": (0.76, 0.96)},
            "REVIEW":  {"weight": 0.18, "confidence_range": (0.45, 0.70)},
        },
        "failed_capture_rate": 0.08,
        "partial_rate": 0.07,
    },
    "high_review": {
        "label": "High Review Volume",
        # Low confidence across the board drives more REVIEW flags
        "predictions": {
            "REUSE":   {"weight": 0.22, "confidence_range": (0.50, 0.72)},
            "RECYCLE": {"weight": 0.22, "confidence_range": (0.50, 0.72)},
            "REVIEW":  {"weight": 0.56, "confidence_range": (0.38, 0.65)},
        },
        "failed_capture_rate": 0.08,
        "partial_rate": 0.07,
    },
    "high_failed_capture": {
        "label": "High Failed Capture",
        "predictions": {
            "REUSE":   {"weight": 0.50, "confidence_range": (0.75, 0.98)},
            "RECYCLE": {"weight": 0.30, "confidence_range": (0.70, 0.95)},
            "REVIEW":  {"weight": 0.20, "confidence_range": (0.45, 0.72)},
        },
        "failed_capture_rate": 0.45,
        "partial_rate": 0.15,
    },
}

VALIDATION_FAILURE_MESSAGES = [
    "Camera timeout on right view",
    "Image too dark: top view",
    "No shoe detected in frame",
    "Motion blur detected on left view",
    "Low brightness on angle_left view",
]


# ---------------------------------------------------------------------------
# Placeholder image generation
# ---------------------------------------------------------------------------

def create_placeholder_images():
    """
    Use Pillow to create simple colored JPEG placeholder images.

    Each view gets a distinct color so it's easy to tell them apart in the UI.
    Images are written to simulation_assets/sample_images/ and served at /sim_images/.
    Skips any file that already exists.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("WARNING: Pillow is not installed. Placeholder images were not created.")
        print("         Run:  pip install Pillow")
        return

    SIM_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    views = {
        "top.jpg":         ((70, 130, 180),  "TOP VIEW"),        # steel blue
        "left.jpg":        ((60, 179, 113),  "LEFT VIEW"),       # sea green
        "right.jpg":       ((205, 133, 63),  "RIGHT VIEW"),      # peru brown
        "angle_left.jpg":  ((147, 112, 219), "ANGLE LEFT"),      # medium purple
        "angle_right.jpg": ((210, 105, 30),  "ANGLE RIGHT"),     # chocolate
    }

    for filename, (color, label) in views.items():
        img_path = SIM_IMAGES_DIR / filename
        if img_path.exists():
            continue

        img = Image.new("RGB", (400, 300), color=color)
        draw = ImageDraw.Draw(img)

        # White label text centered in the image
        draw.text((150, 120), label,         fill=(255, 255, 255))
        draw.text((130, 150), "(SIMULATION)", fill=(220, 220, 220))

        img.save(img_path, "JPEG")

    print(f"Placeholder images ready: {SIM_IMAGES_DIR}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _pick_prediction() -> Tuple[str, float]:
    """Randomly select an AI prediction with a matching confidence score."""
    names   = list(PREDICTIONS.keys())
    weights = [PREDICTIONS[p]["weight"] for p in names]
    choice  = random.choices(names, weights=weights, k=1)[0]
    low, high = PREDICTIONS[choice]["confidence_range"]
    return choice, round(random.uniform(low, high), 4)


def _apply_brand_confidence(confidence: float, brand: str) -> float:
    """Nudge confidence slightly based on brand and clamp to [0.35, 0.99]."""
    delta = BRAND_CONFIDENCE_MODIFIERS.get(brand, 0.0)
    return round(min(0.99, max(0.35, confidence + delta * random.uniform(0.5, 1.5))), 4)


def _pick_prediction_for_condition(cond: dict) -> Tuple[str, float]:
    """Select a prediction and confidence score using a BATCH_CONDITIONS entry."""
    preds   = cond["predictions"]
    names   = list(preds.keys())
    weights = [preds[p]["weight"] for p in names]
    choice  = random.choices(names, weights=weights, k=1)[0]
    low, high = preds[choice]["confidence_range"]
    return choice, round(random.uniform(low, high), 4)


def _random_timestamp(base_time: datetime, spread_minutes: int) -> str:
    """Return a random ISO timestamp within spread_minutes before base_time."""
    offset_seconds = random.randint(0, spread_minutes * 60)
    return (base_time - timedelta(seconds=offset_seconds)).isoformat()


# ---------------------------------------------------------------------------
# Database seeder
# ---------------------------------------------------------------------------

def seed_database(conn: sqlite3.Connection):
    """
    Populate the database with fake inspection records for simulation mode.

    Creates 3 batches spread over the last 3 days and distributes SEED_COUNT
    shoes across them. Does nothing if shoes already exist in the database.
    """
    existing = conn.execute("SELECT COUNT(*) FROM shoes").fetchone()[0]
    if existing > 0:
        return  # already seeded — don't add duplicates

    # All simulated shoes point to the same 5 placeholder images.
    # The frontend renders them normally — users just see the same colored
    # rectangle for every shoe, which is fine for testing the UI.
    images = get_simulation_image_paths()

    # --- Create three batches: 2 days ago, yesterday, today ---
    batches = []
    for day_offset in [2, 1, 0]:
        # Batch represents an 8-hour shift ending at 5 pm
        shift_end = datetime.now().replace(hour=17, minute=0, second=0, microsecond=0)
        shift_end -= timedelta(days=day_offset)
        shift_start = shift_end - timedelta(hours=8)

        batch_id    = generate_batch_id(conn)
        operator_id = random.choice(OPERATORS)

        conn.execute(
            "INSERT INTO batches (id, started_at, ended_at, operator_id, shoe_count)"
            " VALUES (?, ?, ?, ?, 0)",
            (
                batch_id,
                shift_start.isoformat(),
                shift_end.isoformat() if day_offset > 0 else None,  # today still open
                operator_id,
            ),
        )
        batches.append((batch_id, shift_end, operator_id))

    conn.commit()

    # --- Distribute shoes evenly across batches ---
    shoes_per_batch = SEED_COUNT // len(batches)
    remainder       = SEED_COUNT % len(batches)

    for idx, (batch_id, shift_end, operator_id) in enumerate(batches):
        count = shoes_per_batch + (1 if idx < remainder else 0)

        for _ in range(count):
            shoe_id   = generate_shoe_id(conn)
            timestamp = _random_timestamp(shift_end, spread_minutes=480)

            # Realistic capture quality distribution
            validation_status = random.choices(
                ["VALID",  "FAILED_CAPTURE", "PARTIAL"],
                weights=[0.85,   0.08,           0.07],
                k=1,
            )[0]

            validation_errors = None
            if validation_status in ("FAILED_CAPTURE", "PARTIAL"):
                validation_errors = random.choice(VALIDATION_FAILURE_MESSAGES)

            brand = random.choice(BRANDS)

            # AI cannot classify a completely failed capture
            if validation_status == "FAILED_CAPTURE":
                ai_prediction = None
                ai_confidence = None
            else:
                ai_prediction, ai_confidence = _pick_prediction()
                ai_confidence = _apply_brand_confidence(ai_confidence, brand)

            # Shoes the AI flags REVIEW need a human decision; all others are self-service.
            review_status  = "PENDING" if ai_prediction == "REVIEW" else "NOT_REQUIRED"
            final_decision = ai_prediction   # no human override in seed data

            conn.execute(
                """INSERT INTO shoes (
                    id, batch_id, operator_id, timestamp,
                    img_top, img_left, img_right, img_angle_left, img_angle_right,
                    validation_status, validation_errors,
                    ai_prediction, ai_confidence, model_version,
                    final_decision, human_override,
                    review_status, shoe_brand, shoe_color
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?,
                    ?, 0,
                    ?, ?, ?
                )""",
                (
                    shoe_id, batch_id, operator_id, timestamp,
                    images["img_top"], images["img_left"], images["img_right"],
                    images["img_angle_left"], images["img_angle_right"],
                    validation_status, validation_errors,
                    ai_prediction, ai_confidence, MODEL_VERSION,
                    final_decision,
                    review_status, brand, "White",
                ),
            )

        # Sync the shoe count on the batch row
        conn.execute(
            "UPDATE batches"
            " SET shoe_count = (SELECT COUNT(*) FROM shoes WHERE batch_id = ?)"
            " WHERE id = ?",
            (batch_id, batch_id),
        )

    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM shoes").fetchone()[0]
    print(f"Seeded {total} shoe records across {len(batches)} batches.")


# ---------------------------------------------------------------------------
# On-demand generation (demo controls)
# ---------------------------------------------------------------------------

def generate_shoes(conn: sqlite3.Connection, count: int, condition: str) -> int:
    """
    Generate `count` new shoe inspection records using the given condition profile.

    Re-uses today's open batch if one exists; otherwise creates a new one.
    Timestamps are spread within the last 10 minutes so the records appear
    at the top of the Live Feed immediately after generation.
    """
    cond         = BATCH_CONDITIONS.get(condition, BATCH_CONDITIONS["normal_mix"])
    failed_rate  = cond["failed_capture_rate"]
    partial_rate = cond["partial_rate"]
    valid_rate   = 1.0 - failed_rate - partial_rate

    images = get_simulation_image_paths()
    now    = datetime.now()

    # Re-use today's batch or create a new one
    existing = conn.execute(
        "SELECT id, operator_id FROM batches"
        " WHERE date(started_at) = date('now') ORDER BY started_at DESC LIMIT 1"
    ).fetchone()

    if existing:
        batch_id    = existing["id"]
        operator_id = existing["operator_id"]
    else:
        batch_id    = generate_batch_id(conn)
        operator_id = random.choice(OPERATORS)
        conn.execute(
            "INSERT INTO batches (id, started_at, ended_at, operator_id, shoe_count)"
            " VALUES (?, ?, NULL, ?, 0)",
            (batch_id, now.isoformat(), operator_id),
        )

    generated = 0
    for _ in range(count):
        shoe_id   = generate_shoe_id(conn)
        # Spread within the last 10 minutes so records appear recent
        timestamp = (now - timedelta(seconds=random.randint(0, 600))).isoformat()

        brand = random.choice(BRANDS)

        validation_status = random.choices(
            ["VALID",  "FAILED_CAPTURE", "PARTIAL"],
            weights=[valid_rate, failed_rate, partial_rate],
            k=1,
        )[0]

        validation_errors = None
        if validation_status in ("FAILED_CAPTURE", "PARTIAL"):
            validation_errors = random.choice(VALIDATION_FAILURE_MESSAGES)

        if validation_status == "FAILED_CAPTURE":
            ai_prediction = None
            ai_confidence = None
        else:
            ai_prediction, ai_confidence = _pick_prediction_for_condition(cond)
            ai_confidence = _apply_brand_confidence(ai_confidence, brand)

        review_status  = "PENDING" if ai_prediction == "REVIEW" else "NOT_REQUIRED"
        final_decision = ai_prediction

        conn.execute(
            """INSERT INTO shoes (
                id, batch_id, operator_id, timestamp,
                img_top, img_left, img_right, img_angle_left, img_angle_right,
                validation_status, validation_errors,
                ai_prediction, ai_confidence, model_version,
                final_decision, human_override, review_status, shoe_brand, shoe_color
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                shoe_id, batch_id, operator_id, timestamp,
                images["img_top"], images["img_left"], images["img_right"],
                images["img_angle_left"], images["img_angle_right"],
                validation_status, validation_errors,
                ai_prediction, ai_confidence, MODEL_VERSION,
                final_decision, review_status, brand, "White",
            ),
        )
        generated += 1

    conn.execute(
        "UPDATE batches"
        " SET shoe_count = (SELECT COUNT(*) FROM shoes WHERE batch_id = ?)"
        " WHERE id = ?",
        (batch_id, batch_id),
    )
    conn.commit()
    return generated


def reset_simulation(conn: sqlite3.Connection) -> int:
    """
    Wipe all shoes, batches, and system events, then re-seed with fresh data.
    Returns the number of records seeded.
    """
    conn.execute("DELETE FROM shoes")
    conn.execute("DELETE FROM batches")
    conn.execute("DELETE FROM system_events")
    conn.commit()
    seed_database(conn)
    return conn.execute("SELECT COUNT(*) FROM shoes").fetchone()[0]
