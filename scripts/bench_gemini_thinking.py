"""bench_gemini_thinking.py -- does capping Gemini's thinking budget hurt accuracy?

Gemini 2.5 Pro "thinks" before answering and bills those tokens as output, which
was ~90% of the cloud-identify bill (measured 2026-10-12: ~490 prompt, ~920
thinking, ~66 answer tokens per call). This script re-asks the production prompt
(backend.services.cloud_identify._PROMPT + _SCHEMA, no Lens titles, which is what
production sends today) with different thinking budgets and scores the answers.

Items
  gold    every human-confirmed pair (review_status COMPLETED, real final_make):
          true ground truth for BRAND; MODEL only where the human gave one.
  random  recent production pairs with no ground truth: scored by AGREEMENT with
          the baseline answer. A baseline-vs-baseline repeat measures how much
          Gemini disagrees with ITSELF (the noise floor any difference must beat).

Configs
  base    gemini-2.5-pro, dynamic thinking (exactly what production sends)
  b1024 / b512 / b128   gemini-2.5-pro with thinkingBudget N (128 is Pro's minimum)
  flash   gemini-2.5-flash, dynamic thinking (cheap reference)
  base2   baseline again, on a subset, for the noise floor

Usage (from the dash root, dash venv):
  PYTHONPATH=. venv/bin/python3 scripts/bench_gemini_thinking.py run    --out /path/results.jsonl [--random 200] [--repeat 80] [--threads 4] [--max-cost 25]
  PYTHONPATH=. venv/bin/python3 scripts/bench_gemini_thinking.py smoke  --out /path/smoke.jsonl     # 1 item x every config
  PYTHONPATH=. venv/bin/python3 scripts/bench_gemini_thinking.py report --out /path/results.jsonl

Resumable: finished (config, item) pairs in --out are skipped on re-run.
Never prints the API key.
"""
import argparse
import base64
import json
import math
import os
import random
import re
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from backend.config import (GEMINI_API_KEY, GEMINI_URL, IMAGES_DIR, DB_PATH, CLOUD_TIMEOUT)
from backend.services import cloud_identify as ci
from backend.utils.brands import canonical_brand

GOLD_WHERE = ("review_status = 'COMPLETED' AND final_make IS NOT NULL "
              "AND final_make NOT IN ('', 'unknown')")

# name -> (model id, thinkingBudget or None for the API default / dynamic)
CONFIGS = {
    "base":  ("gemini-2.5-pro", None),
    "b1024": ("gemini-2.5-pro", 1024),
    "b512":  ("gemini-2.5-pro", 512),
    "b128":  ("gemini-2.5-pro", 128),
    "flash": ("gemini-2.5-flash", None),
    "base2": ("gemini-2.5-pro", None),
}
# USD per 1M tokens (input, output incl. thinking). List prices as remembered;
# verify against the Google Cloud billing page before quoting savings.
PRICE = {"gemini-2.5-pro": (1.25, 10.0), "gemini-2.5-flash": (0.30, 2.50)}
CALLS_PER_30_DAYS = 22460            # cloud calls in the 30 days to 2026-10-12


# ---------------------------------------------------------------- items ----

def _crop_path(p):
    rel = p.split("/images/", 1)[-1] if "/images/" in p else p.lstrip("/")
    return os.path.join(str(IMAGES_DIR), rel)


def build_items(n_random, seed=7):
    conn = sqlite3.connect(str(DB_PATH)); conn.row_factory = sqlite3.Row
    items = []
    for r in conn.execute(f"SELECT id, image_path, final_make, final_model FROM pairs WHERE {GOLD_WHERE} ORDER BY id"):
        if r["image_path"] and os.path.exists(_crop_path(r["image_path"])):
            fm = (r["final_model"] or "").strip()
            items.append({"key": f"g{r['id']}", "group": "gold", "path": _crop_path(r["image_path"]),
                          "gold_make": r["final_make"], "gold_model": fm if fm.lower() not in ("", "unknown") else None})
    gold_ids = {i["key"][1:] for i in items}
    pool = conn.execute("SELECT id, image_path FROM pairs WHERE prediction_source LIKE 'cloud:%' "
                        "AND created_at >= date('now','-14 days')").fetchall()
    rnd = random.Random(seed); rnd.shuffle(pool)
    for r in pool:
        if len([i for i in items if i["group"] == "random"]) >= n_random:
            break
        if str(r["id"]) in gold_ids or not r["image_path"] or not os.path.exists(_crop_path(r["image_path"])):
            continue
        items.append({"key": f"r{r['id']}", "group": "random", "path": _crop_path(r["image_path"])})
    return items


# ----------------------------------------------------------------- call ----

