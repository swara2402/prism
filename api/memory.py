"""Tenant-bound incident memory API."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.deps import require_api_key
from config.settings import settings
from memory.store import MemoryStore

router = APIRouter(prefix="/memory", tags=["memory"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=settings.max_memory_query_length)
    top_k: int = Field(default=5, ge=1, le=settings.memory_top_k * 20)
    similarity_threshold: float = Field(default=0.0, ge=0.0, le=1.0)


class SearchHit(BaseModel):
    incident_id: str
    similarity: float
    root_cause: str
    resolution: Optional[str]
    services: List[str]
    confidence: float


def _tenant_from_request(request: Request) -> str:
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=500, detail="Authenticated tenant context is unavailable")
    return tenant_id


@router.post("/search", response_model=List[SearchHit])
async def search_memory(body: SearchRequest, request: Request, _api_key: str = Depends(require_api_key)) -> List[SearchHit]:
    tenant_id = _tenant_from_request(request)
    store = MemoryStore.get(tenant_id)
    hits = await store.search(body.query, top_k=body.top_k, similarity_threshold=body.similarity_threshold)
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
        if h.tenant_id == tenant_id
    ]


@router.get("/stats")
async def memory_stats(request: Request, _api_key: str = Depends(require_api_key)) -> dict:
    tenant_id = _tenant_from_request(request)
    store = MemoryStore.get(tenant_id)
    await store.load()
    return {"size": store.size(), "tenant_scoped": True, "faiss_enabled": store._faiss_index is not None}
