"""Local sentence embeddings: all-MiniLM-L6-v2, int8 ONNX, CPU only, no network.

One lazily loaded session per process, one intra-op thread (predictable CPU next to gunicorn
workers), SHA-256-checked model files. Any failure (files missing, checksum mismatch,
QUORUM_INTELLIGENCE=0) makes available() False, and every feature that uses this module
falls back to keyword behaviour: nothing in Quorum requires the model to work.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import threading
from functools import lru_cache
from pathlib import Path

from django.conf import settings

log = logging.getLogger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2-int8"
DIM = 384
SHA256 = {
    "model_quantized.onnx": "afdb6f1a0e45b715d0bb9b11772f032c399babd23bfc31fed1c170afc848bdb1",
    "tokenizer.json": "da0e79933b9ed51798a3ae27893d3c5fa4a201126cef75586296df9b4d2c62a0",
}

_lock = threading.Lock()
_state: dict = {"loaded": False, "session": None, "tok": None, "error": None}


def _model_dir() -> Path:
    configured = getattr(settings, "QUORUM_MODEL_DIR", "")
    return Path(configured) if configured else Path(settings.REPO_DIR) / "models/all-MiniLM-L6-v2"


def _load():
    if _state["loaded"]:
        return
    with _lock:
        if _state["loaded"]:
            return
        try:
            if not getattr(settings, "QUORUM_INTELLIGENCE", True):
                raise RuntimeError("disabled by QUORUM_INTELLIGENCE=0")
            d = _model_dir()
            blobs = {"model_quantized.onnx": (d / "model_quantized.onnx").read_bytes(),
                     # stored gzip-compressed; the checksum is of the upstream (decompressed) file
                     "tokenizer.json": gzip.decompress((d / "tokenizer.json.gz").read_bytes())}
            for name, want in SHA256.items():
                if hashlib.sha256(blobs[name]).hexdigest() != want:
                    raise RuntimeError(f"{name}: checksum mismatch")
            import onnxruntime as ort
            from tokenizers import Tokenizer

            tok = Tokenizer.from_str(blobs["tokenizer.json"].decode("utf-8"))
            tok.enable_truncation(256)
            tok.enable_padding()
            so = ort.SessionOptions()
            so.intra_op_num_threads = 1
            so.inter_op_num_threads = 1
            _state["session"] = ort.InferenceSession(str(d / "model_quantized.onnx"), so,
                                                     providers=["CPUExecutionProvider"])
            _state["tok"] = tok
        except Exception as e:  # noqa: BLE001 - any failure means "fall back", never "crash"
            _state["error"] = str(e)
            log.warning("quorum intelligence: embeddings unavailable (%s); using keyword fallbacks", e)
        _state["loaded"] = True


def available() -> bool:
    _load()
    return _state["session"] is not None


def status() -> dict:
    _load()
    return {"model": MODEL_NAME, "available": _state["session"] is not None, "error": _state["error"]}


def encode(texts):
    """-> numpy array (n, 384), L2-normalised, so a dot product is the cosine similarity."""
    import numpy as np

    _load()
    if _state["session"] is None:
        raise RuntimeError("embeddings unavailable")
    texts = [t if (t and t.strip()) else " " for t in texts]
    out = []
    for i in range(0, len(texts), 32):
        with _lock:  # the tokenizer holds padding state; keep batches serial and short
            enc = _state["tok"].encode_batch(texts[i:i + 32])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            hidden = _state["session"].run(None, {"input_ids": ids, "attention_mask": mask,
                                                  "token_type_ids": np.zeros_like(ids)})[0]
        v = (hidden * mask[..., None]).sum(1) / np.maximum(mask.sum(1, keepdims=True), 1)
        out.append(v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12))
    return np.vstack(out).astype("float32")


@lru_cache(maxsize=2048)
def _cached_one(text: str):
    return encode([text])[0]


def encode_one(text: str):
    return _cached_one((text or "").strip()[:1000])


def match_groups(texts: list[str], groups: list[list[str]]):
    """(len(texts) x len(groups)) matrix: for each text, its best cosine with any phrase of each group."""
    import numpy as np

    S = encode(texts)
    return np.stack([(S @ encode(g).T).max(axis=1) for g in groups], axis=1)


def coverage(text: str, groups: list[list[str]]) -> list[float]:
    """For each group of anchor phrases, the best match with any sentence of `text` (feedback coach)."""
    import re

    sents = [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 3] or [text]
    return match_groups(sents, groups).max(axis=0).tolist()


def content_hash(text: str) -> str:
    return hashlib.sha256(f"{MODEL_NAME}\n{text}".encode()).hexdigest()
