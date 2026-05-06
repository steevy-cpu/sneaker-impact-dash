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
# Simulation placeholder images — 5 shared JPEGs, served at /sim_images/.
SIM_IMAGES_DIR  = BASE_DIR / "simulation_assets" / "sample_images"
# Frontend static files — HTML pages, CSS, JS, served at /frontend/.
FRONTEND_DIR    = BASE_DIR / "frontend"
