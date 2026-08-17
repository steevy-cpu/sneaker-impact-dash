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


def _whiten_bg(crop, seg, ox, oy, dilate, color=205, tighten=True, margin=10):
    """Return `crop` with everything outside the shoe mask(s) painted `color`,
    then (when `tighten`) re-cropped to the mask's bounding box + `margin` px so
    the shoes fill the frame instead of floating in a big gray border.

    `seg` carries either a single `.polygon` (a lone shoe) or, for a pair, its
    two members' polygons on `.member_polys`. Polygons are in source-image
    coords; shift them by (-ox, -oy) into crop space and fill. If no polygon is
    available (e.g. a detector backend without masks), return the crop unchanged
    so we never blank out a real shoe.

    `color` is a neutral grayscale value for the background: pure white (255)
    makes WHITE shoes/laces dissolve into the background (lost silhouette), so the
    default is a mid-light gray (~205) that keeps white shoes' edges while still
    isolating the shoe from the cluttered table.

    Tightening also drops any neighbor shoe that fell inside the loose union box:
    it's masked to gray AND now usually outside the tight crop entirely."""
    import cv2
    import numpy as np

    polys = []
    member_polys = getattr(seg, "member_polys", None)
    if member_polys:                                   # a pair: one mask per shoe
        polys = [p for p in member_polys if p is not None and len(p) >= 3]
    elif getattr(seg, "polygon", None) is not None and len(seg.polygon) >= 3:
        polys = [seg.polygon]                          # a single shoe
    if not polys:
        return crop                                    # nothing to mask -> as-is

    h, w = crop.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    for p in polys:
        pts = np.asarray(p, dtype=np.float32).copy()
        pts[:, 0] -= ox
        pts[:, 1] -= oy
        cv2.fillPoly(mask, [pts.astype(np.int32)], 255)
    if dilate and dilate > 0:                          # grow a touch so we don't
        k = cv2.getStructuringElement(                 # shave the shoe's edge
            cv2.MORPH_ELLIPSE, (int(dilate), int(dilate)))
        mask = cv2.dilate(mask, k)
    out = np.full_like(crop, int(color))
    out[mask > 0] = crop[mask > 0]

    if tighten:
        ys, xs = np.where(mask > 0)
        if len(xs):
            m = int(margin)
            x0, x1 = max(0, int(xs.min()) - m), min(w, int(xs.max()) + 1 + m)
            y0, y1 = max(0, int(ys.min()) - m), min(h, int(ys.max()) + 1 + m)
            out = out[y0:y1, x0:x1]
    return out


def _insole_like(seg, image_area, min_frac, max_frac, min_elong, max_elong):
    """Insole-shaped blob filter for SAM2's unlabeled everything-mode masks:
    mid-size area and a footprint-like elongation (measured 2.6-4.0 on the
    reference photos; table edges / background strips run 13+)."""
    x1, y1, x2, y2 = seg.bbox
    wid, hei = max(1, x2 - x1), max(1, y2 - y1)
    elong = max(wid, hei) / min(wid, hei)
    return (min_frac * image_area < seg.area() < max_frac * image_area
            and min_elong <= elong <= max_elong)


def _drop_nested(segs, containment=0.80):
    """SAM2 everything-mode often emits a mask for an insole AND for regions
    inside it (logo patch, heel pad). Largest-first, a segment whose box is
    mostly inside an already-kept one is a sub-part, not another insole."""
    kept = []
    for s in sorted(segs, key=lambda s: -s.area()):
        x1, y1, x2, y2 = s.bbox
        area = max(1, (x2 - x1) * (y2 - y1))
        nested = False
        for k in kept:
            kx1, ky1, kx2, ky2 = k.bbox
            iw = max(0, min(x2, kx2) - max(x1, kx1))
            ih = max(0, min(y2, ky2) - max(y1, ky1))
            if iw * ih >= containment * area:
                nested = True
                break
        if not nested:
            kept.append(s)
    return kept


