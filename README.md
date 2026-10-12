# Sneaker Impact Dashboard

The web app that runs the Sneaker Impact shoe-recycling station. An operator scans a shipment barcode and photographs a whole table of shoes; a background pipeline cuts the photo into **pairs**, identifies each pair's **color / brand / model** (local models first, cloud vision as fallback), humans review the uncertain ones, and box + brand totals sync to Airtable. Confirmed labels also build a training set for future models.

Backend: FastAPI + SQLite. Frontend: plain HTML/CSS/JS, no build step. The ML engine lives in the `sneaker_impact_training/` git submodule and runs as a subprocess.

> Start with **[docs/architecture.md](docs/architecture.md)** for how the pieces fit, and **[docs/operations.md](docs/operations.md)** for running it in production. `CLAUDE.md` holds the working rules for AI coding sessions.

## Quick start (development)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # APP_MODE=simulation by default
uvicorn backend.main:app --reload          # http://localhost:8000  (API docs at /docs)
```

In `simulation` mode the server seeds fake inspections into an empty `sneakers.db`; no engine, GPU or API keys needed. Production uses `APP_MODE=actual` and the full stack (engine, SAM3 worker, ollama, cloud keys) — see the operations doc.

Tests (stdlib `unittest`):

```bash
venv/bin/python3 -m unittest discover -s backend/tests -t .
```

## Pages

| Group | Page | Purpose |
|-------|------|---------|
| Operations | `capture.html` | Scan barcode, enter box counts, photograph the table (live shoe-box placement guide) |
| | `table_photos.html`, `pairs_review.html` | Browse captured tables and their identified pairs |
| | `quick_label.html` | Fast keyboard review that builds the gold-label set |
| Tools | `labeling.html`, `label_data.html`, `gold_labels.html` | Labeling, the curated training set, gold labels |
| Reports | `tableau.html` | Impact / ESG analytics (default landing page) |
| | `airtable_sync.html` | Outbound sync status and shipment lookups |
| Settings | `system_health.html`, `ai_database.html`, `config.html` | Health & data-quality checks, AI database search, station + camera settings |

`index.html`, `live_feed.html`, `review_queue.html`, `shoe_detail.html` and `analytics.html` are earlier simulation-era pages: still served, no longer in the nav. When the **IT gate** is on (`IT_PASSWORD` set), only Capture, Tableau and Config are public; everything else needs the `/it` login.

## Repository layout

```
backend/            FastAPI app: routes/ (HTTP), services/ (workers, pipeline, integrations),
                    placement/ (capture-guide geometry), utils/, tests/
frontend/           one HTML page per screen + js/ + css/ (bump ?v=N on edited JS/CSS)
deploy/systemd/     the production systemd user units + install.sh
docs/               architecture, operations, Airtable schema, design specs/plans
scripts/            one-off and maintenance tools (backup, retro-normalize, benchmarks, …)
sneaker_impact_training/   ML engine (git submodule)
simulation_assets/  placeholder images for simulation mode
```

Runtime data (gitignored): `sneakers.db`, `images/`, `.env`, `certs/`, `capture_timing.csv`.

## Configuration

All settings are environment variables read by `backend/config.py` (the comments there record the measurements behind each default). `.env.example` is the template; many features ship disabled and are switched on in `.env` — never commit `.env`.
