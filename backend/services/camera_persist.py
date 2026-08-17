"""
camera_persist.py — make camera control settings survive the station's nightly
power-off.

v4l2 controls (brightness, contrast, …) live only in the USB camera's volatile
memory; camerapc1 boots fresh every morning (~08:08) and the camera re-enumerates
with hardware defaults (brightness=32). So: every control set through the config
page is also saved here (app_config key `camera_ctrls:<device>`), and a
background thread periodically re-applies any saved control that drifted from
its saved value. The dash server stays up around the clock, so it is the one
place a re-applier can live.

Fail-safe like camera_control itself: the station being off/unreachable just
means list_controls() returns [] and the cycle is skipped quietly.
"""
import json
import threading
from datetime import datetime

from backend.config import CAMERA_REAPPLY_SECONDS
from backend.database import get_connection
from backend.services import camera_control as cam

_KEY_PREFIX = "camera_ctrls:"


def save_control(device, name, value):
    """Record one control value as the desired setting for `device`."""
    conn = get_connection()
    try:
        key = _KEY_PREFIX + device
        row = conn.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
        ctrls = json.loads(row["value"]) if row else {}
        ctrls[name] = int(value)
        conn.execute(
            "INSERT INTO app_config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(ctrls), datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def clear_saved(device):
    """Forget saved settings for `device` (used by reset-to-defaults)."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM app_config WHERE key = ?", (_KEY_PREFIX + device,))
        conn.commit()
    finally:
        conn.close()


def all_saved():
    """{device: {control: value}} for every device with saved settings."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT key, value FROM app_config WHERE key LIKE ?",
            (_KEY_PREFIX + "%",),
        ).fetchall()
        return {r["key"][len(_KEY_PREFIX):]: json.loads(r["value"]) for r in rows}
    finally:
        conn.close()


def reapply_all():
    """One pass: re-apply saved controls that drifted. Returns count re-applied.
    A control the camera currently reports as inactive (e.g. manual white
    balance while auto is on) fails set_control harmlessly and is skipped."""
    applied = 0
    for device, saved in all_saved().items():
        current = {c["name"]: c["value"] for c in cam.list_controls(device)}
        if not current:            # station off / camera unplugged — try later
            continue
        for name, value in saved.items():
            if name in current and current[name] != value:
                if cam.set_control(device, name, value):
                    applied += 1
                    print(f"[camera] re-applied {name}={value} on {device} "
                          f"(was {current[name]})", flush=True)
    return applied


class _Reapplier:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if CAMERA_REAPPLY_SECONDS <= 0 or not cam.available():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="camera-reapply", daemon=True)
        self._thread.start()
        print(f"[camera] settings re-apply worker started "
              f"(every {CAMERA_REAPPLY_SECONDS}s).", flush=True)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self):
        # Apply once shortly after startup (catches a dash restart that
        # happened while settings had already drifted), then on the interval.
        while not self._stop.is_set():
            try:
                reapply_all()
            except Exception as exc:                   # noqa: BLE001 - never die
                print(f"[camera] re-apply error: {exc}", flush=True)
            self._stop.wait(CAMERA_REAPPLY_SECONDS)


reapplier = _Reapplier()
