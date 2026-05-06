# Sneaker Impact Dashboard

A full-stack shoe inspection dashboard with AI classification simulation. Each shoe is photographed from five angles, classified by an AI model as **REUSE**, **RECYCLE**, or **REVIEW**, and tracked through a human review workflow. The backend runs on FastAPI + SQLite; the frontend is plain HTML/CSS/JavaScript — no build step, no framework.

---

## Quick start

```bash
# 1. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate          # macOS / Linux
# venv\Scripts\activate           # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Copy the environment template
cp .env.example .env

# 4. Start the server (simulation mode is the default)
uvicorn backend.main:app --reload
```

Open the dashboard: **http://localhost:8000**

On first startup the server will automatically:
- Create the SQLite database (`sneakers.db`)
- Generate 5 coloured placeholder images in `simulation_assets/sample_images/`
- Seed 50 fake shoe inspection records across 3 batches (3 days of data)

To re-seed from scratch, delete `sneakers.db` and restart — or use the **Reset Data** button on the System Health page.

---

## App modes

The server has two operating modes, controlled by `APP_MODE` in `.env`:

### Simulation mode (default — `APP_MODE=simulation`)

Everything runs offline with generated data. Use this for development, testing, and demos.

- Seeds fake shoe records on first startup (see `SEED_COUNT` in `.env`)
- Generates randomised AI predictions with realistic confidence scores
- Assigns random shoe brands (Nike, Adidas, Puma, New Balance, ASICS…)
- All shoes are `White` (matches the placeholder images)
- Supports **Live Operations** mode: auto-generates one inspection every 10–20 s to simulate an active warehouse floor

Five batch condition profiles are available via the simulation controls:

| Condition | Reuse | Recycle | Review | Failed Captures |
|-----------|-------|---------|--------|-----------------|
| Normal Mix | 50% | 30% | 20% | 8% |
| Mostly Reusable | 78% | 12% | 10% | 5% |
| Mostly Damaged | 10% | 72% | 18% | 8% |
| High Review Volume | 22% | 22% | 56% | 8% |
| High Failed Capture | 50% | 30% | 20% | 45% |

### Actual mode (`APP_MODE=actual`)

Use this when connected to a real inspection station. No seed data is created.

The inspection station must:
1. Capture 5 images per shoe and write them to disk
2. Run the AI model to produce a prediction and confidence score
3. `POST /api/shoes` with the result

No code changes are required to switch modes — change `.env` and restart the server.

---

## Dashboard pages

| URL | Page | Description |
|-----|------|-------------|
| `/` | **Dashboard Home** | Daily stat cards, recent inspections, activity feed |
| `/frontend/live_feed.html` | **Live Feed** | All shoes, newest first, auto-refreshes every 10 s |
| `/frontend/shoe_detail.html?id=…` | **Shoe Detail** | 5 images, AI result, brand/color, human review form |
| `/frontend/review_queue.html` | **Review Queue** | Shoes pending human decision |
| `/frontend/analytics.html` | **Analytics** | Daily trend charts, brand distribution, insights |
| `/frontend/system_health.html` | **System Health** | Mode, DB stats, simulation controls, Live Ops toggle |

---

## API overview

Interactive docs after starting the server:
- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc

### Shoes

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/shoes` | List shoes — paginated, filterable |
| `GET` | `/api/shoes/{shoe_id}` | Get one shoe record (includes brand, color) |
| `POST` | `/api/shoes` | Create a new inspection record |
| `PATCH` | `/api/shoes/{shoe_id}/decision` | Submit a human review decision |

### Analytics

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/analytics/daily-summary` | Counts and averages for one day |
| `GET` | `/api/analytics/trends?days=14` | Per-day aggregates for the last N days |
| `GET` | `/api/analytics/alerts` | Operational alerts based on current thresholds |
| `GET` | `/api/analytics/activity` | Last 20 events as human-readable messages |
| `GET` | `/api/analytics/brand-distribution` | Per-brand counts and review rates |

