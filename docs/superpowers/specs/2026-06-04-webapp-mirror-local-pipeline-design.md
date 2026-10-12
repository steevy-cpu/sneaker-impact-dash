# Design: Web App (sneaker-impact-dash) Mirror + Local Pipeline Integration

- **Date:** 2026-06-04
- **Status:** Draft — pending user review
- **Author:** Head engineer (Cursor and claude code) + Steeve
- **Workspace:** `/home/sneakerai/Documents/ShoeSort-main`

---

## 1. Background — three systems today

| System | Role today | Identification | Storage | Operator UX |
|--------|-----------|----------------|---------|-------------|
| **ShoeSort** (`shoesort/`, `shoes_web/`, `queue_processor.py`) | Production capture + queue + identify | Cloudflare → Bright Data Lens → OpenAI (brand/model/MSRP) | PostgreSQL + Airtable | **PyQt desktop client** (`shoesort_client/main.py`) — not in repo |
| **sneaker_impact_training** (`sneaker_impact_training/`, own `.git`, remote `steevy-cpu/sneaker_impact_training`) | Table-photo data pipeline | Local: YOLOE+tiling segment → geometric pair → CLIP brand → Ollama `qwen2.5vl` model → CIELAB color | JSON sidecars + `label_data/` | none (CLI) |
| **sneaker-impact-dash** (GitHub `steevy-cpu/sneaker-impact-dash`) | Inspection/review web UI | n/a (displays only) | SQLite | Web (FastAPI + vanilla JS) |

**Key facts discovered during exploration:**

- The operator features we must preserve (metadata popup, "Capture All", barcode entry, Config mode) live in the **PyQt desktop client**, *not* in `shoes_web`. The Flask web app only *displays* box data.
- There is **no live FedEx API**. A scanned barcode (often a FedEx tracking number) is stored and matched against **Airtable** Orders/Shipments records. Those Airtable records are populated separately, most likely from **FedEx invoice exports** (e.g. `Auto FedExInv 644776182.XLS` in `~/Downloads`).
- The dash's data model is **REUSE/RECYCLE/REVIEW per single shoe with 5 camera angles** — it has no fields for box weight, box counts, barcode, brand/model/color, or "many pairs from one table photo".

---

## 2. The new product vision

Photograph **one photo of the whole table** of tied shoe pairs → upload with **box metadata** (weight, good count, end-of-life count, casuals) and a **barcode** → the **server computer** processes it **in the background** (segment → pair → color/brand/model, all **local/free**) → results appear in the **sneaker-impact-dash web UI**.

No live carousel. The web app **mirrors the desktop app's operator workflow but better**, and once validated against the real camera setup, **replaces** the desktop app.

---

## 3. Goals / Non-goals

### Goals
1. Build the new operator + review experience **inside the `sneaker-impact-dash` repo**.
2. Mirror the desktop client's operator workflow on the web: **box-metadata popup, barcode entry, capture trigger, config**.
3. Capture model = **single table photo** (camera at the station via the browser, with file-upload fallback).
4. Server-side **background processing** using the **local** `sneaker_impact_training` pipeline (segment → pair → color → CLIP brand → Ollama model).
5. Extend the dash **schema + API** to hold box metadata, barcode, and `table_photo → pairs(color/make/model)`.
6. Keep **barcode → Airtable** working; design a **pluggable shipment-lookup interface** so a real **FedEx Track API** can be added later without rework.
7. Deliver a real **Config tab** and a **consistent, reliable refresh** mechanism.

### Non-goals (this project)
- Do **not** modify or break the existing PyQt desktop client or the existing ShoeSort Flask/queue backend — they keep running until cutover.
- Do **not** build the live FedEx API now (interface only).
- No paid identification pipeline (Cloudflare/Bright Data/OpenAI) in the new flow — local only.
- No DINOv2 / model-training work (tracked separately in `sneaker_impact_training/HANDOFF.md`).

---

## 4. Strategy: parallel build, then cutover

```
Phase build (no production impact)
  desktop app + ShoeSort backend  ── keep running ──►  production unaffected
  new web app (in dash) ── built & tested in parallel ──►  staging

Validation against the REAL camera/table setup
  ▼
Cutover: replace desktop app with web app once verified
```

This guarantees zero downtime risk: nothing user-facing changes in production until the web app is proven on the actual hardware.

---

## 5. Target architecture