def _run_insoles(args, result):
    """Insole capture mode: same crop/pair/whiten flow as shoes, different
    detector and classifier. YOLOE's text prompts cannot detect insoles
    (verified zero detections at conf 0.01), so segmentation is SAM2
    everything-mode + the shape filter above. Brand is a linear head
    (insole_head.pt) over the SAME DINOv2 embedder the pairing uses — no CLIP,
    no ollama model-ID, no cloud, no shoeness gate."""
    import config
    import cv2
    import numpy as np
    import torch

    from embedder_utils import build_image_embedder
    from pair_utils import pair_shoes_hybrid
    from segment_utils import Sam2Segmenter, _resolve_device
    try:
        from color_utils import classify_color
    except Exception:                                  # noqa: BLE001 - optional
        classify_color = None

    image = cv2.imread(args.image)
    if image is None:
        raise RuntimeError(f"could not read image: {args.image}")
    h, w = image.shape[:2]

    segmenter = Sam2Segmenter(
        getattr(config, "SEGMENT_ESCALATE_SAM_MODEL", "sam2_b.pt"),
        0.1, _resolve_device())
    segs = segmenter.segment(image)
    try:
        segmenter.release()                            # free GPU before DINOv2
    except Exception as exc:                           # noqa: BLE001 - fail safe
        print(f"[engine] segmenter.release() failed: {exc}")

    n_raw = len(segs)
    segs = [s for s in segs
            if _insole_like(s, h * w,
                            getattr(config, "INSOLE_MIN_AREA_FRAC", 0.01),
                            getattr(config, "INSOLE_MAX_AREA_FRAC", 0.60),
                            getattr(config, "INSOLE_MIN_ELONGATION", 1.5),
                            getattr(config, "INSOLE_MAX_ELONGATION", 6.0))]
    segs = _drop_nested(segs)
    print(f"[engine] insole mode: {n_raw} SAM2 masks -> "
          f"{len(segs)} insole-shaped")

    embedder = build_image_embedder(config)

    # Same pairing knobs as the shoe flow — the algorithm (adjacency + cosine
    # veto + color veto) is class-agnostic, and insole pairs get placed
    # touching on the table just like shoe pairs.
    segs = pair_shoes_hybrid(
        image, segs, embedder,
        max_gap_frac=getattr(config, "SEGMENT_PAIR_MAX_GAP", 1.2),
        veto_min=getattr(config, "SEGMENT_PAIR_VETO_MIN", 0.25),
        rescue_min=getattr(config, "SEGMENT_PAIR_RESCUE_MIN", 0.80),
        max_dist_frac=getattr(config, "SEGMENT_PAIR_MAX_DIST_FRAC", 0.20),
        color_veto=getattr(config, "SEGMENT_PAIR_COLOR_VETO", False),
        color_conf_min=getattr(config, "SEGMENT_PAIR_COLOR_CONF_MIN", 0.35),
        color_dist=getattr(config, "SEGMENT_PAIR_COLOR_DIST", 90.0),
        color_name_guard=getattr(config, "SEGMENT_PAIR_COLOR_NAME_GUARD", 0.0),
        gap_outlier_k=getattr(config, "SEGMENT_PAIR_GAP_OUTLIER_K", 0.0),
        gap_outlier_min=getattr(config, "SEGMENT_PAIR_GAP_OUTLIER_MIN", 4),
        log=print,                                      # stdout -> stderr
    )

    payload = torch.load(args.insole_head, map_location="cpu")
    classes = payload["classes"]
    threshold = float(payload.get("unknown_threshold", 0.70))
    head = torch.nn.Linear(int(payload["dim"]), len(classes))
    head.load_state_dict(payload["state_dict"])
    head.eval()
    trained_on = payload.get("embedder")
    emb_name = getattr(embedder, "name", None)
    if trained_on and emb_name and trained_on != emb_name:
        print(f"[engine] WARNING: insole head trained on {trained_on} "
              f"but embedder is {emb_name} — predictions unreliable")

    pad = getattr(config, "SEGMENT_CROP_PAD", 0.04)
    whiten = getattr(config, "SEGMENT_WHITEN_CROP", False)
    whiten_dilate = getattr(config, "SEGMENT_WHITEN_DILATE", 9)
    whiten_color = getattr(config, "SEGMENT_WHITEN_COLOR", 205)
    whiten_tighten = getattr(config, "SEGMENT_WHITEN_TIGHTEN", True)
    whiten_margin = getattr(config, "SEGMENT_WHITEN_TIGHTEN_MARGIN", 10)
    os.makedirs(args.out_dir, exist_ok=True)

    for i, seg in enumerate(segs, 1):
        x1, y1, x2, y2 = _pad(seg.bbox, w, h, pad)
        if x2 <= x1 or y2 <= y1:
            continue
        crop = image[y1:y2, x1:x2]
        if whiten:
            try:
                crop = _whiten_bg(crop, seg, x1, y1, whiten_dilate,
                                  whiten_color, whiten_tighten, whiten_margin)
            except Exception as exc:                   # noqa: BLE001 - fail safe
                print(f"[engine] whiten failed on insole {i}: {exc}")

        color, color_conf = ("unknown", None)
        if classify_color is not None:
            try:
                color, color_conf = classify_color(crop)
            except Exception:                          # noqa: BLE001 - fail safe
                pass

        # Brand: embed the whitened crop (matches how the head was trained)
        # and run the linear head. Below-threshold or explicit unknown class
        # both report "unknown" — those insoles are counted but never pushed
        # to a brand column.
        make, make_conf = ("unknown", None)
        try:
            vec = np.asarray(embedder.embed(crop), dtype=np.float32)
            vec /= (np.linalg.norm(vec) + 1e-8)
            with torch.no_grad():
                probs = head(torch.from_numpy(vec)).softmax(-1)
            make_conf = float(probs.max())
            ci = int(probs.argmax())
            if classes[ci] != "unknown" and make_conf >= threshold:
                make = classes[ci].capitalize()
        except Exception as exc:                       # noqa: BLE001 - fail safe
            print(f"[engine] insole brand classify failed on {i}: {exc}")

        fname = f"{args.id_prefix}_{i}.jpg"
        cv2.imwrite(os.path.join(args.out_dir, fname), crop)

        result["pairs"].append({
            "image_file":       fname,
            "bbox":             [int(x1), int(y1), int(x2), int(y2)],
            "pair_score":       _f(getattr(seg, "pair_score", None)),
            "is_single":        seg.label != "pair",
            "detected_color":   color,
            "color_confidence": _f(color_conf),
            "make":             make,
            "make_confidence":  _f(make_conf),
            "model":            "unknown",
            "model_confidence": None,
            "model_sources":    ["insole_head"],
            "shoe_confidence":  None,
            "embedding":        None,
            "embedder":         None,
        })

    result["engine"] = {"segments": len(segs), "width": w, "height": h,
                        "mode": "insoles"}


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
    ap.add_argument("--skip-local-model-id", action="store_true",
                    help="Skip the per-pair LOCAL ollama model-ID. It's slow "
                         "(~3s/pair) and the dash cloud step overrides it on most "
                         "pairs anyway, so skipping it cuts ~1 min/photo. The "
                         "cloud (or seen-shoe cache) supplies the model instead.")
    ap.add_argument("--no-crop-mask-sam2", action="store_true",
                    help="Don't refine crop masks with SAM2 box-prompt; use the "
                         "(looser) detection masks for whitening instead.")
    ap.add_argument("--mode", default="shoes", choices=["shoes", "insoles"],
                    help="'insoles' = insole capture mode: SAM2 everything-mode "
                         "+ shape filter for detection, insole_head.pt for "
                         "brand; no ollama/cloud/shoeness.")
    ap.add_argument("--insole-head", default=None,
                    help="Path to insole_head.pt (required for --mode insoles).")
    args = ap.parse_args()

    # Make the engine importable and run from its dir (so `import config`, the
    # weights cache, and relative paths all resolve to the engine).
    sys.path.insert(0, args.engine_dir)
    os.chdir(args.engine_dir)
    # The engine prints a lot; keep real stdout clean (only --out-json matters).
    sys.stdout = sys.stderr

    result = {"pairs": [], "engine": {}}

    # Insole mode is a self-contained branch: shared crop/whiten helpers, its
    # own detector + classifier, and the same --out-json contract. The shoe
    # path below is untouched.
    if args.mode == "insoles":
        try:
            if not args.insole_head or not os.path.exists(args.insole_head):
                raise RuntimeError(
                    f"insole head not found: {args.insole_head!r}")
            _run_insoles(args, result)
        except Exception as exc:                       # noqa: BLE001 - report up
            result["error"] = str(exc)
        with open(args.out_json, "w") as f:
            json.dump(result, f)
        sys.exit(1 if result.get("error") else 0)

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
        if args.no_crop_mask_sam2:
            config.SEGMENT_CROP_MASK_SAM2 = False

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
        # Free the segmentation models (esp. SAM2 ~5GB under the escalation
        # hybrid) off the GPU NOW, before the per-pair VLM model-ID loop. Left
        # resident they overflow the 16GB GPU and evict the ollama model, making
        # every model-ID call time out at MODEL_OLLAMA_TIMEOUT. No-op for the
        # plain pipeline; segmentation is done, so the segmenter isn't reused.
        try:
            segmenter.release()
        except Exception as exc:                       # noqa: BLE001 - fail safe
            print(f"[engine] segmenter.release() failed: {exc}")

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
                    max_dist_frac=getattr(config, "SEGMENT_PAIR_MAX_DIST_FRAC", 0.20),
                    color_veto=getattr(config, "SEGMENT_PAIR_COLOR_VETO", False),
                    color_conf_min=getattr(config, "SEGMENT_PAIR_COLOR_CONF_MIN", 0.35),
                    color_dist=getattr(config, "SEGMENT_PAIR_COLOR_DIST", 90.0),
                    color_name_guard=getattr(config, "SEGMENT_PAIR_COLOR_NAME_GUARD", 0.0),
                    gap_outlier_k=getattr(config, "SEGMENT_PAIR_GAP_OUTLIER_K", 0.0),
                    gap_outlier_min=getattr(config, "SEGMENT_PAIR_GAP_OUTLIER_MIN", 4),
                    log=print,                          # stdout is redirected to stderr
                )
            elif method == "visual":
                # Pair by appearance (DINOv2/CLIP) so shoes need not be tied.
                # Reuses the same embedder the model-ID index uses.
                segs = pair_shoes_visual(
                    image, segs, _get_embedder(),
                    spatial_weight=getattr(config, "SEGMENT_PAIR_SPATIAL_WEIGHT", 0.15),
                    min_sim=getattr(config, "SEGMENT_PAIR_MIN_SIM", 0.5),
                    max_dist_frac=getattr(config, "SEGMENT_PAIR_MAX_DIST_FRAC", 0.20),
                    log=print,                          # stdout is redirected to stderr
                )
            else:
                segs = pair_shoes(segs, getattr(config, "SEGMENT_PAIR_MAX_GAP", 1.2))

        # Clean crop masks via SAM2 box-prompt. Detection (YOLOE) is fast but its
        # masks are loose, so a whitened crop still shows the table/neighbors.
        # Here we ask SAM2 for ONE tight mask per final shoe box and swap it in
        # for whitening — cheap (box-prompt, not everything-mode) and it makes
        # crops clean on EVERY table, not just the ones that escalate. Gated +
        # fail-safe: only runs when whitening is on, releases the GPU right after.
        if (getattr(config, "SEGMENT_WHITEN_CROP", False)
                and getattr(config, "SEGMENT_CROP_MASK_SAM2", False)):
            try:
                from segment_utils import Sam2BoxMasker, _resolve_device
                masker = Sam2BoxMasker(
                    getattr(config, "SEGMENT_ESCALATE_SAM_MODEL", "sam2_b.pt"),
                    _resolve_device(),
                    sam_max=getattr(config, "SEGMENT_ESCALATE_SAM_MAX", 1536))
                if masker.ready:
                    flat, refs = [], []     # refs: (seg_idx, member_idx or None)
                    for si, sg in enumerate(segs):
                        mboxes = getattr(sg, "member_boxes", None)
                        if mboxes:
                            for mi, mb in enumerate(mboxes):
                                flat.append(mb); refs.append((si, mi))
                        else:
                            flat.append(sg.bbox); refs.append((si, None))
                    polys = masker.mask_boxes(image, flat)
                    refined = 0
                    for (si, mi), poly in zip(refs, polys):
                        if poly is None:
                            continue
                        sg = segs[si]
                        if mi is None:
                            sg.polygon = poly
                        elif getattr(sg, "member_polys", None) and mi < len(sg.member_polys):
                            sg.member_polys[mi] = poly
                        refined += 1
                    print(f"[engine] SAM2 box-masks refined {refined}/{len(flat)} "
                          "shoe masks for clean crops")
                masker.release()
            except Exception as exc:                   # noqa: BLE001 - fail safe
                print(f"[engine] crop-mask refine failed ({exc}); "
                      "using detection masks.")

        brander = build_brand_classifier(config)
        # Zero-shot "is this footwear?" scorer, REUSING the brand CLIP model
        # (no extra GPU load). Emitted per pair as shoe_confidence so the dash
        # can refuse to spend a cloud call on non-shoes (a blower, box, bag).
        # Fail-safe: any error -> shoeness stays None and the field is omitted.
        shoeness = None
        try:
            from clip_shoeness import ClipShoeness
            _bm, _bp = getattr(brander, "model", None), getattr(brander, "preprocess", None)
            if _bm is not None and _bp is not None:
                _sh = ClipShoeness(model=_bm, preprocess=_bp)
                shoeness = _sh if _sh.ok else None
        except Exception as exc:                           # noqa: BLE001 - fail safe
            print(f"[engine] shoeness init failed ({exc}); continuing without it.")
        # The local model-ID (ollama VLM, ~3s/pair) is optional: the dash cloud
        # step re-IDs most pairs and overrides it, so it's usually wasted work.
        # Skip building it when asked -> model stays "unknown" and the cloud /
        # seen-shoe cache fills it in. Brand (CLIP) is cheap, so it always runs.
        modeler = None if args.skip_local_model_id else build_model_identifier(config)
        pad = getattr(config, "SEGMENT_CROP_PAD", 0.04)
        whiten = getattr(config, "SEGMENT_WHITEN_CROP", False)
        whiten_dilate = getattr(config, "SEGMENT_WHITEN_DILATE", 9)
        whiten_color = getattr(config, "SEGMENT_WHITEN_COLOR", 205)
        whiten_tighten = getattr(config, "SEGMENT_WHITEN_TIGHTEN", True)
        whiten_margin = getattr(config, "SEGMENT_WHITEN_TIGHTEN_MARGIN", 10)
        os.makedirs(args.out_dir, exist_ok=True)

        for i, seg in enumerate(segs, 1):
            x1, y1, x2, y2 = _pad(seg.bbox, w, h, pad)
            if x2 <= x1 or y2 <= y1:
                continue
            crop = image[y1:y2, x1:x2]

            # Optional: white-out everything outside the shoe mask(s), so the
            # saved crop shows ONLY the one/two shoes on a clean white background
            # (the old-system look). Uses the segment's polygon (single) or both
            # member polygons (pair). Fail-safe: any missing mask / error keeps
            # the raw crop. A small dilation avoids shaving the shoe's edge.
            if whiten:
                try:
                    crop = _whiten_bg(crop, seg, x1, y1, whiten_dilate,
                                      whiten_color, whiten_tighten, whiten_margin)
                except Exception as exc:               # noqa: BLE001 - fail safe
                    print(f"[engine] whiten failed on pair {i}: {exc}")

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
            if modeler is not None:
                try:
                    model, model_conf, sources = modeler.identify(crop, make)
                except Exception:                      # noqa: BLE001 - fail safe
                    pass

            # Zero-shot shoeness on the SAME (whitened) crop the cloud would see.
            shoe_conf = shoeness.score(crop) if shoeness is not None else None

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
                "shoe_confidence":  _f(shoe_conf),
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
