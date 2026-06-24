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
                            ENGINE_JOB_TIMEOUT, PAIRS_DIR, SEEN_SHOE_ENABLED)


class EngineError(RuntimeError):
    """Raised when the engine subprocess fails or returns no usable result."""


def process_table_photo(tp_id: str, image_fs_path: str) -> list:
    """Run the engine on one table photo. Returns a list of pair dicts; crops
    are written into PAIRS_DIR as "<tp_id>_<n>.jpg". Raises EngineError on
    failure (the caller marks the job failed)."""
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
    # Only ask the engine to emit per-pair embeddings when the seen-shoe cache
    # is on — otherwise it's pure overhead the live pipeline shouldn't pay.
    if SEEN_SHOE_ENABLED:
        cmd.append("--emit-embedding")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=ENGINE_JOB_TIMEOUT)
        try:
            with open(out_json) as f:
                data = json.load(f)
        except Exception:
            tail = (proc.stderr or "")[-1000:]
            raise EngineError(
                f"engine produced no result (exit {proc.returncode}). {tail}")
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
