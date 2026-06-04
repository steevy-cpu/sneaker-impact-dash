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
