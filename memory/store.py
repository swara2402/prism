"""
memory.store
============

Incident Memory store.

Backed by:

* PostgreSQL ``incident_memory`` table for persistence
* FAISS index (in-memory, persisted to disk) for fast semantic search
* Pure-Python cosine-similarity fallback when FAISS isn't available

Each memory record stores:

* ``incident_id``
* ``text_repr`` (compressed text representation used for embedding)
* ``embedding`` (vector)
* ``root_cause``
* ``resolution``
* ``confidence``
* ``lessons`` (list of strings)
* ``services`` (list of strings)
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from config.logging import get_logger
from config.settings import settings
from database.repositories import add_memory, list_memory
from memory.embeddings import _normalize_dim, embed_text

logger = get_logger(__name__)


@dataclass
class MemoryHit:
    """A single semantic-search hit."""

    incident_id: str
    similarity: float
    root_cause: str
    resolution: Optional[str]
    services: List[str]
    confidence: float
    text_repr: str


class MemoryStore:
    """
    Singleton incident-memory store.

    Holds an in-memory index of all stored embeddings (synced from the
    DB) and answers ``search`` queries using FAISS or pure-Python
    cosine similarity.
    """

    _instance: Optional["MemoryStore"] = None

    def __init__(self) -> None:
        self._ids: List[str] = []  # memory.id list, indexed parallel to vectors
        self._vectors: List[List[float]] = []
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._faiss_index = None
        self._lock = asyncio.Lock()
        self._loaded = False
        self._index_path = Path(settings.faiss_index_path)

    @classmethod
    def get(cls) -> "MemoryStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Drop the singleton so the next ``get()`` builds a fresh store.

        Used for strict test / evaluation isolation: each scenario starts
        from an empty in-memory index instead of inheriting the previous
        run's embeddings.
        """
        cls._instance = None

    # ---------- Loading ----------

    async def load(self) -> None:
        """Load all memory records from PostgreSQL into the in-memory index."""
        async with self._lock:
            if self._loaded:
                return
            try:
                from database.session import AsyncSessionLocal

                async with AsyncSessionLocal() as session:
                    rows = await list_memory(session, limit=10_000)
            except Exception as exc:
                logger.warning("memory_load_failed error=%r", exc)
                raise

            for r in rows:
                self._ids.append(r.id)
                self._vectors.append(
                    _normalize_dim(list(r.embedding or []), settings.embedding_dim)
                )
                self._meta[r.id] = {
                    "incident_id": r.incident_id,
                    "root_cause": r.root_cause,
                    "resolution": r.resolution,
                    "services": list(r.services or []),
                    "confidence": r.confidence,
                    "text_repr": r.text_repr,
                }

            self._rebuild_faiss()
            self._loaded = True
            logger.info(
                "memory_loaded count=%d faiss=%s",
                len(self._ids),
                self._faiss_index is not None,
            )

    # ---------- FAISS ----------

    def _rebuild_faiss(self) -> None:
        """Try to build a FAISS index from the in-memory vectors."""
        if not settings.enable_faiss:
            self._faiss_index = None
            return
        if not self._vectors:
            self._faiss_index = None
            return
        try:
            import faiss

            mat = np.array(self._vectors, dtype=np.float32)
            if mat.ndim != 2 or mat.shape[0] == 0:
                self._faiss_index = None
                return
            dim = mat.shape[1]
            index = faiss.IndexFlatIP(dim)
            index.add(mat)
            self._faiss_index = index
        except Exception as exc:
            logger.warning("faiss_build_failed error=%r", exc)
            self._faiss_index = None

    def _save_faiss_snapshot(self) -> None:
        """Persist the FAISS index + id mapping to disk."""
        if self._faiss_index is None:
            return
        try:
            import faiss

            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            faiss.write_index(self._faiss_index, str(self._index_path) + ".faiss")
            meta_path = str(self._index_path) + ".meta.json"
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump({"ids": self._ids, "meta": self._meta}, f, default=str)
        except Exception as exc:
            logger.warning("faiss_save_failed error=%r", exc)

    # ---------- Insertion ----------

    async def add(
        self,
        incident_id: str,
        text_repr: str,
        root_cause: str,
        resolution: Optional[str],
        confidence: float,
        lessons: Sequence[str],
        services: Sequence[str],
    ) -> str:
        """Embed ``text_repr`` and persist a new memory record."""
        embedding = await embed_text(text_repr)

        try:
            from database.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                rec = await add_memory(
                    session,
                    incident_id=incident_id,
                    text_repr=text_repr,
                    embedding=list(embedding),
                    root_cause=root_cause,
                    resolution=resolution,
                    confidence=confidence,
                    lessons=list(lessons),
                    services=list(services),
                )
                await session.commit()
                mem_id = rec.id
        except Exception as exc:
            logger.warning("memory_persist_failed error=%r", exc)
            mem_id = f"mem_{len(self._ids)}"

        async with self._lock:
            self._ids.append(mem_id)
            self._vectors.append(list(embedding))
            self._meta[mem_id] = {
                "incident_id": incident_id,
                "root_cause": root_cause,
                "resolution": resolution,
                "services": list(services),
                "confidence": confidence,
                "text_repr": text_repr,
            }
            self._rebuild_faiss()
            self._save_faiss_snapshot()

        return mem_id

    # ---------- Search ----------

    async def search(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.0,
    ) -> List[MemoryHit]:
        """Semantic search over incident memory."""
        if not self._loaded:
            await self.load()

        if not self._vectors:
            return []

        q_vec = await embed_text(query)

        async with self._lock:
            if self._faiss_index is not None:
                try:
                    import numpy as np

                    q = np.array([q_vec], dtype=np.float32)
                    scores, indices = self._faiss_index.search(q, min(top_k, len(self._ids)))
                    hits: List[MemoryHit] = []
                    for score, idx in zip(scores[0], indices[0]):
                        if idx < 0 or score < similarity_threshold:
                            continue
                        mem_id = self._ids[idx]
                        m = self._meta[mem_id]
                        hits.append(
                            MemoryHit(
                                incident_id=m["incident_id"],
                                similarity=float(score),
                                root_cause=m["root_cause"],
                                resolution=m["resolution"],
                                services=m["services"],
                                confidence=m["confidence"],
                                text_repr=m["text_repr"],
                            )
                        )
                    return hits
                except Exception as exc:
                    logger.warning("faiss_search_failed error=%r", exc)

            # Fallback: pure-Python cosine
            return self._cosine_search(q_vec, top_k, similarity_threshold)

    def _cosine_search(
        self,
        q_vec: List[float],
        top_k: int,
        similarity_threshold: float,
    ) -> List[MemoryHit]:
        q = np.array(q_vec, dtype=np.float32)
        q_norm = np.linalg.norm(q) or 1.0
        scored: List[tuple[float, str]] = []
        for mem_id, vec in zip(self._ids, self._vectors):
            v = np.array(vec, dtype=np.float32)
            if v.shape != q.shape:
                continue
            v_norm = np.linalg.norm(v) or 1.0
            sim = float(np.dot(q, v) / (q_norm * v_norm))
            if sim >= similarity_threshold:
                scored.append((sim, mem_id))
        scored.sort(key=lambda x: -x[0])
        hits: List[MemoryHit] = []
        for sim, mem_id in scored[:top_k]:
            m = self._meta[mem_id]
            hits.append(
                MemoryHit(
                    incident_id=m["incident_id"],
                    similarity=sim,
                    root_cause=m["root_cause"],
                    resolution=m["resolution"],
                    services=m["services"],
                    confidence=m["confidence"],
                    text_repr=m["text_repr"],
                )
            )
        return hits

    # ---------- Stats ----------

    def size(self) -> int:
        return len(self._ids)
