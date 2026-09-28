"""
api.memory
==========

Endpoints for the Incident Memory store:

* POST /memory/search   - semantic search over past incidents
* GET  /memory/stats    - index size + backend info
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
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
    _api_key: str = Depends(require_api_key),
) -> List[SearchHit]:
    if len(body.query) > 2000:
        from fastapi import HTTPException
        raise HTTPException(422, "query too long (max 2000 characters)")
    store = MemoryStore.get()
    hits = await store.search(
        body.query,
        top_k=body.top_k,
        similarity_threshold=body.similarity_threshold,
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
async def memory_stats(_api_key: str = Depends(require_api_key)) -> dict:
    store = MemoryStore.get()
    return {
        "size": store.size(),
        "faiss_enabled": store._faiss_index is not None,
    }
