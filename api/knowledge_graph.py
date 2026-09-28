"""
api.knowledge_graph
===================

Endpoints for the Enterprise Knowledge Graph (Neo4j-backed).

* POST /kg/services               - upsert a service
* POST /kg/services/{name}/depends-on/{dep}  - record dependency
* POST /kg/changes                - record a recent change
* GET  /kg/services/subgraph      - fetch subgraph for affected services
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from api.deps import require_api_key
from pydantic import BaseModel

from knowledge_graph.store import KnowledgeGraphStore

router = APIRouter(
    prefix="/kg",
    tags=["knowledge_graph"],
    dependencies=[Depends(require_api_key)],
)


class ServiceIn(BaseModel):
    name: str
    team: Optional[str] = None
    tier: Optional[str] = None


class ApiIn(BaseModel):
    path: str
    method: str = "GET"
    service: Optional[str] = None


class ChangeIn(BaseModel):
    service: str
    change_type: str
    timestamp: Optional[str] = None
    description: str = ""


class SubgraphRequest(BaseModel):
    services: List[str]


@router.post("/services")
async def upsert_service(body: ServiceIn) -> dict:
    store = KnowledgeGraphStore.get()
    await store.upsert_service(body.name, team=body.team, tier=body.tier)
    return {"ok": True}


@router.post("/apis")
async def upsert_api(body: ApiIn) -> dict:
    store = KnowledgeGraphStore.get()
    await store.upsert_api(body.path, body.method, body.service)
    return {"ok": True}


@router.post("/services/{name}/depends-on/{dep}")
async def add_dependency(name: str, dep: str, weight: float = 1.0) -> dict:
    store = KnowledgeGraphStore.get()
    await store.link_service_dependency(name, dep, weight=weight)
    return {"ok": True}


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
    store = KnowledgeGraphStore.get()
    return await store.query_service_subgraph(body.services)
