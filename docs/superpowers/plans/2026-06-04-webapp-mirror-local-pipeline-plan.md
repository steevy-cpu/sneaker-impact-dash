# Web App Mirror + Local Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a web app inside the `sneaker-impact-dash` repo that mirrors the ShoeSort desktop operator workflow (table-photo capture, box-metadata popup, barcode) and processes uploads in the background with the local `sneaker_impact_training` engine (segment → pair → color → CLIP brand → Ollama model), writing results into an extended dash database.

**Architecture:** FastAPI + SQLite backend with a background worker thread; vanilla-JS frontend (no build step). The dash is the product/UI/DB; `sneaker_impact_training` is the CV/identification engine imported by the backend. Barcode resolves via a pluggable `ShipmentLookup` (Airtable now, FedEx later). The existing desktop app and ShoeSort backend are left untouched until cutover.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, SQLite (sqlite3 stdlib), Pydantic v2, Pillow, OpenCV (`opencv-python`), Ultralytics (YOLOE), OpenAI CLIP, Ollama (`qwen2.5vl`), pytest + FastAPI `TestClient`.

**Spec:** `docs/superpowers/specs/2026-06-04-webapp-mirror-local-pipeline-design.md` (in the ShoeSort-main repo).

---

## Working directories

- **Dash repo (where code lands):** `/home/sneakerai/Documents/sneaker-impact-dash`
- **Engine (added as submodule in P0):** `sneaker_impact_training/` inside the dash repo, mirror of `github.com/steevy-cpu/sneaker_impact_training`
- All paths below are **relative to the dash repo root** unless absolute.

## Plan structure note (not a placeholder)

This is a multi-subsystem effort. **Phases P0–P3 (the foundation that yields working software end-to-end) are written as full bite-sized TDD tasks with complete code.** Phases **P4–P7** are specified as concrete task lists with exact files, acceptance criteria, and key code — to be expanded into bite-sized steps when reached (their details depend on P0–P3 outputs). This staged detail is deliberate.

## Engine contract (verified from source — used throughout)

All engine functions take the training `config` module and **BGR `numpy` images** (OpenCV). Verified signatures:

- `segment_utils.build_segmenter(cfg)` → object with `.segment(image_bgr) -> list[Segment]`
- `Segment`: attributes `.bbox == (x1, y1, x2, y2)`, `.score: float`, `.label: str`, `.polygon` (np array or None), method `.area()`
- `pair_utils.pair_shoes(segs, max_gap: float) -> list[Segment]`
- `color_utils.classify_color(image_bgr, mask=None) -> (color: str, confidence: float | None)`
- `brand_utils.build_brand_classifier(cfg)` → object with `.classify(image_bgr) -> (make: str, conf: float | None)`
- `model_search.build_model_identifier(cfg)` → object with `.identify(image_bgr, brand) -> (model: str, conf: float | None, sources: list)`

The training modules use **flat imports** (`import config`, `from segment_utils import ...`) and expect their own directory on `sys.path`. The bridge handles this (P3, Task 3.1).

---

# Phase P0 — Baseline & repo setup

**Outcome:** The dash runs locally with a known-good baseline; the engine is available as a submodule; dependencies and `.gitignore` are correct.

### Task 0.1: Add `.gitignore` to the dash

**Files:**
- Create: `.gitignore`

- [ ] **Step 1: Write `.gitignore`**

```gitignore
__pycache__/
*.pyc
.venv/
venv/
.env
sneakers.db
sneakers.db-wal
sneakers.db-shm
images/
simulation_assets/sample_images/
*.pt
*.pth
*.onnx
*.npz
.pytest_cache/
```

- [ ] **Step 2: Verify nothing large is tracked**

Run: `git -C /home/sneakerai/Documents/sneaker-impact-dash status --short`
Expected: only `.gitignore` shows as untracked/new; no `.db`, no images.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore: add .gitignore for db, images, venvs, model weights"
```

### Task 0.2: Add the engine as a git submodule

**Files:**
- Create: `.gitmodules` (via git), `sneaker_impact_training/` (submodule)

- [ ] **Step 1: Add the submodule**

Run:
```bash
cd /home/sneakerai/Documents/sneaker-impact-dash
git submodule add https://github.com/steevy-cpu/sneaker_impact_training.git sneaker_impact_training
```
Expected: clones into `sneaker_impact_training/`, creates `.gitmodules`.

- [ ] **Step 2: Verify the engine modules are present**

Run: `ls sneaker_impact_training/segment_utils.py sneaker_impact_training/config.py`
Expected: both paths exist.

- [ ] **Step 3: Commit**

```bash
git add .gitmodules sneaker_impact_training
git commit -m "chore: add sneaker_impact_training engine as submodule"
```

### Task 0.3: Create a venv and install dependencies

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Extend `requirements.txt`** (append engine + test deps; keep existing fastapi/uvicorn/python-dotenv/Pillow)

```text
fastapi
uvicorn[standard]
python-dotenv
Pillow
opencv-python
numpy
pytest
httpx
```

> Note: heavy engine deps (`ultralytics`, `torch`, `clip`) are installed separately on the GPU server per `sneaker_impact_training/HANDOFF.md`; they are NOT added here so the web/test layer stays light. The bridge degrades gracefully when they are absent (P3).

- [ ] **Step 2: Create venv + install**

Run:
```bash
cd /home/sneakerai/Documents/sneaker-impact-dash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```
Expected: installs without error.

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: pin web + test dependencies"
```

