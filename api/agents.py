"""
api.agents
==========

Endpoints for agent reliability introspection.

* GET /agents               - list agents + reliability stats
* GET /agents/{name}        - one agent's stats
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from api.deps import require_api_key, require_tenant

from agents.registry import list_agent_names
from database.repositories import get_agent_reliability, list_agent_reliabilities
from models.schemas import AgentReliabilityOut

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    dependencies=[Depends(require_api_key)],
)


@router.get("")
async def list_agents(tenant: str = Depends(require_tenant)) -> List[dict]:
    from database.session import AsyncSessionLocal

    names = list_agent_names()
    async with AsyncSessionLocal() as session:
        # One query for the whole page, not one per agent name.
        rows = await list_agent_reliabilities(session, tenant_id=tenant)
    by_name = {r.agent_name: r for r in rows}
    out: List[dict] = []
    for n in names:
        ar = by_name.get(n)
        out.append(
            {
                "name": n,
                "reliability": ar.reliability_score if ar else 0.5,
                "invocations": ar.invocations if ar else 0,
            }
        )
    return out


@router.get("/{name}", response_model=AgentReliabilityOut)
async def get_agent(name: str, tenant: str = Depends(require_tenant)) -> AgentReliabilityOut:
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        ar = await get_agent_reliability(session, name, tenant_id=tenant)
    if ar is None:
        raise HTTPException(404, "Agent not found")
    return AgentReliabilityOut.model_validate(ar)
