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
import time
from datetime import datetime, timedelta

from backend.config import (ENGINE_ENABLED, ENGINE_JOB_TIMEOUT,
                            ENGINE_POLL_SECONDS, IMAGES_DIR,
                            PAIRS_DIR, AUTO_APPROVE_CONF, LOCAL_CONF_MIN,
                            LOCAL_COLOR_CONF_MIN, SEEN_SHOE_ENABLED,
                            SEEN_SHOE_MIN_SIM, SEEN_SHOE_TOP_K, SEEN_SHOE_MIN_AGREE,
                            SEEN_SHOE_DEDUP_SIM, SEEN_SHOE_MAX_ROWS)
from backend.database import get_connection
from backend.services import shoe_memory
from backend.services.cloud_identify import cloud_enabled
from backend.services.cloud_identify import identify as cloud_identify
from backend.services.label_export import export_label
from backend.services.pipeline import process_table_photo
from backend.utils.id_generator import generate_pair_id


def _known(name, conf, threshold):
    """True when a prediction is a real (non-'unknown') label at >= threshold."""
    return (bool(name) and str(name).lower() != "unknown"
            and isinstance(conf, (int, float)) and conf >= threshold)


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
        last_sweep = 0.0
        while not self._stop.is_set():
            # Claiming must never kill the thread (e.g. a transient
            # "database is locked"): one dead worker silently orphans every
            # job it would have run, with nothing in the journal.
            try:
                if self._paused():
                    self._stop.wait(ENGINE_POLL_SECONDS)
                    continue
                if time.monotonic() - last_sweep > 300:
                    self._recover_stale()
                    last_sweep = time.monotonic()
                job = self._claim_one()
            except Exception as exc:                   # noqa: BLE001
                print(f"[worker] claim/sweep error: {exc}", flush=True)
                self._stop.wait(ENGINE_POLL_SECONDS)
                continue
            if not job:
                self._stop.wait(ENGINE_POLL_SECONDS)
                continue
            self._process(job)

    def _paused(self):
        """Operational pause switch — lets maintenance (model swaps, GPU work)
        stop NEW claims without touching captures or in-flight jobs:
            UPDATE app_config SET value='\"1\"' WHERE key='worker_paused'
        (any value other than "1"/missing = running). Checked every poll."""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT value FROM app_config WHERE key = 'worker_paused'"
            ).fetchone()
            return bool(row) and json.loads(row["value"]) in ("1", 1, True)
        except Exception:                              # noqa: BLE001 - fail open
            return False
        finally:
            conn.close()

    def _recover_stale(self):
        """Re-queue 'processing' rows whose claim is older than the engine
        timeout — orphans left by a service restart mid-job or a worker that
        died without marking the job failed. claimed_at IS NULL covers rows
        claimed before that column existed. Resetting to 'pending' is safe:
        pairs are only committed at the end of a successful run, so an
        orphaned job has no pair rows to duplicate."""
        conn = get_connection()
        try:
            cutoff = (datetime.now()
                      - timedelta(seconds=ENGINE_JOB_TIMEOUT + 120)).isoformat()
            cur = conn.execute(
                "UPDATE table_photos SET status = 'pending', claimed_at = NULL "
                "WHERE status = 'processing' "
                "AND (claimed_at IS NULL OR claimed_at < ?)",
                (cutoff,),
            )
            conn.commit()
            if cur.rowcount:
                print(f"[worker] re-queued {cur.rowcount} stale "
                      f"'processing' job(s).", flush=True)
        finally:
            conn.close()

    def _claim_one(self):
        """Atomically claim the oldest pending photo (pending -> processing)."""
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT id, image_path, barcode FROM table_photos "
                "WHERE status = 'pending' AND image_path IS NOT NULL "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            # SERIALIZE GPU work across BOTH service workers (http + https): claim
            # ONLY when nothing else is already processing. Two concurrent engine
            # pipelines (each YOLOE seg + DINOv2 embed + the ollama VLM) over-
            # subscribe the 16GB GPU and reliably WEDGE ollama -> 1800s timeouts
            # (observed 2026-06-24). The `NOT EXISTS` makes this atomic under
            # SQLite's write lock: whichever worker gets the lock first claims,
            # the other sees a processing row and backs off. One-at-a-time is
            # slower but reliable; the stale sweeper still recovers a true hang.
            cur = conn.execute(
                "UPDATE table_photos SET status = 'processing', claimed_at = ? "
                "WHERE id = ? AND status = 'pending' "
                "AND NOT EXISTS (SELECT 1 FROM table_photos WHERE status = 'processing')",
                (datetime.now().isoformat(), row["id"]),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None   # another worker is busy, or claimed this row first
            return {"id": row["id"], "image_path": row["image_path"],
                    "barcode": row["barcode"]}
        finally:
            conn.close()

    def _process(self, job):
        tp_id = job["id"]
        conn = get_connection()
        try:
            fs_path = _image_fs_path(job["image_path"])
            pairs = process_table_photo(tp_id, str(fs_path))

            # Phase 1 — compute everything WITHOUT touching the DB. Cloud
            # identify calls take seconds each; doing them inside a write
            # transaction held the lock for minutes and starved the other
            # service's worker ("database is locked"). All writes happen in
            # one short transaction at the end.
            now = datetime.now().isoformat()
            approved = 0
            prepared = []
            for idx, p in enumerate(pairs, 1):
                # Heartbeat: the cloud phase can legitimately run long on a
                # big photo (90s timeout x retries x pairs can exceed the
                # stale sweeper's cutoff). Refreshing the claim per pair keeps
                # the sweeper from re-queueing a job that is still alive —
                # a single instant autocommit write, no transaction held.
                conn.execute(
                    "UPDATE table_photos SET claimed_at = ? WHERE id = ?",
                    (datetime.now().isoformat(), tp_id),
                )
                conn.commit()
                img_file = p.get("image_file")
                img_url = f"/images/pairs/{img_file}" if img_file else None
                color, color_c = p.get("detected_color"), p.get("color_confidence")
                make, mk_c = p.get("make"), p.get("make_confidence")
                model, md_c = p.get("model"), p.get("model_confidence")
                sources = p.get("model_sources") or []
                source = "local"

                # HYBRID: keep the local prediction only if MAKE and MODEL are
                # both confident (>= LOCAL_CONF_MIN) and not "unknown". Color is
                # NOT part of this gate — it's always taken locally (below).
                # Otherwise ask the cloud model for a better brand + model.
                local_good = (
                    _known(make, mk_c, LOCAL_CONF_MIN)
                    and _known(model, md_c, LOCAL_CONF_MIN)
                )

                # TIER 0 — seen-shoe cache: before paying for a cloud call, look
                # up this crop's appearance embedding against shoes we've already
                # identified. Conservative auto-accept (top-K must agree on
                # brand); a hit reuses the label for free and skips the cloud.
                # Fail-safe + flag-gated: any error or miss just proceeds to cloud.
                from_cache = False
                emb = p.get("embedding")
                emb_name = p.get("embedder")
                if (not local_good) and SEEN_SHOE_ENABLED and emb and emb_name:
                    try:
                        hit = shoe_memory.lookup(
                            conn, emb, embedder=emb_name, min_sim=SEEN_SHOE_MIN_SIM,
                            top_k=SEEN_SHOE_TOP_K, min_agree=SEEN_SHOE_MIN_AGREE,
                            normalize=False)
                    except Exception as exc:           # noqa: BLE001 - never fail a job
                        hit = None
                        print(f"[worker] {tp_id} pair {idx}: cache lookup error: {exc}",
                              flush=True)
                    if hit and _known(hit.get("brand"), 1.0, 0.0):
                        make, model = hit["brand"], hit.get("model")
                        sim = float(hit.get("similarity") or SEEN_SHOE_MIN_SIM)
                        mk_c = md_c = sim          # top-K-agree hit -> treat as confident
                        source = f"cache:{hit.get('source')}:{sim}"
                        sources = [source]
                        p["make"], p["model"] = make, model
                        from_cache = True
                        print(f"[worker] {tp_id} pair {idx}: cache hit -> {make}/{model} "
                              f"(sim {sim}, agree {hit.get('agree')}/{hit.get('k')})")

                if not local_good and not from_cache and img_file and cloud_enabled():
                    cloud = cloud_identify(str(PAIRS_DIR / img_file))
                    if cloud:
                        # Color stays local; the cloud only supplies brand + model.
                        make,  mk_c  = cloud["brand"], cloud["brand_confidence"]
                        model, md_c  = cloud["model"], cloud["model_confidence"]
                        sources = [cloud["source"]]
                        source = cloud["source"]
                        # Reflect the cloud answer in the Airtable brand summary.
                        p["make"], p["model"] = make, model
                        print(f"[worker] {tp_id} pair {idx}: cloud -> "
                              f"{make}/{model}")
                        # WRITE-BACK: remember this paid answer so the next time
                        # the same shoe appears it deflects for free (the cache
                        # compounds). Only store real, usable labels.
                        if (SEEN_SHOE_ENABLED and emb and emb_name
                                and _known(make, 1.0, 0.0) and _known(model, 1.0, 0.0)):
                            try:
                                shoe_memory.remember(
                                    conn, emb, embedder=emb_name, brand=make,
                                    model=model, color=color, source="cloud",
                                    confidence=mk_c, source_ref=f"{tp_id}_{idx}",
                                    dedup_sim=SEEN_SHOE_DEDUP_SIM,
                                    max_rows=SEEN_SHOE_MAX_ROWS, normalize=False)
                            except Exception as exc:   # noqa: BLE001 - never fail a job
                                print(f"[worker] cache write-back error: {exc}",
                                      flush=True)

                # Color is local-only; accept it above its own lower floor, else
                # mark unknown (don't let a weak color guess into the data/name).
                if not _known(color, color_c, LOCAL_COLOR_CONF_MIN):
                    color = "unknown"

                used_cloud = source != "local"
                # Auto-approve high-confidence pairs (local or cloud): no human
                # review and straight into the curated label_data training set.
                confident = (
                    _known(make, mk_c, AUTO_APPROVE_CONF)
                    and _known(model, md_c, AUTO_APPROVE_CONF)
                )
                review_status = "NOT_REQUIRED" if confident else "PENDING"
                final_make = make if confident else None
                final_model = model if confident else None

                prepared.append(
                    (tp_id, img_url, json.dumps(p.get("bbox")), p.get("pair_score"),
                     color, color_c, make, mk_c, model, md_c,
                     json.dumps(sources),
                     review_status, final_make, final_model, source, now),
                )
                if confident:
                    approved += 1
                # Save to label_data: auto-approved local pairs (as before) AND
                # every cloud-predicted pair with a real brand+model (training data).
                # A cache hit is NOT exported — it's a near-duplicate of a crop
                # already in label_data, so re-adding it would inflate the set
                # with non-independent copies.
                has_label = (make and str(make).lower() != "unknown"
                             and model and str(model).lower() != "unknown")
                if img_file and has_label and (confident or used_cloud) and not from_cache:
                    export_label(str(PAIRS_DIR / img_file),
                                 color=color, make=make, model=model,
                                 make_conf=mk_c, model_conf=md_c, color_conf=color_c,
                                 source_photo=tp_id, source_pair=idx,
                                 prediction_source=source)

            # Phase 2 — all DB writes in one short transaction.
            # generate_pair_id reads the table's max ID, so it must run at
            # insert time (each insert is visible to the next on this conn).
            for values in prepared:
                pid = generate_pair_id(conn)
                conn.execute(
                    """INSERT INTO pairs (
                        id, table_photo_id, image_path, bbox, pair_score,
                        detected_color, color_confidence,
                        make, make_confidence, model, model_confidence,
                        model_sources, review_status, final_make, final_model,
                        prediction_source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (pid, *values),
                )
            conn.execute(
                "UPDATE table_photos SET status = 'completed', num_pairs = ?, "
                "processed_at = ?, error_message = NULL WHERE id = ?",
                (len(pairs), now, tp_id),
            )
            conn.commit()
            print(f"[worker] {tp_id}: {len(pairs)} pair(s) "
                  f"({approved} auto-approved) -> completed.")

            # Stage 2 (best-effort, isolated so it can never flip the job to
            # 'failed'): attach the brand summary to the durable outbox row and
            # try to send it. If the shipment isn't in Airtable yet, it stays
            # queued and the retry worker delivers it later. No-op unless a
            # barcode was scanned.
            try:
                from backend.services.airtable_sync import brand_summary_from_pairs
                from backend.services.airtable_outbox import set_brand_summary, try_one_async
                if job.get("barcode"):
                    summary = brand_summary_from_pairs(pairs)
                    if summary:
                        set_brand_summary(conn, tp_id, summary)
                        try_one_async(tp_id)
            except Exception as exc:                   # noqa: BLE001 - never affect status
                print(f"[worker] outbox brand-summary error: {exc}")
        except Exception as exc:                       # noqa: BLE001 - never crash worker
            try:
                conn.rollback()
                conn.execute(
                    "UPDATE table_photos SET status = 'failed', error_message = ? WHERE id = ?",
                    (str(exc)[:500], tp_id),
                )
                conn.commit()
            except Exception as exc2:                  # noqa: BLE001
                # Job stays 'processing'; the stale sweeper re-queues it later.
                print(f"[worker] {tp_id}: could not mark failed: {exc2}",
                      flush=True)
            print(f"[worker] {tp_id}: FAILED — {exc}", flush=True)
        finally:
            conn.close()


# Singleton used by main.py's lifespan.
worker = EngineWorker()
