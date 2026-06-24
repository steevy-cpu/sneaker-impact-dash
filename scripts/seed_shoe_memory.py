#!/usr/bin/env python3
"""
seed_shoe_memory.py — offline tooling for the seen-shoe cache (Tier 0).

Runs under the ENGINE python (GPU torch + the DINOv2 embedder + numpy), NOT the
dash venv. Two modes:

  --mode calibrate   Embed a (stratified) sample of label_data crops and run a
                     LEAVE-ONE-OUT, grouped-by-source_photo nearest-neighbour
                     test: for a sweep of cosine thresholds, report PRECISION
                     (of accepted matches, fraction with the right brand) and
                     COVERAGE (fraction of queries that get an accepted match).
                     Read-only — never writes the live DB. Picks the threshold.

  --mode seed        Embed ALL label_data crops and write them into a target
                     DB's shoe_memory table (gold from the DB overrides silver
                     on conflict). This populates the cache for real.

Grouped LOO (exclude same source_photo) mirrors build_dataset.py's split
discipline so the reported precision isn't inflated by pair/table leakage.
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime


def _canon_brand(b):
    return (b or "").strip().lower()


def load_gold_map(db_path):
    """(source_photo, source_pair) -> {brand, model, color} from confirmed pairs."""
    gold = {}
    if not os.path.exists(db_path):
        return gold
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT table_photo_id, image_path, final_make, final_model, final_color "
            "FROM pairs WHERE review_status='COMPLETED' AND final_make IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        # pair crop filename is "<table_photo_id>_<N>.jpg" -> N is the source_pair
        idx = None
        ip = r["image_path"] or ""
        base = os.path.splitext(os.path.basename(ip))[0]
        if "_" in base:
            tail = base.rsplit("_", 1)[-1]
            if tail.isdigit():
                idx = int(tail)
        if idx is not None:
            gold[(r["table_photo_id"], idx)] = {
                "brand": r["final_make"], "model": r["final_model"],
                "color": r["final_color"],
            }
    conn.close()
    return gold


def collect_entries(label_dir, gold_map, limit=None, per_brand_cap=None):
    """Yield {path, brand, model, color, source, source_photo} from label_data
    sidecars, applying gold overrides. Optional stratified sampling."""
    sidecars = sorted(f for f in os.listdir(label_dir) if f.endswith(".json"))
    by_brand = defaultdict(list)
    out = []
    for fn in sidecars:
        try:
            meta = json.load(open(os.path.join(label_dir, fn)))
        except Exception:
            continue
        crop = os.path.join(label_dir, meta.get("filename") or fn.replace(".json", ".jpg"))
        if not os.path.exists(crop):
            continue
        sp, spair = meta.get("source_photo"), meta.get("source_pair")
        brand = meta.get("make"); model = meta.get("model"); color = meta.get("detected_color")
        source = "silver"
        g = gold_map.get((sp, spair))
        if g:
            brand, model, color, source = g["brand"], g["model"], g.get("color") or color, "gold"
        if not brand or _canon_brand(brand) in ("", "unknown"):
            continue
        rec = {"path": crop, "brand": brand, "model": model, "color": color,
               "source": source, "source_photo": sp or fn}
        by_brand[_canon_brand(brand)].append(rec)

    # stratified: cap per brand, then global limit
    for b, recs in by_brand.items():
        out.extend(recs if not per_brand_cap else recs[:per_brand_cap])
    out.sort(key=lambda r: r["source_photo"])
    if limit:
        # keep brand diversity under a global cap: round-robin by brand
        rr, buckets = [], defaultdict(list)
        for r in out:
            buckets[_canon_brand(r["brand"])].append(r)
        while len(rr) < limit and any(buckets.values()):
            for b in list(buckets.keys()):
                if buckets[b]:
                    rr.append(buckets[b].pop(0))
                    if len(rr) >= limit:
                        break
        out = rr
    return out


def embed_all(entries, embedder, log=print):
    import cv2
    vecs = []
    t0 = datetime.now()
    for i, rec in enumerate(entries):
        img = cv2.imread(rec["path"])
        if img is None:
            vecs.append(None); continue
        try:
            vecs.append(embedder.embed(img))
        except Exception as exc:
            log(f"  embed fail {rec['path']}: {exc}"); vecs.append(None)
        if (i + 1) % 500 == 0:
            dt = (datetime.now() - t0).total_seconds()
            log(f"  embedded {i+1}/{len(entries)}  ({(i+1)/dt:.1f}/s)")
    return vecs


def calibrate(entries, vecs, log=print, save_npz=None):
    import numpy as np
    keep = [(e, v) for e, v in zip(entries, vecs) if v is not None]
    E = [e for e, _ in keep]
    M = np.array([v for _, v in keep], dtype="float32")   # already L2-normalized
    n = len(E)
    log(f"\nCalibration set: {n} embedded crops, dim={M.shape[1] if n else 0}")
    if n < 50:
        log("Too few crops to calibrate."); return
    brands = [_canon_brand(e["brand"]) for e in E]
    colors = [(e.get("color") or "").strip().lower() for e in E]
    photos = [e["source_photo"] for e in E]
    sources = [e.get("source") for e in E]
    if save_npz:
        np.savez_compressed(save_npz, M=M, brands=brands, colors=colors,
                            photos=photos, sources=sources)
        log(f"(saved embeddings -> {save_npz})")
    sims = M @ M.T
    np.fill_diagonal(sims, -1.0)
    # mask out same-photo neighbours (grouped LOO, no pair/table leakage)
    for i in range(n):
        for j in range(n):
            if photos[j] == photos[i]:
                sims[i, j] = -1.0

    order = np.argsort(-sims, axis=1)        # neighbours, nearest first
    thresholds = [0.90, 0.92, 0.93, 0.94, 0.95, 0.96, 0.97, 0.98]

    def report(title, accept_fn):
        log(f"\n{title}\n{'thresh':>7} {'coverage':>9} {'precision':>10} {'accepted':>9}")
        for t in thresholds:
            acc = cor = 0
            for i in range(n):
                r = accept_fn(i, t)
                if r is None:
                    continue
                acc += 1
                if r:
                    cor += 1
            cov, prec = (acc / n), (cor / acc if acc else 0.0)
            log(f"{t:>7.2f} {cov:>9.1%} {(prec if acc else 0):>10.1%} {acc:>9}")

    # (1) nearest-neighbour (baseline)
    def nn(i, t):
        j = order[i, 0]
        if sims[i, j] < t:
            return None
        return brands[i] == brands[j]
    report("[1] nearest neighbour (baseline)", nn)

    # (2) nearest neighbour + COLOR must match
    def nn_color(i, t):
        j = order[i, 0]
        if sims[i, j] < t or (colors[i] and colors[j] and colors[i] != colors[j]):
            # find nearest same-colour neighbour above t instead
            cand = [j2 for j2 in order[i] if sims[i, j2] >= t and colors[j2] == colors[i]]
            if not cand:
                return None
            j = cand[0]
        if sims[i, j] < t:
            return None
        return brands[i] == brands[j]
    report("[2] nearest neighbour + color-guard", nn_color)

    # (3) top-3 neighbours must AGREE on brand (majority>=2), all above t
    def vote(i, t):
        top = [j for j in order[i, :3] if sims[i, j] >= t]
        if len(top) < 2:
            return None
        from collections import Counter
        b, c = Counter(brands[j] for j in top).most_common(1)[0]
        if c < 2:
            return None
        return brands[i] == b
    report("[3] top-3 agreement (majority brand)", vote)

    # (4) nearest neighbour that is a GOLD entry (human-verified)
    def nn_gold(i, t):
        cand = [j for j in order[i] if sims[i, j] >= t and sources[j] == "gold"]
        if not cand:
            return None
        return brands[i] == brands[cand[0]]
    report("[4] nearest GOLD neighbour only", nn_gold)
    log("\nPick the policy/threshold balancing precision (right brand) vs")
    log("coverage (fraction of cloud calls saved). 'correct' is vs the stored")
    log("(mostly Gemini-silver) label, so some 'errors' are silver noise.")


def seed(entries, vecs, db_path, embedder_name, log=print):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend.services import shoe_memory as sm
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    sm.clear(conn, embedder_name)
    added = 0
    for e, v in zip(entries, vecs):
        if v is None:
            continue
        sm.add_entry(conn, list(map(float, v)), embedder=embedder_name,
                     brand=e["brand"], model=e["model"], color=e["color"],
                     source=e["source"], source_ref=os.path.basename(e["path"]),
                     normalize=False)   # embedder output already normalized
        added += 1
    log(f"seeded {added} entries into shoe_memory ({embedder_name})")
    conn.close()


def gold_feed(db_path, pairs_dir, embedder, embedder_name, log=print):
    """DIRECT gold-crop feed: embed each human-confirmed pair crop and upsert it
    into shoe_memory as 'gold', then relabel near-identical silver to match. This
    replaces the sparse (source_photo, source_pair) key-match used at seed time
    (which only caught a couple) — here we embed the actual confirmed crops."""
    import cv2
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from backend.services import shoe_memory as sm
    conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
    # Authoritative rebuild of gold: drop prior gold crops, re-embed from the DB.
    conn.execute("DELETE FROM shoe_memory WHERE source = 'gold'"); conn.commit()
    sm._INDEX.invalidate()
    rows = conn.execute(
        "SELECT id, image_path, final_make, final_model, final_color FROM pairs "
        "WHERE review_status='COMPLETED' AND final_make IS NOT NULL"
    ).fetchall()
    added = corrected = skipped = 0
    for r in rows:
        crop = os.path.join(pairs_dir, os.path.basename(r["image_path"] or ""))
        img = cv2.imread(crop) if os.path.exists(crop) else None
        if img is None:
            skipped += 1; continue
        try:
            vec = [float(x) for x in embedder.embed(img)]
        except Exception as exc:
            log(f"  embed fail {crop}: {exc}"); skipped += 1; continue
        sm.add_entry(conn, vec, embedder=embedder_name, brand=r["final_make"],
                     model=r["final_model"], color=r["final_color"], source="gold",
                     source_ref=str(r["id"]), normalize=False)
        added += 1
        corrected += sm.correct_near(conn, vec, embedder=embedder_name,
                                     brand=r["final_make"], model=r["final_model"],
                                     sim_min=0.985, normalize=False)
    log(f"gold feed: {added} gold crops embedded, {corrected} near-silver corrected, "
        f"{skipped} skipped (missing crop). Total now: {sm.count(conn, embedder_name)}")
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine-dir", required=True)
    ap.add_argument("--label-dir", required=True)
    ap.add_argument("--db", default=None, help="sneakers.db (gold source; seed target)")
    ap.add_argument("--pairs-dir", default=None, help="dash images/pairs dir (for --mode gold)")
    ap.add_argument("--mode", choices=["calibrate", "seed", "gold"], default="calibrate")
    ap.add_argument("--limit", type=int, default=None, help="cap #crops (sampling)")
    ap.add_argument("--save-npz", default=None, help="cache embeddings for fast re-analysis")
    args = ap.parse_args()

    sys.path.insert(0, args.engine_dir)
    os.chdir(args.engine_dir)
    import config
    from embedder_utils import build_image_embedder
    embedder = build_image_embedder(config)
    if not getattr(embedder, "ok", True):
        print("embedder failed to load", file=sys.stderr); sys.exit(1)
    name = getattr(embedder, "name", "unknown")
    print(f"embedder: {name} ({getattr(embedder,'dim','?')}-d)")

    # Gold mode embeds confirmed PAIR CROPS (not label_data) — handle it first.
    if args.mode == "gold":
        if not args.db:
            print("--db required for gold", file=sys.stderr); sys.exit(1)
        pairs_dir = args.pairs_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "images", "pairs")
        gold_feed(args.db, pairs_dir, embedder, name)
        return

    gold = load_gold_map(args.db) if args.db else {}
    print(f"gold labels: {len(gold)}")
    entries = collect_entries(args.label_dir, gold, limit=args.limit)
    print(f"entries to embed: {len(entries)}")
    vecs = embed_all(entries, embedder)

    if args.mode == "calibrate":
        calibrate(entries, vecs, save_npz=args.save_npz)
    else:
        if not args.db:
            print("--db required for seed", file=sys.stderr); sys.exit(1)
        seed(entries, vecs, args.db, name)


if __name__ == "__main__":
    main()
