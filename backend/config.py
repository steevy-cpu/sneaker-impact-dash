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
# (8.3.x → the YOLO11-based yoloe-11* family; the engine's own default
# "yoloe-26s-seg.pt" needs a newer ultralytics / a pre-placed weight).
# 11m since 2026-06-12: with SEGMENT_CONF=0.10 it scores 0.880 count-recall on
# the hand-counted eval set vs 0.655 for 11s@0.25, at the same ~1.2s/photo
# (tile batching absorbs the bigger model). See engine eval_tiling.py.
ENGINE_SEGMENT_MODEL = os.getenv("ENGINE_SEGMENT_MODEL", "yoloe-11m-seg.pt")
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
# Cloud fallback is gated on MAKE + MODEL only (color rarely hits a high bar and
# isn't worth a cloud call). Color is always kept local, with its own lower floor.
LOCAL_CONF_MIN       = float(os.getenv("LOCAL_CONF_MIN", "0.80"))        # make+model gate
LOCAL_COLOR_CONF_MIN = float(os.getenv("LOCAL_COLOR_CONF_MIN", "0.50"))  # accept local color >= this
CLOUD_BACKEND    = os.getenv("CLOUD_BACKEND", "gemini")        # "gemini" | "openai" | "none"
CLOUD_TIMEOUT    = int(os.getenv("CLOUD_TIMEOUT", "90"))       # per-image cloud call (s).
# 90s not 30: gemini-2.5-pro is a reasoning model and regularly thinks past 30s;
# every timeout silently left that pair on its "unknown" local guess. Only the
# background worker waits on this — no web request ever blocks on a cloud call.
# -- Gemini --
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
# Default to Flash: it has a usable FREE tier and is cheap + capable for shoe ID.
# gemini-2.5-pro is more accurate but has ~no free quota (instant 429) — set it
# explicitly once billing is on.
GEMINI_MODEL     = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL       = os.getenv("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta")
# -- OpenAI --
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL     = os.getenv("OPENAI_MODEL", "gpt-5")
OPENAI_URL       = os.getenv("OPENAI_URL", "https://api.openai.com/v1")
# Cloud fallback is active only when enabled AND the selected backend has a key.
_CLOUD_KEYS = {"gemini": GEMINI_API_KEY, "openai": OPENAI_API_KEY}
CLOUD_IDENTIFY_ENABLED = (
    os.getenv("CLOUD_IDENTIFY_ENABLED", "1") not in ("0", "false", "False")
    and bool(_CLOUD_KEYS.get(CLOUD_BACKEND))
)

# --- Seen-shoe cache (Tier 0: reuse a past identification before paying cloud) -
# Before the cloud fallback, the worker looks up an uncertain pair's DINOv2
# embedding against `shoe_memory` (past resolved shoes). A confident neighbour
# reuses that brand/model for FREE, skipping the Gemini call. Built additive +
# OFF: the lookup only runs when SEEN_SHOE_ENABLED=1, so the live flow is
# unchanged until we've calibrated the threshold offline (via /reidentify).
SEEN_SHOE_ENABLED  = os.getenv("SEEN_SHOE_ENABLED", "0") not in ("0", "false", "False")
# Auto-accept policy. SINGLE nearest-neighbour at cosine >= 0.96, set by the
# 2026-06-24 LIVE re-calibration (queried today's real shoes against the cache):
# single-NN @0.96 = ~24% deflection at ~86% brand precision (the precision
# ceiling). The earlier top-3-agreement idea was an artefact of a stratified
# sample — on the natural live distribution it fired ~0% of the time AND was
# LESS precise (DINOv2's silhouette bias makes dense same-shape/wrong-brand
# clusters). So: top_k=1, min_agree=1. Color guard didn't help either.
SEEN_SHOE_MIN_SIM  = float(os.getenv("SEEN_SHOE_MIN_SIM", "0.96"))
SEEN_SHOE_TOP_K    = int(os.getenv("SEEN_SHOE_TOP_K", "1"))     # neighbours to consult
SEEN_SHOE_MIN_AGREE = int(os.getenv("SEEN_SHOE_MIN_AGREE", "1"))  # must share the brand
# Write-back hygiene: don't store a duplicate when an almost-identical shoe is
# already cached (cosine >= DEDUP_SIM) — bump its sighting count instead, so
# repeated shoes don't bloat the cache and slow the lookup. MAX_ROWS caps the
# cache (evicting the least-seen NON-gold rows first; gold is never evicted) so
# the per-pair lookup stays fast as write-back accumulates over time.
SEEN_SHOE_DEDUP_SIM = float(os.getenv("SEEN_SHOE_DEDUP_SIM", "0.985"))
SEEN_SHOE_MAX_ROWS  = int(os.getenv("SEEN_SHOE_MAX_ROWS", "50000"))

# --- FedEx Track (diagnostic lookup for no_row boxes on the Airtable Sync page) -
# Resolve a scanned FedEx tracking number to shipment details (status, weight,
# delivery, shipper). OAuth2 client-credentials. Sandbox base:
# https://apis-sandbox.fedex.com. Dormant until FEDEX_API_KEY + FEDEX_SECRET set.
FEDEX_API_KEY  = os.getenv("FEDEX_API_KEY", "")
FEDEX_SECRET   = os.getenv("FEDEX_SECRET", "")
FEDEX_ACCOUNT  = os.getenv("FEDEX_ACCOUNT", "")          # not needed for Track, kept for later
FEDEX_BASE_URL = os.getenv("FEDEX_BASE_URL", "https://apis.fedex.com")
FEDEX_TIMEOUT  = int(os.getenv("FEDEX_TIMEOUT", "20"))
FEDEX_ENABLED  = (
    os.getenv("FEDEX_ENABLED", "1") not in ("0", "false", "False")
    and bool(FEDEX_API_KEY and FEDEX_SECRET)
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
