"""
label_export.py — copy auto-approved (confident) pairs into the curated
`label_data` set, mirroring the engine's label_export naming:

    shoes_<color>_<makeLowerCamel>_<N>.jpg   +   .json sidecar

This is the clean, training-ready subset the engine's build_catalog_index /
future training consume. Fail-safe: never raises (a failed export must not break
the worker); de-duplicates by (source_photo, source_pair) so reprocesses don't
pile up.
"""
import json
import os
import re
import shutil
from datetime import datetime

from backend.config import LABEL_DATA_DIR


def _camel(make: str) -> str:
    """'New Balance' -> 'newBalance', 'Nike' -> 'nike'."""
    parts = [p for p in re.split(r"[\s_-]+", (make or "").strip()) if p]
    if not parts:
        return "unknown"
    return parts[0].lower() + "".join(w.capitalize() for w in parts[1:])


def _already_exported(folder, source_photo, source_pair) -> bool:
    """Indexed lookup via label_index (perf review 2026-10-05: this used to
    open and parse all 66k sidecars for EVERY exported pair, inside the live
    table pipeline). The index is refreshed first (one scandir, no reads when
    nothing changed) so a sidecar written by another process is seen too."""
    from backend.database import get_connection
    from backend.services import label_index
    conn = get_connection()
    try:
        label_index.refresh(conn)
        return label_index.already_exported(conn, source_photo, source_pair)
    finally:
        conn.close()


def _next_n(folder, color, make) -> int:
    """Next sequence number for shoes_<color>_<make>_N.jpg from the index
    (one LIKE over ~66k indexed rows) instead of a regex over 132k filenames."""
    from backend.database import get_connection
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT filename FROM label_index WHERE LOWER(filename) LIKE LOWER(?)",
            (f"shoes_{color}_{make}_%",)).fetchall()
    finally:
        conn.close()
    pat = re.compile(rf"shoes_{re.escape(color)}_{re.escape(make)}_(\d+)\.jpg$", re.I)
    mx = 0
    for (f,) in rows:
        m = pat.match(f)
        if m:
            mx = max(mx, int(m.group(1)))
    return mx + 1


def export_label(crop_path, *, color, make, model, make_conf, model_conf,
                 source_photo, source_pair, color_conf=None,
                 prediction_source="local"):
    """Copy one confident pair crop into label_data + write its JSON sidecar.
    `prediction_source` is "local" (auto-approved local prediction) or a cloud
    tag like "cloud:gemini:gemini-2.5-pro". Returns the new filename, or None
    (skipped / already exported / failed)."""
    folder = str(LABEL_DATA_DIR)
    try:
        # label_data is a SHOE training set. Insoles arrive in shoe boxes and get
        # confidently branded by the cloud ("Currex RunPro"), so gate here — the
        # one funnel every export path (worker + reidentify) goes through.
        from backend.utils.brands import is_insole_brand
        if is_insole_brand(make):
            return None
        if not os.path.exists(crop_path):
            return None
        if _already_exported(folder, source_photo, source_pair):
            return None
        os.makedirs(folder, exist_ok=True)
        col = (color or "unknown").lower()
        mk = _camel(make)
        base = f"shoes_{col}_{mk}_{_next_n(folder, col, mk)}"
        shutil.copyfile(crop_path, os.path.join(folder, base + ".jpg"))
        meta = {
            "filename":          base + ".jpg",
            "make":              make,
            "model":             model,
            "detected_color":    color,
            "make_confidence":   make_conf,
            "model_confidence":  model_conf,
            "color_confidence":  color_conf,
            "prediction_source": prediction_source,
            "source_photo":      source_photo,
            "source_pair":       source_pair,
            "timestamp":         datetime.now().isoformat(timespec="seconds"),
            "exported_by":       "dash-cloud" if prediction_source.startswith("cloud") else "dash-auto-approve",
        }
        with open(os.path.join(folder, base + ".json"), "w") as f:
            json.dump(meta, f, indent=2)
        try:                                           # index it now (no rescan needed)
            from backend.database import get_connection
            from backend.services import label_index
            conn = get_connection()
            try:
                label_index.record_export(conn, base + ".jpg", meta)
            finally:
                conn.close()
        except Exception as exc:                       # noqa: BLE001 - the next refresh picks it up
            print(f"[label_export] index update failed (non-fatal): {exc}")
        return base + ".jpg"
    except Exception as exc:                           # noqa: BLE001 - fail safe
        print(f"[label_export] failed for {source_photo}/{source_pair}: {exc}")
        return None
