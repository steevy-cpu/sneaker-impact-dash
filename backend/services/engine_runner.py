#!/usr/bin/env python3
"""
engine_runner.py -- run the local identification pipeline on ONE table photo.

Invoked as a SUBPROCESS by the dash worker, using the SYSTEM python (GPU torch +
ultralytics + CLIP) so the dash's own venv stays lightweight. It runs inside the
sneaker_impact_training engine dir and mirrors split_table.py's per-pair logic,
then adds brand + model identification:

    segment (YOLOE + tiling) -> pair (geometric) -> per pair:
        crop -> color (CIELAB) -> brand (CLIP) -> model (Ollama VLM)

Each pair crop is written to --out-dir as "<id-prefix>_<n>.jpg" and the full
result is written as JSON to --out-json:

    {"pairs": [{image_file, bbox, detected_color, color_confidence,
                make, make_confidence, model, model_confidence, model_sources}],
     "engine": {segments, width, height}}            # or {"error": "..."}

All engine progress logging goes to stderr; --out-json is the only structured
output, so the parent can parse it cleanly. Exits non-zero on a hard error.
"""
import argparse
import json
import os
import sys


def _pad(bbox, w, h, frac):
    x1, y1, x2, y2 = bbox
    px = int((x2 - x1) * frac)
    py = int((y2 - y1) * frac)
    return (max(0, int(x1 - px)), max(0, int(y1 - py)),
            min(w, int(x2 + px)), min(h, int(y2 + py)))


def _f(v):
    return float(v) if isinstance(v, (int, float)) else None


