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
    if not os.path.isdir(folder):
        return False
    for f in os.listdir(folder):
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, f)) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if d.get("source_photo") == source_photo and d.get("source_pair") == source_pair:
            return True
    return False


def _next_n(folder, color, make) -> int:
    pat = re.compile(rf"shoes_{re.escape(color)}_{re.escape(make)}_(\d+)\.jpg$", re.I)
    mx = 0
    if os.path.isdir(folder):
        for f in os.listdir(folder):
            m = pat.match(f)
            if m:
                mx = max(mx, int(m.group(1)))
    return mx + 1


def export_label(crop_path, *, color, make, model, make_conf, model_conf,
                 source_photo, source_pair):
    """Copy one confident pair crop into label_data + write its JSON sidecar.
    Returns the new filename, or None (skipped / already exported / failed)."""
    folder = str(LABEL_DATA_DIR)
    try:
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
            "filename":         base + ".jpg",
            "make":             make,
            "model":            model,
            "detected_color":   color,
            "make_confidence":  make_conf,
            "model_confidence": model_conf,
            "source_photo":     source_photo,
            "source_pair":      source_pair,
            "timestamp":        datetime.now().isoformat(timespec="seconds"),
            "exported_by":      "dash-auto-approve",
        }
        with open(os.path.join(folder, base + ".json"), "w") as f:
            json.dump(meta, f, indent=2)
        return base + ".jpg"
    except Exception as exc:                           # noqa: BLE001 - fail safe
        print(f"[label_export] failed for {source_photo}/{source_pair}: {exc}")
        return None