### Task 0.4: Smoke-test the baseline

- [ ] **Step 1: Boot the server**

Run: `. .venv/bin/activate && uvicorn backend.main:app --port 8000 &` then `sleep 4 && curl -s http://localhost:8000/api/health`
Expected: JSON with `"mode": "simulation"`, `"db_connected": true`.

- [ ] **Step 2: Stop the server**

Run: `kill %1` (or the uvicorn PID).
Expected: clean shutdown message.

---

# Phase P1 — Data model + capture/metadata API

**Outcome:** New `table_photos` and `pairs` tables, plus endpoints to create a table photo with box metadata and read it back. No pipeline yet (status stays `pending`).

### Task 1.1: Add schema for `table_photos` and `pairs`

**Files:**
- Modify: `backend/database.py` (the `init_db` executescript + `_add_columns_if_missing`)
- Test: `tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_schema.py
import sqlite3
from backend.database import init_db, get_connection

def test_new_tables_exist(tmp_path, monkeypatch):
    import backend.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "t.db")
    import backend.database as db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db", raising=False)
    init_db()
    conn = get_connection()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"table_photos", "pairs"} <= names
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(table_photos)").fetchall()}
    assert {"barcode", "weight_of_box", "total_good_sneakers",
            "total_end_of_life", "casuals", "status", "num_pairs"} <= cols
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_schema.py -v`
Expected: FAIL (tables/columns missing).

- [ ] **Step 3: Add the tables to `init_db`** (append inside the `executescript` string, before the closing `"""`)

```sql
        -- ----------------------------------------------------------------
        -- table_photos: one row per whole-table photo + box metadata
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS table_photos (
            id                  TEXT PRIMARY KEY,
            batch_id            TEXT NOT NULL,
            operator_id         TEXT NOT NULL,
            image_path          TEXT,
            barcode             TEXT,
            weight_of_box       REAL,
            total_good_sneakers INTEGER,
            total_end_of_life   INTEGER,
            casuals             INTEGER,
            status              TEXT NOT NULL DEFAULT 'pending',
            error_message       TEXT,
            num_pairs           INTEGER NOT NULL DEFAULT 0,
            shipment_info       TEXT,
            created_at          TEXT NOT NULL,
            processed_at        TEXT,
            FOREIGN KEY (batch_id) REFERENCES batches(id)
        );

        -- ----------------------------------------------------------------
        -- pairs: one row per shoe pair found in a table photo
        -- ----------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS pairs (
            id                TEXT PRIMARY KEY,
            table_photo_id    TEXT NOT NULL,
            image_path        TEXT,
            bbox              TEXT,
            detected_color    TEXT,
            color_confidence  REAL,
            make              TEXT,
            make_confidence   REAL,
            model             TEXT,
            model_confidence  REAL,
            model_sources     TEXT,
            review_status     TEXT NOT NULL DEFAULT 'PENDING',
            final_make        TEXT,
            final_model       TEXT,
            notes             TEXT,
            created_at        TEXT NOT NULL,
            FOREIGN KEY (table_photo_id) REFERENCES table_photos(id)
        );

        CREATE INDEX IF NOT EXISTS idx_tp_batch   ON table_photos(batch_id);
        CREATE INDEX IF NOT EXISTS idx_tp_status  ON table_photos(status);
        CREATE INDEX IF NOT EXISTS idx_pairs_tp   ON pairs(table_photo_id);
        CREATE INDEX IF NOT EXISTS idx_pairs_rev  ON pairs(review_status);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/database.py tests/test_schema.py
git commit -m "feat(db): add table_photos and pairs tables"
```

### Task 1.2: ID generators for table photos and pairs

**Files:**
- Modify: `backend/utils/id_generator.py`
- Test: `tests/test_id_generator.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_id_generator.py
from datetime import date
from backend.database import init_db, get_connection
from backend.utils.id_generator import generate_table_photo_id, generate_pair_id

def test_ids_increment(tmp_path, monkeypatch):
    import backend.config as cfg, backend.database as db
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db", raising=False)
    init_db()
    conn = get_connection()
    today = date.today().strftime("%Y%m%d")
    a = generate_table_photo_id(conn)
    assert a == f"TBL-{today}-0001"
    p = generate_pair_id(conn)
    assert p == f"PAIR-{today}-0001"
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_id_generator.py -v`
Expected: FAIL (functions not defined).

