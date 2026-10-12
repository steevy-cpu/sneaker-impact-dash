# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This repo is the live **Sneaker Impact Dashboard**: FastAPI + SQLite + vanilla-JS web app for the shoe-recycling station, running in production on this machine. Read [docs/architecture.md](docs/architecture.md) for the full data flow and [docs/operations.md](docs/operations.md) for the production runbook; this file keeps the rules and gotchas.

`sneaker_impact_training/` is the ML engine, a git submodule with its own `CLAUDE.md` (partly stale — it still describes the older click-to-Recycle flow). It may contain uncommitted work in progress; check `git status` inside it before touching it.

## Commands (run from the repo root)

```bash
# Tests — stdlib unittest, no pytest installed in the venv
./venv/bin/python3 -m unittest discover -s backend/tests -t .
./venv/bin/python3 -m unittest backend.tests.test_partner_resolver            # one module
./venv/bin/python3 -m unittest backend.tests.test_placement.PlacementTests.test_01_one_valid_shoe  # one test

# Dev server (simulation mode is the default; seeds fake data into an empty DB)
./venv/bin/uvicorn backend.main:app --reload        # http://localhost:8000, /docs for Swagger
```

There is no build step, linter, or formatter configured. The frontend is static HTML/CSS/JS served by FastAPI.

### Production runs under systemd **user** services — never `nohup uvicorn`

`sneaker-dash-http` (:8000), `sneaker-dash-https` (:8443, TLS — the station's browser camera needs a secure context), `sneaker-dash-preview` (SAM3 worker, 127.0.0.1:8766), `ollama`, plus a nightly `sneaker-dash-backup` timer. Unit files are versioned in `deploy/systemd/` (`install.sh --check` diffs them against what's installed; `install.sh` never restarts anything).

```bash
systemctl --user restart sneaker-dash-http sneaker-dash-https   # after ANY backend/.env edit (both!)
journalctl --user -u sneaker-dash-https -f                      # logs
```

Backend code and `.env` changes need that restart. Frontend file changes don't (see cache-busters below). No passwordless sudo on this box. Restart when no photo is `processing` if possible.

## Architecture (short)

Capture (`routes/capture.py`) inserts a `table_photos` row (`pending`) and returns immediately → the engine worker (`services/jobqueue.py`, one daemon thread per service) claims it → `services/pipeline.py` runs `services/engine_runner.py` as a subprocess under the **system Python** (`ENGINE_PYTHON`, GPU torch; not the dash venv) → per-pair identification tiers (local → seen-shoe cache `services/shoe_memory.py` → cloud `services/cloud_identify.py` → optional Lens `services/visual_search.py`) → all `pairs` rows written in **one short transaction** → humans review → confirmed labels export to `label_data/` → box data syncs to Airtable via a durable outbox (`services/airtable_outbox.py`; `no_row` = shipment not imported yet, not a matching bug). Details and the process diagram: `docs/architecture.md`.

All make/model writes go through `utils/models.normalize_labels` / `utils/brands` (canonical spellings; `raw_make`/`raw_model` keep the original only when normalization changed it).

### Configuration is env-driven; production differs from code defaults
`backend/config.py` is the single source of settings (loaded from `.env`; `.env.example` is the template; `.env` is gitignored). Many features default **OFF in code** (`SEEN_SHOE_ENABLED`, `VISUAL_SEARCH_ENABLED`, `SHOE_GATE_ENABLED`, `ENGINE_SEGMENT_ESCALATE`, `IT_PASSWORD`, Airtable/FedEx keys). The production `.env` on this machine currently turns on `APP_MODE=actual`, `ENGINE_SEGMENT_BACKEND=sam3`, `PREVIEW_SEGMENT_ENABLED=1`, `SEEN_SHOE_ENABLED=1`, `SHOE_GATE_ENABLED=1` and sets `IT_PASSWORD`; `VISUAL_SEARCH_ENABLED=0`. So code defaults (YOLOE segmentation, simulation mode) are **not** what runs in production — check `.env` (key names only; never print secrets) before reasoning about live behavior. The comments beside each setting in `config.py` record the measurements behind the defaults.

### Database
SQLite `sneakers.db` (WAL, `busy_timeout=5000`) via `database.get_connection()` / the `get_db` FastAPI dependency. **Schema changes are code-only migrations in `database.py`**: new tables go in the `init_db` script with `CREATE TABLE IF NOT EXISTS`; new columns are appended to the list in `_add_columns_if_missing` (idempotent `ALTER TABLE`). Key tables: `table_photos`, `pairs`, `shoe_memory`, `airtable_outbox`, `app_config` (runtime switches); legacy `shoes`/`batches`.

### IT gate
Middleware in `main.py` (`it_gate`): when `IT_PASSWORD` is set, only Capture/Tableau/Config pages and their APIs are public; everything else redirects to `/it`. **A new endpoint that the operator station needs must be added to `_IT_PUBLIC_EXACT` / `_IT_PUBLIC_PREFIXES`**, or it will 401 once the gate is on.

### Frontend
No framework or build. Each `frontend/*.html` page loads `js/<page>.js` plus shared `api.js` (all fetches go through `apiFetch`), `utils.js`, `theme.js`. **Edited JS/CSS must have its `?v=N` query string bumped in the HTML that loads it**, or browsers serve the stale file (HTML itself and `/images/pairs/*` are served `no-cache`). Each page carries its own sidebar markup, so adding a page means adding the link to every page's nav.

### Scripts
`scripts/*.py` locate the repo root as `Path(__file__).parent.parent`, so keep them directly in `scripts/` (don't nest into subfolders). `scripts/backup_dash.sh` is referenced by path from the backup systemd unit.

## Operational rules learned the hard way

- **The live site must stay fast.** Don't put blocking work, network calls, or long write transactions on request handlers or the event loop; keep cloud/engine work in the worker and commit DB writes briefly (long write txns around cloud calls previously caused "database is locked" and stuck jobs).
- **One engine job at a time, one VLM on the GPU.** `_claim_one` refuses to claim while any photo is `processing` (two engines wedge ollama on the 16 GB GPU). Never load a second model into ollama while photos are processing; benchmark with the queue empty. Pause new claims with `app_config` key `worker_paused` = `"1"`. A wedged ollama → `systemctl --user restart ollama`.
- **Stuck `processing` jobs** are re-queued by a sweeper after `ENGINE_JOB_TIMEOUT + 120s` (keyed on `claimed_at`, refreshed by a heartbeat). Engine signal-deaths (exit −11, a libcuda stack over-read) get one automatic retry; `ENGINE_ENV_PAD_KB` is the workaround — leave it on.
- **Don't poison the training set.** Cloud labels below `LABEL_EXPORT_MIN_CONF` and non-shoes must not reach `label_data/` or the seen-shoe cache (a cached wrong label is re-served to every look-alike).
- Before destructive data operations on `sneakers.db`, snapshot it first into `~/Backups/sneaker-dash/manual-snapshots/` (not the repo directory).
