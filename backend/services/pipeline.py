"""
pipeline.py — bridge from the dash to the local identification engine.

Runs engine_runner.py as a subprocess under the SYSTEM python (GPU torch +
ultralytics + CLIP). The dash venv stays lightweight; the heavy CV/ML runs out
of process. Returns the list of detected pairs (dicts) for one table photo.
"""
import json
import os
import subprocess
import tempfile

from backend.config import (ENGINE_DIR, ENGINE_RUNNER, ENGINE_PYTHON,
                            ENGINE_SEGMENT_MODEL, ENGINE_OLLAMA_MODEL,
                            ENGINE_OLLAMA_URL, ENGINE_MODEL_TIMEOUT,
                            ENGINE_JOB_TIMEOUT, PAIRS_DIR, SEEN_SHOE_ENABLED,
                            ENGINE_SEGMENT_ESCALATE, ENGINE_SEGMENT_ESCALATE_MODE,
                            ENGINE_LOCAL_MODEL_ID, ENGINE_CROP_MASK_SAM2,
                            ENGINE_ENV_PAD_KB, ENGINE_INSOLE_HEAD)


class EngineError(RuntimeError):
    """Raised when the engine subprocess fails or returns no usable result."""


def process_table_photo(tp_id: str, image_fs_path: str, mode: str = "shoes") -> list:
    """Run the engine on one table photo. Returns a list of pair dicts; crops
    are written into PAIRS_DIR as "<tp_id>_<n>.jpg". Raises EngineError on
    failure (the caller marks the job failed). mode='insoles' switches the
    engine to the insole branch (SAM2 detection + insole_head.pt brand)."""
    if not os.path.exists(image_fs_path):
        raise EngineError(f"image not found: {image_fs_path}")

    PAIRS_DIR.mkdir(parents=True, exist_ok=True)
    fd, out_json = tempfile.mkstemp(suffix=".json")
    os.close(fd)

    cmd = [
        ENGINE_PYTHON, str(ENGINE_RUNNER),
        "--engine-dir",   str(ENGINE_DIR),
        "--image",        str(image_fs_path),
        "--out-dir",      str(PAIRS_DIR),
        "--out-json",     out_json,
        "--id-prefix",    tp_id,
        "--segment-model", ENGINE_SEGMENT_MODEL,
        "--ollama-model", ENGINE_OLLAMA_MODEL,
        "--ollama-url",   ENGINE_OLLAMA_URL,
        "--model-timeout", str(ENGINE_MODEL_TIMEOUT),
    ]
    if mode == "insoles":
        cmd += ["--mode", "insoles", "--insole-head", str(ENGINE_INSOLE_HEAD)]
    # Only ask the engine to emit per-pair embeddings when the seen-shoe cache
    # is on — otherwise it's pure overhead the live pipeline shouldn't pay
    # (and the insole branch has no seen-shoe cache at all).
    if SEEN_SHOE_ENABLED and mode != "insoles":
        cmd.append("--emit-embedding")
    # SAM2+gate escalation hybrid, opt-in via env (off = current pipeline).
    if ENGINE_SEGMENT_ESCALATE:
        cmd += ["--escalate-sam2", "--escalate-mode", ENGINE_SEGMENT_ESCALATE_MODE]
    # Skip the redundant local model-ID unless explicitly enabled (saves ~1 min).
    if not ENGINE_LOCAL_MODEL_ID:
        cmd.append("--skip-local-model-id")
    # Refine crop masks with SAM2 box-prompt unless explicitly disabled.
    if not ENGINE_CROP_MASK_SAM2:
        cmd.append("--no-crop-mask-sam2")
    # Workaround for the libcuda-init stack over-read (see ENGINE_ENV_PAD_KB
    # in config.py). env=None keeps plain inheritance when the pad is off.
    env = None
    if ENGINE_ENV_PAD_KB > 0:
        env = dict(os.environ,
                   _LIBCUDA_STACK_PAD="X" * (ENGINE_ENV_PAD_KB * 1024))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              env=env, timeout=ENGINE_JOB_TIMEOUT)
        # Surface the engine's pairing decisions to the worker log (journal),
        # even on success — otherwise [pair]/COLOR VETO lines are only visible on
        # a failure tail, making pairing impossible to diagnose live.
        for _l in (proc.stderr or "").splitlines():
            if "[pair]" in _l:
                print(f"[engine {tp_id}] {_l.strip()}", flush=True)
        try:
            with open(out_json) as f:
                data = json.load(f)
        except Exception:
            tail = (proc.stderr or "")[-1000:]
            err = EngineError(
                f"engine produced no result (exit {proc.returncode}). {tail}")
            # A negative returncode = killed by a signal (SIGSEGV etc.) — a
            # crash, not a bad photo. The worker uses this to auto-retry once.
            err.returncode = proc.returncode
            raise err
        if data.get("error"):
            raise EngineError(data["error"])
        return data.get("pairs", [])
    except subprocess.TimeoutExpired:
        raise EngineError(f"engine timed out after {ENGINE_JOB_TIMEOUT}s")
    finally:
        try:
            os.unlink(out_json)
        except OSError:
            pass