- [ ] **Step 3: Add the generators** (append to `backend/utils/id_generator.py`)

```python
def generate_table_photo_id(conn: sqlite3.Connection) -> str:
    """Format: TBL-YYYYMMDD-NNNN"""
    today = date.today().strftime("%Y%m%d")
    prefix = f"TBL-{today}-"
    row = conn.execute(
        "SELECT id FROM table_photos WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    seq = (int(str(row["id"]).split("-")[-1]) + 1) if row else 1
    return f"{prefix}{seq:04d}"


def generate_pair_id(conn: sqlite3.Connection) -> str:
    """Format: PAIR-YYYYMMDD-NNNN"""
    today = date.today().strftime("%Y%m%d")
    prefix = f"PAIR-{today}-"
    row = conn.execute(
        "SELECT id FROM pairs WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
        (f"{prefix}%",),
    ).fetchone()
    seq = (int(str(row["id"]).split("-")[-1]) + 1) if row else 1
    return f"{prefix}{seq:04d}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_id_generator.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/utils/id_generator.py tests/test_id_generator.py
git commit -m "feat(ids): add table photo and pair id generators"
```

### Task 1.3: Pydantic models for capture

**Files:**
- Modify: `backend/models.py`
- Test: covered via Task 1.4 route tests.

- [ ] **Step 1: Add models** (append to `backend/models.py`)

```python
class MetadataCreate(BaseModel):
    operator_id:         str
    barcode:             Optional[str]   = None
    weight_of_box:       Optional[float] = None
    total_good_sneakers: Optional[int]   = None
    total_end_of_life:   Optional[int]   = None
    casuals:             Optional[int]   = None


class PairReview(BaseModel):
    final_make:    Optional[str] = None
    final_model:   Optional[str] = None
    review_status: str           = "COMPLETED"
    notes:         Optional[str] = None
```

- [ ] **Step 2: Commit**

```bash
git add backend/models.py
git commit -m "feat(models): add MetadataCreate and PairReview"
```

### Task 1.4: Capture route (upload table photo + metadata)

**Files:**
- Create: `backend/routes/capture.py`
- Modify: `backend/main.py` (register router)
- Test: `tests/test_capture.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_capture.py
import io
from PIL import Image
from fastapi.testclient import TestClient

def _client(tmp_path, monkeypatch):
    import backend.config as cfg, backend.database as db
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db", raising=False)
    monkeypatch.setattr(cfg, "IMAGES_DIR", tmp_path / "images")
    (tmp_path / "images").mkdir()
    db.init_db()
    from backend.main import app
    return TestClient(app)

def _png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (120, 120, 120)).save(buf, format="PNG")
    return buf.getvalue()

def test_capture_creates_pending_table_photo(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    batch = client.post("/api/batches", json={"operator_id": "OP-1"}).json()
    resp = client.post(
        "/api/capture",
        data={"operator_id": "OP-1", "batch_id": batch["id"],
              "barcode": "1Z999AA10123456784", "weight_of_box": "12.5",
              "total_good_sneakers": "8", "total_end_of_life": "3", "casuals": "2"},
        files={"image": ("table.png", _png_bytes(), "image/png")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["barcode"] == "1Z999AA10123456784"
    assert body["weight_of_box"] == 12.5
    got = client.get(f"/api/table-photos/{body['id']}").json()
    assert got["id"] == body["id"]
    assert got["pairs"] == []

def test_capture_requires_a_box_field(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    batch = client.post("/api/batches", json={"operator_id": "OP-1"}).json()
    resp = client.post(
        "/api/capture",
        data={"operator_id": "OP-1", "batch_id": batch["id"]},
        files={"image": ("table.png", _png_bytes(), "image/png")},
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_capture.py -v`
Expected: FAIL (no `/api/capture` route).

- [ ] **Step 3: Implement the route**

