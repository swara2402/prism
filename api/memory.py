"""
api.memory
==========

Tenant-scoped endpoints for incident memory.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from api.deps import require_api_key
from config.settings import settings
from pydantic import BaseModel, Field

from memory.store import MemoryStore

router = APIRouter(prefix="/memory", tags=["memory"])


class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=5, ge=1, le=settings.memory_top_k * 20)
    similarity_threshold: float = Field(default=0.0, ge=0.0, le=1.0)


class SearchHit(BaseModel):
    incident_id: str
    similarity: float
    root_cause: str
    resolution: Optional[str]
    services: List[str]
    confidence: float


@router.post("/search", response_model=List[SearchHit])
async def search_memory(
    body: SearchRequest,
    request: Request,
    _api_key: str = Depends(require_api_key),
) -> List[SearchHit]:
    if len(body.query) > settings.max_memory_query_length:
        raise HTTPException(422, f"query too long (max {settings.max_memory_query_length} characters)")
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(403, "Tenant context is required")
    store = MemoryStore.get(tenant_id=tenant_id)
    hits = await store.search(
        body.query,
        top_k=body.top_k,
        similarity_threshold=body.similarity_threshold,
        tenant_id=tenant_id,
    )
    return [
        SearchHit(
            incident_id=h.incident_id,
            similarity=round(h.similarity, 3),
            root_cause=h.root_cause,
            resolution=h.resolution,
            services=h.services,
            confidence=h.confidence,
        )
        for h in hits
    ]


@router.get("/stats")
async def memory_stats(
    request: Request,
    _api_key: str = Depends(require_api_key),
) -> dict:
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(403, "Tenant context is required")
    store = MemoryStore.get(tenant_id=tenant_id)
    return {
        "size": store.size(),
        "faiss_enabled": store._faiss_index is not None,
        "tenant_scoped": True,
    }