def call(cfg, item):
    model, budget = CONFIGS[cfg]
    with open(item["path"], "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    gen = {"temperature": 0, "responseMimeType": "application/json", "responseSchema": ci._SCHEMA}
    if budget is not None:
        gen["thinkingConfig"] = {"thinkingBudget": budget}
    body = {"contents": [{"parts": [{"text": ci._PROMPT},
                                    {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}],
            "generationConfig": gen}
    url = f"{GEMINI_URL}/models/{model}:generateContent?key={GEMINI_API_KEY}"
    data = json.dumps(body).encode()
    t0 = time.time(); last = "unknown"
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=max(CLOUD_TIMEOUT, 120)) as r:
                resp = json.loads(r.read().decode())
            ans = json.loads(resp["candidates"][0]["content"]["parts"][0]["text"])
            u = resp.get("usageMetadata", {})
            return {"cfg": cfg, "key": item["key"], "ok": True, "brand": ans.get("brand"), "model": ans.get("model"),
                    "bconf": ans.get("brand_confidence"), "mconf": ans.get("model_confidence"),
                    "prompt": u.get("promptTokenCount", 0), "think": u.get("thoughtsTokenCount", 0),
                    "out": u.get("candidatesTokenCount", 0), "sec": round(time.time() - t0, 2)}
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}"
            if exc.code in (429, 500, 503) and attempt < 3:
                time.sleep(6 * (2 ** attempt)); continue
            break
        except Exception as exc:                                  # noqa: BLE001
            last = type(exc).__name__
            if attempt < 3:
                time.sleep(3 * (attempt + 1)); continue
            break
    return {"cfg": cfg, "key": item["key"], "ok": False, "err": last, "sec": round(time.time() - t0, 2)}


def cost_of(rec):
    pin, pout = PRICE[CONFIGS[rec["cfg"]][0]]
    return (rec.get("prompt", 0) * pin + (rec.get("think", 0) + rec.get("out", 0)) * pout) / 1e6


# --------------------------------------------------------------- scoring ----

def _bkey(s):
    s = (s or "").strip()
    return (canonical_brand(s) or s).lower()


def _mkey(model, brand=None):
    s = re.sub(r"[^a-z0-9 ]+", " ", (model or "").lower())
    for tok in re.sub(r"[^a-z0-9 ]+", " ", (brand or "").lower()).split():
        s = re.sub(rf"\b{re.escape(tok)}\b", " ", s)
    return re.sub(r"\s+", "", s)


def model_match(pred, pred_brand, gold, gold_brand):
    a, b = _mkey(pred, pred_brand), _mkey(gold, gold_brand)
    if not a or not b or a in ("unknown",) or b in ("unknown",):
        return False, False
    exact = a == b
    return exact, exact or (min(len(a), len(b)) >= 4 and (a in b or b in a))


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = k / n; d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d; h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (100 * (c - h), 100 * (c + h))


def mcnemar_p(b, c):
    """Exact two-sided p for discordant counts b (A right, B wrong) and c."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


# ---------------------------------------------------------------- report ----

def report(path, items):
    recs = [json.loads(l) for l in open(path) if l.strip()]
    by = defaultdict(dict)
    for r in recs:
        by[r["cfg"]][r["key"]] = r
    meta = {i["key"]: i for i in items if i["group"] == "gold"}
    # The random sample is re-drawn from a table that keeps growing, so rebuilding
    # it here would NOT match the run. Take the random keys from the results file.
    for r in recs:
        if r["key"].startswith("r"):
            meta.setdefault(r["key"], {"key": r["key"], "group": "random"})
    order = [c for c in CONFIGS if c in by]
    gold = [k for k, i in meta.items() if i["group"] == "gold"]
    gm = [k for k in gold if meta[k]["gold_model"]]
    print(f"\nitems: gold {len(gold)} (with a human model label {len(gm)}), random {sum(1 for i in meta.values() if i['group']=='random')}")

    print("\n== 1. GOLD brand accuracy (truth = human label) ==")
    print(f"{'config':7s} {'n':>4s} {'brand acc':>10s} {'95% CI':>14s}   vs base: gained / lost / p")
    ok_base = {k: (_bkey(by['base'][k]['brand']) == _bkey(meta[k]['gold_make'])) for k in gold if k in by['base'] and by['base'][k]['ok']}
    for cfg in order:
        if cfg == "base2":
            continue
        res = {k: (_bkey(by[cfg][k]['brand']) == _bkey(meta[k]['gold_make'])) for k in gold if k in by[cfg] and by[cfg][k]['ok']}
        n = len(res); kk = sum(res.values()); lo, hi = wilson(kk, n)
        common = [k for k in res if k in ok_base]
        gained = sum(1 for k in common if res[k] and not ok_base[k]); lost = sum(1 for k in common if not res[k] and ok_base[k])
        tail = "" if cfg == "base" else f"   {gained:3d} / {lost:3d} / p={mcnemar_p(gained, lost):.2f}"
        print(f"{cfg:7s} {n:4d} {100*kk/max(1,n):9.1f}% {lo:6.1f}-{hi:5.1f}%{tail}")

    print(f"\n== 2. GOLD model accuracy, only the {len(gm)} items with a human model (exact / lenient) ==")
    for cfg in order:
        if cfg == "base2":
            continue
        ex = le = n = 0
        for k in gm:
            r = by[cfg].get(k)
            if r and r["ok"]:
                e, l = model_match(r["model"], r["brand"], meta[k]["gold_model"], meta[k]["gold_make"]); ex += e; le += l; n += 1
        print(f"{cfg:7s} n={n:3d}  exact {100*ex/max(1,n):5.1f}%   lenient {100*le/max(1,n):5.1f}%")

    print("\n== 3. RANDOM production pairs: agreement with the baseline answer ==")
    rk = [k for k, i in meta.items() if i["group"] == "random"]
    for cfg in order:
        if cfg == "base":
            continue
        ba = ex = le = n = 0
        for k in rk:
            a, b = by["base"].get(k), by[cfg].get(k)
            if a and b and a["ok"] and b["ok"]:
                n += 1; ba += _bkey(a["brand"]) == _bkey(b["brand"])
                e, l = model_match(b["model"], b["brand"], a["model"], a["brand"]); ex += e; le += l
        label = "base2 (same config again = NOISE FLOOR)" if cfg == "base2" else cfg
        print(f"{label:40s} n={n:3d}  brand {100*ba/max(1,n):5.1f}%   model exact {100*ex/max(1,n):5.1f}%  lenient {100*le/max(1,n):5.1f}%")

    print("\n== 4. Tokens, latency, cost, confidence (all items) ==")
    print(f"{'config':7s} {'ok/err':>9s} {'think tok':>9s} {'out tok':>8s} {'sec':>6s} {'$/call':>8s} {'30-day cost':>12s} {'saving':>8s} {'brand>=.8':>10s} {'model>=.5':>10s}")
    base_cost = None
    for cfg in order:
        good = [r for r in by[cfg].values() if r["ok"]]; bad = len(by[cfg]) - len(good)
        if not good:
            continue
        pc = statistics.mean(cost_of(r) for r in good)
        if cfg == "base":
            base_cost = pc
        sav = "" if base_cost is None or cfg == "base" else f"{100*(1-pc/base_cost):5.0f}%"
        bc = 100 * sum(1 for r in good if (r.get("bconf") or 0) >= 0.8) / len(good)
        mc = 100 * sum(1 for r in good if (r.get("mconf") or 0) >= 0.5) / len(good)
        print(f"{cfg:7s} {len(good):4d}/{bad:<4d} {statistics.mean(r['think'] for r in good):9.0f} {statistics.mean(r['out'] for r in good):8.0f} "
              f"{statistics.median(r['sec'] for r in good):6.1f} {pc:8.4f} {pc*CALLS_PER_30_DAYS:11.0f}$ {sav:>8s} {bc:9.0f}% {mc:9.0f}%")
    print(f"\nspent in this run: ${sum(cost_of(r) for r in recs if r['ok']):.2f}")


# ------------------------------------------------------------------- main ----

def run(args, smoke=False):
    items = build_items(args.random)
    done = set()
    if os.path.exists(args.out):
        for l in open(args.out):
            if l.strip():
                r = json.loads(l); done.add((r["cfg"], r["key"]))
    if smoke:
        tasks = [(cfg, items[0]) for cfg in CONFIGS if cfg != "base2"]
    else:
        rnd = random.Random(11)
        rkeys = [i for i in items if i["group"] == "random"][: args.repeat]
        tasks = [(cfg, it) for it in items for cfg in CONFIGS if cfg != "base2"] + [("base2", it) for it in rkeys]
        rnd.shuffle(tasks)                   # interleave configs so API drift hits all equally
    tasks = [t for t in tasks if (t[0], t[1]["key"]) not in done]
    est = sum({"base": .0105, "b1024": .0085, "b512": .0065, "b128": .004, "flash": .0015, "base2": .0105}[c] for c, _ in tasks)
    print(f"{len(items)} items, {len(tasks)} calls to make, estimated ${est:.1f}, max ${args.max_cost}", flush=True)
    if est > args.max_cost:
        print("estimate above --max-cost; aborting"); return
    lock = threading.Lock(); spent = [0.0]; n_done = [0]; stop = [False]
    f = open(args.out, "a")

    def work(t):
        if stop[0]:
            return
        r = call(*t)
        with lock:
            if r["ok"]:
                spent[0] += cost_of(r)
            f.write(json.dumps(r) + "\n"); f.flush(); n_done[0] += 1
            if n_done[0] % 50 == 0 or smoke:
                print(f"  {n_done[0]}/{len(tasks)} done, spent ${spent[0]:.2f}", flush=True)
            if spent[0] > args.max_cost:
                stop[0] = True
    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        list(ex.map(work, tasks))
    f.close()
    print(f"finished: {n_done[0]} calls, spent ${spent[0]:.2f}", flush=True)
    if smoke:
        for l in open(args.out):
            r = json.loads(l); print({k: r.get(k) for k in ("cfg", "ok", "brand", "model", "prompt", "think", "out", "sec", "err")})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "smoke", "report"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--random", type=int, default=200)
    ap.add_argument("--repeat", type=int, default=80)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--max-cost", type=float, default=25.0)
    a = ap.parse_args()
    if a.cmd == "report":
        report(a.out, build_items(a.random))
    else:
        run(a, smoke=(a.cmd == "smoke"))