def main():
    ap = argparse.ArgumentParser(description="Run the pipeline on one table photo.")
    ap.add_argument("--engine-dir", required=True, help="sneaker_impact_training dir")
    ap.add_argument("--image", required=True)
    ap.add_argument("--out-dir", required=True, help="where pair crops are written")
    ap.add_argument("--out-json", required=True, help="where the result JSON is written")
    ap.add_argument("--id-prefix", default="pair")
    ap.add_argument("--segment-model", default=None)
    ap.add_argument("--ollama-model", default=None)
    ap.add_argument("--ollama-url", default=None)
    ap.add_argument("--model-timeout", type=int, default=None)
    ap.add_argument("--emit-embedding", action="store_true",
                    help="Emit a DINOv2 appearance embedding per pair (for the "
                         "dash seen-shoe cache). Off by default = no extra work.")
    ap.add_argument("--escalate-sam2", action="store_true",
                    help="Turn on the SAM2+gate escalation hybrid (YOLOE runs "
                         "every time; SAM2+gate kicks in on weak results). Off "
                         "by default = identical to the current pipeline.")
    ap.add_argument("--escalate-mode", default=None,
                    help="Escalation trigger: 'weak' (only weak YOLOE results) "
                         "or 'always' (every photo). Default = engine config.")
    args = ap.parse_args()

    # Make the engine importable and run from its dir (so `import config`, the
    # weights cache, and relative paths all resolve to the engine).
    sys.path.insert(0, args.engine_dir)
    os.chdir(args.engine_dir)
    # The engine prints a lot; keep real stdout clean (only --out-json matters).
    sys.stdout = sys.stderr

    result = {"pairs": [], "engine": {}}
    try:
        import config
        import cv2

        # Dash-side overrides (engine defaults are otherwise fine).
        config.ENABLE_COLOR_DETECTION = True
        config.SEGMENT_PAIR = True
        config.BRAND_BACKEND = "clip"
        config.MODEL_BACKEND = "ollama"
        if args.segment_model:
            config.SEGMENT_MODEL = args.segment_model
        if args.ollama_model:
            config.MODEL_OLLAMA_MODEL = args.ollama_model
        if args.ollama_url:
            config.MODEL_OLLAMA_URL = args.ollama_url
        if args.model_timeout:
            config.MODEL_OLLAMA_TIMEOUT = args.model_timeout
        if args.escalate_sam2:
            config.SEGMENT_ESCALATE_SAM2 = True
        if args.escalate_mode:
            config.SEGMENT_ESCALATE_MODE = args.escalate_mode

        from segment_utils import build_segmenter
        from pair_utils import pair_shoes, pair_shoes_hybrid, pair_shoes_visual
        from brand_utils import build_brand_classifier
        from model_search import build_model_identifier
        try:
            from color_utils import classify_color
        except Exception:                              # noqa: BLE001 - optional
            classify_color = None

        image = cv2.imread(args.image)
        if image is None:
            raise RuntimeError(f"could not read image: {args.image}")
        h, w = image.shape[:2]

        segmenter = build_segmenter(config)
        segs = segmenter.segment(image)

        min_frac = getattr(config, "SEGMENT_MIN_AREA_FRAC", 0.0)
        if min_frac > 0:
            area = h * w
            segs = [s for s in segs if s.area() >= min_frac * area]
        # An appearance embedder is built lazily and SHARED: the visual/hybrid
        # pairing methods need it, and --emit-embedding reuses the very same
        # instance to embed each final crop (no second model load on the GPU).
        embedder = None

        def _get_embedder():
            nonlocal embedder
            if embedder is None:
                from embedder_utils import build_image_embedder
                embedder = build_image_embedder(config)
            return embedder

        if getattr(config, "SEGMENT_PAIR", True):
            method = getattr(config, "SEGMENT_PAIR_METHOD", "visual")
            if method == "hybrid":
                # Adjacency-first with an appearance veto + high-bar visual
                # rescue. See pair_shoes_hybrid for the measured rationale.
                segs = pair_shoes_hybrid(
                    image, segs, _get_embedder(),
                    max_gap_frac=getattr(config, "SEGMENT_PAIR_MAX_GAP", 1.2),
                    veto_min=getattr(config, "SEGMENT_PAIR_VETO_MIN", 0.25),
                    rescue_min=getattr(config, "SEGMENT_PAIR_RESCUE_MIN", 0.80),
                    log=print,                          # stdout is redirected to stderr
                )
            elif method == "visual":
                # Pair by appearance (DINOv2/CLIP) so shoes need not be tied.
                # Reuses the same embedder the model-ID index uses.
                segs = pair_shoes_visual(
                    image, segs, _get_embedder(),
                    spatial_weight=getattr(config, "SEGMENT_PAIR_SPATIAL_WEIGHT", 0.15),
                    min_sim=getattr(config, "SEGMENT_PAIR_MIN_SIM", 0.5),
                    log=print,                          # stdout is redirected to stderr
                )
            else:
                segs = pair_shoes(segs, getattr(config, "SEGMENT_PAIR_MAX_GAP", 1.2))

        brander = build_brand_classifier(config)
        modeler = build_model_identifier(config)
        pad = getattr(config, "SEGMENT_CROP_PAD", 0.04)
        os.makedirs(args.out_dir, exist_ok=True)

        for i, seg in enumerate(segs, 1):
            x1, y1, x2, y2 = _pad(seg.bbox, w, h, pad)
            if x2 <= x1 or y2 <= y1:
                continue
            crop = image[y1:y2, x1:x2]

            color, color_conf = ("unknown", None)
            if classify_color is not None:
                try:
                    color, color_conf = classify_color(crop)
                except Exception:                      # noqa: BLE001 - fail safe
                    pass
            make, make_conf = ("unknown", None)
            try:
                make, make_conf = brander.classify(crop)
            except Exception:                          # noqa: BLE001 - fail safe
                pass
            model, model_conf, sources = ("unknown", None, [])
            try:
                model, model_conf, sources = modeler.identify(crop, make)
            except Exception:                          # noqa: BLE001 - fail safe
                pass

            fname = f"{args.id_prefix}_{i}.jpg"
            cv2.imwrite(os.path.join(args.out_dir, fname), crop)

            # Optional: a DINOv2 appearance embedding for the dash seen-shoe
            # cache. Reuses the pairing embedder (no extra model load). Fail-safe:
            # a bad embed just omits the field, never breaks identification.
            embedding, embedder_name = None, None
            if args.emit_embedding:
                try:
                    emb = _get_embedder()
                    vec = emb.embed(crop)
                    embedding = [float(x) for x in vec]
                    embedder_name = getattr(emb, "name", None)
                except Exception as exc:               # noqa: BLE001 - fail safe
                    print(f"[engine] embed failed for {fname}: {exc}")

            result["pairs"].append({
                "image_file":       fname,
                "bbox":             [int(x1), int(y1), int(x2), int(y2)],
                "pair_score":       _f(getattr(seg, "pair_score", None)),
                "is_single":        seg.label != "pair",
                "detected_color":   color,
                "color_confidence": _f(color_conf),
                "make":             make,
                "make_confidence":  _f(make_conf),
                "model":            model,
                "model_confidence": _f(model_conf),
                "model_sources":    sources or [],
                "embedding":        embedding,
                "embedder":         embedder_name,
            })

        result["engine"] = {"segments": len(segs), "width": w, "height": h}
    except Exception as exc:                           # noqa: BLE001 - report up
        result["error"] = str(exc)

    with open(args.out_json, "w") as f:
        json.dump(result, f)
    sys.exit(1 if result.get("error") else 0)


if __name__ == "__main__":
    main()
