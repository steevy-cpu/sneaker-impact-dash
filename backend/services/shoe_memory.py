"""
shoe_memory.py — the "seen-shoe" cache (Tier 0 of brand/model identification).

Before paying for a cloud call on an uncertain pair, the worker looks up the
pair's DINOv2 appearance embedding here; a confident nearest-neighbour reuses
that brand/model for free. Embeddings are L2-normalized float32, so cosine
similarity is just a dot product — no numpy needed (kept out of the lightweight
dash venv). A small in-process snapshot is cached and refreshed only when the
table changes, so repeated lookups in the single background worker stay cheap.

Trust model: only ever POPULATED from reliable labels (gold > silver), never
low-confidence guesses; a high similarity threshold guards against DINOv2's
silhouette-over-identity bias assigning the wrong brand. All entries are stamped
with the `embedder` identity so the cache can be invalidated if the engine ever
swaps embedders (CLIP and the DINOv2 variants produce different-sized vectors).
"""
import sqlite3
import struct
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Vector (de)serialization — packed little-endian float32
# ---------------------------------------------------------------------------

def pack_vector(vec) -> bytes:
    """Pack a sequence of floats into a float32 BLOB."""
    return struct.pack(f"<{len(vec)}f", *(float(x) for x in vec))


def unpack_vector(blob: bytes) -> list:
    """Unpack a float32 BLOB back into a list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _l2_normalize(vec) -> list:
    """Return an L2-normalized copy (defensive — embedder output is already
    normalized, but seeding from external vectors shouldn't assume it)."""
    s = 0.0
    for x in vec:
        s += float(x) * float(x)
    norm = s ** 0.5
    if norm == 0:
        return [0.0] * len(vec)
    return [float(x) / norm for x in vec]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def add_entry(conn: sqlite3.Connection, vec, *, embedder: str, brand=None,
              model=None, color=None, source="silver", confidence=None,
              source_ref=None, normalize=True) -> int:
    """Insert one shoe into the memory. Returns the new row id. Caller decides
    trust (source/confidence); this does not gate. Invalidates the snapshot."""
    v = _l2_normalize(vec) if normalize else list(vec)
    now = datetime.now().isoformat()
    cur = conn.execute(
        """INSERT INTO shoe_memory
           (embedding, dim, embedder, brand, model, color, source, confidence,
            source_ref, n_seen, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
        (pack_vector(v), len(v), embedder, brand, model, color, source,
         confidence, source_ref, now, now),
    )
    conn.commit()
    # Keep a warm snapshot consistent without a full reload (write-back path).
    _INDEX.append(embedder, v, {"id": cur.lastrowid, "brand": brand, "model": model,
                                "color": color, "source": source,
                                "confidence": confidence, "source_ref": source_ref})
    return cur.lastrowid


def count(conn: sqlite3.Connection, embedder: Optional[str] = None) -> int:
    if embedder:
        return conn.execute(
            "SELECT COUNT(*) FROM shoe_memory WHERE embedder = ?", (embedder,)
        ).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM shoe_memory").fetchone()[0]


def clear(conn: sqlite3.Connection, embedder: Optional[str] = None) -> int:
    """Delete all entries (optionally only for one embedder). For rebuilds."""
    if embedder:
        cur = conn.execute("DELETE FROM shoe_memory WHERE embedder = ?", (embedder,))
    else:
        cur = conn.execute("DELETE FROM shoe_memory")
    conn.commit()
    _INDEX.invalidate()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Lookup (cached in-process snapshot, numpy-vectorized when available)
# ---------------------------------------------------------------------------

try:
    import numpy as _np                               # CPU-only; pinned <2.4 in the venv
    _HAVE_NUMPY = True
except Exception:                                      # pragma: no cover - fallback
    _np = None
    _HAVE_NUMPY = False


class _MemoryIndex:
    """Holds the loaded vectors for ONE embedder, rebuilt when its row count
    changes. Vectorized with numpy — a single matrix-vector product per lookup
    (~ms over 10k x 1024) instead of a pure-Python dot per row (~seconds), which
    made the cache a net slowdown. Pure-Python fallback kept for a numpy-less env.
    The background worker is single-threaded, so a module-level snapshot is safe."""

    def __init__(self):
        self.embedder = None
        self.count = -1
        self.meta = []            # list of meta dicts, parallel to rows
        self.mat = None           # numpy (N, D) float32 when numpy is present
        self.rows = []            # [(vec_list, meta)] for the pure-Python fallback

    def invalidate(self):
        self.count = -1

    def append(self, embedder: str, vec, meta: dict):
        """Keep a warm snapshot warm after a single insert (write-back)."""
        if self.embedder != embedder or self.count < 0:
            return
        self.meta.append(meta)
        if _HAVE_NUMPY:
            v = _np.asarray(vec, dtype="float32").reshape(1, -1)
            self.mat = v if self.mat is None else _np.vstack([self.mat, v])
        else:
            self.rows.append((list(vec), meta))
        self.count += 1

    def ensure(self, conn: sqlite3.Connection, embedder: str):
        n = count(conn, embedder)
        if self.embedder == embedder and self.count == n:
            return
        meta, vecs, rows = [], [], []
        for r in conn.execute(
            "SELECT id, embedding, brand, model, color, source, confidence, source_ref "
            "FROM shoe_memory WHERE embedder = ?", (embedder,)
        ).fetchall():
            m = {"id": r["id"], "brand": r["brand"], "model": r["model"],
                 "color": r["color"], "source": r["source"],
                 "confidence": r["confidence"], "source_ref": r["source_ref"]}
            meta.append(m)
            if _HAVE_NUMPY:
                vecs.append(_np.frombuffer(r["embedding"], dtype="<f4"))
            else:
                rows.append((unpack_vector(r["embedding"]), m))
        self.embedder, self.count, self.meta = embedder, n, meta
        if _HAVE_NUMPY:
            self.mat = _np.vstack(vecs).astype("float32") if vecs else None
            self.rows = []
        else:
            self.rows, self.mat = rows, None


_INDEX = _MemoryIndex()


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def lookup(conn: sqlite3.Connection, vec, *, embedder: str, min_sim: float,
           top_k: int = 3, min_agree: int = 2, normalize: bool = True) -> Optional[dict]:
    """Conservative auto-accept lookup: consult the TOP-K nearest neighbours
    (cosine >= min_sim) and accept only when >= min_agree of them AGREE on brand.
    Returns the majority brand with the nearest agreeing neighbour's model/color,
    else None. This vote — not a higher threshold — is what lifts precision past
    DINOv2's silhouette-over-identity bias (see the 2026-06-24 calibration).

    Result: {brand, model, color, source, confidence, similarity, agree, k,
    source_ref}. top_k=1 degrades to a plain nearest-neighbour match."""
    _INDEX.ensure(conn, embedder)
    if _INDEX.count <= 0:
        return None

    if _HAVE_NUMPY and _INDEX.mat is not None:
        q = _np.asarray(vec, dtype="float32")
        if normalize:
            q = q / (float(_np.linalg.norm(q)) or 1.0)
        if q.shape[0] != _INDEX.mat.shape[1]:
            return None
        sims = _INDEX.mat @ q                          # (N,) cosine (rows L2-normalized)
        cand = _np.where(sims >= min_sim)[0]
        if cand.size < min_agree:
            return None
        k = min(max(1, top_k), int(cand.size))
        topc = cand[_np.argsort(sims[cand])[::-1][:k]]
        top = [(float(sims[i]), _INDEX.meta[int(i)]) for i in topc]
    else:
        q = _l2_normalize(vec) if normalize else list(vec)
        scored = []
        for v, meta in _INDEX.rows:
            if len(v) == len(q):
                s = _dot(q, v)
                if s >= min_sim:
                    scored.append((s, meta))
        if len(scored) < min_agree:
            return None
        scored.sort(key=lambda s: s[0], reverse=True)
        top = scored[:max(1, top_k)]

    # Majority brand among the top-k (case-insensitive tally).
    tally = {}
    for sim, meta in top:
        b = (meta.get("brand") or "").strip().lower()
        if not b or b == "unknown":
            continue
        tally[b] = tally.get(b, 0) + 1
    if not tally:
        return None
    win_brand, agree = max(tally.items(), key=lambda kv: kv[1])
    if agree < min_agree:
        return None

    # Represent the match with the NEAREST neighbour of the winning brand.
    for sim, meta in top:
        if (meta.get("brand") or "").strip().lower() == win_brand:
            out = dict(meta)
            out["similarity"] = round(sim, 4)
            out["agree"] = agree
            out["k"] = len(top)
            return out
    return None


# ---------------------------------------------------------------------------
# Maintenance: dedup write-back, size-cap eviction, gold correction
# ---------------------------------------------------------------------------

def nearest(conn: sqlite3.Connection, vec, *, embedder: str,
            normalize: bool = True) -> Optional[tuple]:
    """Return (similarity, meta) of the single nearest entry (no threshold), or
    None when the cache is empty. meta includes the row 'id'."""
    _INDEX.ensure(conn, embedder)
    if _INDEX.count <= 0:
        return None
    if _HAVE_NUMPY and _INDEX.mat is not None:
        q = _np.asarray(vec, dtype="float32")
        if normalize:
            q = q / (float(_np.linalg.norm(q)) or 1.0)
        if q.shape[0] != _INDEX.mat.shape[1]:
            return None
        sims = _INDEX.mat @ q
        i = int(_np.argmax(sims))
        return (float(sims[i]), _INDEX.meta[i])
    q = _l2_normalize(vec) if normalize else list(vec)
    best, best_sim = None, -1.0
    for v, meta in _INDEX.rows:
        if len(v) == len(q):
            s = _dot(q, v)
            if s > best_sim:
                best_sim, best = s, meta
    return (best_sim, best) if best is not None else None


def bump_seen(conn: sqlite3.Connection, entry_id: int):
    """Increment an entry's n_seen (a duplicate sighting). Does NOT touch the
    in-memory snapshot — n_seen isn't used by lookup, only by eviction."""
    conn.execute(
        "UPDATE shoe_memory SET n_seen = n_seen + 1, updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), entry_id),
    )
    conn.commit()


def trim(conn: sqlite3.Connection, embedder: str, max_rows: int) -> int:
    """Keep the cache bounded: when over max_rows, evict the least-valuable
    NON-gold rows (lowest n_seen, then oldest). Gold (human-verified) is never
    evicted. Returns how many were removed. No-op when max_rows<=0."""
    if max_rows <= 0:
        return 0
    n = count(conn, embedder)
    if n <= max_rows:
        return 0
    excess = n - max_rows
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM shoe_memory WHERE embedder = ? AND source != 'gold' "
        "ORDER BY n_seen ASC, updated_at ASC LIMIT ?", (embedder, excess)
    ).fetchall()]
    if ids:
        conn.executemany("DELETE FROM shoe_memory WHERE id = ?", [(i,) for i in ids])
        conn.commit()
        _INDEX.invalidate()
    return len(ids)