### Export

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/export/shoes.csv` | Download all records as CSV (optional date filters) |

### Simulation

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/simulation/generate` | Generate N inspections with a chosen condition profile |
| `POST` | `/api/simulation/reset` | Wipe all data and re-seed 50 fresh records |

### Batches / Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/batches` | List all batches |
| `POST` | `/api/batches` | Open a new batch |
| `GET` | `/api/health` | Mode, DB status, counts, last capture, storage |

---

## Configuration reference

Copy `.env.example` to `.env` before running:

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_MODE` | `simulation` | `simulation` or `actual` |
| `SEED_COUNT` | `50` | Fake shoes created on first simulation startup |
| `MODEL_VERSION` | `v1.0-sim` | Stored in every shoe record; shown on System Health |

---

## Project structure

```
sneaker_impact_dash/
├── backend/
│   ├── main.py              # FastAPI app, static mounts, router registration
│   ├── config.py            # Environment variables and directory paths
│   ├── database.py          # SQLite connection, schema + migration, FastAPI dependency
│   ├── models.py            # Pydantic request/response models
│   ├── routes/
│   │   ├── shoes.py         # Shoe CRUD + human review decision
│   │   ├── batches.py       # Batch management
│   │   ├── health.py        # System health endpoint
│   │   ├── analytics.py     # Daily summary, trends, alerts, activity, brand distribution
│   │   ├── export.py        # CSV export
│   │   └── simulation.py    # Generate / reset simulation data
│   ├── services/
│   │   ├── simulation.py    # Brand/condition-based generation, database seeding
│   │   └── actual.py        # Stub for real inspection station integration
│   └── utils/
│       ├── id_generator.py  # SHOE-YYYYMMDD-NNNN / BATCH-YYYYMMDD-NNN
│       └── image_utils.py   # URL path helpers for simulation and actual images
│
├── frontend/
│   ├── index.html           # Dashboard Home
│   ├── live_feed.html       # Live Feed (auto-refreshes every 10 s)
│   ├── shoe_detail.html     # Shoe Detail + human review form
│   ├── review_queue.html    # Review Queue
│   ├── analytics.html       # Analytics charts + brand distribution
│   ├── system_health.html   # System Health + simulation controls
│   ├── assets/              # Logo and static images
│   ├── css/
│   │   ├── base.css         # Design tokens, app shell, typography, dark mode
│   │   └── components.css   # Cards, tables, badges, forms, charts, activity feed
│   └── js/
│       ├── api.js           # Shared fetch wrapper — all API calls go through here
│       ├── utils.js         # Formatting helpers, badge HTML, DOM utilities
│       ├── icons.js         # Lucide SVG icon registry (inline, no CDN dependency)
│       ├── theme.js         # Light/dark mode — runs in <head> to prevent FOUC
│       ├── simulation_controls.js  # Demo controls + Live Ops auto-generation
│       ├── dashboard.js     # Dashboard Home + activity feed + live polling
│       ├── live_feed.js     # Live Feed table + auto-refresh
│       ├── shoe_detail.js   # Shoe Detail rendering + override form
│       ├── review_queue.js  # Review Queue table
│       ├── analytics.js     # Chart.js charts + brand doughnut + insights
│       └── system_health.js # Health grid + alerts + camera cards
│
├── simulation_assets/
│   └── sample_images/       # 5 placeholder JPEGs — served at /sim_images/
├── .env.example             # Environment template (copy to .env before running)
├── requirements.txt
└── README.md
```

---

## Review / override workflow

1. AI classifies the shoe → `ai_prediction` + `ai_confidence` are stored.
2. If `ai_prediction = REVIEW`, `review_status` is set to `PENDING`.
3. A human opens the Shoe Detail page and submits a decision via the override form.
4. `PATCH /api/shoes/{id}/decision` updates `final_decision` → `review_status = COMPLETED`.
5. If the human chose differently from the AI, `human_override = 1` and `override_reason` is stored.

---

## Light / dark mode

The UI supports light and dark themes. The preference is stored in `localStorage` under the key `si_theme`. `theme.js` is loaded synchronously in `<head>` to prevent a flash of the wrong theme on reload.
