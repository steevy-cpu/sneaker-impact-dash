"""
simulation.py — Simulation control endpoints.

  POST /api/simulation/generate  — create N fake shoe inspections with a chosen condition
  POST /api/simulation/reset     — wipe all data and re-seed from scratch

Both endpoints are guarded: they return 400 in actual mode.
"""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.config import APP_MODE
from backend.database import get_db
from backend.services.simulation import BATCH_CONDITIONS, generate_shoes, reset_simulation

router = APIRouter(prefix="/api/simulation", tags=["Simulation"])

VALID_CONDITIONS = set(BATCH_CONDITIONS.keys())


class GenerateRequest(BaseModel):
    count: int = Field(1, ge=1, le=50, description="Number of shoes to generate (1–50)")
    condition: str = Field("normal_mix", description="Batch condition profile")


@router.post("/generate", summary="Generate fake shoe inspections")
def generate(req: GenerateRequest, conn: sqlite3.Connection = Depends(get_db)):
    """
    Adds `count` new shoe inspection records using the chosen condition profile.
    Records are timestamped within the last 10 minutes so they appear at the
    top of the Live Feed.
    """
    if APP_MODE != "simulation":
        raise HTTPException(400, "Only available in simulation mode")
    if req.condition not in VALID_CONDITIONS:
        raise HTTPException(400, f"Invalid condition. Valid options: {sorted(VALID_CONDITIONS)}")

    generated = generate_shoes(conn, req.count, req.condition)
    total = conn.execute("SELECT COUNT(*) FROM shoes").fetchone()[0]
    return {"generated": generated, "total": total}


@router.post("/reset", summary="Wipe all data and re-seed")
def reset(conn: sqlite3.Connection = Depends(get_db)):
    """
    Deletes every shoe, batch, and system event, then calls seed_database()
    to restore the default 50-record starting state across 3 batches.
    """
    if APP_MODE != "simulation":
        raise HTTPException(400, "Only available in simulation mode")

    total = reset_simulation(conn)
    return {"message": "Simulation reset complete", "seeded": total}