```python
# backend/routes/capture.py
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from backend.config import IMAGES_DIR
from backend.database import get_db
from backend.utils.id_generator import generate_table_photo_id

router = APIRouter(tags=["Capture"])


def table_photo_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "batch_id": row["batch_id"], "operator_id": row["operator_id"],
        "image_path": row["image_path"], "barcode": row["barcode"],
        "weight_of_box": row["weight_of_box"],
        "total_good_sneakers": row["total_good_sneakers"],
        "total_end_of_life": row["total_end_of_life"], "casuals": row["casuals"],
        "status": row["status"], "error_message": row["error_message"],
        "num_pairs": row["num_pairs"],
        "shipment_info": json.loads(row["shipment_info"]) if row["shipment_info"] else None,
        "created_at": row["created_at"], "processed_at": row["processed_at"],
    }


@router.post("/api/capture", status_code=201, summary="Upload a table photo + box metadata")
def capture(
    image:               UploadFile = File(...),
    operator_id:         str        = Form(...),
    batch_id:            str        = Form(...),
    barcode:             Optional[str]   = Form(None),
    weight_of_box:       Optional[float] = Form(None),
    total_good_sneakers: Optional[int]   = Form(None),
    total_end_of_life:   Optional[int]   = Form(None),
    casuals:             Optional[int]   = Form(None),
    conn: sqlite3.Connection = Depends(get_db),
):
    batch = conn.execute("SELECT id FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if not batch:
        raise HTTPException(400, f"Batch '{batch_id}' does not exist")

    # mirror the desktop rule: at least one box field must be provided and > 0
    box_values = [weight_of_box, total_good_sneakers, total_end_of_life, casuals]
    if not any(v is not None and v > 0 for v in box_values):
        raise HTTPException(400, "At least one box field (weight/good/eol/casuals) must be > 0")

    tp_id = generate_table_photo_id(conn)
    now = datetime.now().isoformat()
    dest_dir = Path(IMAGES_DIR) / tp_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(image.filename or "table.jpg").suffix or ".jpg"
    abs_path = dest_dir / f"table{suffix}"
    abs_path.write_bytes(image.file.read())
    image_url = f"/images/{tp_id}/table{suffix}"

    conn.execute(
        """INSERT INTO table_photos (
            id, batch_id, operator_id, image_path, barcode,
            weight_of_box, total_good_sneakers, total_end_of_life, casuals,
            status, num_pairs, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
        (tp_id, batch_id, operator_id, image_url, barcode,
         weight_of_box, total_good_sneakers, total_end_of_life, casuals, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM table_photos WHERE id = ?", (tp_id,)).fetchone()
    return table_photo_to_dict(row)


@router.get("/api/table-photos", summary="List table photos")
def list_table_photos(
    status: Optional[str] = None,
    batch_id: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
):
    filters, params = [], []
    if status:   filters.append("status = ?");   params.append(status)
    if batch_id: filters.append("batch_id = ?"); params.append(batch_id)
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    rows = conn.execute(
        f"SELECT * FROM table_photos {where} ORDER BY created_at DESC", params
    ).fetchall()
    return [table_photo_to_dict(r) for r in rows]


@router.get("/api/table-photos/{tp_id}", summary="Get a table photo + its pairs")
def get_table_photo(tp_id: str, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute("SELECT * FROM table_photos WHERE id = ?", (tp_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"Table photo '{tp_id}' not found")
    pairs = conn.execute(
        "SELECT * FROM pairs WHERE table_photo_id = ? ORDER BY id", (tp_id,)
    ).fetchall()
    out = table_photo_to_dict(row)
    out["pairs"] = [dict(p) for p in pairs]
    return out


@router.get("/api/table-photos/{tp_id}/status", summary="Lightweight status for polling")
def table_photo_status(tp_id: str, conn: sqlite3.Connection = Depends(get_db)):
    row = conn.execute(
        "SELECT status, num_pairs, error_message FROM table_photos WHERE id = ?", (tp_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, f"Table photo '{tp_id}' not found")
    return {"status": row["status"], "num_pairs": row["num_pairs"],
            "error_message": row["error_message"]}
```

- [ ] **Step 4: Register the router** in `backend/main.py`

Change the import line:
```python
from backend.routes import analytics, batches, capture, export, health, shoes, simulation
```
And add after `app.include_router(shoes.router)`:
```python
app.include_router(capture.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_capture.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add backend/routes/capture.py backend/main.py tests/test_capture.py
git commit -m "feat(api): capture endpoint + table-photo read endpoints"
```

---

# Phase P2 — Operator capture page (web parity)

**Outcome:** A browser page that previews a camera, captures one table photo, opens the box-metadata popup (weight/good/EOL/casuals + barcode), and POSTs to `/api/capture`. File-upload fallback for no-camera testing.

### Task 2.1: Capture page HTML

**Files:**
- Create: `frontend/capture.html`
- Modify: `frontend/index.html` (add nav link to Capture) — follow the existing sidebar nav markup pattern in that file.