```
┌─ sneaker-impact-dash  (THE PRODUCT) ───────────────────────────────────┐
│  Frontend (vanilla JS, no build step)                                   │
│   • Capture page: live camera (getUserMedia) → ONE table photo          │
│   • Box-metadata popup: weight, good, end-of-life, casuals (+ barcode)  │
│   • Review/Live/Analytics pages (extended for pairs)                    │
│   • Config tab (station settings)                                       │
│   • Consistent refresh (poll + optional SSE), NO carousel               │
│  Backend (FastAPI + SQLite)                                             │
│   • POST table photo + metadata  → enqueue job                         │
│   • Background worker thread/process: calls the local engine           │
│   • Shipment lookup interface (Airtable now, FedEx later)              │
└───────────────────────┬─────────────────────────────────────────────────┘
                        │  (in-process call or subprocess)
                        ▼
┌─ Local identification engine (from sneaker_impact_training) ───────────┐
│  segment (YOLOE+tiling) → pair (geometric) → color (CIELAB)            │
│  → brand (CLIP zero-shot) → model (Ollama qwen2.5vl)                   │
│  Runs on the SERVER COMPUTER (CUDA GPU)                                │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
              writes pairs back to the dash SQLite DB
                        │
                        ▼ (best-effort sync)
              Airtable (logistics/reporting); FedEx later
```

- **Dash** = UI + API + DB + operator UX.
- **`sneaker_impact_training`** = the CV/identification library, imported by the dash backend worker. Keep its own config/modules; the dash calls `split_table` / `identify_brands` / `identify_models` equivalents programmatically.
- **Integrations** = Airtable (reuse mapping concepts from `shoesort/api/airtable_api`), FedEx interface stub.

---

## 6. Repo & code layout (inside the dash)

Clone `sneaker-impact-dash` into the project as a real working copy (not the `/tmp` review clone). Proposed additions:

```
sneaker-impact-dash/
├── backend/
│   ├── routes/
│   │   ├── capture.py        # NEW: POST table photo + metadata, job status
│   │   ├── pairs.py          # NEW: list/get pairs for a table photo / batch
│   │   ├── config_station.py # NEW: read/write station config
│   │   ├── shoes.py batches.py health.py analytics.py ...  (existing)
│   ├── services/
│   │   ├── pipeline.py       # NEW: bridge to sneaker_impact_training engine
│   │   ├── jobqueue.py       # NEW: background job queue + worker
│   │   ├── shipment_lookup.py# NEW: interface; AirtableLookup impl; FedExLookup stub
│   ├── models.py             # EXTEND: TablePhoto, Pair, box metadata, barcode
│   ├── database.py           # EXTEND: new tables + migrations
│   └── config.py             # EXTEND: engine paths, integration toggles
├── frontend/
│   ├── capture.html + js/capture.js     # NEW: capture page + metadata popup
│   ├── config.html  + js/config.js      # NEW: config tab
│   ├── (existing pages, extended for pairs)
│   └── js/refresh.js                    # NEW: shared refresh helper
└── engine/  (option)        # thin import shim to sneaker_impact_training
```

**Decision needed at implementation time:** import `sneaker_impact_training` as (a) a sibling package on `PYTHONPATH`, (b) a git submodule of the dash, or (c) vendored. Default recommendation: **git submodule** so it keeps its own history/remote and updates cleanly.

---

## 7. Data model (extended dash SQLite)

Keep existing `batches`, `shoes`, `system_events`. Add:

### `table_photos`
| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | `TBL-YYYYMMDD-NNNN` |
| `batch_id` | TEXT FK | → `batches.id` |
| `operator_id` | TEXT | |
| `image_path` | TEXT | stored original table photo |
| `barcode` | TEXT | scanned/typed; may be FedEx tracking # |
| `weight_of_box` | REAL | lbs |
| `total_good_sneakers` | INTEGER | |
| `total_end_of_life` | INTEGER | |
| `casuals` | INTEGER | |
| `status` | TEXT | `pending` / `processing` / `completed` / `failed` |
| `error_message` | TEXT | nullable |
| `num_pairs` | INTEGER | filled after processing |
| `created_at` / `processed_at` | TEXT | ISO |
| `shipment_info` | TEXT(JSON) | resolved partner/weight/address from lookup |

### `pairs`
| Column | Type | Notes |
|--------|------|-------|
| `id` | TEXT PK | `PAIR-YYYYMMDD-NNNN` |
| `table_photo_id` | TEXT FK | → `table_photos.id` |
| `image_path` | TEXT | cropped pair image |
| `bbox` | TEXT(JSON) | source bbox |
| `detected_color` | TEXT | |
| `color_confidence` | REAL | |
| `make` | TEXT | brand |
| `make_confidence` | REAL | |
| `model` | TEXT | |
| `model_confidence` | REAL | |
| `model_sources` | TEXT(JSON) | |
| `review_status` | TEXT | `NOT_REQUIRED`/`PENDING`/`COMPLETED` |
| `final_make` / `final_model` | TEXT | human-confirmed overrides |
| `created_at` | TEXT | |

