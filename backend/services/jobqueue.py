"""
jobqueue.py — background worker that drains `pending` table photos.

A single daemon thread polls for table_photos in `pending` (with an image),
claims one (-> `processing`), runs the local engine via pipeline.process_table_photo,
writes the resulting `pairs` rows, and marks the photo `completed` (or `failed`
with an error message). Fail-safe per job: one bad photo never stops the worker.

Single worker initially (models load once per subprocess; bounds GPU/VRAM use).
"""
import json
import threading
from datetime import datetime

from backend.config import (ENGINE_ENABLED, ENGINE_POLL_SECONDS, IMAGES_DIR)
from backend.database import get_connection
from backend.services.pipeline import process_table_photo
from backend.utils.id_generator import generate_pair_id


def _image_fs_path(image_url: str):
    """Map a stored image URL ("/images/table_photos/X.jpg") to a filesystem path."""
    rel = image_url.replace("/images/", "", 1)
    return IMAGES_DIR / rel


class EngineWorker:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not ENGINE_ENABLED:
            print("[worker] ENGINE_ENABLED=0 — background processing disabled.")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="engine-worker", daemon=True)
        self._thread.start()
        print("[worker] engine worker started.")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    # -- internals --------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            job = self._claim_one()
            if not job:
                self._stop.wait(ENGINE_POLL_SECONDS)
                continue
            self._process(job)

    def _claim_one(self):
        """Atomically claim the oldest pending photo (pending -> processing)."""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id, image_path FROM table_photos "
                "WHERE status = 'pending' AND image_path IS NOT NULL "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            cur = conn.execute(
                "UPDATE table_photos SET status = 'processing' "
                "WHERE id = ? AND status = 'pending'",
                (row["id"],),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None   # claimed by someone else between SELECT and UPDATE
            return {"id": row["id"], "image_path": row["image_path"]}
        finally:
            conn.close()

    def _process(self, job):
        tp_id = job["id"]
        conn = get_connection()
        try:
            fs_path = _image_fs_path(job["image_path"])
            pairs = process_table_photo(tp_id, str(fs_path))

            now = datetime.now().isoformat()
            for p in pairs:
                pid = generate_pair_id(conn)
                img_file = p.get("image_file")
                img_url = f"/images/pairs/{img_file}" if img_file else None
                conn.execute(
                    """INSERT INTO pairs (
                        id, table_photo_id, image_path, bbox,
                        detected_color, color_confidence,
                        make, make_confidence, model, model_confidence,
                        model_sources, review_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
                    (pid, tp_id, img_url, json.dumps(p.get("bbox")),
                     p.get("detected_color"), p.get("color_confidence"),
                     p.get("make"), p.get("make_confidence"),
                     p.get("model"), p.get("model_confidence"),
                     json.dumps(p.get("model_sources") or []), now),
                )
            conn.execute(
                "UPDATE table_photos SET status = 'completed', num_pairs = ?, "
                "processed_at = ?, error_message = NULL WHERE id = ?",
                (len(pairs), now, tp_id),
            )
            conn.commit()
            print(f"[worker] {tp_id}: {len(pairs)} pair(s) -> completed.")
        except Exception as exc:                       # noqa: BLE001 - never crash worker
            try:
                conn.rollback()
                conn.execute(
                    "UPDATE table_photos SET status = 'failed', error_message = ? WHERE id = ?",
                    (str(exc)[:500], tp_id),
                )
                conn.commit()
            except Exception:                          # noqa: BLE001
                pass
            print(f"[worker] {tp_id}: FAILED — {exc}")
        finally:
            conn.close()


# Singleton used by main.py's lifespan.
worker = EngineWorker()
