"""
reidentify.py — backfill pipeline: give EVERY pair a brand + model (no 'unknown')
and re-push the corrected brand summary to Airtable.

A box may hold e.g. 21 pairs but only show a few brands because the local
pipeline returned 'unknown' for the rest. This re-runs the CLOUD identifier
(prompted to always commit to a best guess + confidence) on every pair whose
make/model is still unknown, updates the pair, re-exports it to label_data, then
recomputes the brand summary and sends it to Airtable.

Runs in a background daemon thread (cloud calls are slow). Single-flight: one
run at a time; progress is exposed via status().
"""
import json
import threading
from datetime import datetime

from backend.config import PAIRS_DIR, AUTO_APPROVE_CONF
from backend.database import get_connection
from backend.services.airtable_outbox import set_brand_summary, try_one_async
from backend.services.airtable_sync import brand_summary_from_pairs
from backend.services.cloud_identify import cloud_enabled
from backend.services.cloud_identify import identify as cloud_identify
from backend.services.visual_search import lens_titles, visual_search_enabled
from backend.services.label_export import export_label

_lock = threading.Lock()
_state = {"running": False, "scope": None, "total": 0, "done": 0,
          "pairs_fixed": 0, "current": None, "started_at": None, "finished_at": None}


def _is_unknown(v):
    return v is None or str(v).strip().lower() in ("", "unknown")


def status():
    return dict(_state)


def _crop_path(image_path):
    if not image_path:
        return None
    return PAIRS_DIR / image_path.replace("/images/pairs/", "", 1)


def _reidentify_one(conn, tp_id):
    """Fill brand+model for every unknown pair of one table photo via the cloud,
    re-export to label_data, then recompute + push the brand summary. Returns the
    count of pairs fixed."""
    rows = conn.execute("SELECT * FROM pairs WHERE table_photo_id = ?", (tp_id,)).fetchall()
    fixed = 0
    for r in rows:
        if not (_is_unknown(r["make"]) or _is_unknown(r["model"])):
            continue
        crop = _crop_path(r["image_path"])
        if not crop or not crop.exists():
            continue
        # Visual-search tier: Google Lens titles as extra evidence for Gemini.
        titles = lens_titles(str(crop)) if visual_search_enabled() else []
        res = cloud_identify(str(crop), lens_titles=titles or None)
        if not res:
            continue
        if titles:
            res["source"] = res["source"] + "+lens"
        make, mk_c = res["brand"], res["brand_confidence"]
        model, md_c = res["model"], res["model_confidence"]
        # Color stays local unless it too was unknown — then take the cloud's.
        color, color_c = r["detected_color"], r["color_confidence"]
        if _is_unknown(color):
            color, color_c = res["color"], res["color_confidence"]

        confident = (
            not _is_unknown(make) and not _is_unknown(model)
            and isinstance(mk_c, (int, float)) and mk_c >= AUTO_APPROVE_CONF
            and isinstance(md_c, (int, float)) and md_c >= AUTO_APPROVE_CONF
        )
        review_status = "NOT_REQUIRED" if confident else "PENDING"
        conn.execute(
            """UPDATE pairs SET make=?, make_confidence=?, model=?, model_confidence=?,
                   detected_color=?, color_confidence=?, model_sources=?,
                   prediction_source=?, review_status=?, final_make=?, final_model=?
               WHERE id=?""",
            (make, mk_c, model, md_c, color, color_c,
             json.dumps([res["source"]]), res["source"], review_status,
             make if confident else r["final_make"],
             model if confident else r["final_model"], r["id"]),
        )
        fname = (r["image_path"] or "").split("/")[-1]
        if fname:
            export_label(str(PAIRS_DIR / fname), color=color, make=make, model=model,
                         make_conf=mk_c, model_conf=md_c, color_conf=color_c,
                         source_photo=tp_id, source_pair=r["id"],
                         prediction_source=res["source"])
        fixed += 1

    conn.commit()
    # Recompute the brand summary from the (now complete) pairs and push it.
    pairs = [dict(x) for x in conn.execute(
        "SELECT * FROM pairs WHERE table_photo_id = ?", (tp_id,)).fetchall()]
    summary = brand_summary_from_pairs(pairs)
    if summary:
        set_brand_summary(conn, tp_id, summary)
        try_one_async(tp_id)
    return fixed


def _incomplete_table_photos(conn):
    """Table photos that still have at least one unknown-make/model pair."""
    return [r[0] for r in conn.execute(
        """SELECT DISTINCT table_photo_id FROM pairs
           WHERE make IS NULL OR TRIM(make)='' OR LOWER(make)='unknown'
              OR model IS NULL OR TRIM(model)='' OR LOWER(model)='unknown'""")]


def _run(scope, tp_ids):
    conn = get_connection()
    try:
        _state.update(running=True, scope=scope, total=len(tp_ids), done=0,
                      pairs_fixed=0, current=None, finished_at=None,
                      started_at=datetime.now().isoformat())
        for tp in tp_ids:
            _state["current"] = tp
            try:
                _state["pairs_fixed"] += _reidentify_one(conn, tp)
            except Exception as exc:                       # noqa: BLE001 - per-item safe
                print(f"[reidentify] {tp} error: {exc}")
            _state["done"] += 1
        print(f"[reidentify] done: {_state['done']} photo(s), "
              f"{_state['pairs_fixed']} pair(s) re-identified.")
    finally:
        _state.update(running=False, current=None, finished_at=datetime.now().isoformat())
        conn.close()
        _lock.release()


def start(tp_id=None):
    """Begin a background reidentify run — one table photo (tp_id) or all
    incomplete ones. Returns immediately. Single-flight."""
    if not cloud_enabled():
        return {"started": False, "error": "cloud identify is disabled (no cloud API key)"}
    if not _lock.acquire(blocking=False):
        return {"started": False, "error": "a reidentify run is already in progress",
                "status": status()}
    try:
        conn = get_connection()
        try:
            tp_ids = [tp_id] if tp_id else _incomplete_table_photos(conn)
        finally:
            conn.close()
    except Exception:
        _lock.release()
        raise
    threading.Thread(target=_run, args=("one" if tp_id else "all", tp_ids),
                     name="reidentify", daemon=True).start()
    return {"started": True, "count": len(tp_ids)}
