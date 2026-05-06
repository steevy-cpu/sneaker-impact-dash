# Sneaker Impact Dashboard — Architecture Plan
**Step 1: Foundation Planning**
Date: 2026-05-04

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        BROWSER                              │
│   HTML + CSS + Vanilla JS  (6 pages, no framework)          │
│   Fetches JSON from API · Renders images from /images/      │
└────────────────────────┬────────────────────────────────────┘
                         │ HTTP (JSON + image files)
┌────────────────────────▼────────────────────────────────────┐
│                     FASTAPI SERVER                          │
│   /api/*   — JSON endpoints                                 │
│   /images/ — static file serving for shoe images           │
│   /static/ — CSS, JS, HTML pages                           │
│                                                             │
│   ┌─────────────────┐     ┌──────────────────────────────┐ │
│   │  Route handlers │ ──► │  Services layer               │ │
│   └─────────────────┘     │  (business logic)             │ │
│                           └──────────┬───────────────────┘ │
│                                      │                      │
│   ┌──────────────────────────────────▼──────────────────┐  │
│   │  MODE SWITCH  (env var: APP_MODE=simulation|actual)  │  │
│   │                                                      │  │
│   │  SimulationDataProvider  |  ActualDataProvider       │  │
│   └──────────────────────────────────────────────────────┘  │
└──────────────┬────────────────────────┬─────────────────────┘
               │                        │
    ┌──────────▼──────┐       ┌─────────▼─────────┐
    │   SQLite DB     │       │  Image files       │
    │  sneakers.db    │       │  /images/          │
    └─────────────────┘       │    /{batch_id}/    │
                              │      /{shoe_id}/   │
                              │        top.jpg     │
                              │        left.jpg    │
                              │        right.jpg   │
                              │        angle_left  │
                              │        angle_right │
                              └───────────────────┘
```

**Key principles:**
- One process: FastAPI serves both the API and the static frontend files.
- Mode is controlled by a single environment variable (`APP_MODE`).
- The database stores only metadata and file paths, never image blobs.
- No external dependencies beyond Python stdlib + FastAPI + SQLite.

---

## 2. Folder Structure

```
sneaker_impact_dash/
│
├── backend/
│   ├── main.py                  # FastAPI app entry point
│   ├── config.py                # APP_MODE, paths, constants
│   ├── database.py              # SQLite connection, init schema
│   ├── models.py                # Pydantic models (request/response shapes)
│   ├── routes/
│   │   ├── shoes.py             # /api/shoes/* endpoints
│   │   ├── batches.py           # /api/batches/* endpoints
│   │   ├── decisions.py         # /api/decisions/* (overrides)
│   │   ├── analytics.py         # /api/analytics/* endpoints
│   │   └── health.py            # /api/health endpoint
│   ├── services/
│   │   ├── base.py              # Abstract DataProvider interface
│   │   ├── simulation.py        # SimulationDataProvider
│   │   └── actual.py            # ActualDataProvider (stub until hardware ready)
│   └── utils/
│       ├── id_generator.py      # Shoe ID and batch ID generation
│       └── image_utils.py       # Image path helpers
│
├── frontend/
│   ├── index.html               # Dashboard Home
│   ├── live_feed.html           # Live Feed
│   ├── shoe_detail.html         # Shoe Detail Page
│   ├── review_queue.html        # Review Queue
│   ├── analytics.html           # Analytics Page
│   ├── system_health.html       # System Health Page
│   ├── css/
│   │   ├── base.css             # Reset, typography, layout
│   │   ├── components.css       # Cards, tables, badges, buttons
│   │   └── pages.css            # Page-specific overrides
│   └── js/
│       ├── api.js               # Shared fetch wrapper (all API calls)
│       ├── utils.js             # Shared helpers (date format, badge colors)
│       ├── dashboard.js         # Home page logic
│       ├── live_feed.js
│       ├── shoe_detail.js
│       ├── review_queue.js
│       ├── analytics.js
│       └── system_health.js
│
├── images/                      # Image storage root (served as static)
│   └── {batch_id}/
│       └── {shoe_id}/
│           ├── top.jpg
│           ├── left.jpg
│           ├── right.jpg
│           ├── angle_left.jpg
│           └── angle_right.jpg
│
├── simulation_assets/           # Placeholder images and seed data
│   ├── sample_images/           # 5 generic shoe images reused in sim mode
│   └── seed_data.json           # Optional: pre-baked inspection records
│
├── sneakers.db                  # SQLite database (created at startup)
├── .env                         # APP_MODE=simulation (or actual)
├── requirements.txt
└── README.md
```

---

## 3. Database Design

### Table: `shoes`
Stores one record per shoe inspection.

| Column             | Type    | Notes                                          |
|--------------------|---------|------------------------------------------------|
| id                 | TEXT PK | Generated shoe ID (see ID strategy below)      |
| batch_id           | TEXT    | FK → batches.id                               |
| operator_id        | TEXT    | Who ran the inspection                         |
| timestamp          | TEXT    | ISO 8601 datetime                              |
| img_top            | TEXT    | Relative path: images/{batch}/{shoe}/top.jpg   |
| img_left           | TEXT    | Relative path                                  |
| img_right          | TEXT    | Relative path                                  |
| img_angle_left     | TEXT    | Relative path                                  |
| img_angle_right    | TEXT    | Relative path                                  |
| validation_status  | TEXT    | VALID / FAILED_CAPTURE / PARTIAL               |
| ai_prediction      | TEXT    | REUSE / RECYCLE / REVIEW                       |
| ai_confidence      | REAL    | 0.0 – 1.0                                      |
| final_decision     | TEXT    | REUSE / RECYCLE / REVIEW (may differ from AI)  |
| human_override     | INTEGER | 0 = no override, 1 = overridden                |
| override_reason    | TEXT    | Nullable; required when human_override = 1     |
| notes              | TEXT    | Optional free text                             |

### Table: `batches`
Groups shoes processed together.

| Column       | Type    | Notes                          |
|--------------|---------|--------------------------------|
| id           | TEXT PK | Generated batch ID             |
| started_at   | TEXT    | ISO 8601                       |
| ended_at     | TEXT    | Nullable; set when batch closes|
| operator_id  | TEXT    |                                |
| shoe_count   | INTEGER | Updated as shoes are added     |

### Table: `system_events`
Lightweight log for health monitoring.

| Column     | Type | Notes                                     |
|------------|------|-------------------------------------------|
| id         | INTEGER PK AUTOINCREMENT |                          |
| event_type | TEXT | CAPTURE_FAIL / MODEL_ERROR / STARTUP etc. |
| message    | TEXT |                                           |
| timestamp  | TEXT |                                           |

**SQLite rationale:**
- Zero server setup, single file, easy to back up (just copy the file).
- Handles hundreds of thousands of records easily at this scale.
- If future scale demands PostgreSQL, the schema and queries are standard SQL — migration is straightforward.

---

## 4. API Design

All endpoints return JSON. Prefix: `/api/`

### Shoes

| Method | Path                        | Description                            |
|--------|-----------------------------|----------------------------------------|
| GET    | /api/shoes                  | List shoes (paginated, filterable)     |
| GET    | /api/shoes/{shoe_id}        | Single shoe detail                     |
| POST   | /api/shoes                  | Create new inspection record           |
| PATCH  | /api/shoes/{shoe_id}/decision | Submit human override decision       |

Query params for `GET /api/shoes`:
- `page`, `page_size` (default 20)
- `batch_id`
- `ai_prediction` (REUSE / RECYCLE / REVIEW)
- `human_override` (true/false)
- `date` (YYYY-MM-DD)
- `validation_status`

### Batches

| Method | Path                  | Description                    |
|--------|-----------------------|--------------------------------|
| GET    | /api/batches          | List batches                   |
| GET    | /api/batches/{id}     | Batch detail + shoe count      |
| POST   | /api/batches          | Open new batch                 |
| PATCH  | /api/batches/{id}     | Close batch (set ended_at)     |

### Analytics

| Method | Path                          | Description                         |
|--------|-------------------------------|-------------------------------------|
| GET    | /api/analytics/daily-summary  | Counts and averages for a given date |
| GET    | /api/analytics/trends         | Daily aggregates over a date range   |
| GET    | /api/analytics/override-rate  | Human override statistics            |

### System Health

| Method | Path            | Description                              |
|--------|-----------------|------------------------------------------|
| GET    | /api/health     | Mode, DB status, last capture, error count, storage usage |

### Images

Images are served as static files directly by FastAPI:
`GET /images/{batch_id}/{shoe_id}/{view}.jpg`

No separate API endpoint — the frontend constructs image URLs from paths stored in shoe records.

---

## 5. Simulation Mode Design

**Trigger:** `APP_MODE=simulation` in `.env`

**What simulation mode does:**

1. On startup, if the database is empty, seeds it with N fake inspection records
   (configurable, e.g. 200 records across 5 batches).
2. A background task or on-demand endpoint generates a new fake shoe record every
   few seconds to simulate a live feed.
3. AI predictions are generated randomly with weighted probabilities:
   - REUSE: 50%, RECYCLE: 30%, REVIEW: 20%
4. Confidence scores are random floats in realistic ranges per prediction:
   - REUSE: 0.75–0.98, RECYCLE: 0.70–0.95, REVIEW: 0.45–0.72
5. Image paths all point to the 5 placeholder images in `simulation_assets/sample_images/`.
   Every simulated shoe "has" 5 images — they just happen to be the same 5 placeholders.
6. No real operator is needed — operator IDs cycle through a fixed list (OP-001..OP-005).

**Data flow:**
```
SimulationDataProvider.generate_shoe()
  → assign shoe_id + batch_id
  → pick random ai_prediction + confidence
  → set img_* paths to simulation_assets/sample_images/
  → set validation_status = VALID
  → insert into shoes table
  → return shoe record
```

**Switching between modes:** No code changes needed — just change `.env` and restart.

---

## 6. Actual Mode Design

**Trigger:** `APP_MODE=actual` in `.env`

**What actual mode does:**

1. Exposes `POST /api/shoes` for the real inspection station to push records.
2. The inspection station (camera + AI model) sends a JSON payload with:
   - shoe_id (or requests one from `GET /api/shoes/generate-id`)
   - batch_id
   - ai_prediction, ai_confidence
   - image paths (files already saved to the images/ folder by the station)
   - validation_status
3. FastAPI validates and inserts the record.
4. Images must be written to disk by the inspection station before the API call.
   FastAPI does NOT move or process images — it only records the paths.

**Data flow:**
```
Real inspection station
  → captures 5 images → saves to images/{batch}/{shoe}/
  → runs AI model → gets prediction + confidence
  → POST /api/shoes  (JSON payload)
  → ActualDataProvider.create_shoe(payload)
  → validates required fields
  → inserts into shoes table
  → returns created record
```

**ActualDataProvider** is a thin wrapper — mostly just validation + DB insert.
The stub can be written in Step 2 even before hardware is available.

---

## 7. Image Storage

**Root folder:** `images/` (at project root, served as static by FastAPI)

**Path convention:**
```
images/{batch_id}/{shoe_id}/top.jpg
images/{batch_id}/{shoe_id}/left.jpg
images/{batch_id}/{shoe_id}/right.jpg
images/{batch_id}/{shoe_id}/angle_left.jpg
images/{batch_id}/{shoe_id}/angle_right.jpg
```

**What gets stored in DB:** Relative paths only.
Example: `images/BATCH-20260504-001/SHOE-20260504-0042/top.jpg`

**Frontend constructs URLs:** `/images/{batch_id}/{shoe_id}/top.jpg`
FastAPI serves this directory as a static mount.

**Simulation mode images:**
All 5 placeholder files live in `simulation_assets/sample_images/`.
DB records point to these paths. No folder-per-shoe is created in simulation.

**Image naming:** Fixed names (top, left, right, angle_left, angle_right) — no hashes,
no UUIDs in filenames. The folder hierarchy provides uniqueness.

---

## 8. ID Generation Strategy

### Shoe ID
Format: `SHOE-{YYYYMMDD}-{4-digit-seq}`
Example: `SHOE-20260504-0001`

- Sequence resets daily (or continues — both work; daily reset is more readable).
- Actual mode: inspection station may request an ID from the API before capture,
  or generate its own in the same format.
- Uniqueness guarantee: date + sequence is unique within the database.

### Batch ID
Format: `BATCH-{YYYYMMDD}-{3-digit-seq}`
Example: `BATCH-20260504-001`

- New batch opened by operator or automatically at shift start.
- Batch ID is human-readable for easy log searching.

**Implementation:** `utils/id_generator.py` queries the DB for the highest existing
sequence for today and increments. Simple, no external library needed.

---

## 9. Frontend Page Plan

All pages share the same nav bar. Each page is a standalone HTML file.
`api.js` provides a shared `apiFetch(path, options)` wrapper used by all pages.

### Page 1 — Dashboard Home (`index.html`)
- Loads: `GET /api/analytics/daily-summary?date=today`
- Displays: 4 stat cards (total today, reuse, recycle, review), average confidence bar,
  failed captures count, last 5 shoe thumbnails.

### Page 2 — Live Feed (`live_feed.html`)
- Loads: `GET /api/shoes?page=1&page_size=20` (newest first)
- Displays: table/card list, each row shows shoe ID, timestamp, 5 thumbnails,
  AI prediction badge, confidence score.
- Auto-refreshes every 10 seconds via `setInterval`.

### Page 3 — Shoe Detail (`shoe_detail.html?id=SHOE-...`)
- Reads `shoe_id` from URL query param.
- Loads: `GET /api/shoes/{shoe_id}`
- Displays: 5 images in a grid, all metadata fields, AI result, final decision.
- Override form: dropdown (REUSE / RECYCLE / REVIEW) + reason textarea + submit.
  Calls `PATCH /api/shoes/{id}/decision`.

### Page 4 — Review Queue (`review_queue.html`)
- Loads: `GET /api/shoes?ai_prediction=REVIEW&human_override=false`
- Displays: list of shoes pending human review, sorted by oldest first.
- Each row links to Shoe Detail page.
- Shows count of pending items.

### Page 5 — Analytics (`analytics.html`)
- Loads: `GET /api/analytics/trends?from=...&to=...`
- Displays:
  - Line chart: daily totals over time (drawn with Canvas API or a tiny lib like Chart.js)
  - Bar chart: REUSE / RECYCLE / REVIEW breakdown per day
  - Override rate over time
  - Table: AI vs human decision agreement rate

### Page 6 — System Health (`system_health.html`)
- Loads: `GET /api/health`
- Displays:
  - Mode indicator (SIMULATION / ACTUAL) — prominent colored badge
  - Camera status placeholders (always "N/A" in simulation)
  - Last capture time
  - Validation error count
  - Model version (from config or health response)
  - Storage usage (disk space of images/ folder)

---

## 10. Build Order

### Phase 1 — Backend skeleton (build first)
1. `requirements.txt`, `config.py`, `.env`
2. `database.py` — create schema, init on startup
3. `utils/id_generator.py`
4. `services/base.py` (abstract interface)
5. `services/simulation.py` — seed data + fake record generator
6. `routes/shoes.py` — GET list, GET detail, POST create
7. `main.py` — wire FastAPI, mount static dirs, load mode

**Goal:** API returns real JSON. Can test with curl or browser.

### Phase 2 — Frontend core (build second)
1. `api.js` and `utils.js`
2. `base.css` and `components.css`
3. `index.html` + `dashboard.js` (stat cards)
4. `live_feed.html` + `live_feed.js`
5. `shoe_detail.html` + `shoe_detail.js` (view only, no override yet)

**Goal:** Can browse shoes and see images from a browser.

### Phase 3 — Review + override (build third)
1. `routes/decisions.py` — PATCH endpoint
2. Override form in `shoe_detail.js`
3. `review_queue.html` + `review_queue.js`

**Goal:** Human reviewer can make decisions from the browser.

### Phase 4 — Analytics + health
1. `routes/analytics.py`
2. `analytics.html` + `analytics.js` (charts)
3. `routes/health.py`
4. `system_health.html` + `system_health.js`

### Phase 5 — Actual mode stub
1. `services/actual.py` — real POST /api/shoes handler
2. Test with Postman or a simple test script before connecting hardware

---

## 11. Risks and Simplifications

| Risk / Decision                        | Choice Made                          | Rationale                                     |
|----------------------------------------|--------------------------------------|-----------------------------------------------|
| SQLite vs PostgreSQL                   | SQLite                               | Zero config, single file, sufficient for scale|
| Authentication                         | None (Phase 1)                       | Local network only; add basic auth later if needed |
| Image upload via API vs file system    | File system only; API stores paths   | Avoids large uploads; inspection station writes directly |
| Chart library                          | Chart.js (small, CDN-loaded)         | Canvas API alone is too much code; Chart.js is tiny and offline-capable if bundled |
| Realtime updates (WebSocket vs polling)| Polling (setInterval 10s)            | Simpler; at this data rate, polling is fine   |
| Pagination                             | Cursor-free offset pagination        | Sufficient; cursor pagination is overkill here |
| ORM vs raw SQL                         | Raw SQL with sqlite3 module          | Fewer dependencies; schema is simple enough   |
| Date/time storage                      | ISO 8601 TEXT in SQLite              | SQLite has no native datetime; TEXT is portable and sortable |
| Frontend routing                       | Multi-page HTML (no SPA)             | Simplest possible; no build step, no bundler  |

**Biggest simplification:** No authentication in Phase 1. This is intentional — the system is
designed for a local network industrial environment. If it needs to be exposed more broadly,
add HTTP Basic Auth or a session cookie in a later phase without changing the architecture.
