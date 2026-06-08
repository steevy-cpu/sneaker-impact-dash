import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Project root — one level above this file (backend/)
BASE_DIR = Path(__file__).parent.parent

# --- Mode ---
# "simulation": seeds fake data on startup, uses placeholder images.
# "actual":     accepts real records from the inspection station, serves live images.
# Affects: startup seeding, image URL paths, UI mode badge, System Health banner.
APP_MODE = os.getenv("APP_MODE", "simulation")

# --- Seeding ---
# Number of fake shoe records created when the database is empty (simulation mode only).
# To force a re-seed: delete sneakers.db and restart the server.
SEED_COUNT = int(os.getenv("SEED_COUNT", "50"))

# --- AI model version ---
# Stored on every shoe record (model_version field) and shown on the System Health page.
# Update when you deploy a new model so historical records reflect which model classified them.
MODEL_VERSION = os.getenv("MODEL_VERSION", "v1.0-sim")

# --- Paths ---
# SQLite database — single file auto-created on first startup.
DB_PATH         = BASE_DIR / "sneakers.db"
# Real shoe images — one folder per batch/shoe, served at /images/.
IMAGES_DIR      = BASE_DIR / "images"
# Whole-table photos (new flow) — served at /images/table_photos/<id>.jpg.
TABLE_PHOTOS_DIR = IMAGES_DIR / "table_photos"
# Cropped pair images (new flow) — served at /images/pairs/<id>.jpg.
PAIRS_DIR        = IMAGES_DIR / "pairs"
# Simulation placeholder images — 5 shared JPEGs, served at /sim_images/.
SIM_IMAGES_DIR  = BASE_DIR / "simulation_assets" / "sample_images"
# Frontend static files — HTML pages, CSS, JS, served at /frontend/.
FRONTEND_DIR    = BASE_DIR / "frontend"

# --- Camera control (v4l2) --------------------------------------------------
# In the split deployment the camera lives on the Ubuntu station, NOT on the
# dash server, so v4l2-ctl must run there over SSH. Set CAMERA_HOST to an ssh
# target (e.g. "camerapc1@192.168.1.170", needs passwordless key auth from the
# server) to control the station's camera remotely; leave blank to control a
# camera attached to THIS machine.
CAMERA_HOST     = os.getenv("CAMERA_HOST", "")
CAMERA_SSH_OPTS = os.getenv(
    "CAMERA_SSH_OPTS",
    "-o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new",
)

# --- Local identification engine (sneaker_impact_training submodule) --------
# The background worker (P3) runs the pipeline (segment -> pair -> color ->
# brand -> model) in a SUBPROCESS using the SYSTEM python that has the GPU torch
# stack + ultralytics + CLIP. The dash venv stays lightweight; see backend/services.
ENGINE_ENABLED    = os.getenv("ENGINE_ENABLED", "1") not in ("0", "false", "False")
ENGINE_DIR        = BASE_DIR / "sneaker_impact_training"            # the submodule
ENGINE_RUNNER     = Path(os.getenv("ENGINE_RUNNER", BASE_DIR / "backend" / "services" / "engine_runner.py"))
# System Python with torch(cu128)+ultralytics+clip — NOT the dash venv.
ENGINE_PYTHON       = os.getenv("ENGINE_PYTHON", "/usr/bin/python3")
# Segmentation weights — must be auto-downloadable by the installed ultralytics
# (8.3.x → the YOLO11-based "yoloe-11s-seg.pt"; the engine's own default
# "yoloe-26s-seg.pt" needs a newer ultralytics / a pre-placed weight).
ENGINE_SEGMENT_MODEL = os.getenv("ENGINE_SEGMENT_MODEL", "yoloe-11s-seg.pt")
ENGINE_OLLAMA_MODEL = os.getenv("ENGINE_OLLAMA_MODEL", "qwen2.5vl:32b")
ENGINE_OLLAMA_URL   = os.getenv("ENGINE_OLLAMA_URL", "http://localhost:11434")
ENGINE_MODEL_TIMEOUT = int(os.getenv("ENGINE_MODEL_TIMEOUT", "240"))  # per-pair VLM call
ENGINE_JOB_TIMEOUT   = int(os.getenv("ENGINE_JOB_TIMEOUT", "1800"))   # per whole photo
ENGINE_POLL_SECONDS  = float(os.getenv("ENGINE_POLL_SECONDS", "3"))   # worker poll interval

