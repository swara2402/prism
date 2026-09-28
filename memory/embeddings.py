"""
memory.embeddings
=================

Embedding service.

Uses ``sentence-transformers`` (preferred) with graceful fallback to
Ollama embeddings, and ultimately to a deterministic hash-based
pseudo-embedding if neither is available.

The chosen backend is logged once on first use.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from config.settings import settings
from utils.llm import llm_client

logger = logging.getLogger(__name__)

_BACKEND: Optional[str] = None
_MODEL = None


def _get_sentence_transformer():
    """Lazily load the sentence-transformers model."""
    global _MODEL, _BACKEND
    if _MODEL is not None:
        return _MODEL
    try:
        from sentence_transformers import SentenceTransformer

        _MODEL = SentenceTransformer(settings.sentence_transformer_model)
        _BACKEND = "sentence-transformers"
        logger.info("embeddings_backend_loaded backend=%s", _BACKEND)
        return _MODEL
    except Exception as exc:
        logger.warning("sentence_transformer_load_failed error=%r", exc)
        _BACKEND = "fallback_hash"
        return None


def _normalize_dim(vec: List[float], dim: int) -> List[float]:
    """Truncate or zero-pad ``vec`` to a fixed dimension.

    Different embedding backends return vectors of different sizes
    (sentence-transformers / hash fallback = ``embedding_dim``, Ollama
    ``nomic-embed-text`` = 768).  FAISS requires a consistent dimension,
    so every vector is normalized to the configured dimension.
    """
    if len(vec) == dim:
        return vec
    if len(vec) > dim:
        return vec[:dim]
    return vec + [0.0] * (dim - len(vec))


async def embed_text(text: str) -> List[float]:
    """Embed ``text`` into a dense vector of fixed dimension."""
    # Try sentence-transformers first (CPU, fast, offline)
    model = _get_sentence_transformer()
    if model is not None:
        loop = asyncio.get_running_loop()
        vec = await loop.run_in_executor(
            None, lambda: model.encode(text, normalize_embeddings=True).tolist()
        )
        return _normalize_dim(vec, settings.embedding_dim)

    # Try Ollama
    try:
        vec = await llm_client.embeddings(text)
        if vec:
            return _normalize_dim(vec, settings.embedding_dim)
    except Exception as exc:
        logger.warning("ollama_embeddings_failed error=%r", exc)

    # Fallback to deterministic hash-based embedding
    return llm_client._pseudo_embedding(text, settings.embedding_dim)


async def embed_batch(texts: List[str]) -> List[List[float]]:
    """Embed a list of texts (parallel)."""
    tasks = [embed_text(t) for t in texts]
    return await asyncio.gather(*tasks)