This preserves the dash's review workflow (`review_status`, human override) but at the **pair** level, matching the new flow. The box-level totals (good/EOL/casuals/weight) live on `table_photos`.

---

## 8. API design (additions)

| Method | Path | Body / Query | Purpose |
|--------|------|--------------|---------|
| POST | `/api/metadata` | `{barcode, weight_of_box, total_good_sneakers, total_end_of_life, casuals, operator_id}` | Fast-track metadata (mirror existing ShoeSort `/api/metadata`), resolves shipment lookup, returns record id |
| POST | `/api/capture` | multipart: `image`, `barcode`, box fields, `operator_id`, `batch_id?` | Upload table photo + metadata → create `table_photos` row (`pending`) → enqueue job |
| GET | `/api/table-photos` | `page,page_size,status,batch_id` | List table photos |
| GET | `/api/table-photos/{id}` | — | One table photo + its pairs + status |
| GET | `/api/table-photos/{id}/status` | — | Lightweight status for polling |
| GET | `/api/pairs` | `table_photo_id` / filters | List pairs |
| PATCH | `/api/pairs/{id}/review` | `{final_make, final_model, review_status, notes}` | Human confirm/override |
| GET/PUT | `/api/config/station` | station config JSON | Config tab |
| GET | `/api/shipment/{barcode}` | — | Resolve shipment info via lookup interface |

**Compatibility:** keep `/api/metadata` and the multipart field names aligned with the existing ShoeSort contract (`barcode`, `total_end_of_life`, `total_good_sneakers`, `casuals`, `weight_of_box`) so existing tooling/operators experience parity.

---

## 9. Operator capture UX (mirror desktop, but better)

Mirror the desktop `BoxDataDialog` + Capture All on the web:

1. **Capture page** shows a live camera feed via `getUserMedia` (USB camera at the station pointed at the table). File-upload fallback for testing / no-camera.
2. **Capture button** (mirrors "Capture All"; keyboard shortcut Space/Enter; ~3s debounce to prevent double submits) → opens the **box-metadata popup**.
3. **Box-metadata popup** fields (exact parity): `barcode`, `weight_of_box`, `total_good_sneakers`, `total_end_of_life`, `casuals`. Validation: at least one box field > 0 (matches desktop). Barcode field optimized for USB scanner input (rapid keystrokes + Enter).
4. On confirm → `POST /api/capture` (photo + metadata) → toast "queued", page returns to ready state. Background processing happens server-side.
5. **Barcode resolution**: on barcode entry/blur, call `/api/shipment/{barcode}` to show partner/weight if found (Airtable now).

**Improvements over desktop:** inline shipment preview, clearer queued/processing/done status, no app install, works from any station browser.

---

## 10. Server background pipeline

- A **background worker** (thread or separate process) drains a job queue of `pending` table photos.
- For each: call `services/pipeline.py`, which invokes the `sneaker_impact_training` engine programmatically:
  1. `segment` (YOLOE + tiling) → single shoes
  2. `pair` (geometric) → pairs
  3. `color` (CIELAB) per pair
  4. `brand` (CLIP zero-shot) per pair
  5. `model` (Ollama `qwen2.5vl`) per pair
- Write `pairs` rows + update `table_photos.status`, `num_pairs`, `processed_at`.
- **GPU**: runs on the server computer's CUDA GPU. Single-worker initially (models loaded once, reused) to bound VRAM; concurrency tuned later.
- **Ollama** must be running on the server (`ollama serve` + `ollama pull qwen2.5vl:7b`).

---

## 11. Barcode / shipment lookup (Airtable now, FedEx later)

Define one interface so the data source is swappable:

```python
class ShipmentLookup(Protocol):
    def resolve(self, barcode: str) -> ShipmentInfo | None: ...

class AirtableShipmentLookup:   # implemented now
    # match barcode against Airtable Orders/Shipments (carrier, weight, partner, address)

class FedExShipmentLookup:      # stub now; OAuth2 Track API later
    # POST developer.fedex.com /track/v1/trackingnumbers; fall back to Airtable
```

- `config.py` selects the active lookup(s); a **chained** lookup (FedEx → Airtable fallback) is the eventual target.
- `ShipmentInfo`: `{carrier, tracking_number, partner, weight, address, status, raw}`.
- Reuse the field mapping concepts from `shoesort/api/airtable_api/` (Orders: `Barcode`, `Carrier`, `Weight`, `Partner`).

---