# Pairs whose make AND model confidence are >= this are auto-approved (review
# not required, final make/model set) and exported to label_data. NOTE: the
# Ollama model confidence is uncalibrated (~0.95 flat), so in practice this
# gate is driven mostly by the make (CLIP) confidence.
AUTO_APPROVE_CONF = float(os.getenv("AUTO_APPROVE_CONF", "0.80"))
# Curated, training-ready subset (shoes_<color>_<make>_<N>.jpg + JSON) that the
# auto-approved pairs are copied into — feeds the engine's catalog/training.
# Defaults into the engine submodule's label_data dir.
LABEL_DATA_DIR = Path(os.getenv("LABEL_DATA_DIR", str(ENGINE_DIR / "label_data")))

# --- Hybrid local -> cloud identification -----------------------------------
# When the LOCAL prediction isn't confident enough (color, make AND model each
# below LOCAL_CONF_MIN, or any is "unknown"), fall back to a CLOUD vision model
# for a better {color, brand, model}. The cloud prediction replaces the local
# one and the crop + prediction are saved to label_data for future training.
LOCAL_CONF_MIN   = float(os.getenv("LOCAL_CONF_MIN", "0.80"))  # keep local only if all 3 >= this
CLOUD_BACKEND    = os.getenv("CLOUD_BACKEND", "gemini")        # "gemini" | "none"
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
# Default to Flash: it has a usable FREE tier and is cheap + capable for shoe ID.
# gemini-2.5-pro is more accurate but has ~no free quota (instant 429) — set it
# explicitly once billing is on.
GEMINI_MODEL     = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL       = os.getenv("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta")
CLOUD_TIMEOUT    = int(os.getenv("CLOUD_TIMEOUT", "30"))       # per-image cloud call (s)
# Cloud fallback is active only when explicitly enabled AND a key is present.
CLOUD_IDENTIFY_ENABLED = (
    os.getenv("CLOUD_IDENTIFY_ENABLED", "1") not in ("0", "false", "False")
    and CLOUD_BACKEND != "none" and bool(GEMINI_API_KEY)
)

# --- Shipment lookup (barcode -> shipment/order info) -----------------------
# Pluggable: resolve a scanned barcode (often a FedEx tracking #) to its
# shipment record. "airtable" matches against the company's Airtable (populated
# from FedEx invoice exports); "fedex" is a stub for a future live Track API.
# Comma-separated order = lookup priority (first hit wins). Env-only, no
# credentials are committed — stays unconfigured (fail-safe) until you set them.
SHIPMENT_LOOKUP_SOURCES  = os.getenv("SHIPMENT_LOOKUP_SOURCES", "airtable")
SHIPMENT_CACHE_TTL       = int(os.getenv("SHIPMENT_CACHE_TTL", "300"))   # seconds
SHIPMENT_BARCODE_TRIM    = int(os.getenv("SHIPMENT_BARCODE_TRIM", "12")) # match last N digits; 0 = full
AIRTABLE_API_KEY         = os.getenv("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID         = os.getenv("AIRTABLE_BASE_ID", "")
AIRTABLE_SHIPMENTS_TABLE = os.getenv("AIRTABLE_SHIPMENTS_TABLE", "Shipments Received")
AIRTABLE_PARTNERS_TABLE  = os.getenv("AIRTABLE_PARTNERS_TABLE", "Partners")
# Outbound 2-stage write sync (dash -> Airtable: box metadata on capture, brand
# summary on completion). Dormant until AIRTABLE_API_KEY + AIRTABLE_BASE_ID are
# set (then auto-activates on restart). Set to 0 to keep reads but disable writes.
AIRTABLE_SYNC_ENABLED = os.getenv("AIRTABLE_SYNC_ENABLED", "1") not in ("0", "false", "False")
# Durable outbox: every capture's box data + brand summary is saved locally and
# retried until its shipment exists in Airtable and the update lands (so data is
# never lost if the shipment is imported AFTER it was scanned). How often the
# background retry worker re-attempts pending rows.
OUTBOX_RETRY_SECONDS = int(os.getenv("OUTBOX_RETRY_SECONDS", "300"))  # 5 min
