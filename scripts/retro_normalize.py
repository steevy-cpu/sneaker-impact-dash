"""Phase C of [[brand-model-dedup]]: one-time retroactive normalization of every
stored brand/model label, using the same normalize_labels() that Phase B applies
to new writes.

Targets:
  1. pairs.make/model + final_make/final_model  (raw_make/raw_model audit cols
     record the pre-normalization make/model when changed; never clobbered)
  2. shoe_memory.brand/model                    (seen-shoe cache — raw spelling
     variants can split its top-K agreement votes)
  3. label_data/*.json sidecars                 (training labels)

Usage (from sneaker-impact-dash root, services may stay up — short txns):
    venv/bin/python3 scripts/retro_normalize.py            # DRY RUN (default)
    venv/bin/python3 scripts/retro_normalize.py --apply    # write changes

Take a DB backup + sidecar tar first. Dry run is read-only.
"""
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.config import LABEL_DATA_DIR          # noqa: E402
from backend.utils.models import normalize_labels  # noqa: E402

APPLY = "--apply" in sys.argv
mode = "APPLY" if APPLY else "DRY RUN"
print(f"=== retro_normalize [{mode}] ===")

changes = Counter()          # (old_make/old_model -> new) sample counter


def norm_pair(make, model):
    nm, nmd = normalize_labels(make, model)
    changed = (nm != make) or (nmd != model)
    if changed:
        changes[f"{make}/{model} -> {nm}/{nmd}"] += 1
    return nm, nmd, changed


# ---- 1. pairs ----
conn = sqlite3.connect(str(ROOT / "sneakers.db"), timeout=30)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, make, model, final_make, final_model, raw_make, raw_model "
    "FROM pairs").fetchall()
n_pred = n_final = 0
updates = []
for r in rows:
    nm, nmd, ch1 = norm_pair(r["make"], r["model"])
    nfm, nfmd, ch2 = norm_pair(r["final_make"], r["final_model"])
    if not (ch1 or ch2):
        continue
    if ch1:
        n_pred += 1
    if ch2:
        n_final += 1
    updates.append((
        nm, nmd, nfm, nfmd,
        r["raw_make"] or (r["make"] if nm != r["make"] else None),
        r["raw_model"] or (r["model"] if nmd != r["model"] else None),
        r["id"],
    ))
print(f"pairs: {len(rows)} rows scanned, {len(updates)} to update "
      f"({n_pred} prediction labels, {n_final} final labels)")
if APPLY and updates:
    conn.executemany(
        "UPDATE pairs SET make=?, model=?, final_make=?, final_model=?, "
        "raw_make=?, raw_model=? WHERE id=?", updates)
    conn.commit()
    print(f"pairs: {len(updates)} rows updated")

# ---- 2. shoe_memory (seen-shoe cache) ----
mem = conn.execute("SELECT id, brand, model FROM shoe_memory").fetchall()
mem_updates = []
for r in mem:
    nb, nmd, ch = norm_pair(r["brand"], r["model"])
    if ch:
        mem_updates.append((nb, nmd, r["id"]))
print(f"shoe_memory: {len(mem)} rows scanned, {len(mem_updates)} to update")
if APPLY and mem_updates:
    conn.executemany(
        "UPDATE shoe_memory SET brand=?, model=? WHERE id=?", mem_updates)
    conn.commit()
    print(f"shoe_memory: {len(mem_updates)} rows updated")
conn.close()

# ---- 3. label_data sidecars ----
side_changed = side_total = side_errors = 0
for fname in os.listdir(LABEL_DATA_DIR):
    if not fname.endswith(".json"):
        continue
    side_total += 1
    fp = os.path.join(LABEL_DATA_DIR, fname)
    try:
        with open(fp) as fh:
            d = json.load(fh)
    except Exception:
        side_errors += 1
        continue
    nm, nmd, ch = norm_pair(d.get("make"), d.get("model"))
    if not ch:
        continue
    side_changed += 1
    if APPLY:
        d["make"], d["model"] = nm, nmd
        with open(fp, "w") as fh:
            json.dump(d, fh, indent=2)
print(f"label_data: {side_total} sidecars scanned, {side_changed} "
      f"{'updated' if APPLY else 'to update'}, {side_errors} unreadable")

print("\ntop 25 transforms (by occurrence):")
for k, n in changes.most_common(25):
    print(f"  {n:6}  {k}")
print(f"\ntotal distinct transforms: {len(changes)}")
