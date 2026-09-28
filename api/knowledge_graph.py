"""
api.knowledge_graph
===================

Authenticated endpoints for the Enterprise Knowledge Graph.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from api.deps import require_api_key
from knowledge_graph.store import KnowledgeGraphStore

router = APIRouter(
    prefix="/kg",
    tags=["knowledge_graph"],
    dependencies=[Depends(require_api_key)],
)


class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    team: Optional[str] = Field(default=None, max_length=200)
    tier: Optional[str] = Field(default=None, max_length=50)


class ApiIn(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    method: str = Field(default="GET", min_length=1, max_length=10)
    service: Optional[str] = Field(default=None, max_length=200)


class ChangeIn(BaseModel):
    service: str = Field(min_length=1, max_length=200)
    change_type: str = Field(min_length=1, max_length=100)
    timestamp: Optional[str] = Field(default=None, max_length=100)
    description: str = Field(default="", max_length=5000)


class SubgraphRequest(BaseModel):
    services: List[str] = Field(min_length=1, max_length=50)


@router.post("/services")
async def upsert_service(body: ServiceIn) -> dict:
    store = KnowledgeGraphStore.get()
    await store.upsert_service(body.name, team=body.team, tier=body.tier)
    return {"ok": True, "service": body.name}


@router.post("/apis")
async def upsert_api(body: ApiIn) -> dict:
    method = body.method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}:
        raise HTTPException(422, "Unsupported HTTP method")
    store = KnowledgeGraphStore.get()
    await store.upsert_api(body.path, method, body.service)
    return {"ok": True, "path": body.path, "method": method}


@router.post("/services/{name}/depends-on/{dep}")
async def add_dependency(
    name: str = Path(min_length=1, max_length=200),
    dep: str = Path(min_length=1, max_length=200),
    weight: float = Query(default=1.0, ge=0.0, le=1.0),
) -> dict:
    if name == dep:
        raise HTTPException(422, "A service cannot depend on itself")
    store = KnowledgeGraphStore.get()
    await store.link_service_dependency(name, dep, weight=weight)
    return {"ok": True, "from": name, "to": dep, "weight": weight}


@router.post("/changes")
async def record_change(body: ChangeIn) -> dict:
    store = KnowledgeGraphStore.get()
    await store.record_recent_change(
        service=body.service,
        change_type=body.change_type,
        timestamp=body.timestamp,
        description=body.description,
    )
    return {"ok": True}


@router.post("/services/subgraph")
async def subgraph(body: SubgraphRequest) -> dict:
    services = [s.strip() for s in body.services if s.strip()]
    if not services:
        raise HTTPException(422, "At least one service is required")
    store = KnowledgeGraphStore.get()
    return await store.query_service_subgraph(services[:50])
