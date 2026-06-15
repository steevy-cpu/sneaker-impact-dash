"""bench_vlm.py — benchmark local VLMs on our own labeled shoe crops.

Picks N crops from label_data whose labels came from the cloud (Gemini Pro =
pseudo ground truth), asks each candidate model for {brand, model} with the
same JSON-forced prompt style the pipeline uses, and scores:
  - brand accuracy (exact, case-insensitive)
  - model accuracy (loose: token overlap with the label)
  - median latency per crop

Usage: python3 scripts/bench_vlm.py qwen2.5vl:7b qwen3-vl:8b [--n 30]
"""
import base64
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.request

DASH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL_DIR = os.path.join(DASH, "sneaker_impact_training", "label_data")
OLLAMA = "http://localhost:11434"

PROMPT = (
    "You are identifying ONE used sneaker from a cropped, top-down photo taken "
    "at a shoe-recycling station. From the logo, silhouette, midsole/outsole "
    "and any visible text, determine the brand (manufacturer, e.g. Nike, "
    "Adidas, New Balance, Hoka, Saucony, Brooks, Asics, On, Vans, Converse, "
    "Skechers, Under Armour, Puma, Reebok) and the specific model name. "
    "NEVER answer \"unknown\" — commit to your best guess and express doubt "
    "via the confidence scores in [0,1]. Respond ONLY with JSON: "
    '{"brand": str, "model": str, "brand_confidence": num, "model_confidence": num}'
)


def load_eval_set(n, seed=42):
    entries = []
    for fn in sorted(os.listdir(LABEL_DIR)):
        if not fn.endswith(".json"):
            continue
        meta = json.load(open(os.path.join(LABEL_DIR, fn)))
        img = os.path.join(LABEL_DIR, fn[:-5] + ".jpg")
        src = str(meta.get("prediction_source", ""))
        if (os.path.isfile(img) and meta.get("make") and meta.get("model")
                and src.startswith("cloud")):
            entries.append({"img": img, "brand": meta["make"], "model": meta["model"]})
    random.Random(seed).shuffle(entries)
    return entries[:n]


def ask(model, img_path):
    b64 = base64.b64encode(open(img_path, "rb").read()).decode()
    body = json.dumps({
        "model": model,
        "prompt": PROMPT,
        "images": [b64],
        "format": "json",
        "stream": False,
        "think": False,          # thinking models: skip the reasoning preamble
        "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        resp = json.load(r)
    # ollama + qwen3-vl quirk: with format=json the output can land in
    # "thinking" while "response" comes back empty — take whichever parses.
    return json.loads(resp.get("response") or resp.get("thinking") or "")


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", str(s).lower()).strip()


def brand_match(pred, truth):
    p, t = norm(pred), norm(truth)
    return p == t or p in t or t in p


def model_match(pred, truth):
    pt = set(norm(pred).split())
    tt = set(norm(truth).split())
    if not pt or not tt:
        return False
    return len(pt & tt) / len(tt) >= 0.5     # half the label's tokens found


def main():
    argv = sys.argv[1:]
    n = 30
    if "--n" in argv:
        i = argv.index("--n")
        n = int(argv[i + 1])
        del argv[i:i + 2]
    gap = 0.0
    if "--sleep" in argv:
        i = argv.index("--sleep")
        gap = float(argv[i + 1])
        del argv[i:i + 2]
    args = argv
    eval_set = load_eval_set(n)
    print(f"eval set: {len(eval_set)} cloud-labeled crops\n")

    for model in args:
        ok_b = ok_m = err = 0
        times = []
        misses = []
        for e in eval_set:
            if gap:
                time.sleep(gap)      # be polite: let production calls interleave
            t0 = time.time()
            try:
                out = ask(model, e["img"])
            except Exception as exc:                     # noqa: BLE001
                err += 1
                continue
            times.append(time.time() - t0)
            b = brand_match(out.get("brand"), e["brand"])
            m = model_match(out.get("model"), e["model"])
            ok_b += b
            ok_m += m
            if not b:
                misses.append(f"      {os.path.basename(e['img'])}: "
                              f"said {out.get('brand')}/{out.get('model')} "
                              f"truth {e['brand']}/{e['model']}")
        total = len(eval_set) - err
        med = statistics.median(times) if times else 0
        print(f"== {model}")
        print(f"   brand: {ok_b}/{total} ({100*ok_b/max(total,1):.0f}%)   "
              f"model: {ok_m}/{total} ({100*ok_m/max(total,1):.0f}%)   "
              f"median {med:.1f}s/crop   errors {err}")
        for line in misses[:6]:
            print(line)
        print()


if __name__ == "__main__":
    main()
