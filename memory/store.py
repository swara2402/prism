"""Persistent incident memory with tenant-scoped, provenance-aware retrieval."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
from sqlalchemy import select

from config.logging import get_logger
from config.settings import settings
from database import models as dbm
from database.repositories import add_memory
from memory.embeddings import _normalize_dim, embed_text

logger = get_logger(__name__)


@dataclass
class MemoryHit:
    incident_id: str
    similarity: float
    root_cause: str
    resolution: Optional[str]
    services: List[str]
    confidence: float
    text_repr: str
    tenant_id: Optional[str] = None
    provenance: str = "historical_incident"


class MemoryStore:
    # Each authenticated tenant gets an independent process-local index. The
    # old single mutable index could switch from tenant A to tenant B while a
    # concurrent request was still reading it.
    _instances: Dict[str, "MemoryStore"] = {}
    _legacy_instance: Optional["MemoryStore"] = None
    _instances_lock = asyncio.Lock()

    def __init__(self, tenant_id: Optional[str] = None) -> None:
        self.tenant_id = tenant_id
        self._ids: List[str] = []
        self._vectors: List[List[float]] = []
        self._meta: Dict[str, Dict[str, Any]] = {}
        self._faiss_index = None
        self._lock = asyncio.Lock()
        self._loaded = False
        base = Path(settings.faiss_index_path)
        if tenant_id:
            safe_tenant = "".join(c if c.isalnum() or c in "-_" else "_" for c in tenant_id)
            self._index_path = base.parent / f"{base.name}_{safe_tenant}"
        else:
            self._index_path = base

    @classmethod
    def get(cls, tenant_id: Optional[str] = None) -> "MemoryStore":
        """Return an index permanently bound to one tenant.

        ``tenant_id=None`` is retained only for legacy internal callers. API
        paths must always pass the authenticated tenant explicitly.
        """
        if tenant_id is None:
            if cls._legacy_instance is None:
                cls._legacy_instance = cls()
            return cls._legacy_instance
        store = cls._instances.get(tenant_id)
        if store is None:
            store = cls(tenant_id=tenant_id)
            cls._instances[tenant_id] = store
        return store

    @classmethod
    def reset(cls) -> None:
        cls._instances.clear()
        cls._legacy_instance = None

    async def load(self, tenant_id: Optional[str] = None) -> None:
        """Load only memories belonging to the store's bound tenant."""
        effective_tenant = self.tenant_id
        if effective_tenant is None:
            # Legacy stores may be explicitly scoped for a one-off internal
            # operation, but never silently load all tenants.
            effective_tenant = tenant_id
        if not effective_tenant:
            raise ValueError("MemoryStore requires an authenticated tenant scope")
        if self.tenant_id and tenant_id and tenant_id != self.tenant_id:
            raise ValueError("MemoryStore tenant scope cannot be changed")

        async with self._lock:
            if self._loaded:
                return
            self._ids.clear()
            self._vectors.clear()
            self._meta.clear()
            self._faiss_index = None

            from database.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                stmt = (
                    select(dbm.IncidentMemory, dbm.Incident.tenant_id)
                    .join(dbm.Incident, dbm.Incident.id == dbm.IncidentMemory.incident_id)
                    .where(dbm.Incident.tenant_id == effective_tenant)
                    .order_by(dbm.IncidentMemory.created_at.desc())
                    .limit(10_000)
                )
                rows = (await session.execute(stmt)).all()

            for record, row_tenant_id in rows:
                self._ids.append(record.id)
                self._vectors.append(_normalize_dim(list(record.embedding or []), settings.embedding_dim))
                self._meta[record.id] = {
                    "incident_id": record.incident_id,
                    "tenant_id": row_tenant_id,
                    "root_cause": record.root_cause,
                    "resolution": record.resolution,
                    "services": list(record.services or []),
                    "confidence": record.confidence,
                    "text_repr": record.text_repr,
                }

            self._rebuild_faiss()
            self._loaded = True
            logger.info("memory_loaded count=%d tenant=%s faiss=%s", len(self._ids), effective_tenant, self._faiss_index is not None)

    def _rebuild_faiss(self) -> None:
        if not settings.enable_faiss or not self._vectors:
            self._faiss_index = None
            return
        try:
            import faiss
            mat = np.array(self._vectors, dtype=np.float32)
            if mat.ndim != 2 or mat.shape[0] == 0:
                self._faiss_index = None
                return
            index = faiss.IndexFlatIP(mat.shape[1])
            index.add(mat)
            self._faiss_index = index
        except Exception as exc:
            logger.warning("faiss_build_failed error=%r", exc)
            self._faiss_index = None

    def _save_faiss_snapshot(self) -> None:
        if self._faiss_index is None or not self.tenant_id:
            return
        try:
            import faiss
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            faiss.write_index(self._faiss_index, str(self._index_path) + ".faiss")
            with open(str(self._index_path) + ".meta.json", "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "tenant_id": self.tenant_id,
                        "embedding_model": settings.sentence_transformer_model,
                        "embedding_dim": settings.embedding_dim,
                        "ids": self._ids,
                        "meta": self._meta,
                    },
                    f,
                    default=str,
                )
        except Exception as exc:
            logger.warning("faiss_save_failed error=%r", exc)

    async def add(
        self,
        incident_id: str,
        text_repr: str,
        root_cause: str,
        resolution: Optional[str],
        confidence: float,
        lessons: Sequence[str],
        services: Sequence[str],
        *,
        tenant_id: Optional[str] = None,
    ) -> str:
        effective_tenant = self.tenant_id or tenant_id
        if not effective_tenant:
            raise ValueError("Memory writes require an authenticated tenant scope")
        if self.tenant_id and tenant_id and tenant_id != self.tenant_id:
            raise ValueError("MemoryStore tenant scope cannot be changed")

        embedding = await embed_text(text_repr)
        from database.session import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            incident = await session.get(dbm.Incident, incident_id)
            if incident is None or incident.tenant_id != effective_tenant:
                raise ValueError("Incident does not belong to the requested tenant")
            rec = await add_memory(
                session,
                incident_id=incident_id,
                text_repr=text_repr,
                embedding=list(embedding),
                root_cause=root_cause,
                resolution=resolution,
                confidence=max(0.0, min(1.0, confidence)),
                lessons=list(lessons),
                services=list(services),
            )
            await session.commit()
            mem_id = rec.id

        async with self._lock:
            if not self._loaded:
                # Load this tenant's existing records before appending so a
                # first write cannot discard older memories.
                self._lock.release()
                try:
                    await self.load(effective_tenant)
                finally:
                    await self._lock.acquire()
            self._ids.append(mem_id)
            self._vectors.append(_normalize_dim(list(embedding), settings.embedding_dim))
            self._meta[mem_id] = {
                "incident_id": incident_id,
                "tenant_id": effective_tenant,
                "root_cause": root_cause,
                "resolution": resolution,
                "services": list(services),
                "confidence": confidence,
                "text_repr": text_repr,
            }
            self._rebuild_faiss()
            self._save_faiss_snapshot()
        return mem_id

    async def search(
        self,
        query: str,
        top_k: int = 5,
        similarity_threshold: float = 0.0,
        *,
        tenant_id: Optional[str] = None,
    ) -> List[MemoryHit]:
        effective_tenant = self.tenant_id or tenant_id
        if not effective_tenant:
            raise ValueError("Memory searches require an authenticated tenant scope")
        if self.tenant_id and tenant_id and tenant_id != self.tenant_id:
            raise ValueError("MemoryStore tenant scope cannot be changed")
        if not self._loaded:
            await self.load(effective_tenant)
        q_vec = await embed_text(query)
        async with self._lock:
            if not self._vectors:
                return []
            if self._faiss_index is not None:
                try:
                    q = np.array([q_vec], dtype=np.float32)
                    scores, indices = self._faiss_index.search(q, min(top_k, len(self._ids)))
                    return [
                        self._hit(float(score), self._ids[idx])
                        for score, idx in zip(scores[0], indices[0])
                        if idx >= 0 and score >= similarity_threshold
                    ]
                except Exception as exc:
                    logger.warning("faiss_search_failed error=%r", exc)
            return self._cosine_search(q_vec, top_k, similarity_threshold)

    def _hit(self, similarity: float, mem_id: str) -> MemoryHit:
        m = self._meta[mem_id]
        return MemoryHit(
            incident_id=m["incident_id"],
            similarity=similarity,
            root_cause=m["root_cause"],
            resolution=m["resolution"],
            services=m["services"],
            confidence=m["confidence"],
            text_repr=m["text_repr"],
            tenant_id=m.get("tenant_id"),
        )

    def _cosine_search(self, q_vec: List[float], top_k: int, similarity_threshold: float) -> List[MemoryHit]:
        q = np.array(q_vec, dtype=np.float32)
        q_norm = np.linalg.norm(q) or 1.0
        scored: List[tuple[float, str]] = []
        for mem_id, vec in zip(self._ids, self._vectors):
            v = np.array(vec, dtype=np.float32)
            if v.shape != q.shape:
                continue
            sim = float(np.dot(q, v) / (q_norm * (np.linalg.norm(v) or 1.0)))
            if sim >= similarity_threshold:
                scored.append((sim, mem_id))
        scored.sort(key=lambda x: -x[0])
        return [self._hit(sim, mem_id) for sim, mem_id in scored[:top_k]]

    def size(self) -> int:
        return len(self._ids)
