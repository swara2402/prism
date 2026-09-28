"""
api.patterns
============

Endpoints for the Self-Learning Pattern Generator:

* GET    /patterns/pending        - list patterns awaiting approval
* POST   /patterns/{id}/approve   - approve a pattern
* GET    /patterns/approved       - list approved patterns
* POST   /patterns/match          - match raw logs against approved patterns
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from api.deps import require_api_key
from pydantic import BaseModel

from database.repositories import list_approved_patterns, list_pending_patterns
from learning.pattern_generator import (
    approve_pattern_by_id,
    match_approved_patterns,
)
from models.schemas import PatternApprove, PatternOut

router = APIRouter(
    prefix="/patterns",
    tags=["patterns"],
    dependencies=[Depends(require_api_key)],
)


class MatchRequest(BaseModel):
    logs: List[str]


class MatchHit(BaseModel):
    pattern_id: str
    root_cause_hint: str
    confidence: float
    matched_signature: str


@router.get("/pending", response_model=List[PatternOut])
async def pending_patterns() -> List[PatternOut]:
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = await list_pending_patterns(session)
    return [PatternOut.model_validate(r) for r in rows]


@router.get("/approved", response_model=List[PatternOut])
async def approved_patterns() -> List[PatternOut]:
    from database.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        rows = await list_approved_patterns(session)
    return [PatternOut.model_validate(r) for r in rows]


@router.post("/{pattern_id}/approve", response_model=dict)
async def approve(pattern_id: str, body: PatternApprove) -> dict:
    if body.pattern_id != pattern_id:
        raise HTTPException(400, "pattern_id mismatch")
    ok = await approve_pattern_by_id(pattern_id, body.approver)
    if not ok:
        raise HTTPException(404, "Pattern not found")
    return {"approved": True, "pattern_id": pattern_id}


@router.post("/match", response_model=List[MatchHit])
async def match_patterns(body: MatchRequest) -> List[MatchHit]:
    hits = await match_approved_patterns(body.logs)
    return [MatchHit(**h) for h in hits]
