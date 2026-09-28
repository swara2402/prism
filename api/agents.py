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
from api.deps import require_api_key

from agents.registry import list_agent_names
from database.repositories import get_agent_reliability
from models.schemas import AgentReliabilityOut

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    dependencies=[Depends(require_api_key)],
)


@router.get("")
async def list_agents() -> List[dict]:
    from database.session import AsyncSessionLocal

    names = list_agent_names()
    out: List[dict] = []
    async with AsyncSessionLocal() as session:
        for n in names:
            ar = await get_agent_reliability(session, n)
            out.append(
                {
                    "name": n,
                    "reliability": ar.reliability_score if ar else 0.5,
                    "invocations": ar.invocations if ar else 0,
                }
            )
    return out


@router.get("/{name}", response_model=AgentReliabilityOut)
async def get_agent(name: str) -> AgentReliabilityOut:
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        ar = await get_agent_reliability(session, name)
    if ar is None:
        raise HTTPException(404, "Agent not found")
    return AgentReliabilityOut.model_validate(ar)
