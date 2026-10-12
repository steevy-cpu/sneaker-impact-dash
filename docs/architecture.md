# Architecture

How the dashboard turns a photo of a table of shoes into labeled pairs and Airtable rows. (The original May-2026 simulation-era plan this file replaced is in git history.)

## Processes

```
 Station browser ──HTTPS :8443 / HTTP :8000──►  uvicorn  backend.main:app   (two services, same code, one SQLite file)
 (capture / review)                              │  routes/*  ── fast, never wait on ML or network
                                                 │  background threads (started in main.py lifespan):
                                                 │    engine worker · Airtable outbox retrier ·
                                                 │    camera-settings re-applier · partner resolver
                                                 ▼
   sneakers.db (WAL) · images/ · sneaker_impact_training/label_data/
        ▲                │ subprocess (system python, GPU)          HTTP
        │                ▼                                           │
        │      services/engine_runner.py ── segment → pair → crop → color → brand ──► SAM3 preview worker :8766
        │                                                                              (sneaker-dash-preview; also
        └── cloud identify (Gemini) · seen-shoe cache · optional Lens search · Airtable    serves the capture overlay)
```

Production units (systemd user services, see `deploy/systemd/`): `sneaker-dash-http`, `sneaker-dash-https`, `sneaker-dash-preview`, `ollama`, and a nightly `sneaker-dash-backup` timer. Both dash services run a worker thread, so GPU work is serialized in the database (below).

## Request → result flow

1. **Capture** — `frontend/capture.html` + `routes/capture.py`. The operator scans a FedEx/UPS barcode, enters box counts and weight, and uploads one table photo. `POST /api/capture` inserts a `table_photos` row with `status='pending'` and returns at once. `POST /api/metadata` is the image-less fast track. A repeat barcode returns 409 and the page offers overwrite/cancel.
2. **Claim** — `services/jobqueue.py`. The worker claims the oldest pending photo with one atomic `UPDATE … WHERE NOT EXISTS (processing)`, so only **one engine job runs at a time across both services**. A heartbeat refreshes `claimed_at`; a sweeper re-queues `processing` rows older than the engine timeout.
3. **Engine** — `services/pipeline.py` launches `services/engine_runner.py` under the system Python (`ENGINE_PYTHON`) so the dash venv stays light. Stages: segment (SAM3 through the shared preview worker in production; YOLOE with tiling is the code default) → pair shoes by DINOv2 appearance similarity → crop with a whitened background → color (CIELAB) → brand. Crops go to `images/pairs/<TBL-id>_<n>.jpg`; results come back as JSON through a temp file.
4. **Identify** — per pair, in `jobqueue._process`: local result → seen-shoe cache (`services/shoe_memory.py`, DINOv2 nearest neighbour) → cloud fallback (`services/cloud_identify.py`, Gemini, run `CLOUD_CONCURRENCY` wide) → optional Google Lens titles (`services/visual_search.py`). A shoeness gate drops non-shoes. Labels pass through `utils/models.normalize_labels` / `utils/brands` (canonical spellings; the pre-normalization strings are kept in `raw_make`/`raw_model` only when they changed).
5. **Persist** — all network and compute happens first; the `pairs` rows are written in **one short transaction** at the end, then the photo is marked `completed`.
6. **Review** — pairs at or above `AUTO_APPROVE_CONF` are auto-approved; between `IDENTIFY_MIN_CONF` and that they are reported but queued for a human; below it the field is `unknown`. Quick Label / Pairs Review record human decisions (`final_*`, `label_action`, `sample_mode`).
7. **Export** — confirmed pairs above `LABEL_EXPORT_MIN_CONF` are copied to `label_data/` (`services/label_export.py`), the training set.
8. **Sync** — `services/airtable_outbox.py` writes box data (on capture) and the brand summary (on completion) to Airtable "Shipments Received". Every write is queued in `airtable_outbox` and retried every `OUTBOX_RETRY_SECONDS` until the shipment exists, because shipments are often imported after being scanned. `services/partner_resolver.py` keeps the partner name correct per table.

## Capture-page live guide

`services/preview_worker.py` is a separate resident process that holds a single SAM3 model. It draws the live box overlay on the capture page *and* is the segmenter the engine calls (`ENGINE_SAM3_REMOTE_URL`), so only one SAM3 ever sits in VRAM. `backend/placement/` turns its detections into GREEN / RED / YELLOW readiness per station and for the whole table (pure geometry, no model). Kill switch: `PREVIEW_SEGMENT_ENABLED=0`.

## Data model (SQLite, `backend/database.py`)

| Table | Role |
|-------|------|
| `table_photos` | One row per captured table: image, barcode, box counts/weight, `status` (`pending` → `processing` → `completed`/`failed`), `claimed_at`, `capture_mode` (`shoes`/`insoles`) |
| `pairs` | One row per detected pair: crop, color/make/model + confidences, `prediction_source`, review status, human `final_*` labels |
| `shoe_memory` | Seen-shoe cache: embeddings of resolved shoes |
| `airtable_outbox` | Durable queue of pending Airtable writes |
| `app_config` | Runtime key/value switches (station config, camera controls, `worker_paused`) |
| `batches`, `shoes`, `system_events` | Earlier inspection model; still used by the simulation pages and event log |

Schema changes are code-only: new tables in `init_db`'s `CREATE TABLE IF NOT EXISTS` script, new columns appended to `_add_columns_if_missing` (idempotent `ALTER TABLE`). There is no migration framework.

## Cross-cutting

- **Configuration** — `backend/config.py`, environment-driven. Code defaults are conservative (simulation mode, features off); the production `.env` turns on `actual` mode, SAM3, the seen-shoe cache, the shoe gate and the IT gate. Check `.env` before reasoning about live behavior.
- **IT gate** — middleware in `main.py`. With `IT_PASSWORD` set, only Capture, Tableau and Config (and the APIs they need) are public; everything else redirects to `/it`. New operator-facing endpoints must be added to `_IT_PUBLIC_EXACT` / `_IT_PUBLIC_PREFIXES`.
- **Frontend** — no framework. Each page loads `js/<page>.js` plus shared `api.js`, `utils.js`, `theme.js`; every page carries its own sidebar markup. HTML and `/images/pairs/*` are served `no-cache`; JS/CSS rely on a manual `?v=N` query bump.
- **Cloud & cost** — the Gemini call dominates spend (thinking tokens ≈ 90% of it); the seen-shoe cache and confidence gates exist to avoid calls and to keep wrong labels out of the cache and training set.
