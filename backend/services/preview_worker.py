"""
preview_worker.py -- THE resident SAM3 for the whole system: the capture
page's live shoe-box guide AND the engine's table segmentation share this one
model (shared-worker design, 2026-09-10).

Why a separate process: SAM3 takes ~10s to load but ~0.5s per frame once
resident, so per-request loading is unusable -- and holding an 848M model
inside the uvicorn worker would tie preview VRAM to the website's lifecycle.
This tiny stdlib HTTP server owns the model. Two callers:
  * the dash forwards capture-page frames (/api/capture-preview) -> /segment;
  * the engine (segment_utils.RemoteSam3Segmenter) sends the table photo's
    PATH -> /segment-path, instead of loading its own copy. Measured
    2026-09-10: a fresh engine process paid ~74s (13s build + 61s warmup)
    per table just to load SAM3 -- sharing removes that entirely.
One model resident (~4GB) = the guide never has to pause for processing,
and VRAM is the same as before (the engine's duplicate copy is gone).
Requests serialize on one lock: an engine call waits at most one guide frame
(~0.8s), a guide tick waits at most one table segmentation (~1s).

VRAM discipline: model loads lazily on the first request after boot and then
STAYS resident. /release (CPU offload) still exists for manual use only.

Run under the engine env (needs ultralytics >= 8.4 + sam3.pt), cwd = engine dir:
  venv-sam3/bin/python3 ../backend/services/preview_worker.py --port 8766
Managed by the sneaker-dash-preview systemd user service in production.

Protocol (all JSON):
  GET  /health         -> {"ok": true, "loaded": bool}
  POST /segment        -> body = JPEG bytes; reply {"ok": true, "width": W,
                          "height": H, "ms": int, "boxes": [{"x1", "y1",
                          "x2", "y2", "score", "label", "polygon": [[x,y],..]
                          or null}, ...]}  (source-image coords)
  POST /segment-path   -> body = JSON {"path": "/abs/photo.jpg"}; same reply
                          (engine path: photo already on disk, no upload)
  POST /release        -> {"ok": true, "loaded": false}  (manual/emergency)
Errors reply {"ok": false, "error": "..."} with status 200 so the dash-side
forwarder has exactly one failure path (its own timeout).
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# The script runs by absolute path, so Python puts backend/services/ on
# sys.path -- NOT the cwd (the engine dir) where config/segment_utils live.
sys.path.insert(0, os.getcwd())

MAX_BODY = 4 * 1024 * 1024          # frames arrive ~100KB; reject anything wild

# CPU thread cap (2026-10-05): torch defaults to one thread per core (24) for
# the small CPU-side work around each frame (decode, pre/post-processing) --
# measured ~590% CPU for a 0.8 s/frame GPU job, i.e. 6 cores thrashing and the
# box's load average at 7-12 with nothing else running. 4 threads is plenty;
# the GPU does the real work.
import torch
torch.set_num_threads(4)

_lock = threading.Lock()            # one inference (and one load) at a time
_segmenter = None


def _get_segmenter():
    """Build the SAME chain production uses (sam3 + conf + ROI filter), lazily."""
    global _segmenter
    if _segmenter is None:
        import config                                    # engine dir is cwd
        import segment_utils as su

        class _Cfg:                                      # config view, not a mutation
            def __getattr__(self, name):
                return getattr(config, name)
        cfg = _Cfg()
        cfg.__dict__["SEGMENT_BACKEND"] = "sam3"
        cfg.__dict__["SEGMENT_MODEL"] = "sam3.pt"
        cfg.__dict__["SEGMENT_SAM3_REMOTE_URL"] = ""      # we ARE the remote
        _segmenter = su.build_segmenter(cfg)
        print("[preview] SAM3 loaded", flush=True)
    return _segmenter


_offloaded = False


def _torch_module():
    """The underlying torch module of the predictor, or None."""
    if _segmenter is None:
        return None
    pred = getattr(_segmenter, "predictor", None) or getattr(
        getattr(_segmenter, "base", None), "predictor", None)
    m = getattr(pred, "model", None)
    return m if hasattr(m, "to") else None


def _release():
    """Free VRAM for the engine. Offload weights to CPU RAM (reload is then a
    ~1-2s PCIe copy) instead of destroying the model (a full rebuild measured
    52s -- the guide would stay dark a minute after every table). Falls back
    to a hard drop if offload fails."""
    global _segmenter, _offloaded
    if _segmenter is None or _offloaded:
        return
    m = _torch_module()
    try:
        if m is None:
            raise RuntimeError("no torch module to offload")
        m.to("cpu")
        import torch
        torch.cuda.empty_cache()
        _offloaded = True
        print("[preview] SAM3 offloaded to CPU", flush=True)
    except Exception as exc:                             # noqa: BLE001 - fallback
        print(f"[preview] offload failed ({exc}); hard release", flush=True)
        try:
            _segmenter.release()
        except Exception:                                # noqa: BLE001
            pass
        _segmenter = None
        _offloaded = False


def _ensure_on_gpu():
    global _offloaded
    if _offloaded:
        m = _torch_module()
        if m is not None:
            m.to("cuda")
        _offloaded = False
        print("[preview] SAM3 back on GPU", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                           # journald noise control
        pass

    def _reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass                        # caller gave up waiting; work is done

    def do_GET(self):
        if self.path == "/health":
            return self._reply({"ok": True, "loaded": _segmenter is not None})
        return self._reply({"ok": False, "error": "unknown path"})

    def do_POST(self):
        if self.path == "/release":
            with _lock:
                _release()
            return self._reply({"ok": True, "loaded": False})
        if self.path not in ("/segment", "/segment-path"):
            return self._reply({"ok": False, "error": "unknown path"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if not 0 < n <= MAX_BODY:
                return self._reply({"ok": False, "error": f"bad size {n}"})
            data = self.rfile.read(n)
            import cv2
            if self.path == "/segment-path":
                path = json.loads(data).get("path", "")
                img = cv2.imread(path) if path else None
                if img is None:
                    return self._reply({"ok": False, "error": f"unreadable {path!r}"})
            else:
                import numpy as np
                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    return self._reply({"ok": False, "error": "not a decodable image"})
            t0 = time.time()
            with _lock:
                seg = _get_segmenter()
                _ensure_on_gpu()
                segs = seg.segment(img)
            # Polygons ride along for the engine (pairing's color veto and the
            # crop white-out need them); ~100 pts/shoe, trivial on loopback.
            boxes = []
            for s in segs:
                poly = None
                if getattr(s, "polygon", None) is not None and len(s.polygon) >= 3:
                    poly = [[int(x), int(y)] for x, y in s.polygon.tolist()]
                boxes.append({"x1": int(s.bbox[0]), "y1": int(s.bbox[1]),
                              "x2": int(s.bbox[2]), "y2": int(s.bbox[3]),
                              "score": round(float(s.score), 3),
                              "label": str(s.label), "polygon": poly})
            return self._reply({"ok": True, "width": img.shape[1],
                                "height": img.shape[0],
                                "ms": int((time.time() - t0) * 1000),
                                "boxes": boxes})
        except Exception as exc:                         # noqa: BLE001 - fail safe
            return self._reply({"ok": False, "error": str(exc)})


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[preview] listening on 127.0.0.1:{args.port} (model not loaded)",
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