def remember(conn: sqlite3.Connection, vec, *, embedder: str, brand=None, model=None,
             color=None, source="cloud", confidence=None, source_ref=None,
             dedup_sim: float = 0.985, max_rows: int = 0, normalize: bool = False) -> int:
    """Write-back with DEDUP + SIZE-CAP. If a near-identical entry already exists
    (cosine >= dedup_sim — i.e. we've seen this exact shoe), just bump its n_seen
    instead of storing a duplicate (so repeated shoes don't bloat the cache and
    slow lookups). Otherwise add it, then trim to max_rows. Returns the row id
    (existing on dedup, new otherwise)."""
    near = nearest(conn, vec, embedder=embedder, normalize=normalize)
    if near and near[0] >= dedup_sim and near[1].get("id") is not None:
        bump_seen(conn, near[1]["id"])
        return near[1]["id"]
    rid = add_entry(conn, vec, embedder=embedder, brand=brand, model=model,
                    color=color, source=source, confidence=confidence,
                    source_ref=source_ref, normalize=normalize)
    if max_rows and max_rows > 0:
        trim(conn, embedder, max_rows)
    return rid


def correct_near(conn: sqlite3.Connection, vec, *, embedder: str, brand, model,
                 sim_min: float = 0.985, normalize: bool = False) -> int:
    """Gold correction: relabel near-identical NON-gold entries (cosine >= sim_min,
    i.e. other crops of the SAME shoe) to a confirmed gold brand/model, so a
    wrong silver label can't keep matching. Returns how many were corrected."""
    _INDEX.ensure(conn, embedder)
    if not (_HAVE_NUMPY and _INDEX.mat is not None):
        return 0
    q = _np.asarray(vec, dtype="float32")
    if normalize:
        q = q / (float(_np.linalg.norm(q)) or 1.0)
    if q.shape[0] != _INDEX.mat.shape[1]:
        return 0
    sims = _INDEX.mat @ q
    bl = (brand or "").strip().lower()
    fix = [int(_INDEX.meta[i]["id"]) for i in _np.where(sims >= sim_min)[0]
           if _INDEX.meta[i].get("source") != "gold"
           and (_INDEX.meta[i].get("brand") or "").strip().lower() != bl]
    if not fix:
        return 0
    conn.executemany(
        "UPDATE shoe_memory SET brand = ?, model = ?, source = 'gold-corrected', "
        "updated_at = ? WHERE id = ?",
        [(brand, model, datetime.now().isoformat(), i) for i in fix],
    )
    conn.commit()
    _INDEX.invalidate()
    return len(fix)
