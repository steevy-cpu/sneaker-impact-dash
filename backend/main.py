"""
Sneaker Impact Dashboard — FastAPI backend entry point.

Startup sequence:
  1. Create image storage directories (required before app.mount)
  2. Mount static file directories (/images, /sim_images)
  3. Register API routers
  4. Lifespan: create DB tables, seed simulation data

Run:  uvicorn backend.main:app --reload
Docs: http://localhost:8000/docs
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.config import (APP_MODE, IMAGES_DIR, SIM_IMAGES_DIR, FRONTEND_DIR,
                            TABLE_PHOTOS_DIR, PAIRS_DIR, LABEL_DATA_DIR)
from backend.database import init_db, get_connection
from backend.routes import (airtable_outbox, analytics, batches, capture,
                            config_station, export, health, label_data, labeling,
                            pairs, public_crops, reidentify, shipment, shoes,
                            simulation, tableau)

# Directories must exist before app.mount() is called (mount happens at import time)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
SIM_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
FRONTEND_DIR.mkdir(parents=True, exist_ok=True)
# New table-photo-flow image dirs (served under the existing /images mount)
TABLE_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
PAIRS_DIR.mkdir(parents=True, exist_ok=True)
# Curated auto-approved training set (lives in the engine submodule by default);
# create it so the /label_images static mount below always has a directory.
LABEL_DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"\n{'=' * 52}")
    print(f"  Sneaker Impact Dashboard  —  {APP_MODE.upper()} MODE")
    print(f"{'=' * 52}\n")

    # 1. Create DB schema
    init_db()

    # 2. Simulation-only setup
    if APP_MODE == "simulation":
        from backend.services.simulation import create_placeholder_images, seed_database
        create_placeholder_images()
        conn = get_connection()
        seed_database(conn)
        conn.close()

    # 3. Start the background engine worker (drains 'pending' table photos)
    from backend.services.jobqueue import worker
    worker.start()

    # 4. Start the Airtable outbox retry worker (delivers queued box data once
    #    its shipment appears in Airtable). Cheap no-op while writes are off.
    from backend.services.airtable_outbox import retrier
    retrier.start()

    yield  # --- app is live here ---

    worker.stop()
    retrier.stop()
    print("\nShutting down cleanly.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Sneaker Impact Dashboard API",
    description=(
        "Backend for the shoe inspection and AI classification dashboard.\n\n"
        "Switch between **simulation** and **actual** mode via the `APP_MODE` "
        "environment variable in `.env`."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Allow all origins so the frontend (opened as a local HTML file or on a
# different port) can call the API without CORS errors.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# HTML pages have no "?v=" cache-buster in their URL (unlike the JS/CSS),
# so StaticFiles' ETag-only response lets browsers heuristically serve a
# stale page — e.g. an old nav bar missing a newly added link. Force HTML
# to always revalidate (304 when unchanged, fresh content when not).
#
# Pair crops have the SAME problem: a reprocess OVERWRITES the crop in place
# (same filename TBL-..._N.jpg), so the browser keeps showing the old cached
# image (e.g. before the background-whitening change). Force those to revalidate
# too — StaticFiles still sends ETag/Last-Modified, so it's a cheap 304 when
# unchanged and the fresh crop after a reprocess. Shoe images under /images are
# write-once, so they stay fully cacheable (only /images/pairs/ is overwritten).
@app.middleware("http")
async def revalidate_html(request, call_next):
    response = await call_next(request)
    if (response.headers.get("content-type", "").startswith("text/html")
            or request.url.path.startswith("/images/pairs/")):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# Static file mounts
# These must come BEFORE router includes so FastAPI resolves them first.
# ---------------------------------------------------------------------------

# Real shoe images (actual mode) — served at /images/{batch}/{shoe}/{view}.jpg
app.mount("/images",     StaticFiles(directory=str(IMAGES_DIR)),    name="images")

# Simulation placeholder images — served at /sim_images/{view}.jpg
app.mount("/sim_images", StaticFiles(directory=str(SIM_IMAGES_DIR)), name="sim_images")

# Curated label_data crops — served at /label_images/shoes_<color>_<make>_<N>.jpg
app.mount("/label_images", StaticFiles(directory=str(LABEL_DATA_DIR)), name="label_images")

# Frontend pages — served at /frontend/index.html etc.
# html=True makes /frontend/ serve index.html automatically
app.mount("/frontend", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

# ---------------------------------------------------------------------------
# API routers
# ---------------------------------------------------------------------------

app.include_router(shoes.router)
app.include_router(batches.router)
app.include_router(health.router)
app.include_router(analytics.router)
app.include_router(simulation.router)
app.include_router(export.router)
app.include_router(capture.router)   # new table-photo flow: /api/capture, /api/metadata, /api/table-photos
app.include_router(pairs.router)     # new table-photo flow: /api/pairs
app.include_router(shipment.router)  # barcode -> shipment lookup: /api/shipment/{barcode}
app.include_router(config_station.router)  # config tab + v4l2 camera control: /api/config/*, /api/camera/*
app.include_router(label_data.router)      # curated training set browser: /api/label-data
app.include_router(airtable_outbox.router) # durable send-when-available queue: /api/airtable-outbox
app.include_router(reidentify.router)      # backfill brand+model for unknown pairs: /api/reidentify
app.include_router(tableau.router)         # dataset/AI/sync visualizations: /api/tableau/stats
app.include_router(labeling.router)        # multi-worker table claiming: /api/labeling/*
app.include_router(public_crops.router)     # signed public crop serving (Lens): /public/crop/*


# ---------------------------------------------------------------------------
# Root — redirect to the dashboard UI
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    # Land on Tableau (the Dashboard nav was retired). /frontend/ still serves
    # index.html directly if anyone hits it.
    return RedirectResponse(url="/frontend/tableau.html")
