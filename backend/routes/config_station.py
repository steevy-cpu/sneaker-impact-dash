"""
Config API — server-side station config + live camera control (v4l2) + a
read-only integrations status.

  GET/PUT /api/config/station         shared station settings (JSON in app_config)
  GET     /api/config/integrations    read-only: is Airtable / engine configured
  GET     /api/camera/devices         cameras on the station box
  GET     /api/camera/controls        v4l2 controls for a device (sliders)
  POST    /api/camera/control         set one control live
  POST    /api/camera/reset           reset a device's controls to defaults
  GET     /api/camera/resolutions     supported resolutions
"""
import json
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.database import get_db
from backend.services import camera_control as cam
from backend.services import camera_persist

router = APIRouter(prefix="/api", tags=["Config"])

_STATION_KEY = "station"


# --- Station config (shared, server-side) -------------------------------

@router.get("/config/station", summary="Get the station config")
def get_station_config(conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT value FROM app_config WHERE key = ?", (_STATION_KEY,)).fetchone()
    return json.loads(row["value"]) if row else {}


@router.put("/config/station", summary="Save the station config")
def put_station_config(payload: dict = Body(...), conn: sqlite3.Connection = Depends(get_db)):
    conn.execute(
        "INSERT INTO app_config (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (_STATION_KEY, json.dumps(payload), datetime.now().isoformat()),
    )
    conn.commit()
    return {"saved": True, "config": payload}


# --- Integrations status (read-only, no secrets) ------------------------

@router.get("/config/integrations", summary="Integration status (no secrets)")
def get_integrations():
    from backend.services.shipment_lookup import is_configured as lookup_configured
    from backend.services.airtable_sync import sync_enabled
    from backend import config as cfg
    return {
        "airtable": {
            "lookup_configured": lookup_configured(),
            "sync_enabled":      sync_enabled(),
            "base_id_set":       bool(cfg.AIRTABLE_BASE_ID),
            "api_key_set":       bool(cfg.AIRTABLE_API_KEY),
            "shipments_table":   cfg.AIRTABLE_SHIPMENTS_TABLE,
            "sources":           cfg.SHIPMENT_LOOKUP_SOURCES,
        },
        "engine": {
            "enabled":       cfg.ENGINE_ENABLED,
            "ollama_model":  cfg.ENGINE_OLLAMA_MODEL,
            "segment_model": cfg.ENGINE_SEGMENT_MODEL,
            "auto_approve":  cfg.AUTO_APPROVE_CONF,
        },
    }


# --- Camera control (server-side v4l2) ----------------------------------

class ControlSet(BaseModel):
    device: str
    name:   str
    value:  int


@router.get("/camera/devices", summary="Cameras on the station box")
def camera_devices():
    return {"available": cam.available(), "devices": cam.list_devices()}


@router.get("/camera/controls", summary="v4l2 controls for a device")
def camera_controls(device: str = Query(..., description="e.g. /dev/video0")):
    return {"device": device, "available": cam.available(),
            "controls": cam.list_controls(device)}


@router.post("/camera/control", summary="Set one camera control (live)")
def camera_set_control(body: ControlSet):
    if not cam.available():
        raise HTTPException(status_code=503, detail="v4l2-ctl not available on the server")
    ok = cam.set_control(body.device, body.name, body.value)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not set {body.name}={body.value}")
    # Persist so the nightly station power-off can't revert it (the re-apply
    # worker restores saved values each morning).
    camera_persist.save_control(body.device, body.name, body.value)
    return {"ok": True, "device": body.device, "name": body.name, "value": body.value}


@router.post("/camera/reset", summary="Reset a camera's controls to defaults")
def camera_reset(device: str = Body(..., embed=True)):
    if not cam.available():
        raise HTTPException(status_code=503, detail="v4l2-ctl not available on the server")
    camera_persist.clear_saved(device)
    return {"reset": cam.reset_controls(device)}


@router.get("/camera/resolutions", summary="Supported resolutions for a device")
def camera_resolutions(device: str = Query(...)):
    return {"device": device, "resolutions": cam.list_resolutions(device)}