- [ ] **Step 1: Create `frontend/capture.html`** with: a `<video id="cam">` preview, a "Use file instead" `<input type="file">` fallback, a **Capture** button, and a hidden metadata modal containing inputs `#barcode`, `#weight_of_box`, `#total_good_sneakers`, `#total_end_of_life`, `#casuals`, plus **Submit**/**Cancel**. Reuse the dash's existing CSS classes/header/sidebar from `frontend/index.html` for visual consistency. Include `<script src="js/capture.js"></script>`.

- [ ] **Step 2: Manual check** — open `http://localhost:8000/frontend/capture.html`; the page renders with the header/sidebar and a Capture button.

- [ ] **Step 3: Commit**

```bash
git add frontend/capture.html frontend/index.html
git commit -m "feat(ui): capture page scaffold + nav link"
```

### Task 2.2: Capture page JS (camera, popup, submit)

**Files:**
- Create: `frontend/js/capture.js`

- [ ] **Step 1: Implement `capture.js`** with this behavior (use the existing `js/api.js` `apiFetch` base URL pattern):
  - On load: `navigator.mediaDevices.getUserMedia({video: true})` → set `video.srcObject`. On failure, hide the video and show the file-input fallback.
  - Ensure a batch exists: `POST /api/batches {operator_id}` once per session, cache `batch_id` in `localStorage`.
  - **Capture** (button click, or `Space`/`Enter` keydown with a 3s debounce flag): grab the current frame to a `<canvas>` → `canvas.toBlob()` (or use the chosen file). Store the blob, then open the metadata modal.
  - On barcode field `blur`: `GET /api/shipment/{barcode}` (added P5); if it 404s or errors, ignore (fail-safe). When present, show partner/weight inline.
  - **Submit**: validate at least one box field > 0 (else show error, keep modal open). Build `FormData` with `image`, `operator_id`, `batch_id`, `barcode`, and the four box fields → `POST /api/capture`. On 201: toast "Queued <id>", close modal, reset.
  - **Cancel**: close modal, discard blob.

- [ ] **Step 2: Manual end-to-end check** — with the server running, capture (or upload) a photo, fill weight=10, submit. Confirm a 201 and that `GET /api/table-photos` shows the new `pending` row.

- [ ] **Step 3: Commit**

```bash
git add frontend/js/capture.js
git commit -m "feat(ui): capture camera + metadata popup + submit"
```

---

# Phase P3 — Background worker + local pipeline bridge

**Outcome:** Pending table photos are processed in the background: segment → pair → color → brand → model; `pairs` rows are written and `table_photos.status` becomes `completed` (or `failed`). The bridge degrades gracefully when heavy deps/Ollama are unavailable.

### Task 3.1: Pipeline bridge (engine adapter)

**Files:**
- Create: `backend/services/pipeline.py`
- Test: `tests/test_pipeline_bridge.py`

- [ ] **Step 1: Write the failing test** (uses a fake engine injected via params — no GPU needed)

```python
# tests/test_pipeline_bridge.py
import numpy as np
from backend.services.pipeline import process_table_image

class _Seg:
    def __init__(self): self.bbox = (0, 0, 10, 10); self.score = 0.9; \
        self.label = "shoe"; self.polygon = None
    def area(self): return 100

class _Segmenter:
    def segment(self, img): return [_Seg()]
class _Brander:
    def classify(self, img): return ("Nike", 0.81)
class _Modeler:
    def identify(self, img, brand): return ("Air Force 1", 0.95, ["ollama"])

def test_process_table_image_returns_pairs():
    img = np.zeros((40, 40, 3), dtype="uint8")
    pairs = process_table_image(
        img,
        segmenter=_Segmenter(),
        brander=_Brander(),
        modeler=_Modeler(),
        pair_fn=lambda segs, gap: segs,
        color_fn=lambda crop, mask=None: ("gray", 0.7),
    )
    assert len(pairs) == 1
    p = pairs[0]
    assert p["make"] == "Nike" and p["make_confidence"] == 0.81
    assert p["model"] == "Air Force 1"
    assert p["detected_color"] == "gray"
    assert p["bbox"] == [0, 0, 10, 10]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline_bridge.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the bridge**

```python
# backend/services/pipeline.py
"""
Bridge from the dash to the sneaker_impact_training engine.

The engine modules use flat imports and expect their own dir on sys.path.
build_engine() wires that up and constructs the real components; process_
table_image() is engine-agnostic (components are injected) so it is unit-
testable without GPU/torch/Ollama.
"""
import sys
from pathlib import Path

_ENGINE_DIR = Path(__file__).resolve().parents[2] / "sneaker_impact_training"


def build_engine():
    """Construct real engine components. Raises if heavy deps are missing."""
    if str(_ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(_ENGINE_DIR))
    import config as engine_config
    from segment_utils import build_segmenter
    from brand_utils import build_brand_classifier
    from model_search import build_model_identifier
    from pair_utils import pair_shoes
    from color_utils import classify_color
    return {
        "segmenter": build_segmenter(engine_config),
        "brander": build_brand_classifier(engine_config),
        "modeler": build_model_identifier(engine_config),
        "pair_fn": pair_shoes,
        "color_fn": classify_color,
        "max_gap": getattr(engine_config, "SEGMENT_PAIR_MAX_GAP", 1.2),
        "pad": getattr(engine_config, "SEGMENT_CROP_PAD", 0.0),
    }


def _pad_bbox(bbox, w, h, frac):
    x1, y1, x2, y2 = bbox
    px, py = int((x2 - x1) * frac), int((y2 - y1) * frac)
    return (max(0, x1 - px), max(0, y1 - py), min(w, x2 + px), min(h, y2 + py))


def process_table_image(image_bgr, *, segmenter, brander, modeler,
                        pair_fn, color_fn, max_gap=1.2, pad=0.0):
    """Run the full pipeline on one BGR image. Returns a list of pair dicts."""
    h, w = image_bgr.shape[:2]
    segs = segmenter.segment(image_bgr)
    segs = pair_fn(segs, max_gap)

    results = []
    for seg in segs:
        x1, y1, x2, y2 = _pad_bbox(seg.bbox, w, h, pad)
        if x2 <= x1 or y2 <= y1:
            continue
        crop = image_bgr[y1:y2, x1:x2]
        color, color_conf = color_fn(crop)
        make, make_conf = brander.classify(crop)
        model, model_conf, sources = modeler.identify(crop, make)
        results.append({
            "bbox": [x1, y1, x2, y2],
            "detected_color": color, "color_confidence": color_conf,
            "make": make, "make_confidence": make_conf,
            "model": model, "model_confidence": model_conf,
            "model_sources": sources,
        })
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pipeline_bridge.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/services/pipeline.py tests/test_pipeline_bridge.py
git commit -m "feat(pipeline): engine bridge (injectable, unit-tested)"
```

### Task 3.2: Background job worker

**Files:**
- Create: `backend/services/jobqueue.py`
- Modify: `backend/main.py` (start worker in `lifespan`)
- Test: `tests/test_jobqueue.py`

- [ ] **Step 1: Write the failing test** (worker uses an injected `process_fn` + a crop loader, so no real engine)

```python
# tests/test_jobqueue.py
import json, numpy as np
from backend.database import init_db, get_connection
from backend.services.jobqueue import process_one_pending

def _seed_pending(conn, tp_id):
    from datetime import datetime
    conn.execute("INSERT INTO batches (id, started_at, operator_id, shoe_count) "
                 "VALUES ('B1', ?, 'OP', 0)", (datetime.now().isoformat(),))
    conn.execute("INSERT INTO table_photos (id, batch_id, operator_id, image_path, "
                 "status, num_pairs, created_at) VALUES (?, 'B1', 'OP', '/x.jpg', "
                 "'pending', 0, ?)", (tp_id, datetime.now().isoformat()))
    conn.commit()

def test_process_one_pending_writes_pairs(tmp_path, monkeypatch):
    import backend.config as cfg, backend.database as db
    monkeypatch.setattr(cfg, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db", raising=False)
    init_db()
    conn = get_connection(); _seed_pending(conn, "TBL-X-0001"); conn.close()

    fake_pairs = [{"bbox": [0,0,1,1], "detected_color": "red", "color_confidence": 0.6,
                   "make": "Nike", "make_confidence": 0.8, "model": "Dunk",
                   "model_confidence": 0.9, "model_sources": ["ollama"]}]
    did = process_one_pending(
        load_image=lambda path: np.zeros((4,4,3), "uint8"),
        process_fn=lambda img: fake_pairs,
    )
    assert did is True
    conn = get_connection()
    tp = conn.execute("SELECT status, num_pairs FROM table_photos WHERE id='TBL-X-0001'").fetchone()
    assert tp["status"] == "completed" and tp["num_pairs"] == 1
    pr = conn.execute("SELECT make, model_sources FROM pairs").fetchone()
    assert pr["make"] == "Nike" and json.loads(pr["model_sources"]) == ["ollama"]
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_jobqueue.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement the worker**

```python
# backend/services/jobqueue.py
"""
Background worker: drains 'pending' table_photos one at a time, runs the
pipeline, writes pairs, and marks the row completed/failed. Single worker
keeps GPU/VRAM use bounded. process_one_pending() is injectable for tests.
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

from backend.config import IMAGES_DIR
from backend.database import get_connection
from backend.utils.id_generator import generate_pair_id

_stop = threading.Event()


def _default_load_image(image_url: str):
    # image_url like "/images/TBL-.../table.jpg" -> absolute path under IMAGES_DIR
    rel = image_url.split("/images/", 1)[-1]
    return cv2.imread(str(Path(IMAGES_DIR) / rel))


def process_one_pending(*, load_image=_default_load_image, process_fn=None) -> bool:
    """Process the oldest pending row. Returns True if one was handled."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM table_photos WHERE status='pending' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if not row:
            return False
        tp_id = row["id"]
        conn.execute("UPDATE table_photos SET status='processing' WHERE id=?", (tp_id,))
        conn.commit()
        try:
            if process_fn is None:
                from backend.services.pipeline import build_engine, process_table_image
                eng = build_engine()
                process_fn = lambda img: process_table_image(
                    img, segmenter=eng["segmenter"], brander=eng["brander"],
                    modeler=eng["modeler"], pair_fn=eng["pair_fn"],
                    color_fn=eng["color_fn"], max_gap=eng["max_gap"], pad=eng["pad"])
            img = load_image(row["image_path"])
            if img is None:
                raise ValueError(f"could not read image {row['image_path']}")
            pairs = process_fn(img)
            now = datetime.now().isoformat()
            for p in pairs:
                conn.execute(
                    """INSERT INTO pairs (id, table_photo_id, image_path, bbox,
                        detected_color, color_confidence, make, make_confidence,
                        model, model_confidence, model_sources, review_status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
                    (generate_pair_id(conn), tp_id, row["image_path"],
                     json.dumps(p["bbox"]), p["detected_color"], p["color_confidence"],
                     p["make"], p["make_confidence"], p["model"], p["model_confidence"],
                     json.dumps(p["model_sources"]), now))
            conn.execute(
                "UPDATE table_photos SET status='completed', num_pairs=?, processed_at=? WHERE id=?",
                (len(pairs), now, tp_id))
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - never crash the worker
            conn.execute("UPDATE table_photos SET status='failed', error_message=? WHERE id=?",
                         (str(exc)[:500], tp_id))
            conn.commit()
        return True
    finally:
        conn.close()


def _loop(poll_seconds: float = 1.0):
    while not _stop.is_set():
        try:
            handled = process_one_pending()
        except Exception as exc:  # noqa: BLE001
            print(f"[worker] error: {exc}"); handled = False
        if not handled:
            time.sleep(poll_seconds)


def start_worker():
    _stop.clear()
    t = threading.Thread(target=_loop, daemon=True, name="pipeline-worker")
    t.start()
    return t


def stop_worker():
    _stop.set()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_jobqueue.py -v`
Expected: PASS.

- [ ] **Step 5: Start the worker in `lifespan`** (`backend/main.py`)

In the `lifespan` function, after `init_db()` and the simulation block, before `yield`, add:
```python
    from backend.services.jobqueue import start_worker, stop_worker
    start_worker()
```
And after `yield`:
```python
    stop_worker()
```

- [ ] **Step 6: Run the full suite**

Run: `pytest -v`
Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/services/jobqueue.py backend/main.py tests/test_jobqueue.py
git commit -m "feat(worker): background pipeline worker draining pending photos"
```

### Task 3.3: GPU server smoke test (manual, on the server computer)

- [ ] **Step 1:** On the GPU server, install engine deps (`pip install ultralytics`), ensure weights + `ollama serve` + `ollama pull qwen2.5vl:7b`.
- [ ] **Step 2:** Boot the dash, POST a real table photo via the capture page.
- [ ] **Step 3:** Poll `GET /api/table-photos/{id}/status` until `completed`; confirm `pairs` populated with color/make/model. If Ollama is down, confirm `model` is `unknown` but color+make still fill (graceful degrade).

---

# Phase P4 — Review / Live / Analytics pages for pairs

**Outcome:** Operators can see processed table photos, drill into pairs, and confirm/override make/model.

### Task 4.1: Pair review endpoint
**Files:** Create `backend/routes/pairs.py`; register in `backend/main.py`; Test `tests/test_pairs.py`.
- `GET /api/pairs?table_photo_id=...` → list pair rows.
- `PATCH /api/pairs/{id}/review` (body `PairReview`) → set `final_make`, `final_model`, `review_status`, `notes`; 404 if missing.
- **Acceptance:** test creates a pair, PATCHes it, asserts `final_make`/`review_status` persisted.

### Task 4.2: Table-photos list page
**Files:** Create `frontend/table_photos.html` + `frontend/js/table_photos.js`; nav link.
- Table of photos (newest first): id, barcode, status badge, num_pairs, created_at. Row → detail page.
- **Acceptance:** page lists rows from `GET /api/table-photos` and reflects status.

### Task 4.3: Table-photo detail page (pairs + review)
**Files:** Create `frontend/table_photo_detail.html` + `frontend/js/table_photo_detail.js`.
- Show the table photo image + box metadata; grid of pairs with crop, color/make/model + confidences; inline confirm/override form calling `PATCH /api/pairs/{id}/review`.
- **Acceptance:** editing a pair persists and re-renders as COMPLETED.

### Task 4.4: Extend analytics for pairs (optional, keep existing shoe analytics intact)
**Files:** Modify `backend/routes/analytics.py` (add a pairs-by-brand endpoint) + `frontend/js/analytics.js`.
- **Acceptance:** brand distribution reflects `pairs.make`.

---

# Phase P5 — Shipment lookup (Airtable now, FedEx interface)

**Outcome:** Barcode resolves to shipment info via a pluggable interface; Airtable implemented, FedEx stubbed.

### Task 5.1: ShipmentLookup interface + Airtable impl
**Files:** Create `backend/services/shipment_lookup.py`; Test `tests/test_shipment_lookup.py`.
- Define `ShipmentInfo` dataclass `{carrier, tracking_number, partner, weight, address, status, raw}`.
- `class AirtableShipmentLookup` — query Airtable Orders/Shipments by barcode (reuse field mapping from `shoesort/api/airtable_api/`: `Barcode`, `Carrier`, `Weight`, `Partner`). Fail-safe: return `None` on any error.
- `class FedExShipmentLookup` — stub raising `NotImplementedError` internally but returning `None` (so it's safe to wire later).
- `class ChainedLookup([...])` — first non-None wins.
- `build_lookup(config)` selects active lookups.
- **Acceptance:** unit test with a mocked Airtable HTTP layer returns a populated `ShipmentInfo`; error path returns `None`.

### Task 5.2: Shipment endpoint + capture integration
**Files:** Create `backend/routes/shipment.py`; register; modify `backend/routes/capture.py` to resolve + store `shipment_info` on capture (best-effort).
- `GET /api/shipment/{barcode}` → `ShipmentInfo` or 404.
- **Acceptance:** capture with a known barcode stores `shipment_info` JSON; unknown barcode still succeeds (null).

---

# Phase P6 — Config tab + unified refresh

**Outcome:** A real station config UI and consistent refresh across pages.

### Task 6.1: Station config storage + API
**Files:** Create `backend/routes/config_station.py` (+ a `station_config` table or JSON file); Test `tests/test_config_station.py`.
- `GET /api/config/station`, `PUT /api/config/station` (server URL, camera device id, resolution, operator/station id, pipeline toggles, integration toggles).
- **Acceptance:** PUT then GET round-trips the config.

### Task 6.2: Config tab UI
**Files:** Create `frontend/config.html` + `frontend/js/config.js`; nav link.
- Form bound to `/api/config/station`; camera dropdown from `navigator.mediaDevices.enumerateDevices()`; persists selection.
- **Acceptance:** changing + saving config persists across reload.

### Task 6.3: Unified refresh helper
**Files:** Create `frontend/js/refresh.js`; refactor `table_photos.js`, `table_photo_detail.js`, `dashboard.js` to use it; remove carousel polling.
- Visibility-aware polling (pause when `document.hidden`); status pages poll `/status` every ~2s while `processing`, stop on terminal; manual Refresh button.
- **Acceptance:** processing detail page auto-updates to completed without manual reload; hidden tab stops polling.

---

# Phase P7 — Hardware validation + cutover

**Outcome:** The web app is validated on the real camera/table setup and becomes the primary capture path.

### Task 7.1: End-to-end hardware validation (manual)
- Real camera at the station → capture a real table → confirm pairs/color/make/model and box metadata; compare throughput/accuracy vs desktop app. Record results.

### Task 7.2: Deployment
**Files:** Add a `deployment/` note or systemd unit for the dash on the server (port distinct from existing ShoeSort `:5002`).
- **Acceptance:** dash auto-starts; worker runs; survives reboot.

### Task 7.3: Cutover
- Switch operators to the web app; retire the desktop client. Keep the desktop app available as fallback for one cycle.

---

## Self-review checklist (completed by author)

- **Spec coverage:** P1 (schema/API §7–8), P2 (capture UX §9), P3 (worker+engine §10), P4 (review pages, §7 pair fields), P5 (shipment lookup §11), P6 (config §12 + refresh §13), P7 (cutover §16). Error handling (§14) covered in capture validation + worker try/except + fail-safe lookup. Testing (§15) covered by per-task pytest + manual GPU/hardware gates.
- **Type consistency:** pair dict keys (`bbox`, `detected_color`, `color_confidence`, `make`, `make_confidence`, `model`, `model_confidence`, `model_sources`) are identical across `pipeline.py`, `jobqueue.py`, the `pairs` table, and tests. Engine signatures match verified source.
- **Placeholders:** none in P0–P3 (full code). P4–P7 are intentionally task-level (documented above) and must be expanded to bite-sized TDD steps when reached.
- **Open risks (from spec §18):** browser camera (UVC) assumption — validate in P7; engine weights/Ollama presence on server — P3.3; engine import boundary — resolved via submodule (P0.2) + sys.path shim (P3.1).
```
