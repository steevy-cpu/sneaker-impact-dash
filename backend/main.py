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

from backend.config import APP_MODE, IMAGES_DIR, SIM_IMAGES_DIR, FRONTEND_DIR
from backend.database import init_db, get_connection
from backend.routes import analytics, batches, export, health, shoes, simulation

# Directories must exist before app.mount() is called (mount happens at import time)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
SIM_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
FRONTEND_DIR.mkdir(parents=True, exist_ok=True)


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

    yield  # --- app is live here ---

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

# ---------------------------------------------------------------------------
# Static file mounts
# These must come BEFORE router includes so FastAPI resolves them first.
# ---------------------------------------------------------------------------

# Real shoe images (actual mode) — served at /images/{batch}/{shoe}/{view}.jpg
app.mount("/images",     StaticFiles(directory=str(IMAGES_DIR)),    name="images")

# Simulation placeholder images — served at /sim_images/{view}.jpg
app.mount("/sim_images", StaticFiles(directory=str(SIM_IMAGES_DIR)), name="sim_images")

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


# ---------------------------------------------------------------------------
# Root — redirect to the dashboard UI
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/frontend/")
