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
    _INDEX.append(embedder, v, {"brand": brand, "model": model, "color": color,
                                "source": source, "confidence": confidence,
                                "source_ref": source_ref})
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
# Lookup (cached in-process snapshot, refreshed on table change)
# ---------------------------------------------------------------------------

class _MemoryIndex:
    """Holds the loaded vectors for ONE embedder, rebuilt when the row count for
    that embedder changes. The background worker is single-threaded, so a plain
    module-level snapshot is safe and avoids re-reading the BLOBs every lookup."""

    def __init__(self):
        self.embedder = None
        self.count = -1
        self.rows = []   # list of (vec_list, meta_dict)

    def invalidate(self):
        self.count = -1

    def append(self, embedder: str, vec, meta: dict):
        """Keep a warm snapshot warm after a single insert (write-back), instead
        of forcing a full reload on the next lookup."""
        if self.embedder == embedder and self.count >= 0:
            self.rows.append((list(vec), meta))
            self.count += 1

    def ensure(self, conn: sqlite3.Connection, embedder: str):
        n = count(conn, embedder)
        if self.embedder == embedder and self.count == n:
            return
        rows = []
        for r in conn.execute(
            "SELECT embedding, brand, model, color, source, confidence, source_ref "
            "FROM shoe_memory WHERE embedder = ?", (embedder,)
        ).fetchall():
            rows.append((
                unpack_vector(r["embedding"]),
                {"brand": r["brand"], "model": r["model"], "color": r["color"],
                 "source": r["source"], "confidence": r["confidence"],
                 "source_ref": r["source_ref"]},
            ))
        self.embedder, self.count, self.rows = embedder, n, rows


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
    source_ref}. Pure-Python dot over a cached snapshot; top_k=1 degrades to a
    plain nearest-neighbour match."""
    q = _l2_normalize(vec) if normalize else list(vec)
    _INDEX.ensure(conn, embedder)

    scored = []
    for v, meta in _INDEX.rows:
        if len(v) != len(q):
            continue
        sim = _dot(q, v)
        if sim >= min_sim:
            scored.append((sim, meta))
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
