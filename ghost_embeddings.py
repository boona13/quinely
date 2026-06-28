"""
ghost_embeddings.py — Shared, pluggable text-embedding provider.

Ghost historically embedded text with a 128-dim hash bag-of-words ("SimpleEmbedding").
That is deterministic and offline but semantically weak — it only matches on shared
tokens, so "reset my password" and "recover my login" look unrelated.

This module adds a real *neural* embedding backend while preserving Ghost's
no-API-key, cross-platform, graceful-fallback philosophy:

  * Neural backend: ``model2vec`` static embeddings (pure-Python + numpy wheels,
    no torch, ~30 MB CPU model). Loaded lazily, downloaded once, then cached.
  * Fallback: the legacy hash embedder, used automatically when ``model2vec`` is
    unavailable, the model can't be fetched (offline), or neural is disabled.

Both the vector-memory store and the hybrid-memory providers share this single
embedder so the whole agent benefits from one consistent vector space.

Public API
----------
    configure(model=..., enable_neural=...)   # optional, call once at boot
    get_embedder() -> Embedder                # process-wide singleton
    Embedder.embed(text) -> list[float]
    Embedder.embed_batch(texts) -> list[list[float]]
    Embedder.model_id / .dim / .is_neural
    Embedder.hash_embed(text) / .hash_dim     # legacy 128-dim space (for migration)
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import List, Optional

log = logging.getLogger("ghost.embeddings")

DEFAULT_MODEL = "minishlab/potion-base-8M"  # 256-dim static model, CPU, ~30MB

# Module-level configuration (overridable via configure()).
_cfg_model: str = DEFAULT_MODEL
_cfg_enable_neural: bool = True

_HASH_DIM = 128
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "must", "shall", "can", "need", "to", "of", "in", "for", "on",
    "with", "at", "by", "from", "as", "into", "through", "during", "before",
    "after", "above", "below", "between", "under", "and", "but", "or", "yet",
    "so", "if", "because", "although", "though", "while", "where", "when",
    "that", "which", "who", "whom", "whose", "what", "this", "these", "those",
    "i", "you", "he", "she", "it", "we", "they", "me", "him", "her", "us",
    "them", "my", "your", "his", "its", "our", "their",
})
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _hash_embed(text: str) -> List[float]:
    """Legacy 128-dim hash bag-of-words embedding (deterministic, offline)."""
    text = (text or "").lower()
    tokens = [t for t in _TOKEN_RE.findall(text)
              if t not in _STOPWORDS and len(t) > 2]
    bigrams = [f"{tokens[i]}_{tokens[i+1]}" for i in range(len(tokens) - 1)]
    features = tokens + bigrams
    vec = [0.0] * _HASH_DIM
    if not features:
        return vec
    for feat in features:
        idx = int(hashlib.md5(feat.encode()).hexdigest()[:8], 16) % _HASH_DIM
        vec[idx] += 1.0
    mag = sum(x * x for x in vec) ** 0.5
    if mag > 0:
        vec = [x / mag for x in vec]
    return vec


class Embedder:
    """Process-wide embedder. Tries a neural backend, falls back to hashing.

    The neural model is loaded lazily on first use so importing this module is
    cheap and never blocks (or fails) at import time.
    """

    def __init__(self, model: Optional[str] = None, enable_neural: bool = True):
        self._model_name = model or _cfg_model
        self._enable_neural = enable_neural
        self._neural = None            # the loaded model2vec StaticModel, or None
        self._neural_tried = False
        self._neural_failed = False
        self._dim = _HASH_DIM
        self._model_id = f"hash-{_HASH_DIM}"
        self._lock = threading.Lock()

    # -- neural backend ----------------------------------------------------
    def _ensure_neural(self):
        if self._neural is not None or self._neural_failed or not self._enable_neural:
            return
        with self._lock:
            if self._neural is not None or self._neural_failed:
                return
            self._neural_tried = True
            try:
                from model2vec import StaticModel
                m = StaticModel.from_pretrained(self._model_name)
                self._neural = m
                self._dim = int(m.dim)
                self._model_id = f"model2vec:{self._model_name}:{self._dim}"
                log.info("Neural embeddings active: %s (dim=%d)",
                         self._model_name, self._dim)
            except Exception as e:
                self._neural_failed = True
                self._dim = _HASH_DIM
                self._model_id = f"hash-{_HASH_DIM}"
                log.warning("Neural embeddings unavailable (%s); using hash "
                            "fallback. %s", type(e).__name__, e)

    def warmup(self) -> bool:
        """Force-load the neural model. Returns True if neural is active."""
        self._ensure_neural()
        return self.is_neural

    # -- properties --------------------------------------------------------
    @property
    def is_neural(self) -> bool:
        return self._neural is not None

    @property
    def dim(self) -> int:
        # Resolve lazily so callers get the true neural dim once warmed.
        if self._enable_neural and not self._neural_tried and not self._neural_failed:
            self._ensure_neural()
        return self._dim

    @property
    def model_id(self) -> str:
        if self._enable_neural and not self._neural_tried and not self._neural_failed:
            self._ensure_neural()
        return self._model_id

    @property
    def hash_dim(self) -> int:
        return _HASH_DIM

    # -- embedding ---------------------------------------------------------
    def embed(self, text: str) -> List[float]:
        self._ensure_neural()
        if self._neural is not None:
            try:
                return self._neural.encode([text or ""])[0].tolist()
            except Exception as e:
                log.debug("neural embed failed, falling back: %s", e)
        return _hash_embed(text)

    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        self._ensure_neural()
        if self._neural is not None:
            try:
                arr = self._neural.encode([t or "" for t in texts])
                return [row.tolist() for row in arr]
            except Exception as e:
                log.debug("neural batch embed failed, falling back: %s", e)
        return [_hash_embed(t) for t in texts]

    def hash_embed(self, text: str) -> List[float]:
        """Always the legacy 128-dim hash embedding (for cross-space fallback)."""
        return _hash_embed(text)


# --------------------------------------------------------------------------
# Singleton
# --------------------------------------------------------------------------
_embedder: Optional[Embedder] = None
_embedder_lock = threading.Lock()


def configure(model: Optional[str] = None, enable_neural: Optional[bool] = None):
    """Set process-wide defaults. Call once at boot before get_embedder()."""
    global _cfg_model, _cfg_enable_neural, _embedder
    if model:
        _cfg_model = model
    if enable_neural is not None:
        _cfg_enable_neural = bool(enable_neural)
    # Reset singleton so new config takes effect.
    with _embedder_lock:
        _embedder = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        with _embedder_lock:
            if _embedder is None:
                _embedder = Embedder(model=_cfg_model,
                                     enable_neural=_cfg_enable_neural)
    return _embedder