## 12. Config tab

Replace the dash's `.env`-only + simulation panel with a real **Config tab** (`/api/config/station`, stored in DB or a JSON file):

- **Server**: API base URL, storage paths.
- **Camera**: device selection (enumerate `getUserMedia` devices), resolution, preferred camera persisted per station.
- **Pipeline toggles**: brand/model on/off, confidence thresholds, Ollama model name.
- **Integrations**: Airtable on/off + base id; FedEx (disabled placeholder).
- **Operator**: operator id, station id.

Persisted server-side so a station's config survives reloads (fixes the desktop "config mode" gap).

---

## 13. Refresh strategy

Replace the dash's ad-hoc per-page `setInterval`s with one consistent helper (`frontend/js/refresh.js`):

- **Default**: smart polling (visibility-aware: pause when tab hidden, back off when idle).
- **Capture/processing status**: poll `/api/table-photos/{id}/status` every ~2s while `processing`, stop when terminal.
- **Optional SSE** (`/api/stream/updates`) for push when a table photo completes, falling back to polling.
- Manual **Refresh** button everywhere; consistent loading/error states.
- Remove carousel polling entirely.

---

## 14. Error handling

- **Capture upload**: validate image present + at least one box field; 4xx with clear message; client returns to ready.
- **Pipeline failure**: mark `table_photos.status='failed'`, store `error_message`, surface in UI with a **Reprocess** action; never crash the worker (catch per-job).
- **Ollama/GPU unavailable**: pipeline degrades — still produce color + brand, leave `model` null/unknown; log clearly.
- **Shipment lookup**: fail-safe (returns `None`, never blocks capture), mirroring the existing fail-safe Airtable/dashboard clients.
- **Offline capture** (future parity with desktop offline queue): out of scope for v1, noted as risk.

---

## 15. Testing strategy

- **Unit**: data-model migrations, API request/response shapes, `ShipmentLookup` (mock Airtable), pipeline bridge with a stub engine.
- **Integration**: POST `/api/capture` with a sample table photo → assert `table_photos` + `pairs` rows created after the worker runs (use a small fixture image / fake engine).
- **Pipeline smoke**: run the real `sneaker_impact_training` engine on 1–2 sample table photos on the server GPU; assert pairs + color/brand/model populate.
- **Manual hardware validation** (cutover gate): real camera at the station, real table, end-to-end through review — this is the go/no-go for replacing the desktop app.

---

## 16. Cutover / migration plan

1. Build + test the web app in parallel (production untouched).
2. Stand it up on the server computer alongside the existing stack (different port).
3. Validate against the real camera/table setup (Section 15 manual gate).
4. Pilot with operators; compare results/throughput to the desktop app.
5. Once accepted, make the web app the primary capture path; retire the desktop client.
6. (Later) add live FedEx Track API behind the existing lookup interface.

---

## 17. Phases / milestones

| Phase | Deliverable | Independently valuable? |
|-------|-------------|--------------------------|
| **P0** | Clone dash into project; submodule `sneaker_impact_training`; run dash locally | Yes — working baseline |
| **P1** | Extend schema + API (`table_photos`, `pairs`, capture/metadata endpoints) | Yes — data backbone |
| **P2** | Capture page + box-metadata popup + barcode (web parity) | Yes — operator UX |
| **P3** | Background worker + local pipeline bridge (segment→pair→color→brand→model) | Yes — the core engine |
| **P4** | Review/Live/Analytics pages extended for pairs | Yes — closes the loop |
| **P5** | Shipment lookup (Airtable) + FedEx interface stub | Yes — logistics |
| **P6** | Config tab + unified refresh | Yes — polish/ops |
| **P7** | Hardware validation + cutover | Yes — go live |

---

## 18. Open questions / risks

1. **Camera access in browser**: assumes a USB camera usable via `getUserMedia` at the station. If the real camera isn't a UVC/web-accessible device, we may need a tiny local capture agent. *To verify on the real setup.*
2. **`sneaker_impact_training` data/models not in repo** (git-ignored): segmentation/model weights, Ollama models, catalog must be present on the server (per `HANDOFF.md`).
3. **GPU/VRAM budget** on the server when running YOLOE + CLIP + Ollama together.
4. **Brand/model confidence is uncalibrated** (CLIP mid-range; Ollama flat ~0.95) — human review remains the safety net; reflect that in the review UI.
5. **Import boundary** for `sneaker_impact_training` (submodule vs PYTHONPATH) — decide in P0.
6. **AGPL note**: YOLOE/YOLO26 is AGPL-3.0 (fine internally; revisit before shipping a product per `HANDOFF.md`).
```
